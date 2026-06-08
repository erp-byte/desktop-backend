"""QC parameter catalog — read service.

Backs GET /api/v1/qc/parameters. Returns the RM-check parameter catalog
(qc_parameter, extended by migration 036) so the inspection readings picker and
the parameters reference page can render grouped, typed inputs.
"""

from __future__ import annotations


async def list_parameters(pool, *, active_only: bool = True) -> list[dict]:
    """Return the parameter catalog ordered by group then sort_order.

    Only rows that carry a `code` (the canonical RM-check params seeded by 036)
    are returned — legacy/un-coded rows are ignored.
    """
    where = "code IS NOT NULL"
    if active_only:
        where += " AND is_active IS TRUE"
    rows = await pool.fetch(
        f"""
        SELECT parameter_id, code, name, unit, param_group,
               data_type, value_kind, spec_note, sort_order, is_active
          FROM qc_parameter
         WHERE {where}
         ORDER BY sort_order NULLS LAST, param_group, name
        """
    )
    return [
        {
            "parameter_id": r["parameter_id"],
            "code": r["code"],
            "name": r["name"],
            "unit": r["unit"],
            "param_group": r["param_group"],
            "data_type": r["data_type"],
            "value_kind": r["value_kind"],
            "spec_note": r["spec_note"],
            "sort_order": r["sort_order"],
            "is_active": r["is_active"],
        }
        for r in rows
    ]
