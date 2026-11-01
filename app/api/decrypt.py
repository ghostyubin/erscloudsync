"""独立解密的 HTTP 接口。

把「选一个目录 + 输密码 + 解到另一个目录」这套动作暴露给界面，对应 Synology
的 Cloud Sync Decryption Tool。它**和同步任务、数据库、加密设置都没有关系**：
密钥来自每个文件头里的钥匙牌，所以在任何一台部署了本程序的机器上都能用。
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import decrypt_job as dj

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/decrypt", tags=["decrypt"])


class ScanRequest(BaseModel):
    path: str


class StartRequest(BaseModel):
    src: str
    dst: str
    password: str
    # 把没加密的文件也一并复制过去，用于还原一个明文密文混合的目录
    copy_plain: bool = False
    # 输出目录里已经有同名文件时是否覆盖；默认跳过，免得手滑覆盖别的东西
    overwrite: bool = False


@router.get("/roots")
async def get_roots():
    """可选的根目录。界面上的目录浏览器只在这几个根里走。"""
    return {"roots": dj.allowed_roots()}


@router.post("/scan")
async def scan_path(req: ScanRequest):
    """先数一遍：多少个密文、多少个明文、多大。不写任何文件。"""
    try:
        src = dj.check_source(req.path)
        return await asyncio.to_thread(dj.scan, src)
    except dj.DecryptJobError as exc:
        raise HTTPException(400, str(exc))


@router.post("/jobs")
async def create_job(req: StartRequest):
    try:
        job_id = await dj.start(req.src, req.dst, req.password,
                                copy_plain=req.copy_plain,
                                overwrite=req.overwrite)
    except dj.DecryptJobError as exc:
        raise HTTPException(400, str(exc))
    return {"id": job_id, **dj.get_job(job_id)}


@router.get("/jobs")
async def list_jobs():
    return {"jobs": dj.list_jobs()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = dj.get_job(job_id)
    if not job:
        raise HTTPException(404, "没有这个解密任务")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    if not await dj.cancel_job(job_id):
        raise HTTPException(400, "这个任务不在运行中")
    return {"status": "cancelling"}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    try:
        if not dj.remove_job(job_id):
            raise HTTPException(404, "没有这个解密任务")
    except dj.DecryptJobError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "deleted"}
