"""R12 notify-QC dispatcher.

A plug-in shaped service: today the dispatch is logged to
qc_notification_log_v2 and the registered hook is a no-op. When the
real transport (WhatsApp / push / email) is ready, the implementer
swaps the registered hook for the live function - no router or schema
change required.

Usage:

    # In your transport bootstrap (e.g. app.main on startup):
    from app.modules.production.services.qc_notify import register_qc_notify_hook

    async def my_real_hook(*, job_card_id, recipients, note, dispatched_by):
        # ... transport-specific send ...
        return {"delivered": True, "channel": "whatsapp", "ids": [...]}

    register_qc_notify_hook(my_real_hook)

    # Endpoint (POST /job-cards-v2/{id}/notify-qc) calls dispatch().

Contract for hook return values:
    {
        "delivered":   bool,          # was the message accepted by transport?
        "channel"?:    str,           # 'whatsapp' | 'push' | 'email' | ...
        "reason"?:     str,           # when delivered=False
        ...anything-else              # echoed into delivery_meta JSONB
    }
"""
from __future__ import annotations

import inspect
import logging
import traceback
from typing import Any, Awaitable, Callable

from app.core.helpers import insert_with_pk_retry, new_short_time_id

logger = logging.getLogger(__name__)


QCNotifyHook = Callable[..., Awaitable[dict]]


async def _default_noop_hook(**_kwargs: Any) -> dict:
    """The default hook: explicit no-op so the endpoint logs the
    dispatch attempt and signals 'no transport wired yet' to the
    caller. Replace via register_qc_notify_hook(...).
    """
    return {"delivered": False, "reason": "no_hook_registered"}


_HOOK: QCNotifyHook = _default_noop_hook


def register_qc_notify_hook(hook: QCNotifyHook) -> None:
    """Swap the active dispatch hook.

    Multi-worker note: each uvicorn worker imports this module
    independently, so each worker has its own `_HOOK` slot. Call this
    function from a FastAPI `startup` event (or any module-import-time
    path) so EVERY worker registers - registering via an HTTP endpoint
    only affects the worker that handled the request.

    B8 M4: validates the hook is an async callable up front so a
    sync-by-mistake hook surfaces at registration, not deep inside
    dispatch.
    """
    global _HOOK
    if not (callable(hook) and inspect.iscoroutinefunction(hook)):
        raise TypeError(
            "QC notify hook must be an async callable "
            f"(got {type(hook).__name__}, async={inspect.iscoroutinefunction(hook)})"
        )
    if _HOOK is not _default_noop_hook:
        logger.warning(
            "Replacing existing QC notify hook %s with %s",
            getattr(_HOOK, "__name__", _HOOK),
            getattr(hook, "__name__", hook),
        )
    _HOOK = hook
    logger.info("QC notify hook registered: %s", getattr(hook, "__name__", hook))


def get_qc_notify_hook() -> QCNotifyHook:
    """Inspect the current hook. Used by tests + admin diagnostics."""
    return _HOOK


