"""Verify the rows inserted by _jc_seed_po896.py."""
import asyncio
import asyncpg

DB = "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db"


async def main():
    c = await asyncpg.connect(DB)
    try:
        po = await c.fetchrow(
            "SELECT prod_order_number, fg_sku_name, batch_number, batch_size_kg, "
            "       status, factory, floor, entity, customer_name "
            "  FROM production_order WHERE prod_order_number = $1",
            "896",
        )
        print("PO:", dict(po) if po else None)

        jc = await c.fetchrow(
            "SELECT job_card_number, status, assigned_to_team_leader, team_members, "
            "       start_time::text AS start_time, end_time::text AS end_time, "
            "       total_time_min, sales_order_ref, batch_number, "
            "       control_sample_gm, fumigation, metal_detector_used, "
            "       roasting_pasteurization, magnets_used, mrp, ean, bu "
            "  FROM job_card WHERE job_card_number = $1",
            "896/1",
        )
        print("JC:", dict(jc) if jc else None)

        if jc:
            rm = await c.fetch(
                "SELECT material_sku_name, reqd_qty, issued_qty, gross_qty, "
                "       loss_pct, batch_no, uom, status "
                "  FROM job_card_rm_indent "
                " WHERE job_card_id = (SELECT job_card_id FROM job_card "
                "                       WHERE job_card_number = $1)",
                "896/1",
            )
            print("RM rows:")
            for r in rm:
                print(" ", dict(r))

            pm = await c.fetch(
                "SELECT material_sku_name, reqd_qty, issued_qty, gross_qty, "
                "       loss_pct, uom, status "
                "  FROM job_card_pm_indent "
                " WHERE job_card_id = (SELECT job_card_id FROM job_card "
                "                       WHERE job_card_number = $1)",
                "896/1",
            )
            print("PM rows:")
            for r in pm:
                print(" ", dict(r))

            out = await c.fetchrow(
                "SELECT fg_expected_units, fg_actual_units, fg_expected_kg, "
                "       fg_actual_kg, rm_consumed_kg, process_loss_kg, "
                "       net_output_kg, yield_pct "
                "  FROM job_card_output "
                " WHERE job_card_id = (SELECT job_card_id FROM job_card "
                "                       WHERE job_card_number = $1)",
                "896/1",
            )
            print("Output:", dict(out) if out else None)
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
