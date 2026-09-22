# Stores box scan and manual print — design

Date: 2026-09-19. Status: approved in chat (three parts), not committed.

## Goal

Stores → Production Indents → **Scan** (on an Issued floor requisition) records
the boxes store sends for that request: boxes that already carry a sticker are
scanned, boxes without one get a sticker printed (Manual print). Today the
dialog keeps everything in browser memory; this design stores it.

Decisions taken in chat:

- Manually printed boxes are stored in **`sfg_box`**, under a new
  `item_type = 'rm'`.
- A store scan looks boxes up in the **same tables as the job card's Raw
  Material scanner** (`sfg_box`, `po_box` + `po_line`, then the universal
  identify over the legacy warehouse / cold tables).
- Store scans are recorded in a **new per-request table**
  (`floor_requisition_box`), not in `jc_box_scan`. The floor still scans the
  boxes into the job card (`jc_box_scan`) as today, so RM issued is counted once.

## Part 1 — data model (migration `113_floor_requisition_box.sql`)

1. `sfg_box.chk_box_item_type` becomes `item_type IN ('sfg','fg','rm')`.
   Nothing else on `sfg_box` changes.
2. A manually printed box is one `sfg_box` row:

   | column | value |
   |---|---|
   | carton_id | `<new_short_time_id()>-<counter>`; the counter continues per job card (same SQL as `create_wip_boxes`); PK collision → `insert_with_pk_retry` re-rolls the base |
   | item_type | `'rm'` |
   | job_card_id / job_card_number | the request's job card (`job_card_number` NULL if the job card cannot be read) |
   | sfg_code / fg_sku_name | the article name (FG cartons already store the name in `sfg_code`) |
   | entity | the job card's entity |
   | floor | the request's floor |
   | stage_bucket | `'Stores'` |
   | batch_code | the section's LOT (sfg_box has no lot column since 067) |
   | net_weight / gross_weight / units | as entered |
   | status | `'PRINTED'` |
   | created_by | the actor |

   Sticker: Material-In's label, QR `{"tx": "<request no>", "bi": "<carton_id>"}`.
3. New table `floor_requisition_box` — store's record of what was sent:

   | column | notes |
   |---|---|
   | requisition_id BIGINT NOT NULL → floor_requisition | |
   | box_code TEXT NOT NULL | carton_id / box_id |
   | source TEXT NOT NULL | `'printed'` or `'scanned'` |
   | box_table TEXT NOT NULL | where the box was found: `sfg_box`, `po_box`, or the identify branch |
   | box_number INT NULL | printed only: request-wide running number shown as "Box #" |
   | transaction_no TEXT NULL | |
   | article TEXT NOT NULL | |
   | stock_type TEXT NULL | printed only: `Fresh Stock` / `Off Grade/Rejection` |
   | lot_number TEXT NULL | |
   | net_weight / gross_weight NUMERIC(15,3) NULL, count INT NULL | |
   | recorded_by TEXT NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now() | |

   PRIMARY KEY (requisition_id, box_code); UNIQUE (requisition_id, box_number)
   for non-NULL box numbers.
4. Other effects:
   - Boxes printing list and batch caps read `item_type = 'sfg'` only — unaffected.
   - `get_jc_genealogy`'s "produced" list gets `AND item_type <> 'rm'`.
   - The job card RM scanner resolves `sfg_box` by carton_id, so the floor can
     scan a store sticker and get article + weights.

The user applies 113 on Supabase (local) and RDS (deploy). Not applied by us.

## Part 2 — API (`/api/v1/floor-requisitions`, same permissions and place scope)

| endpoint | perm | behaviour |
|---|---|---|
| `GET /{id}/boxes` | view | rows newest first + totals (boxes, net, gross, count) |
| `POST /{id}/boxes/scan` `{code}` | issue | resolve and record one box |
| `POST /{id}/boxes/print` `{article, stock_type, boxes:[{box_number, net_weight, gross_weight?, count?, lot_number?}]}` | issue | one transaction: `sfg_box` rows + request rows; returns the new rows (ids) in order. LOT is per box, as Material-In's box table lets each row carry its own |
| `DELETE /{id}/boxes/{code}` | issue | remove one box |

Rules:

- Writes only while the request is `issued` (409 `not_issued` otherwise). Place
  outside the caller's grants → place_scope's 403.
- Scan resolution (outside a transaction, as the job card's): parse
  `{"tx","bi"}`; `sfg_box` by carton_id; `po_box` (+ `po_line.sku_name`) by
  box_id and, when the label has `tx`, transaction_no; then `identify_box(raw)`.
  - not found → 404 `box_not_found` ("print a sticker under Manual print");
  - identify ambiguous → 409 `ambiguous_box`;
  - `sfg_box` status CANCELLED → 409 `box_cancelled`;
  - already on this request → 409 `duplicate_box`;
  - article differs from the request's material → recorded, response and list
    row carry `article_mismatch: true`.
- Print: 1–500 boxes; net > 0, gross ≥ net when given, count whole ≥ 0,
  box_number ≥ 1 and not already used on the request (409 `box_number_taken`);
  article required (≤ 500 chars).
- Delete: scanned → delete the row. Printed → delete the row and its `sfg_box`
  row (`item_type='rm'`, status PRINTED) in one transaction; refused (409
  `box_in_use`) when `jc_box_scan` has it (a missing `jc_box_scan` table counts
  as not in use).
- Errors in the requisition shape `{error, message, details}`.

## Part 3 — dialog

- Loads the request's boxes on open (loading / error + Retry). No "not saved" note.
- Scan → `POST /boxes/scan` with the raw QR. Duplicate → the red camera chip;
  other refusals → red message; mismatch → amber "Different article" tag.
- Manual print: Article (stock take picker), carton weight, sections, Generate
  (draft only). Print (row / all / range) → client checks → `POST /boxes/print`
  → `printLabels` with the returned ids → printed rows leave the draft and
  appear in the list. Saved but print window failed → message points to 🖨.
- Draft box numbers start after the highest box_number on the request and in
  the other drafts.
- List: 🖨 reprint (from stored data) on printed rows; ✕ remove; per-article and
  running totals from the server.

## Testing

- Server (pytest, fake connection as in `test_floor_requisition_service.py`):
  scan found in each lookup step, not found, ambiguous, cancelled, duplicate,
  not issued, place refused, mismatch flag; print validation, rows written
  (`'rm'`, PRINTED, article as code), ids in order, box number taken; delete
  scanned / printed / in use / no `jc_box_scan`; list totals; migration file
  content; genealogy filter.
- Web: helper test (next box number), typecheck, lint.
