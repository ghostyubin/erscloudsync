"""Cloud connection management API endpoints."""

import asyncio
import base64
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app import database as db
from app.providers.baidu import BaiduProvider
from app.providers.p115 import Provider115
from app.providers.base import CloudProvider
from app.utils.helpers import get_provider
from app.config import BAIDU_APP_KEY, BAIDU_APP_SECRET, BAIDU_REDIRECT_URI, BAIDU_DEFAULT_APP_KEY, BAIDU_DEFAULT_APP_SECRET, BAIDU_OAUTH_TOOL_URL

router = APIRouter(prefix="/api/connections", tags=["connections"])


class CreateConnectionRequest(BaseModel):
    name: str
    type: str  # 'baidu' or '115'
    credentials: dict = {}


class BaiduTokenRequest(BaseModel):
    name: str
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0


class BaiduCodeRequest(BaseModel):
    name: str
    code: str


class BaiduAppConfigRequest(BaseModel):
    app_key: str
    app_secret: str = ""


class BaiduOAuthStartRequest(BaseModel):
    name: str = "我的百度网盘"


class BaiduRefreshTokenRequest(BaseModel):
    name: str = "我的百度网盘"
    refresh_token: str


class Cookies115Request(BaseModel):
    name: str
    cookies: dict


@router.get("")
async def list_connections():
    connections = await db.db_list_connections()
    return {"connections": connections}