async def dispatch(conn, *, job_card_id: int,
                   recipients: list[dict],
                   note: str | None,
                   dispatched_by: int | None) -> dict:
    """Run the registered hook for each recipient and log every attempt to
    qc_notification_log_v2. Returns a roll-up of per-recipient outcomes.

    `recipients` is a list of dicts shaped like
        {"user_id": int, "phone"?: str, "full_name"?: str}
    so the future transports have the channel-specific addressables to
    work with.

    The log row's delivery_status is one of:
        'delivered' - hook said {"delivered": True}
        'failed'    - hook said {"delivered": False} or raised

    B8 C2 / M1: the caller MUST pass a connection that is NOT inside an
    outer transaction. Each recipient gets its own short per-row txn so
    the (possibly slow) hook is invoked between txns - never holding a
    Postgres lock for the duration of the transport round-trip.
    Caller can verify with `conn.is_in_transaction()` if defensive.
    """
    outcomes: list[dict] = []
    for r in recipients:
        # B8 L2: pass a defensive copy so a misbehaving hook can't mutate
        # the dict and corrupt subsequent iterations.
        r_copy = dict(r)
        recipient_user_id = r_copy.get("user_id")
        try:
            hook_result = await _HOOK(
                job_card_id=job_card_id,
                recipient=r_copy,
                note=note,
                dispatched_by=dispatched_by,
            )
            if not isinstance(hook_result, dict):
                hook_result = {
                    "delivered": False,
                    "reason": f"hook_returned_non_dict: {type(hook_result).__name__}",
                }
        except Exception as exc:  # noqa: BLE001
            # B8 H1: capture exc_type + last few trace frames into the
            # log row so forensics doesn't need to time-match the app
            # log later.
            logger.exception("QC notify hook raised: %s", exc)
            hook_result = {
                "delivered": False,
                "reason": f"hook_error: {exc!r}",
                "exc_type": type(exc).__name__,
                "trace_tail": traceback.format_exc().splitlines()[-5:],
            }

        delivered       = bool(hook_result.get("delivered"))
        delivery_status = "delivered" if delivered else "failed"
        # B8 M3: channel falls back to 'hook' when missing OR explicitly None.
        channel = hook_result.get("channel") or "hook"

        async def _insert(
            _recipient_user_id=recipient_user_id,
            _dispatched_by=dispatched_by,
            _note=note,
            _hook_result=hook_result,
            _delivery_status=delivery_status,
            _channel=channel,
        ):
            return await conn.fetchrow(
                """
                INSERT INTO qc_notification_log_v2 (
                    notification_id, job_card_id, recipient_user_id,
                    dispatched_by, channel, note, delivery_status,
                    delivery_meta
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                RETURNING notification_id
                """,
                new_short_time_id(),
                job_card_id,
                _recipient_user_id,
                _dispatched_by,
                _channel,
                _note,
                _delivery_status,
                _serialize_meta(_hook_result),
            )

        # Per-recipient txn scope: the hook already ran (above, OUTSIDE
        # any txn). insert_with_pk_retry requires an outer txn, so open
        # a tight one just for this INSERT. Locks held: this row only,
        # for the duration of the INSERT (microseconds).
        async with conn.transaction():
            row = await insert_with_pk_retry(conn, _insert)
        outcomes.append({
            "recipient_user_id": recipient_user_id,
            "notification_id":   row["notification_id"],
            "delivered":         delivered,
            "reason":            hook_result.get("reason"),
        })

    return {
        "dispatched":   len([o for o in outcomes if o["delivered"]]),
        "failed":       len([o for o in outcomes if not o["delivered"]]),
        "outcomes":     outcomes,
    }


def _serialize_meta(hook_result: dict) -> str:
    """JSON-safe encoder for delivery_meta - drops non-serialisable values
    rather than crashing the whole notify call.

    B8 C1 fix: catches RecursionError (cyclic structures) and any
    Exception-derived issue so a misbehaving hook can never abort the
    dispatch loop. Worst case: delivery_meta carries a one-key sentinel
    explaining what went wrong instead of the hook result.
    """
    import json
    safe: dict = {}
    for k, v in (hook_result or {}).items():
        try:
            json.dumps(v)
            safe[k] = v
        except (TypeError, ValueError, RecursionError):
            try:
                safe[k] = repr(v)
            except Exception:  # noqa: BLE001
                safe[k] = "<unrepresentable>"
        except Exception as exc:  # noqa: BLE001
            safe[k] = f"<serialize_error: {type(exc).__name__}>"
    try:
        return json.dumps(safe)
    except (TypeError, ValueError, RecursionError) as exc:
        return json.dumps({
            "_serialize_error": f"{type(exc).__name__}: {exc}",
            "_keys": list(safe.keys()),
        })
