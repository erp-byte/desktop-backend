"""Webhook management API — CRUD for endpoints, subscriptions, delivery log."""

import asyncio
import json
import logging
import secrets
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.modules.auth.middleware import require_permission
from .event_bus import Event, event_bus
from .signer import sign_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


# ── Schemas ──

class EndpointCreate(BaseModel):
    entity: str
    url: str
    description: str = ""

class EndpointUpdate(BaseModel):
    url: str | None = None
    description: str | None = None
    is_active: bool | None = None

class SubscriptionCreate(BaseModel):
    endpoint_id: int
    event_type: str


# ── Endpoints ──

@router.post("/endpoints")
async def create_endpoint(body: EndpointCreate, request: Request,
                          user=Depends(require_permission("production", "webhooks", action="create"))):
    pool = request.app.state.db_pool
    secret = secrets.token_hex(32)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_endpoint (entity, url, secret, description, created_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, entity, url, description, is_active, created_at
            """,
            body.entity, body.url, secret, body.description, user.full_name,
        )
    result = dict(row)
    result["secret"] = secret  # Only returned on creation
    return result


@router.get("/endpoints")
async def list_endpoints(request: Request, entity: str = "",
                         user=Depends(require_permission("production", "webhooks", action="view"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if entity:
            rows = await conn.fetch(
                "SELECT id, entity, url, description, is_active, created_by, created_at FROM webhook_endpoint WHERE entity = $1 ORDER BY id",
                entity,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, entity, url, description, is_active, created_by, created_at FROM webhook_endpoint ORDER BY id"
            )
    return [dict(r) for r in rows]


@router.put("/endpoints/{endpoint_id}")
async def update_endpoint(endpoint_id: int, body: EndpointUpdate, request: Request,
                          user=Depends(require_permission("production", "webhooks", action="edit"))):
    pool = request.app.state.db_pool
    updates, params, idx = [], [], 1
    if body.url is not None:
        updates.append(f"url=${idx}"); params.append(body.url); idx += 1
    if body.description is not None:
        updates.append(f"description=${idx}"); params.append(body.description); idx += 1
    if body.is_active is not None:
        updates.append(f"is_active=${idx}"); params.append(body.is_active); idx += 1
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates.append(f"updated_at=NOW()")
    params.append(endpoint_id)
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE webhook_endpoint SET {', '.join(updates)} WHERE id=${idx}", *params,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, "Endpoint not found")
    return {"updated": True}


@router.delete("/endpoints/{endpoint_id}")
async def deactivate_endpoint(endpoint_id: int, request: Request,
                              user=Depends(require_permission("production", "webhooks", action="delete"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE webhook_endpoint SET is_active = FALSE, updated_at = NOW() WHERE id = $1",
            endpoint_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, "Endpoint not found")
    return {"deactivated": True}


# ── Subscriptions ──

@router.post("/subscriptions")
async def create_subscription(body: SubscriptionCreate, request: Request,
                               user=Depends(require_permission("production", "webhooks", action="create"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO webhook_subscription (endpoint_id, event_type)
                VALUES ($1, $2)
                RETURNING id, endpoint_id, event_type, is_active, created_at
                """,
                body.endpoint_id, body.event_type,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Subscription already exists")
            raise
    return dict(row)


@router.get("/subscriptions")
async def list_subscriptions(request: Request, endpoint_id: int = 0,
                              user=Depends(require_permission("production", "webhooks", action="view"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if endpoint_id:
            rows = await conn.fetch(
                "SELECT * FROM webhook_subscription WHERE endpoint_id = $1 ORDER BY id", endpoint_id,
            )
        else:
            rows = await conn.fetch("SELECT * FROM webhook_subscription ORDER BY id")
    return [dict(r) for r in rows]


@router.delete("/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int, request: Request,
                               user=Depends(require_permission("production", "webhooks", action="delete"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM webhook_subscription WHERE id = $1", sub_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Subscription not found")
    return {"deleted": True}


# ── Deliveries ──

@router.get("/deliveries")
async def list_deliveries(request: Request,
                           endpoint_id: Optional[int] = None, event_type: Optional[str] = None,
                           status: Optional[str] = None, page: int = 1, page_size: int = 50,
                           user=Depends(require_permission("production", "webhooks", action="view"))):
    pool = request.app.state.db_pool
    conditions, params, idx = [], [], 1
    if endpoint_id:
        conditions.append(f"endpoint_id=${idx}"); params.append(endpoint_id); idx += 1
    if event_type:
        conditions.append(f"event_type=${idx}"); params.append(event_type); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM webhook_delivery WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT id, endpoint_id, event_type, event_id, status, attempts, last_attempt_at, response_code, response_body, created_at FROM webhook_delivery WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, page_size, offset,
        )
    return {"total": total, "page": page, "results": [dict(r) for r in rows]}


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(delivery_id: int, request: Request,
                          user=Depends(require_permission("production", "webhooks", action="edit"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM webhook_delivery WHERE id = $1 AND status IN ('failed', 'exhausted')",
            delivery_id,
        )
        if not row:
            raise HTTPException(404, "Delivery not found or not in failed/exhausted status")
        await conn.execute(
            "UPDATE webhook_delivery SET status = 'pending', attempts = 0 WHERE id = $1",
            delivery_id,
        )
    # Re-dispatch directly to the webhook endpoint (not the bus, to avoid duplicate WS notifications)
    async with pool.acquire() as conn:
        ep = await conn.fetchrow(
            "SELECT id AS endpoint_id, url, secret, entity FROM webhook_endpoint WHERE id = $1",
            row["endpoint_id"],
        )
    if not ep:
        raise HTTPException(404, "Associated endpoint no longer exists")
    payload_data = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    event = Event(
        event_type=row["event_type"],
        entity=ep["entity"],
        payload=payload_data.get("data", payload_data),
        event_id=row["event_id"],
        actor=payload_data.get("actor", "system"),
    )
    from .dispatcher import retry_single_delivery
    asyncio.create_task(retry_single_delivery(pool, ep, event, delivery_id))
    return {"retried": True, "delivery_id": delivery_id}


@router.post("/test")
async def test_webhook(request: Request,
                        endpoint_id: int = 0, url: str = "",
                        user=Depends(require_permission("production", "webhooks", action="create"))):
    """Send a test ping event to a webhook endpoint."""
    if endpoint_id:
        pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            ep = await conn.fetchrow("SELECT url, secret FROM webhook_endpoint WHERE id = $1", endpoint_id)
        if not ep:
            raise HTTPException(404, "Endpoint not found")
        target_url = ep["url"]
        secret = ep["secret"]
    elif url:
        target_url = url
        secret = "test-secret"
    else:
        raise HTTPException(400, "Provide endpoint_id or url")

    body = json.dumps({"event_id": "test", "event_type": "ping", "timestamp": "", "actor": "test", "data": {}})
    signature = sign_payload(secret, body)
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": signature, "X-Webhook-Event": "ping"}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(target_url, content=body, headers=headers)
            return {"status_code": resp.status_code, "body": resp.text[:500]}
        except Exception as e:
            return {"error": str(e)}