@router.get("/{conn_id}")
async def get_connection(conn_id: int):
    conn = await db.db_get_connection(conn_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    # Don't expose full credentials
    conn["credentials"] = {k: "***" for k in conn.get("credentials", {})}
    return conn


@router.post("/baidu/token")
async def create_baidu_token_connection(req: BaiduTokenRequest):
    """Create a Baidu connection using a manually provided access token."""
    credentials = {
        "access_token": req.access_token,
        "refresh_token": req.refresh_token,
        "expires_at": req.expires_at or (time.time() + 2592000),
    }
    # Test the connection
    provider = BaiduProvider(credentials)
    try:
        if not await provider.test_connection():
            raise HTTPException(400, "Failed to connect with provided token")
    finally:
        await provider.close()

    conn_id = await db.db_create_connection(req.name, "baidu", credentials, "connected")
    return {"id": conn_id, "status": "connected"}


@router.post("/baidu/code")
async def create_baidu_code_connection(req: BaiduCodeRequest):
    """Create a Baidu connection using an OAuth authorization code (legacy manual)."""
    from app.config import get_baidu_app_credentials
    app_key, app_secret = await get_baidu_app_credentials()
    if not app_key:
        raise HTTPException(400, "Baidu app key not configured. Please configure it in the UI first.")

    try:
        credentials = await BaiduProvider.exchange_code(req.code, app_key, app_secret, BAIDU_REDIRECT_URI)
    except Exception as e:
        raise HTTPException(400, f"Code exchange failed: {e}")

    # Test connection with detailed error reporting
    provider = BaiduProvider(credentials)
    try:
        test_ok, test_msg = await provider.test_connection_detailed()
    except Exception as e:
        test_ok = False
        test_msg = str(e)
    finally:
        await provider.close()

    if not test_ok:
        raise HTTPException(400, f"令牌有效但连接测试失败: {test_msg}")

    conn_id = await db.db_create_connection(req.name, "baidu", credentials, "connected")
    return {"id": conn_id, "name": req.name, "status": "connected"}


# ---- Baidu App Config (stored in database) ----

@router.get("/baidu/app-config")
async def get_baidu_app_config():
    """Get Baidu app configuration status."""
    from app.config import get_baidu_app_credentials
    app_key, app_secret = await get_baidu_app_credentials()
    # If using the built-in key+secret, report as "built-in" so the UI knows
    is_builtin = (app_key == BAIDU_DEFAULT_APP_KEY and app_secret == BAIDU_DEFAULT_APP_SECRET)
    return {
        "app_key": app_key if is_builtin else (app_key[:4] + "***" if app_key else ""),
        "app_secret": "***" if app_secret else "",
        "app_key_configured": bool(app_key),
        "app_secret_configured": bool(app_secret),
        "using_builtin": is_builtin,
        "redirect_uri": BAIDU_REDIRECT_URI,
    }


@router.post("/baidu/app-config")
async def save_baidu_app_config(req: BaiduAppConfigRequest):
    """Save custom Baidu app key and secret to database (optional override).

    Both App Key and App Secret are required for the authorization code flow
    and token refresh.
    """
    if not req.app_key:
        raise HTTPException(400, "App Key is required")
    await db.db_set_setting("baidu_app_key", req.app_key)
    if req.app_secret:
        await db.db_set_setting("baidu_app_secret", req.app_secret)
    return {"status": "saved"}


# ---- Baidu OAuth via refresh_token (authorization code flow) ----

@router.get("/baidu/oauth-tool")
async def get_baidu_oauth_tool_url():
    """Get the OAuth tool URL for the user to obtain a refresh_token.

    The user visits this URL in their browser, authorizes on Baidu,
    and receives a refresh_token from AList's callback page.
    They then paste the refresh_token into our app.
    """
    return {"url": BAIDU_OAUTH_TOOL_URL}


@router.post("/baidu/refresh-token")
async def create_baidu_from_refresh_token(req: BaiduRefreshTokenRequest):
    """Create a Baidu connection using a refresh_token.

    The user obtains the refresh_token from AList's OAuth tool page.
    We exchange it for an access_token using the built-in AList credentials.
    """
    from app.config import get_baidu_app_credentials
    app_key, app_secret = await get_baidu_app_credentials()
    if not app_key or not app_secret:
        raise HTTPException(500, "Baidu app credentials not configured")

    try:
        credentials = await BaiduProvider.exchange_refresh_token(
            req.refresh_token, app_key, app_secret
        )
    except Exception as e:
        raise HTTPException(400, f"刷新令牌无效: {e}")

    # Test the connection with detailed error reporting
    provider = BaiduProvider(credentials)
    try:
        test_ok, test_msg = await provider.test_connection_detailed()
    except Exception as e:
        test_ok = False
        test_msg = str(e)
    finally:
        await provider.close()

    if not test_ok:
        raise HTTPException(400, f"令牌有效但连接测试失败: {test_msg}")

    conn_id = await db.db_create_connection(req.name, "baidu", credentials, "connected")
    return {"id": conn_id, "name": req.name, "status": "connected"}


@router.get("/baidu/config")
async def get_baidu_config():
    """Get Baidu OAuth configuration status."""
    from app.config import get_baidu_app_credentials
    app_key, app_secret = await get_baidu_app_credentials()
    return {
        "app_key_configured": bool(app_key),
        "app_secret_configured": bool(app_secret),
        "redirect_uri": BAIDU_REDIRECT_URI,
    }


# ---- 115 Connection Endpoints ----

@router.get("/115/qr-token")
async def get_115_qr_token():
    """Get 115 QR code login token."""
    try:
        token_data = await Provider115.get_qr_token()
        return token_data
    except Exception as e:
        raise HTTPException(400, f"Failed to get QR token: {e}")


@router.get("/115/qr-image")
async def get_115_qr_image(token: str):
    """Get 115 QR code image as base64."""
    try:
        image_bytes = await Provider115.get_qr_image(token)
        b64 = base64.b64encode(image_bytes).decode()
        return {"image": f"data:image/png;base64,{b64}"}
    except Exception as e:
        raise HTTPException(400, f"Failed to get QR image: {e}")


@router.post("/115/qr-poll")
async def poll_115_qr_status(request: dict):
    """Poll 115 QR code login status."""
    try:
        result = await Provider115.poll_qr_login(
            request.get("uid", ""),
            request.get("token", ""),
            request.get("sign", ""),
            request.get("time", 0),
        )
        if result.get("status") == 2 and result.get("cookies"):
            # Login confirmed, but don't create connection yet
            # Return cookies for the frontend to create connection with a name
            return {
                "status": 2,
                "message": "Login confirmed",
                "cookies": result["cookies"],
            }
        return result
    except Exception as e:
        raise HTTPException(400, f"Poll failed: {e}")


@router.post("/115/cookies")
async def create_115_cookies_connection(req: Cookies115Request):
    """Create a 115 connection using cookies."""
    # Test connection
    provider = Provider115({"cookies": req.cookies})
    try:
        if not await provider.test_connection():
            raise HTTPException(400, "Failed to connect with provided cookies")
    finally:
        await provider.close()

    conn_id = await db.db_create_connection(req.name, "115", {"cookies": req.cookies}, "connected")
    return {"id": conn_id, "status": "connected"}


# ---- Common Connection Endpoints ----

@router.post("/{conn_id}/test")
async def test_connection(conn_id: int):
    """Test a connection."""
    conn = await db.db_get_connection(conn_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    provider = get_provider(conn["type"], conn["credentials"])
    try:
        success = await provider.test_connection()
        await db.db_update_connection(conn_id, status="connected" if success else "error")
        return {"connected": success}
    finally:
        await provider.close()


@router.delete("/{conn_id}")
async def delete_connection(conn_id: int):
    """Delete a connection and its tasks."""
    # Delete associated tasks first
    tasks = await db.db_list_tasks()
    for task in tasks:
        if task["connection_id"] == conn_id:
            await db.db_delete_task(task["id"])
    await db.db_delete_connection(conn_id)
    return {"deleted": True}


@router.get("/{conn_id}/browse")
async def browse_remote(conn_id: int, path: str = "/"):
    """Browse remote directory structure."""
    conn = await db.db_get_connection(conn_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    provider = get_provider(conn["type"], conn["credentials"])
    try:
        files = await provider.list_files(path)
        return {
            "path": path,
            "entries": [
                {
                    "path": f.path,
                    "name": f.name,
                    "is_dir": f.is_dir,
                    "size": f.size,
                    "modify_time": f.modify_time,
                }
                for f in files
            ],
        }
    except Exception as e:
        raise HTTPException(400, f"Browse failed: {e}")
    finally:
        await provider.close()
