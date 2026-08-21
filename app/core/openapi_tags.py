"""Swagger UI grouping: every operation is tagged "<Module> - <Feature>".

Applied centrally rather than as `tags=[...]` on ~600 route decorators — one place to
change, and no way for a new endpoint to silently land in the wrong group (an untagged
route still gets a derived tag from its own path).

The feature is the first non-parameter path segment after the module prefix:

    /api/v1/transfer/transfer-in/{id}/acknowledge  ->  "Transfer - Transfer In"
    /api/v1/production/job-cards-v2/{id}           ->  "Production - Job Cards"
    /api/v1/so                                      ->  "Sales Orders - Core"

FEATURES exists to fix the two things path-derivation gets wrong: unreadable segments
("pending-stock"), and near-duplicate segments that should be ONE group in the UI
(job-cards + job-cards-v2, box-lookup + box-lookup-by-id + bulk-entry-box-lookup).
Anything unmapped falls back to a title-cased segment, so new routes group sensibly
with no edit here.
"""
from __future__ import annotations

from fastapi.routing import APIRoute

API_PREFIX = "/api/v1/"

# First path segment -> module display name.
MODULES: dict[str, str] = {
    "auth": "Auth",
    "so": "Sales Orders",
    "purchase": "Purchase",
    "po": "Purchase Orders",
    "receipt": "Receipt",
    "qc": "QC",
    "ncr": "NCR",
    "production": "Production",
    "amendments": "Amendments",
    "vendors": "Vendors",
    "sample": "Sample",
    "transfer": "Transfer",
    "job-work": "Job Work",
    "cold-storage": "Cold Storage",
    "customer-returns": "Customer Returns",
    "packing-details": "Packing Details",
    "lookups": "Lookups",
    "webhooks": "Webhooks",
    "ws": "WebSocket",
}

# Order modules appear in Swagger UI — business flow, not alphabetical.
MODULE_ORDER: list[str] = [
    "auth", "lookups",
    "so", "purchase", "po", "receipt",
    "qc", "ncr",
    "production", "amendments",
    "transfer", "job-work", "cold-storage",
    "sample", "vendors", "customer-returns", "packing-details",
    "webhooks", "ws",
]

# Per-module: raw path segment -> feature label. Several segments may map to the SAME
# label; that is the point — they collapse into one Swagger group.
FEATURES: dict[str, dict[str, str]] = {
    "transfer": {
        "requests": "Requests",
        "transfers": "Transfer Out",
        "transfer-in": "Transfer In",
        "pending-stock": "Pending Stock (In Transit)",
        "inner-transfer": "Inner Cold",
        "box-lookup": "Box Lookups",
        "box-lookup-by-id": "Box Lookups",
        "bulk-entry-box-lookup": "Box Lookups",
        "dropdowns": "Dropdowns & Search",
        "categorial-search": "Dropdowns & Search",
        "categorial-dropdown": "Dropdowns & Search",
        "dashboard": "Dashboard",
    },
    "job-work": {
        "out": "Material Out",
        "out-by-id": "Material Out",
        "material-in": "Material In",
        "all-sku-dropdown": "Article Picker",
        "all-sku-search": "Article Picker",
        "sku-detail": "Article Picker",
        "extract-excel": "Import",
        "extract-pdf": "Import",
        "reports": "Dashboard",
    },
    "production": {
        "job-cards": "Job Cards",
        "job-cards-v2": "Job Cards",
        "plans": "Plans",
        "plans-v2": "Plans",
        "fulfillment": "Fulfillment",
        "fulfillment-v2": "Fulfillment",
        "indents": "Indents",
        "production-indents": "Indents",
        "sfg-boxes": "SFG",
        "sfg-canonical": "SFG",
        "issue-notes": "Issue Notes",
        "material-documents": "Material Documents",
        "movement-types": "Material Documents",
        "balance-scan": "Scanning",
        "scan-identify": "Scanning",
        "boxes": "Scanning",
        "floor-inventory": "Floor Inventory",
        "floors": "Floor Inventory",
        "day-end": "Day End",
        "routing-gaps": "Routing",
        "offgrade": "Off-grade & Loss",
        "loss": "Off-grade & Loss",
        "discrepancy": "Off-grade & Loss",
        "rtv": "RTV",
    },
    "so": {
        "update": "Updates",
        "update-confirm": "Updates",
        "update-preview": "Updates",
        "upload": "Upload",
        "upload-so-book": "Upload",
        "sku-lookup": "Lookups",
        "gst-reconciliation": "GST Reconciliation",
    },
    "vendors": {
        "documents": "Documents",
        "with-documents": "Documents",
        "extract-bulk": "Documents",
        "submit-staged": "Onboarding",
        "approve": "Onboarding",
        "paged": "Listing",
        "search": "Listing",
    },
    "customer-returns": {
        "box": "Boxes",
        "boxes": "Boxes",
        "box-edit-log": "Boxes",
        "approve": "Approval",
        "send-for-approval": "Approval",
        "email-action": "Email",
        "email-routing": "Email",
    },
    "po": {
        "walk-in-intimation": "Intimation",
        "intimation": "Intimation",
        "receipt-summary": "Receipts",
    },
    "receipt": {
        "coa": "CoA",
        "coa-upload": "CoA",
    },
    "sample": {
        "npd-dev-job-cards": "NPD",
        "npd-drafts": "NPD",
        "npd-requisitions": "NPD",
        "requisitions": "Requisitions",
        "rm-issue-forms": "RM Issue Forms",
        "bom-browse": "BOMs",
        "boms": "BOMs",
        "sales-pocs": "Contacts",
        "business-heads": "Contacts",
        "whatsapp": "Notifications",
        "email": "Notifications",
    },
    "packing-details": {
        "batch-token": "Public Access",
        "by-encrypted-batch": "Public Access",
        "public": "Public Access",
    },
}

