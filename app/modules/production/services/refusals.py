"""A service's refusal ({"error": code, "message": ...}) as an HTTP error.

Services in this module return refusals as dicts rather than raising. Routes
must check them INSIDE their transaction — raise_if_refused() — so whatever the
service wrote before refusing rolls back, and must never pass one back as a 200
(the screen then reads it as success: "Created undefined job cards"). to_http()
gives every code an error status; an unknown code is still a 400.
"""
from __future__ import annotations

from fastapi import HTTPException

NOT_FOUND = frozenset({
    "line_not_found", "job_card_not_found", "no_job_cards", "not_found", "plan_not_found",
    "not_found_or_already_cancelled", "not_found_or_invalid_status",
})
CONFLICT = frozenset({
    "plan_has_started_cards", "plan_incomplete", "plan_closed", "below_carded",
    "job_cards_already_exist", "already_carded", "merge_conflict",
    "line_fully_carded", "exceeds_balance", "not_editable",
    "cannot_remove_terminal", "cannot_remove_started_midchain",
    "cannot_reorder_started_region",
    "jc_terminal", "no_next_stage", "chain_broken", "over_dispatch",
    "multiple_chains", "merged_run", "nothing_to_distribute", "no_open_packaging",
    "packaging_has_previous_stage",
    "not_a_group", "already_started", "no_common_rm", "bom_changes_on_merged_lines",
})


class ServiceRefusal(Exception):
    """Raised inside a transaction so it rolls back; carries the service's dict."""

    def __init__(self, result: dict):
        super().__init__(result.get("error"))
        self.result = result


def raise_if_refused(result):
    if isinstance(result, dict) and result.get("error"):
        raise ServiceRefusal(result)
    return result


def refusal_status(code: str | None) -> int:
    if code in NOT_FOUND:
        return 404
    if code in CONFLICT:
        return 409
    return 400


def to_http(refusal: ServiceRefusal, *, messages: dict[str, str] | None = None) -> HTTPException:
    detail = dict(refusal.result)
    code = detail.get("error")
    if messages and code in messages:
        detail["message"] = messages[code]
    if not detail.get("message"):
        detail["message"] = str(code or "error").replace("_", " ").capitalize()
    return HTTPException(status_code=refusal_status(code), detail=detail)
