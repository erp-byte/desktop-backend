"""WebSocket token issuance and connection endpoint."""

import logging
from datetime import datetime, timezone, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.modules.auth.middleware import get_current_user
from .broadcaster import manager

logger = logging.getLogger(__name__)

router = APIRouter()


class WSTokenResponse(BaseModel):
    token: str
    expires_in: int


@router.post("/api/v1/ws/token", response_model=WSTokenResponse)
async def issue_ws_token(request: Request, user=Depends(get_current_user)):
    """Issue a short-lived JWT for WebSocket authentication."""
    settings = request.app.state.settings
    secret = settings.WS_TOKEN_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="WebSocket not configured")

    expiry_minutes = settings.WS_TOKEN_EXPIRY_MINUTES
    exp = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)

    payload = {
        "sub": str(user.user_id),
        "role": user.role_name,
        "entity": user.entity,
        "exp": exp,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return WSTokenResponse(token=token, expires_in=expiry_minutes * 60)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint — authenticate via token query param, then receive events."""
    token = ws.query_params.get("token")
    if not token:
        await ws.accept()
        await ws.close(code=4001, reason="Missing token")
        return

    settings = ws.app.state.settings
    secret = settings.WS_TOKEN_SECRET
    if not secret:
        await ws.accept()
        await ws.close(code=4003, reason="WebSocket not configured")
        return

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        await ws.accept()
        await ws.close(code=4002, reason="Token expired")
        return
    except jwt.InvalidTokenError:
        await ws.accept()
        await ws.close(code=4001, reason="Invalid token")
        return

    user_id = int(payload["sub"])
    role = payload["role"]
    entity = payload["entity"]

    await ws.accept()
    ws_id = await manager.connect(ws, user_id, role, entity)

    try:
        # Keep connection alive — read and discard client messages (pings/pongs)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws_id)
