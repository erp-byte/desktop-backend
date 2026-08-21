"""Accounting CRUD — invariants that only a real Postgres can prove.

This service is almost entirely SQL: per-line diffing, ON CONFLICT inference
against partial vs non-partial indexes, and NUMERIC round-tripping. A fake conn
cannot check any of that — the three bugs this suite was written after all
slipped past hand-review and only surfaced against a live database:

  1. consumption.uom / issued_qty are NOT NULL with no default, so the INSERT
     raised NotNullViolationError.
  2. The additives unique index is PARTIAL, so ON CONFLICT had to repeat
     `WHERE deleted_at IS NULL` or Postgres refused to match it.
  3. issued_qty was missing from the quantity-comparison set, so Decimal('0.000')
     compared against float 0.0 AS TEXT and reported a phantom change on every
     save.

Everything runs inside a transaction that is always rolled back, so the target
database is left byte-identical.

Skipped unless DATABASE_URL is set AND migration 092 has been applied. Run:
    PYTHONPATH=. python -m pytest tests/services/test_accounting_crud.py
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from app.modules.production.services import jc_accounting_crud as svc  # noqa: E402


def _dsn() -> str | None:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DATABASE_URL="):
            return line.strip().split("=", 1)[1]
    return None


DSN = _dsn()
pytestmark = pytest.mark.skipif(DSN is None, reason="no DATABASE_URL configured")


async def _pick_fixture(conn):
    """An unlocked, non-terminal JC that owns at least one batch, plus a
    bom_line_id that actually exists — FKs are enforced, and a hard-coded id
    from another environment fails."""
    row = await conn.fetchrow(
        """
        SELECT j.job_card_id, j.plan_id, j.bom_id, b.batch_id
        FROM   job_card_v2 j
        JOIN   job_card_batch_v2 b ON b.job_card_id = j.job_card_id
        WHERE  j.deleted_at IS NULL AND j.is_locked = FALSE
          AND  j.status NOT IN ('completed', 'closed', 'cancelled')
          AND  b.status = 'open'
        ORDER  BY j.job_card_id
        LIMIT  1
        """
    )
    if row is None:
        return None
    bom_line_id = await conn.fetchval(
        "SELECT bom_line_id FROM bom_line WHERE bom_id=$1 ORDER BY bom_line_id LIMIT 1",
        row["bom_id"],
    )
    if bom_line_id is None:
        return None
    material = await conn.fetchval(
        "SELECT material_sku_name FROM bom_line WHERE bom_line_id=$1", bom_line_id)
    return {"job_card_id": row["job_card_id"], "plan_id": row["plan_id"],
            "batch_id": row["batch_id"], "bom_line_id": bom_line_id,
            "material": material}


def _payload(fx: dict) -> dict:
    return {
        "output_qty_kg": 149.8, "output_qty_units": None,
        "output_kind": "SFG", "uom": "KGS",
        "rm_consumed_kg": 150.0,
        "process_loss_kg": 0.0, "process_loss_remark": None,
        "rm_consumed": [{"bom_line_id": fx["bom_line_id"],
                         "material_sku_name": fx["material"],
                         "consumed_qty": 150.0, "input_kind": "RM",
                         "source_dispatch_id": None, "remarks": None}],
        "pm_consumed": [],
        "byproducts": [{"category": "dust", "qty_kg": 0.2, "uom": "KGS",
                        "material_name": fx["material"],
                        "bom_line_id": fx["bom_line_id"], "remarks": None}],
        "balance_materials": [{"material_name": fx["material"],
                               "balance_type": "returned", "qty_kg": 0.0,
                               "bom_line_id": fx["bom_line_id"],
                               "material_id": None, "remarks": None}],
        "additives": [{"sku_name": "Salt", "material_name": None,
                       "qty_kg": 0.0, "remarks": None}],
        "qc": {"passed": True, "remarks": None, "corrective_action": None,
               "inspector": "QC Test"},
        "notes": None, "admin_override": False,
    }


@pytest_asyncio.fixture
async def ctx():
    """Connection + fixture ids inside an always-rolled-back transaction."""
    conn = await asyncpg.connect(DSN, timeout=30)
    try:
        has_092 = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='job_card_output_v2' AND column_name='deleted_at')")
        if not has_092:
            pytest.skip("migration 092 not applied to this database")
        fx = await _pick_fixture(conn)
        if fx is None:
            pytest.skip("no suitable job card / batch / bom_line fixture")
        tx = conn.transaction()
        await tx.start()
        try:
            for t in ("job_card_output_v2", "job_card_material_consumption_v2",
                      "job_card_byproducts_v2", "job_card_balance_material_v2",
                      "job_card_additive_consumption_v2", "job_card_qc_v2",
                      "job_card_accounting_v2"):
                await conn.execute(f"DELETE FROM {t} WHERE job_card_id=$1",
                                   fx["job_card_id"])
            yield conn, fx
        finally:
            await tx.rollback()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_create_persists_every_field(ctx):
    """All six sections round-trip — the "make sure all fields are saved" ask."""
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    created = await svc.create_record(conn, **ids, payload=_payload(fx), actor="t")
    assert created.get("created"), created

    got = await svc.get_record(conn, **ids)
    assert got["output_qty_kg"] == 149.8
    assert got["output_kind"] == "SFG"
    assert got["uom"] == "KGS"
    assert got["rm_consumed_kg"] == 150.0
    assert len(got["rm_consumed"]) == 1
    assert got["rm_consumed"][0]["consumed_qty"] == 150.0
    assert got["rm_consumed"][0]["bom_line_id"] == fx["bom_line_id"]
    assert got["pm_consumed"] == []
    assert len(got["byproducts"]) == 1 and got["byproducts"][0]["qty_kg"] == 0.2
    assert len(got["balance_materials"]) == 1
    assert len(got["additives"]) == 1 and got["additives"][0]["sku_name"] == "Salt"
    assert got["qc"]["passed"] is True and got["qc"]["inspector"] == "QC Test"
    # Balance derived server-side: 150 in == 149.8 out + 0.2 off-grade.
    assert got["balance"]["is_balanced"] is True
    assert got["balance"]["balance_difference_qty"] == 0.0


@pytest.mark.asyncio
async def test_update_writes_only_what_changed(ctx):
    """The stated update rule: compare field by field, write only differences."""
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_payload(fx), actor="t")

    p = _payload(fx)
    p["output_qty_kg"] = 148.5          # one scalar
    p["byproducts"][0]["qty_kg"] = 1.5  # one line
    res = await svc.update_record(conn, **ids, payload=p, actor="t")

    assert [c["field"] for c in res["changes"]["output"]] == ["output_qty_kg"]
    assert res["changes"]["byproducts"]["updated"] == 1
    # Untouched sections must not be rewritten — this is what keeps the JC edit
    # log a record of real operator edits rather than save noise.
    assert res["changes"]["consumption"]["unchanged"] == 1
    assert res["changes"]["consumption"]["updated"] == 0
    assert res["changes"]["balance_materials"]["unchanged"] == 1
    assert res["changes"]["additives"]["unchanged"] == 1


@pytest.mark.asyncio
async def test_resaving_identical_payload_writes_nothing(ctx):
    """Regression for the Decimal-vs-float trap: NUMERIC comes back as Decimal,
    and any numeric column missing from _QTY_COLS compares AS TEXT
    ('0.000' != '0.0'), reporting a phantom change on every save."""
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_payload(fx), actor="t")

    res = await svc.update_record(conn, **ids, payload=_payload(fx), actor="t")
    assert res["changes"]["output"] == []
    writes = sum(res["changes"][s][k]
                 for s in ("consumption", "byproducts", "balance_materials", "additives")
                 for k in ("inserted", "updated", "deleted"))
    assert writes == 0, res["changes"]


@pytest.mark.asyncio
async def test_dropped_line_can_be_re_added(ctx):
    """A soft-deleted row still occupies its (non-partial) unique key, so the
    insert path has to resurrect rather than collide."""
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_payload(fx), actor="t")

    dropped = _payload(fx)
    dropped["byproducts"] = []
    await svc.update_record(conn, **ids, payload=dropped, actor="t")
    assert (await svc.get_record(conn, **ids))["byproducts"] == []

    await svc.update_record(conn, **ids, payload=_payload(fx), actor="t")
    back = await svc.get_record(conn, **ids)
    assert len(back["byproducts"]) == 1
    assert back["byproducts"][0]["qty_kg"] == 0.2


@pytest.mark.asyncio
async def test_identity_guards(ctx):
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_payload(fx), actor="t")

    wrong_plan = await svc.get_record(conn, job_card_id=fx["job_card_id"],
                                      plan_id=11111111, batch_id=fx["batch_id"])
    assert wrong_plan["error"] == "plan_mismatch"

    again = await svc.create_record(conn, **ids, payload=_payload(fx), actor="t")
    assert again["error"] == "record_exists"

    dup = _payload(fx)
    dup["rm_consumed"].append(dict(dup["rm_consumed"][0]))
    clash = await svc.update_record(conn, **ids, payload=dup, actor="t")
    assert clash["error"] == "duplicate_line"


@pytest.mark.asyncio
async def test_delete_is_soft_and_never_leaves_an_empty_record_balanced(ctx):
    """Deleting must not hand the R9 close gate a free pass.

    An emptied record has total_input 0, which puts save_accounting on its
    ABSOLUTE tolerance branch (|diff| <= 0.05) and computes as balanced. A record
    with no rows is not balanced, it is absent — delete_record stamps
    is_balanced FALSE so the job card cannot close on deleted figures.
    """
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_payload(fx), actor="t")

    res = await svc.delete_record(conn, **ids, actor="t")
    assert res["deleted"] is True
    assert res["record_empty"] is True
    assert res["balance"]["is_balanced"] is False, res["balance"]

    assert (await svc.get_record(conn, **ids))["error"] == "record_not_found"

    # Soft: the rows are still physically there, carrying their audit stamp.
    surviving = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_material_consumption_v2 "
        "WHERE job_card_id=$1 AND deleted_at IS NOT NULL", fx["job_card_id"])
    assert surviving >= 1

    gate = await conn.fetchval(
        "SELECT BOOL_AND(COALESCE(is_balanced, FALSE)) FROM job_card_accounting_v2 "
        "WHERE job_card_id=$1", fx["job_card_id"])
    assert gate is False


# ---------------------------------------------------------------------------
# Full-field persistence — read back from the RAW TABLES, not through
# get_record(), so a shaping bug cannot stand in for a persistence bug. Every
# value below is distinct and non-default: a field that silently failed to save
# would still read back as 0/None and pass a weaker check.
# ---------------------------------------------------------------------------

_FULL = {
    "output_qty_kg": 149.8, "output_qty_units": 1234.0,
    "output_kind": "SFG", "uom": "KGS", "rm_consumed_kg": 150.0,
    "process_loss_kg": 0.35, "process_loss_remark": "moisture loss during sorting",
    "notes": "full-field persistence audit note", "admin_override": False,
    "pm_consumed": [],
}


def _full_payload(fx):
    p = dict(_FULL)
    p["rm_consumed"] = [{"bom_line_id": fx["bom_line_id"],
                         "material_sku_name": fx["material"], "consumed_qty": 150.0,
                         "input_kind": "RM", "source_dispatch_id": None,
                         "remarks": "rm line remark"}]
    p["byproducts"] = [{"category": "dust", "qty_kg": 0.2, "uom": "KGS",
                        "material_name": fx["material"],
                        "bom_line_id": fx["bom_line_id"], "remarks": "byproduct remark"}]
    p["balance_materials"] = [{"material_name": fx["material"], "balance_type": "returned",
                               "qty_kg": 0.45, "bom_line_id": fx["bom_line_id"],
                               "material_id": 777, "remarks": "balance remark"}]
    p["additives"] = [{"sku_name": "Salt", "material_name": None, "qty_kg": 0.12,
                       "remarks": "additive remark"}]
    p["qc"] = {"passed": True, "remarks": "qc findings text",
               "corrective_action": "corrective action text", "inspector": "QC Ravi"}
    return p


def _num(v):
    from decimal import Decimal
    return float(v) if isinstance(v, Decimal) else v


@pytest.mark.asyncio
async def test_every_field_reaches_the_database(ctx):
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    assert (await svc.create_record(conn, **ids, payload=_full_payload(fx),
                                    actor="audit")).get("created")
    jc, b = fx["job_card_id"], fx["batch_id"]

    o = await conn.fetchrow("SELECT * FROM job_card_output_v2 WHERE job_card_id=$1 "
                            "AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert o is not None, "no output row persisted"
    assert _num(o["output_qty_kg"]) == 149.8
    assert _num(o["output_qty_units"]) == 1234.0
    assert o["output_kind"] == "SFG" and o["uom"] == "KGS"
    assert _num(o["rm_consumed_kg"]) == 150.0
    assert _num(o["process_loss_kg"]) == 0.35
    assert o["process_loss_remark"] == "moisture loss during sorting"
    assert o["notes"] == "full-field persistence audit note"
    assert o["recorded_by"] == "audit"

    r = await conn.fetchrow("SELECT * FROM job_card_material_consumption_v2 "
                            "WHERE job_card_id=$1 AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert _num(r["actual_consumed_qty"]) == 150.0
    assert r["input_kind"] == "RM" and r["uom"] == "KGS"
    assert _num(r["issued_qty"]) == 0.0          # NOT NULL with no default
    assert r["bom_line_id"] == fx["bom_line_id"]
    assert r["remarks"] == "rm line remark"

    bp = await conn.fetchrow("SELECT * FROM job_card_byproducts_v2 WHERE job_card_id=$1 "
                             "AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert bp["category"] == "dust" and _num(bp["quantity"]) == 0.2
    assert bp["uom"] == "KGS" and bp["material_name"] == fx["material"]
    assert bp["bom_line_id"] == fx["bom_line_id"] and bp["remarks"] == "byproduct remark"

    bm = await conn.fetchrow("SELECT * FROM job_card_balance_material_v2 WHERE job_card_id=$1 "
                             "AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert bm["balance_type"] == "returned" and _num(bm["qty_kg"]) == 0.45
    assert bm["material_id"] == 777 and bm["remarks"] == "balance remark"

    ad = await conn.fetchrow("SELECT * FROM job_card_additive_consumption_v2 "
                             "WHERE job_card_id=$1 AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert ad["sku_name"] == "Salt" and _num(ad["qty_kg"]) == 0.12
    assert ad["remarks"] == "additive remark"

    qc = await conn.fetchrow("SELECT * FROM job_card_qc_v2 WHERE job_card_id=$1 "
                             "AND COALESCE(batch_id,0)=$2 AND deleted_at IS NULL", jc, b)
    assert qc["result"] == "pass" and qc["findings"] == "qc findings text"
    assert qc["corrective_action"] == "corrective action text"
    assert qc["inspector_user"] == "QC Ravi"

    ac = await conn.fetchrow("SELECT * FROM job_card_accounting_v2 WHERE job_card_id=$1 "
                             "AND COALESCE(batch_id,0)=$2", jc, b)
    assert _num(ac["total_input_qty"]) == 150.0
    assert _num(ac["output_qty"]) == 149.8
    assert _num(ac["process_loss_qty"]) == 0.35
    assert _num(ac["offgrade_total_qty"]) == 0.2
    assert _num(ac["balance_material_qty"]) == 0.45
    # 149.8 + 0.35 + 0.2 + 0.45 = 150.80 -> genuinely unbalanced, and it says so.
    assert _num(ac["total_accounted_qty"]) == 150.8
    assert _num(ac["balance_difference_qty"]) == -0.8
    assert ac["is_balanced"] is False


@pytest.mark.asyncio
async def test_wire_model_drops_no_field(ctx):
    """Over HTTP the body is parsed by AccountingRecordBody BEFORE the service
    sees it, so a field missing from that model is dropped in production while a
    service-level test still passes. Diff the parsed payload against the wire."""
    from app.modules.production.router_accounting_crud import AccountingRecordBody
    conn, fx = ctx
    wire = _full_payload(fx)
    parsed = AccountingRecordBody(**wire).model_dump()

    def walk(prefix, sent, got, out):
        if isinstance(sent, dict):
            for k, v in sent.items():
                if not isinstance(got, dict) or k not in got:
                    out.append(f"{prefix}{k} absent from model")
                    continue
                walk(f"{prefix}{k}.", v, got[k], out)
        elif isinstance(sent, list):
            for i, v in enumerate(sent):
                walk(f"{prefix}[{i}].", v, got[i], out)
        elif sent != got:
            out.append(f"{prefix.rstrip('.')}: sent {sent!r}, model gave {got!r}")

    problems = []
    walk("", wire, parsed, problems)
    assert not problems, problems


@pytest.mark.asyncio
async def test_updated_values_actually_land_in_the_tables(ctx):
    """update_record REPORTS what it changed; that is not proof the new value
    reached the row. Read the changed values back with raw SQL."""
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_full_payload(fx), actor="audit")

    p = _full_payload(fx)
    p.update({"output_qty_kg": 148.25, "output_qty_units": 999.0,
              "output_kind": "WIP", "uom": "GMS", "rm_consumed_kg": 151.5,
              "process_loss_kg": 0.75, "process_loss_remark": "UPDATED loss",
              "notes": "UPDATED note"})
    p["rm_consumed"][0].update({"consumed_qty": 151.5, "remarks": "UPDATED rm"})
    p["byproducts"][0].update({"qty_kg": 1.75, "remarks": "UPDATED bp"})
    p["balance_materials"][0].update({"qty_kg": 0.99, "remarks": "UPDATED bm"})
    p["additives"][0].update({"qty_kg": 0.88, "remarks": "UPDATED add"})
    p["qc"] = {"passed": False, "remarks": "UPDATED findings",
               "corrective_action": "UPDATED corrective", "inspector": "QC Meera"}
    await svc.update_record(conn, **ids, payload=p, actor="audit")

    jc, b = fx["job_card_id"], fx["batch_id"]
    o = await conn.fetchrow("SELECT * FROM job_card_output_v2 WHERE job_card_id=$1 "
                            "AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert _num(o["output_qty_kg"]) == 148.25 and _num(o["output_qty_units"]) == 999.0
    assert o["output_kind"] == "WIP" and o["uom"] == "GMS"
    assert _num(o["rm_consumed_kg"]) == 151.5 and _num(o["process_loss_kg"]) == 0.75
    assert o["process_loss_remark"] == "UPDATED loss" and o["notes"] == "UPDATED note"

    r = await conn.fetchrow("SELECT * FROM job_card_material_consumption_v2 "
                            "WHERE job_card_id=$1 AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert _num(r["actual_consumed_qty"]) == 151.5 and r["remarks"] == "UPDATED rm"

    bp = await conn.fetchrow("SELECT * FROM job_card_byproducts_v2 WHERE job_card_id=$1 "
                             "AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert _num(bp["quantity"]) == 1.75 and bp["remarks"] == "UPDATED bp"

    bm = await conn.fetchrow("SELECT * FROM job_card_balance_material_v2 WHERE job_card_id=$1 "
                             "AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert _num(bm["qty_kg"]) == 0.99 and bm["remarks"] == "UPDATED bm"

    ad = await conn.fetchrow("SELECT * FROM job_card_additive_consumption_v2 "
                             "WHERE job_card_id=$1 AND batch_id=$2 AND deleted_at IS NULL", jc, b)
    assert _num(ad["qty_kg"]) == 0.88 and ad["remarks"] == "UPDATED add"

    qc = await conn.fetchrow("SELECT * FROM job_card_qc_v2 WHERE job_card_id=$1 "
                             "AND COALESCE(batch_id,0)=$2 AND deleted_at IS NULL", jc, b)
    assert qc["result"] == "fail" and qc["findings"] == "UPDATED findings"
    assert qc["corrective_action"] == "UPDATED corrective"
    assert qc["inspector_user"] == "QC Meera"

    ac = await conn.fetchrow("SELECT * FROM job_card_accounting_v2 WHERE job_card_id=$1 "
                             "AND COALESCE(batch_id,0)=$2", jc, b)
    assert _num(ac["total_input_qty"]) == 151.5
    assert _num(ac["output_qty"]) == 148.25
    assert _num(ac["offgrade_total_qty"]) == 1.75


# ---------------------------------------------------------------------------
# close_batch x accounting record — the interaction that migration 092 created.
#
# job_card_output_v2 has TWO writers: the accounting record (this module) and
# close_batch. The table used to be append-only, which is why production
# accumulated 453 duplicate (job_card_id, batch_id) groups. 092 collapses those
# and pins ONE LIVE ROW PER BATCH with a partial unique index.
#
# That made close_batch's bare INSERT a landmine: the moment a batch with an
# accounting record is closed, it raises UniqueViolationError. insert_with_pk_retry
# only swallows *_pkey violations (core/helpers.py:103) and re-raises the rest, so
# the whole close transaction — batch close, auto-dispatch, downstream unlock —
# rolls back. Every batch close on a JC with saved accounting would 500.
#
# close_batch was changed to an upsert (job_card_batch_v2.py:444). These tests
# exist because that fix had NO coverage: nothing in the suite called close_batch,
# so the regression could return silently.
# ---------------------------------------------------------------------------


async def _close_the_batch(conn, fx, produced=149.8, notes=None):
    from app.modules.production.services import job_card_batch_v2 as batch_svc
    return await batch_svc.close_batch(
        conn, batch_id=fx["batch_id"], job_card_id=fx["job_card_id"],
        produced_qty_kg=produced, closed_by="test", notes=notes,
        # The record under test is deliberately unbalanced; the balance gate is
        # a separate concern and has its own tests.
        allow_unbalanced=True,
    )


async def _live_output_rows(conn, fx):
    return await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_output_v2 "
        "WHERE job_card_id=$1 AND batch_id=$2 AND deleted_at IS NULL",
        fx["job_card_id"], fx["batch_id"])


@pytest.mark.asyncio
async def test_closing_a_batch_that_has_an_accounting_record_does_not_collide(ctx):
    """The reported 500: save accounting, then close the batch."""
    import asyncpg as _pg
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_full_payload(fx), actor="test")
    assert await _live_output_rows(conn, fx) == 1

    try:
        result = await _close_the_batch(conn, fx)
    except _pg.exceptions.UniqueViolationError as e:
        pytest.fail(
            "close_batch collided with the accounting record's output row — the "
            "partial unique index from migration 092 is being hit by a bare "
            f"INSERT again: {e}")

    assert not result.get("error"), result
    # Upsert, not append: the invariant the partial unique index encodes.
    assert await _live_output_rows(conn, fx) == 1


@pytest.mark.asyncio
async def test_batch_close_updates_the_existing_row_and_keeps_the_accounting_note(ctx):
    """close_batch is authoritative for produced_qty_kg, but must not wipe the
    note the accounting record wrote — hence COALESCE on notes in the upsert."""
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_full_payload(fx), actor="test")

    await _close_the_batch(conn, fx, produced=147.25, notes=None)

    row = await conn.fetchrow(
        "SELECT output_qty_kg, notes, recorded_by FROM job_card_output_v2 "
        "WHERE job_card_id=$1 AND batch_id=$2 AND deleted_at IS NULL",
        fx["job_card_id"], fx["batch_id"])
    assert _num(row["output_qty_kg"]) == 147.25, "close_batch must win the produced qty"
    assert row["notes"] == "full-field persistence audit note", (
        "close_batch wiped the accounting record's note — the COALESCE guard on "
        "notes in the upsert is not working")


@pytest.mark.asyncio
async def test_accounting_stays_editable_after_the_batch_is_closed(ctx):
    """Closing a batch must not strand its accounting record. POST is correctly
    refused on a closed batch (batch_not_open — use PUT), but PUT and DELETE
    have to keep working or a closed batch's figures can never be corrected."""
    conn, fx = ctx
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    await svc.create_record(conn, **ids, payload=_full_payload(fx), actor="test")
    await _close_the_batch(conn, fx)

    status = await conn.fetchval(
        "SELECT status FROM job_card_batch_v2 WHERE batch_id=$1", fx["batch_id"])
    assert status == "closed"

    # POST is refused — creating a NEW record against a closed batch is not a
    # thing; the row already exists.
    again = await svc.create_record(conn, **ids, payload=_full_payload(fx), actor="test")
    assert again.get("error") in ("batch_not_open", "record_exists"), again

    # PUT must still work.
    p = _full_payload(fx)
    p["output_qty_kg"] = 141.5
    upd = await svc.update_record(conn, **ids, payload=p, actor="test")
    assert upd.get("updated"), upd
    got = await svc.get_record(conn, **ids)
    assert got["output_qty_kg"] == 141.5

    # And so must DELETE.
    dele = await svc.delete_record(conn, **ids, actor="test")
    assert dele.get("deleted") is True, dele


@pytest.mark.asyncio
async def test_batch_close_works_when_no_accounting_record_exists(ctx):
    """Regression guard for the other direction: the upsert must not have broken
    the ordinary path where close_batch is the FIRST writer of the output row."""
    conn, fx = ctx
    assert await _live_output_rows(conn, fx) == 0

    result = await _close_the_batch(conn, fx, produced=150.0)
    assert not result.get("error"), result
    assert await _live_output_rows(conn, fx) == 1

    row = await conn.fetchrow(
        "SELECT output_qty_kg FROM job_card_output_v2 WHERE job_card_id=$1 "
        "AND batch_id=$2 AND deleted_at IS NULL", fx["job_card_id"], fx["batch_id"])
    assert _num(row["output_qty_kg"]) == 150.0
