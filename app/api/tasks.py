"""Sync task management API endpoints."""

import os
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app import database as db
from app.services import crypto
from app.services.sync_engine import SyncEngine
from app.services.scheduler import scheduler
from app.config import LOCAL_SYNC_ROOT

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _progress_payload(progress) -> dict:
    """Serialize sync progress for the UI.

    `completed_files` / `percent` are computed here so the frontend never has
    to re-derive them from raw counters (the old wiring displayed
    "0/83 · 100%" because the label only used processed_files while the
    percentage also added skipped and failed).
    """
    return {
        "phase": progress.phase,
        "total_files": progress.total_files,
        "noop_files": progress.noop_files,
        "completed_files": progress.completed_files,
        "percent": progress.percent,
        "processed_files": progress.processed_files,
        "failed_files": progress.failed_files,
        "skipped_files": progress.skipped_files,
        "total_bytes": progress.total_bytes,
        "transferred_bytes": progress.transferred_bytes,
        "speed": progress.speed,
        "active_transfers": _transfers_with_speed(progress),
    }


def _transfers_with_speed(progress) -> list:
    """Build active_transfers list, computing per-file speed on the server.

    The frontend can compute speed from transferred-byte delta between polls,
    but that's unreliable when many transfers upload at similar rates (the
    stale `transferred` value for in-flight files can stay constant while
    server-side the bytes are flowing through the socket). Computing speed
    server-side gives accurate per-file rates.

    Returns a list of dicts, each augmented with a `speed` field in bytes/s.
    """
    now = time.time()
    samples = SyncEngine._transfer_samples.setdefault(progress.task_id, {})
    out = []
    for t in progress.active_transfers.values():
        prev = samples.get(t.transfer_id)
        speed = 0.0
        if prev is not None:
            prev_bytes, prev_time = prev
            dt = now - prev_time
            if dt > 0:
                delta = t.transferred - prev_bytes
                if delta > 0:
                    speed = delta / dt
        samples[t.transfer_id] = (t.transferred, now)
        out.append({
            "transfer_id": t.transfer_id,
            "file_path": t.file_path,
            "action": t.action,
            "file_size": t.file_size,
            "transferred": t.transferred,
            "speed": speed,
        })
    # Drop samples for transfers that ended so the dict doesn't grow
    # unbounded across sync runs.
    active_ids = set(progress.active_transfers.keys())
    stale = [tid for tid in samples if tid not in active_ids]
    for tid in stale:
        samples.pop(tid, None)
    return out


class CreateTaskRequest(BaseModel):
    name: str
    connection_id: int
    local_path: str
    remote_path: str = "/"
    sync_mode: str = "bidirectional"  # bidirectional, upload_only, download_only
    schedule_type: str = "manual"  # realtime, scheduled, manual
    schedule_interval: int = 0  # seconds, for scheduled type
    filter_include: str = ""
    filter_exclude: str = ""
    max_file_size: int = 0
    delete_after_sync: int = 0
    enc_enabled: bool = False
    # bsenc = 自研 AES-256-GCM（默认）；syno = 群晖 Cloud Sync 兼容格式
    enc_format: str = "bsenc"


class UpdateTaskRequest(BaseModel):
    name: Optional[str] = None
    local_path: Optional[str] = None
    remote_path: Optional[str] = None
    sync_mode: Optional[str] = None
    schedule_type: Optional[str] = None
    schedule_interval: Optional[int] = None
    filter_include: Optional[str] = None
    filter_exclude: Optional[str] = None
    max_file_size: Optional[int] = None
    delete_after_sync: Optional[int] = None
    enc_enabled: Optional[bool] = None
    enc_format: Optional[str] = None
    status: Optional[str] = None


class TaskKeyRequest(BaseModel):
    password: str


def _enc_format(value) -> str:
    """归一化加密格式。只有两种，写错就直接告诉用户，别静默回退。"""
    fmt = (value or "bsenc").strip().lower()
    if fmt not in ("bsenc", "syno"):
        raise HTTPException(400, "加密格式只能是 bsenc（推荐）或 syno（群晖兼容）")
    return fmt


async def _require_syno_ready() -> None:
    """群晖格式要用密码原文派生密钥，库里没有就明确告诉用户怎么办。"""
    try:
        await crypto.keyring.syno_password()
    except crypto.CryptoError as exc:
        raise HTTPException(400, str(exc))