# Only where the group name alone is genuinely ambiguous.
TAG_DESCRIPTIONS: dict[str, str] = {
    "Transfer - Pending Stock (In Transit)":
        "Boxes dispatched but not yet received — deducted from the source, not yet posted "
        "to the destination.",
    "Transfer - Inner Cold":
        "Lot re-labelling inside one cold store. Does not move stock between units.",
    "Job Work - Material Out":
        "Material dispatched to a third party for processing, against a delivery challan. "
        "Creating one DELETES the scanned boxes from cold storage.",
    "Transfer - Box Lookups":
        "Resolve a scanned or typed box for the Transfer-Out form (TR- inward, BE- bulk "
        "entry, CR- customer return).",
}

# Segments that must not be lower-cased by the title-caser.
ACRONYMS = {
    "ai": "AI", "api": "API", "bom": "BOM", "capa": "CAPA", "coa": "CoA", "fg": "FG",
    "gst": "GST", "id": "ID", "mrp": "MRP", "ncr": "NCR", "npd": "NPD", "po": "PO",
    "qc": "QC", "rm": "RM", "rtv": "RTV", "sfg": "SFG", "sku": "SKU", "so": "SO",
    "ws": "WS",
}

ROOT_FEATURE = "Core"       # module-root operations, e.g. GET/POST /api/v1/so
SYSTEM_TAG = "System"       # /health, /internal/*, anything outside /api/v1


def _titleize(segment: str) -> str:
    """"pending-stock" -> "Pending Stock"; "sfg-boxes" -> "SFG Boxes"."""
    return " ".join(ACRONYMS.get(w, w.capitalize()) for w in segment.split("-") if w)


def tag_for_path(path: str) -> str:
    """The "<Module> - <Feature>" tag a path belongs to."""
    if not path.startswith(API_PREFIX):
        return SYSTEM_TAG
    parts = [p for p in path[len(API_PREFIX):].split("/") if p]
    if not parts:
        return SYSTEM_TAG
    module_seg, *rest = parts
    module = MODULES.get(module_seg, _titleize(module_seg))
    # First segment that is not a path parameter ({id}, {challan_no}, …).
    feature_seg = next((s for s in rest if not s.startswith("{")), "")
    if not feature_seg:
        return f"{module} - {ROOT_FEATURE}"
    feature = FEATURES.get(module_seg, {}).get(feature_seg) or _titleize(feature_seg)
    return f"{module} - {feature}"


def _sort_key(tag: str) -> tuple[int, str, int, str]:
    """Modules in MODULE_ORDER, then features alphabetically with Core first."""
    module, _, feature = tag.partition(" - ")
    seg = next((s for s, name in MODULES.items() if name == module), None)
    module_rank = MODULE_ORDER.index(seg) if seg in MODULE_ORDER else len(MODULE_ORDER)
    if tag == SYSTEM_TAG:
        module_rank = len(MODULE_ORDER) + 1
    return (module_rank, module, 0 if feature == ROOT_FEATURE else 1, feature)


def apply_module_feature_tags(app) -> list[dict[str, str]]:
    """Retag every route as "<Module> - <Feature>" and return the ordered tag metadata
    for `app.openapi_tags`. Call AFTER every include_router and BEFORE the schema is
    first generated (FastAPI caches it in app.openapi_schema).

    Route-level tags are REPLACED, not appended: the router-level tag would otherwise
    also apply and every operation would show up under two groups.
    """
    seen: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        tag = tag_for_path(route.path)
        route.tags = [tag]
        seen.add(tag)
    return [
        {"name": t, **({"description": TAG_DESCRIPTIONS[t]} if t in TAG_DESCRIPTIONS else {})}
        for t in sorted(seen, key=_sort_key)
    ]
