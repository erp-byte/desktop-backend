# `test_db/` — Supabase schema replica for SFG / Job-Card

Reference + replication assets for standing up the Candor warehouse schema on a **Supabase** test
database. AWS RDS is untouched — switching back is just restoring the old `DATABASE_URL`.

```
test_db/
  supabase/   supabase_schema_all_tables.sql   ← the one file to run on Supabase
  db/         build-source SQL components (reference)
  md/         this README + the SFG playbook + the change checklist
```

The canonical SFG migrations (`050_sfg_foundation.sql`, `053_sfg_box.sql`) live in the **main**
`app/db/` track and are wired into `scripts/migrate.py` — they are **not** in this folder.

---

## `supabase/` — run this on Supabase

**`supabase_schema_all_tables.sql`** is the complete schema-only DDL: **170 tables + 2 views +
319 indexes + 179 FK constraints + the `e_extraction_status` type** — every NON-legacy table at its
final shape (base CREATEs **plus** all later ALTER-added columns). The 5 v1 "legacy" job-card tables
are excluded. It was produced by a clean from-scratch build on Postgres 16 and **round-trips into a
fresh DB with 0 errors**.

Run it once on a fresh Supabase database:

```bash
psql "postgresql://postgres.<ref>:<enc-pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require" \
  -f app/db/test_db/supabase/supabase_schema_all_tables.sql
# …or paste it into the Supabase SQL editor
```

Connection rules: use the **session pooler** (`:5432`, IPv4) or the direct host — **not** the
transaction pooler (`:6543`, breaks prepared statements). Append `?sslmode=require`. Percent-encode the
password (`@` → `%40`, `*` → `%2A`). Schema only — no data, no owners/grants. Generated on PG16;
restores cleanly on Supabase (PG15+).

---

## `db/` — build-source components (reference)

These are the out-of-band pieces server_replica needs but doesn't create itself. They are already
**folded into** `supabase_schema_all_tables.sql`; kept here as documented source. (The build runner and
the smoke-seed were removed — the consolidated schema is the deliverable.)

| File | What it is |
|---|---|
| `0000_pre_objects.sql` | Out-of-band TYPEs the migrations reference but never create (`e_extraction_status`). |
| `000_external_stub_tables.sql` | The 14 transfer/vendor/`coa_document` tables (owned by a separate backend) + out-of-band column patches (`po_header.is_deleted`, `coa_document.uploaded_at`). |
| `100_drop_legacy.sql` | Drops the 5 v1 legacy job-card tables. |

---

## v2 production + planning tables included

`production_plan_v2` / `_line_v2` / `_step_v2`, `so_fulfillment_v2`, `job_card_v2` + all satellites
(rm/pm indents, output, dispatch, sign-off, accounting, consumption, byproducts, annexures, balance,
qc, phase, additives, consumption_variance), `bom_amendment_request_v2`, the `job_card_batch_v2` **view**,
plus `sfg_box` and the SFG columns on `job_card_v2` / `bom_process_route`.

## How the schema was made buildable from scratch

Running the source SQL on a clean Postgres surfaced genuine fresh-build gaps that prod hides (it has
out-of-band objects). Fixed: created the missing `e_extraction_status` type; added the out-of-band
`coa_document.uploaded_at` + `po_header.is_deleted` columns; and **excluded `037_jc_batch_cleanup`**
(it renames `job_card_phase_v2 → job_card_batch_v2` as a table, contradicting the **view** model that
`038`/`045` and prod actually run). Result: a full from-scratch build applies with 0 errors and the
dump round-trips with 0 errors.

> Note: the 14 external stub tables carry only the wiring's columns — their canonical DDL lives in the
> transfer/vendor backend. And these fixes live in this replica; `scripts/migrate.py` still has the same
> latent gaps for a from-scratch prod deploy (a separate fix if wanted).

## `md/` — docs

- `README.md` — this file.
- `SFG_JobCard_Execution_Playbook.md` — the vertical-slice build playbook (DB+backend+frontend per slice, review gates).
- `SFG_JobCard_Change_Checklist.xlsx` — the per-file change ledger.