async def _export_task_key(task_id: int) -> Optional[dict]:
    """把任务的私钥打包成文件内容（Synology 那个 private.pem 的等价物）。"""
    task = await db.db_get_task(task_id)
    if not task or not int(task.get("enc_enabled") or 0):
        return None
    dek = await crypto.keyring.dek_for_task(task)
    return crypto.keyring.task_keyfile(task, dek)


@router.get("")
async def list_tasks():
    tasks = await db.db_list_tasks()
    result = []
    for task in tasks:
        progress = SyncEngine.get_progress(task["id"])
        task["is_running"] = SyncEngine.is_running(task["id"])
        if progress:
            task["progress"] = _progress_payload(progress)
        result.append(task)
    return {"tasks": result}


@router.get("/{task_id}")
async def get_task(task_id: int):
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task["is_running"] = SyncEngine.is_running(task_id)
    progress = SyncEngine.get_progress(task_id)
    if progress:
        task["progress"] = _progress_payload(progress)
    return task


@router.post("")
async def create_task(req: CreateTaskRequest):
    # Verify connection exists
    conn = await db.db_get_connection(req.connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    if req.enc_enabled and not await crypto.keyring.initialized():
        raise HTTPException(400, "请先在「全局设置 → 客户端加密」里设置密码")

    fmt = _enc_format(req.enc_format)
    if req.enc_enabled and fmt == "syno":
        await _require_syno_ready()

    task_id = await db.db_create_task(
        name=req.name,
        connection_id=req.connection_id,
        local_path=req.local_path,
        remote_path=req.remote_path,
        sync_mode=req.sync_mode,
        schedule_type=req.schedule_type,
        schedule_interval=req.schedule_interval,
        filter_include=req.filter_include,
        filter_exclude=req.filter_exclude,
        max_file_size=req.max_file_size,
        delete_after_sync=req.delete_after_sync,
        enc_enabled=1 if req.enc_enabled else 0,
        enc_format=fmt,
    )

    # Schedule the task
    task = await db.db_get_task(task_id)
    if task:
        await scheduler.schedule_task(task)

    result = {"id": task_id, "status": "created", "enc_format": fmt}
    if req.enc_enabled and task:
        # 创建时就生成该任务的密钥并交出去，对应 Synology「首次链接到公有云
        # 时自动导出 private.pem」。用户丢了密码时，这个文件是唯一出路，
        # 所以必须在这一步就给他，不能等到第一次上传才生成。
        try:
            if fmt == "syno":
                keys = await crypto.keyring.setup_syno_keys(task)
                result["syno_private_pem"] = keys["private_pem"].decode("ascii")
                result["syno_public_pem"] = keys["public_pem"].decode("ascii")
            else:
                keyfile = await _export_task_key(task_id)
                if keyfile:
                    result["private_key"] = keyfile
        except crypto.CryptoError as exc:
            result["key_error"] = f"没能导出任务私钥：{exc}（加密任务仍可运行，请稍后到任务里手动导出）"

    return result


@router.put("/{task_id}")
async def update_task(task_id: int, req: UpdateTaskRequest):
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if "enc_enabled" in updates:
        enabled = bool(updates["enc_enabled"])
        if enabled and not await crypto.keyring.initialized():
            raise HTTPException(400, "请先在「全局设置 → 客户端加密」里设置密码")
        updates["enc_enabled"] = 1 if enabled else 0
    if "enc_format" in updates:
        updates["enc_format"] = _enc_format(updates["enc_format"])

    will_encrypt = int(updates.get("enc_enabled", task.get("enc_enabled") or 0) or 0)
    fmt = updates.get("enc_format") or _enc_format(task.get("enc_format"))
    if will_encrypt and fmt == "syno":
        await _require_syno_ready()

    if updates:
        await db.db_update_task(task_id, **updates)

    # Reschedule if needed
    if "schedule_type" in updates or "schedule_interval" in updates or "local_path" in updates:
        updated_task = await db.db_get_task(task_id)
        if updated_task:
            await scheduler.schedule_task(updated_task)

    result = {"status": "updated"}
    fresh = await db.db_get_task(task_id)
    if fresh and int(fresh.get("enc_enabled") or 0) and fmt == "syno":
        # 第一次切到群晖格式（或者老任务还没有密钥对）时才生成，已经有就沿用，
        # 免得用户换一下别的设置就把旧 private.pem 弄废了。
        if not (fresh.get("syno_pubkey") or "").strip():
            try:
                keys = await crypto.keyring.setup_syno_keys(fresh)
                result["syno_private_pem"] = keys["private_pem"].decode("ascii")
                result["syno_public_pem"] = keys["public_pem"].decode("ascii")
            except crypto.CryptoError as exc:
                result["key_error"] = f"没能导出任务私钥：{exc}"
    elif updates.get("enc_enabled") == 1:
        try:
            keyfile = await _export_task_key(task_id)
            if keyfile:
                result["private_key"] = keyfile
        except crypto.CryptoError as exc:
            result["key_error"] = f"没能导出任务私钥：{exc}"

    return result


@router.delete("/{task_id}")
async def delete_task(task_id: int):
    await scheduler.unschedule_task(task_id)
    await db.db_delete_task(task_id)
    return {"deleted": True}


@router.post("/{task_id}/key")
async def export_task_key(task_id: int, req: TaskKeyRequest):
    """重新导出这个任务的私钥文件（丢了旧文件、或者创建时没存下来）。

    要重输密码：这个文件等于该任务全部云端文件的解密权。
    """
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not int(task.get("enc_enabled") or 0):
        raise HTTPException(400, "这个任务没有开启加密")
    if not await crypto.keyring.initialized():
        raise HTTPException(400, "还没有设置加密密码")
    if not await crypto.keyring.verify_password(req.password):
        raise HTTPException(400, "密码不正确")
    try:
        if _enc_format(task.get("enc_format")) == "syno":
            # 群晖格式要的是 PEM 本身——丢进官方解密工具的「私钥」一栏就能用
            pem = await crypto.keyring.syno_private_pem(task)
            return {"format": "syno-taskkey", "task_id": task_id,
                    "task_name": task.get("name") or "",
                    "file_name": "private.pem",
                    "private_pem": pem.decode("ascii"),
                    "note": "群晖 Cloud Sync Decryption Tool 里选「私钥」，把这段内容"
                            "存成 private.pem 即可（和密码二选一，都能单独解密）。"}
        payload = await _export_task_key(task_id)
    except crypto.CryptoError as exc:
        raise HTTPException(400, str(exc))
    if not payload:
        raise HTTPException(400, "无法导出这个任务的私钥")
    return payload


@router.post("/{task_id}/sync")
async def sync_now(task_id: int):
    """Trigger an immediate sync for a task."""
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if SyncEngine.is_running(task_id):
        raise HTTPException(409, "Task is already running")

    # Run sync in background (SyncEngine.spawn keeps a strong reference so the
    # task cannot be garbage collected mid-run)
    SyncEngine.spawn(task_id)
    return {"status": "syncing", "task_id": task_id}


@router.post("/{task_id}/pause")
async def pause_task(task_id: int):
    # If the task is currently running, request it to stop
    if SyncEngine.is_running(task_id):
        SyncEngine.request_stop(task_id)
    await scheduler.unschedule_task(task_id)
    await db.db_update_task(task_id, status="paused")
    return {"status": "paused"}


@router.post("/{task_id}/resume")
async def resume_task(task_id: int):
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await db.db_update_task(task_id, status="idle")
    await scheduler.schedule_task(task)
    return {"status": "resumed"}


class CancelTransferRequest(BaseModel):
    transfer_id: int


@router.post("/{task_id}/cancel-transfer")
async def cancel_transfer(task_id: int, req: CancelTransferRequest):
    """Cancel a specific file transfer within a running sync task."""
    if not SyncEngine.is_running(task_id):
        raise HTTPException(409, "Task is not running")
    SyncEngine.cancel_transfer(task_id, req.transfer_id)
    return {"status": "cancelled", "transfer_id": req.transfer_id}


class IgnoreFileRequest(BaseModel):
    # Absolute local path, absolute remote path or task-relative path — the
    # normaliser below turns all three into the same key.
    path: str
    action: str = ""
    # Size of the file that was skipped, so the record panel can show it.
    size: int = 0


def _task_rel_path(task: dict, raw_path: str) -> str:
    """Normalise any of the paths the UI might send into a relative key."""
    raw = (raw_path or "").strip().replace("\\", "/")
    if not raw:
        raise HTTPException(400, "path is required")
    for base in (task.get("local_path") or "", task.get("remote_path") or ""):
        base = (base or "").rstrip("/")
        if base and (raw == base or raw.startswith(base + "/")):
            raw = raw[len(base):]
            break
    rel = "/" + raw.lstrip("/")
    # Same shape the engine stores: forward slashes, single leading slash.
    return rel.replace("//", "/")


def _record_paths(task: dict, rel: str) -> tuple[str, str]:
    """Build the (local, remote) absolute paths a record row should carry.

    Records mirror what the engine writes: the local side is the path the
    container sees (/sync/...), the remote side is the cloud path.
    """
    local = (task.get("local_path") or "/sync").rstrip("/") + rel
    remote = (task.get("remote_path") or "").rstrip("/") + rel
    return local, remote


def _rel_from_record(task: dict, rec: dict) -> str:
    """Recover the task-relative path of a record, or "" if it is gone.

    Upload rows put the local path in ``source`` and downloads put it in
    ``target``, so both columns have to be tried against both task roots.
    """
    bases = [b.rstrip("/") for b in
             (task.get("local_path"), task.get("remote_path")) if b]
    for cand in (rec.get("source"), rec.get("target")):
        cand = (cand or "").strip().replace("\\", "/")
        if not cand:
            continue
        for base in bases:
            if cand == base or cand.startswith(base + "/"):
                return "/" + cand[len(base):].lstrip("/")
    return ""


async def _resolve_if_synced(task: dict, rel: str, action: str) -> bool:
    """Resolve a failed/skipped record as success when the file is already
    on the cloud and unchanged locally, instead of scheduling a sync that
    would decide "No changes" and write nothing — which left the row sitting
    there with a Retry button forever.

    The check is offline on purpose: a sync_state row exists only after a
    completed transfer, so state + unchanged local mtime/size means the file
    went through. If the cloud copy was deleted meanwhile, the next scan
    re-uploads it strictly (sync_state is not trusted for existence), so we
    are not hiding anything.
    """
    st = await db.db_get_sync_state_entry(task["id"], rel)
    if not st:
        return False
    local_path = (task.get("local_path") or "/sync").rstrip("/") + rel
    try:
        stat = os.stat(local_path)
    except OSError:
        return False
    if st.get("file_size") != stat.st_size or \
            abs((st.get("file_mtime") or 0) - stat.st_mtime) > 2:
        return False

    local_abs, remote_abs = _record_paths(task, rel)
    src, dst = (remote_abs, local_abs) if action == "download" \
        else (local_abs, remote_abs)
    conn = await db.db_get_connection(task.get("connection_id") or 0)
    await db.db_add_record(
        task["id"], action,
        task_name=task.get("name", ""),
        connection=(conn or {}).get("name", ""),
        file_name=os.path.basename(rel) or rel,
        source=src, target=dst,
        file_size=stat.st_size, status="success",
    )
    return True


@router.get("/{task_id}/ignored")
async def list_ignored(task_id: int):
    """Files the user skipped — they are no longer queued for transfer."""
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {"ignored": await db.db_list_ignored(task_id)}


@router.post("/{task_id}/ignore-file")
async def ignore_file(task_id: int, req: IgnoreFileRequest):
    """Permanently skip one file for this task.

    "Skip" in the transfer list used to only abort the in-flight transfer, so
    the very next cycle queued the same file again. This persists the decision.
    """
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    rel = _task_rel_path(task, req.path)
    await db.db_add_ignored(task_id, rel, req.action or "")
    # A file that is ignored should not carry a stale backoff entry around.
    await db.db_clear_failure(task_id, rel)

    # A skip is a user decision, not a failure, but it must still show up in
    # the transfer panel: otherwise the file vanishes from every counter with
    # no trace of why, and there is nowhere to undo it from.
    action = req.action if req.action in ("upload", "download") else (
        "download" if task.get("sync_mode") == "download_only" else "upload")
    local_abs, remote_abs = _record_paths(task, rel)
    src, dst = (remote_abs, local_abs) if action == "download" \
        else (local_abs, remote_abs)
    conn = await db.db_get_connection(task.get("connection_id") or 0)
    await db.db_add_record(
        task_id, action,
        task_name=task.get("name", ""),
        connection=(conn or {}).get("name", ""),
        file_name=os.path.basename(rel) or rel,
        source=src, target=dst,
        file_size=int(req.size or 0), status="skipped",
        error="已跳过（手动）",
    )
    return {"ignored": True, "path": rel}


@router.post("/{task_id}/records/{record_id}/retry")
async def retry_record(task_id: int, record_id: int):
    """Re-queue one failed or skipped file from the transfer panel.

    Nothing is transferred here: the per-file failure/ignore state is cleared
    and a sync is spawned, so the file goes through the normal scan — the same
    path a scheduled run takes. The row itself is left alone; if the file goes
    through, a fresh success row appears above it.
    """
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    rec = await db.db_get_record(record_id)
    if not rec or int(rec.get("task_id") or 0) != task_id:
        raise HTTPException(404, "Record not found for this task")
    if rec.get("status") not in ("failed", "skipped"):
        raise HTTPException(400, "Only failed or skipped records can be retried")

    rel = _rel_from_record(task, rec)
    if not rel:
        raise HTTPException(
            422, "Cannot work out which file this record refers to "
                 "(the task's paths may have changed)")

    await db.db_remove_ignored(task_id, rel)
    await db.db_clear_failure(task_id, rel)

    # Fast path: the file is already on the cloud and unchanged since — a
    # sync cycle would decide "No changes" and write nothing, leaving this
    # row (and its Retry button) around forever. Record it as success now.
    if rec.get("action") in ("upload", "download") and \
            await _resolve_if_synced(task, rel, rec["action"]):
        return {"status": "already_synced", "path": rel,
                "already_running": False}

    # request_sync, not spawn: a bare spawn is a no-op while a cycle is
    # running, and that cycle's operation list was computed before this
    # click — a quiet directory would then never produce the event that
    # picks the file back up.
    mode = SyncEngine.request_sync(task_id)
    return {"status": mode, "path": rel,
            "already_running": mode == "queued"}


@router.delete("/{task_id}/ignore-file")
async def unignore_file(task_id: int, path: str):
    """Undo a skip so the file is picked up on the next scan."""
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    rel = _task_rel_path(task, path)
    await db.db_remove_ignored(task_id, rel)
    await db.db_clear_failure(task_id, rel)
    return {"ignored": False, "path": rel}


@router.get("/{task_id}/failures")
async def list_failures(task_id: int):
    """Files currently waiting out a retry backoff (for the UI to explain)."""
    failures = await db.db_get_failures(task_id)
    now = time.time()
    out = []
    for rel, row in failures.items():
        out.append({
            "path": rel,
            "attempts": row.get("attempts", 0),
            "last_error": row.get("last_error", ""),
            "last_error_at": row.get("last_error_at", 0),
            "next_retry_at": row.get("next_retry_at", 0),
            "retry_in": max(0, round(float(row.get("next_retry_at") or 0) - now)),
        })
    out.sort(key=lambda x: x["next_retry_at"])
    return {"failures": out}



@router.get("/{task_id}/logs")
async def get_logs(task_id: int, limit: int = 200, offset: int = 0):
    logs = await db.db_list_logs(task_id, limit, offset)
    return {"logs": logs}


@router.get("/{task_id}/history")
async def get_history(task_id: int, limit: int = 20):
    history = await db.db_list_history(task_id, limit)
    return {"history": history}


@router.get("/{task_id}/progress")
async def get_progress(task_id: int):
    progress = SyncEngine.get_progress(task_id)
    if not progress:
        return {"is_running": False}
    return {
        "is_running": progress.is_running,
        "phase": progress.phase,
        "total_files": progress.total_files,
        "noop_files": progress.noop_files,
        "completed_files": progress.completed_files,
        "percent": progress.percent,
        "processed_files": progress.processed_files,
        "failed_files": progress.failed_files,
        "skipped_files": progress.skipped_files,
        "transferred_bytes": progress.transferred_bytes,
        "speed": progress.speed,
        "active_transfers": [
            {
                "transfer_id": t.transfer_id,
                "file_path": t.file_path,
                "action": t.action,
                "file_size": t.file_size,
                "transferred": t.transferred,
            }
            for t in progress.active_transfers.values()
        ],
        "started_at": progress.started_at,
        "elapsed": time.time() - progress.started_at if progress.started_at else 0,
    }
