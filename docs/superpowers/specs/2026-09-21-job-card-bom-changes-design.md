# Per-job-card BOM changes: design

Date: 2026-09-21. Status: approved in chat; revised after two adversarial reviews of the code (43
confirmed findings folded in); not committed.

## Goal

On the job card's **Material allocation and requisition** tab, the BOM articles table gets:

- a **✕** in its actions column that removes an RM or PM article from this job card's BOM;
- an **Add article** control below the table.

The BOM in the BOM module (`bom_header` / `bom_line`) is never written.

A change applies to the **whole job card**: every stage card of its chain. That runs from stage 1,
where the RM/PM indents, Required and most requests live, to the packing stage, where PM is
consumed. It reaches everything on those cards that reads the BOM:
- the Material allocation tab and its Request buttons;
- Accounting: consumption, Returned to store, off-grade, extra giveaway, the summary;
- Save Output;
- floor requisitions;
- receiving material;
- the job card PDF;
- Android, which reads the same detail payload.

Decisions taken in chat:

| Question | Decision |
|---|---|
| Where a change applies | The whole job card: every stage card of the chain (see "Scope" below) |
| Required quantity on an added article | Optional: kg for RM, pcs for PM |
| Removing an article that has a raised (not yet issued) request | Allowed; the request stays open and Stores can still issue it |
| Where Add article picks from | SKU master RM/PM only; no free text |
| Removing an article with saved figures on the job card | Refused |
| Clearing a saved consumption / off-grade figure in Accounting | Saves a 0, so the figure really clears and removal then works |
| The job card is rebuilt (quantity/steps edit) | The changes carry over to the rebuilt cards |

### Scope: the job card's chain

A job card (`job_card_v2`) is one stage. Stage cards are linked through `prev_job_card_id`.

**The chain** is a card plus the cards linked to it through the previous stage on the **same plan
line**. Its **first card** is found by walking `prev_job_card_id` back while the plan line stays
the same. Changes are stored against that first card (`scope_job_card_id`).

Why the chain and not the plan line:
- **Partial chains.** A plan line can be carded as several partial chains
  (`PLAN-…-S1`, `PLAN-…-S1-B2`, each sized to part of the line; `create_job_cards_for_line`
  1136-1262).
  - Each chain is its own job card, with its own indents.
  - A change on one chain doesn't touch another.
  - A chain carded later starts from the BOM module's list.
- **Live data.** No line has more than one live chain (RDS, 2026-09-21), so for every job card
  today "the chain" and "every stage of the line" are the same set of cards.

**Merged process runs** (`create_merged_process_run`, none on RDS):
- The shared process cards sit on the primary line, so they form one chain.
- Each member line's packing card follows a process card from another line, so it is the first
  card of its own chain.
- So RM changes are made on the process cards, and a member's PM changes on that member's packing
  card.

## Background: what the code does today

**BOM lines and indents:**
- Every stage card carries the line's `bom_id`.
- `get_job_card` (`job_card_v2.py` ~4969) returns the whole BOM, `bom_line WHERE bom_id = $1`, as
  `bom_lines` on every stage.
- RM and PM indent lines are materialised on the chain's first stage only (1008-1021).
- Accounting shows RM on the first stage and PM on PM-bearing stages (`keepArticle`, `page.tsx`
  3302-3311).
- `replace_job_cards_for_line` hard-deletes every chain of the line and rebuilds one.
- `create_merged_process_run` hard-deletes the members' cards and builds the process chain on the
  primary line, with the primary's `bom_id`.

**On the web:**
- `computeArticles` (`page.tsx` 542) builds the Accounting article catalogue from `bom_lines`,
  falling back to the indent lines when `bom_lines` is empty.
- `_MaterialAllocationTab.tsx` does the same.
- Client keys fall back to the name when `bom_line_id` is null:
  `b${bom_line_id}` / `n${material_sku_name}`.

**Server paths that read `bom_line` for a job card:**

1. **Save Output: `POST /job-cards-v2/{id}/outputs`**
   - `ConsumedLineV2.bom_line_id` is a required `int` (`router.py` 6360).
   - The id check (400 `invalid_bom_line`) and both `upsert_consumption_lines` calls sit inside
     `if submitted_bom_lines:` (6677-6722).
   - Consumption is stored by name. The upsert key is
     `(job_card_id, COALESCE(batch_id,0), material_sku_name)`, exact spelling.
   - Returns and extra giveaway go through `replace_balance_materials`, which deletes and
     re-inserts the batch's rows, including rows at 0.
   - Off-grade goes through `save_byproducts`, keyed `(card, batch, category, material_name)`.
2. **Extra giveaway check:** `replace_balance_materials` (~3800) looks the article up in
   `bom_line`, by id or by name.
3. **Floor requisition raise: `raise_requisition`**
   - Accepts an article that is on the card's `bom_line` or its indents.
   - Requisitions are tracking only: they never write the indent `issued_qty`.
4. **Receive material** (`router.py` ~8690-8760)
   - Matches each box to an RM indent row by name (`ILIKE`), inside a per-box savepoint whose
     `except Exception` turns any failure into a per-box `save_failed`.
   - It adds to `issued_qty`, which is the canonical RM input
     (`page.tsx` 3828-3830, 4103-4104; `_derive_accounting_payload`).

**Returns index.** Returns rows are unique on
`uq_jcbm_v2_jc_batch_bom_type (job_card_id, COALESCE(batch_id,0), COALESCE(bom_line_id,0), balance_type)`.
- The web sends one `returned` row per article, even at 0.
- So two articles with no BOM line in one batch collide, and the save fails with a 500.
- Migration 044 creates the index. **The runner re-executes every file on every deploy**, with no
  ledger and no try/except, so one failing file stops every later one.
- The record screen (`jc_accounting_crud._BALANCE`) keys returns by `(bom_line_id, balance_type)`,
  with an `ON CONFLICT` clause that repeats the index.

**Clearing figures.** The web's Save Output drops consumption at 0 or below (`page.tsx` 4386-4387)
and off-grade at 0 or below (4418). Both server writers only upsert. So a cleared figure is never
cleared: the old value stays. Control sample and the pm_* rows already send an explicit 0 on a
clear (the W3-CRIT-2 pattern, 4436-4470).

**Rows with no batch.** Legacy rows with `batch_id NULL` show under every batch (`matchesBatch`,
`outputAccounting.ts` 86).
- A save for batch X never reaches them: both upserts key on `COALESCE(batch_id,0)`, despite
  their "promote batch_id" comments.
- RDS has 120 such consumption rows and 68 such off-grade rows with a quantity, on lines that are
  still open.

**Job card locks.** Every stage after the first is created `is_locked`, with
`status = 'locked'` and `locked_reason = 'awaiting_previous_stage'`, until handoff.
- `assert_not_locked` refuses those cards with `{"error": "locked"}`.
- Requisitions and PATCH `/job-cards-v2/{id}` are not lock-gated.
- Cancelled cards have `deleted_at` set.

**Unused tables:**
- **Amendments pair.** The hidden Amendments tab's maker/checker pair,
  `one_off_material_add` / `one_off_material_remove`, writes `jc_material_exception_v2`. Nothing
  reads it (0 rows on RDS). It needs a checker for every change, so it is not used, and it is left
  as it is.
- **SO-level override.** `fulfillment_bom_override_v2` is an override at SO level that indents
  ignore. It is not used either.

**Live data (RDS, read-only, 2026-09-21):**
- 1,363 job cards, all with a BOM that has lines. 554 plan lines have an open card.
- No line has more than one chain, and there are no merged process runs.
- `bom_line.item_type`: 4,904 `rm`, 2,289 `pm`, 94 `sfg`.
- `all_sku.item_type`: 735 `rm`, 1,579 `pm`.
- There are 105 returns rows without a BOM line, all extra giveaway.

## Part 1: data model

### 1a. The change table (migration `115_job_card_bom_change.sql`)

```sql
CREATE TABLE IF NOT EXISTS job_card_bom_change (
    change_id               BIGINT PRIMARY KEY,       -- new_short_time_id(), insert_with_pk_retry
    scope_job_card_id       BIGINT NOT NULL,          -- the chain's first card; no FK: a rebuild
                                                      -- re-points it (2i)
    plan_line_id            BIGINT NOT NULL
                            REFERENCES production_plan_line_v2 (plan_line_id) ON DELETE CASCADE,
    change_type             TEXT   NOT NULL,          -- 'removed' | 'added'
    material_sku_name       TEXT   NOT NULL,          -- the BOM's / SKU master's spelling
    item_type               TEXT   NOT NULL,          -- 'rm' | 'pm', stored LOWER(BTRIM(...))
    sku_id                  INT,                      -- added: the all_sku row; removed: NULL
    required_qty            NUMERIC(15,3),            -- added only, optional
    required_unit           TEXT,                     -- 'kg' for rm, 'pcs' for pm
    note                    TEXT,
    made_on_job_card_id     BIGINT NOT NULL,          -- audit snapshot (the card may be rebuilt)
    made_on_job_card_number TEXT   NOT NULL,          -- audit snapshot
    changed_by              TEXT   NOT NULL,
    changed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    undone_by               TEXT,
    undone_at               TIMESTAMPTZ,
    undo_reason             TEXT,                     -- NULL = undone by a user; else e.g. 'rebuild'
    CONSTRAINT chk_jcbc_type          CHECK (change_type IN ('removed','added')),
    CONSTRAINT chk_jcbc_item_type     CHECK (item_type IN ('rm','pm')),
    CONSTRAINT chk_jcbc_required_pair CHECK ((required_qty IS NULL) = (required_unit IS NULL)),
    CONSTRAINT chk_jcbc_required      CHECK (required_qty IS NULL OR (
        change_type = 'added' AND required_qty > 0
        AND required_unit = CASE item_type WHEN 'rm' THEN 'kg' ELSE 'pcs' END
        AND (item_type = 'rm' OR required_qty = trunc(required_qty)))),
    CONSTRAINT chk_jcbc_sku           CHECK ((change_type = 'added') = (sku_id IS NOT NULL)),
    CONSTRAINT chk_jcbc_undone        CHECK ((undone_at IS NULL) = (undone_by IS NULL))
);
-- One live change per article per job card; undoing keeps the row as history.
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_card_bom_change_live
    ON job_card_bom_change (scope_job_card_id, UPPER(BTRIM(material_sku_name)))
    WHERE undone_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_job_card_bom_change_line ON job_card_bom_change (plan_line_id);
```

**Design choices:**
- **No FK to `all_sku`.** The Tally refresh upserts that table, and the name is a snapshot, the
  way `bom_line` keeps `material_sku_name`.
- **`plan_line_id`** carries the FK, for clean-up when a plan line is deleted. It is also the lock
  (2c).
- **Article identity** is `UPPER(BTRIM(material_sku_name))` everywhere, as in floor stock and
  requisitions (`rules.article_key`).

### 1b. Returns rows unique per article (migrations `044` edited, `115`)

**The new index:**

```sql
uq_jcbm_v2_jc_batch_line_type ON job_card_balance_material_v2 (
    job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0),
    (CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END),
    balance_type)
```

It is the old key with one column added, so it can't fail on data the old index accepted. The
105 `CONSOLIDATED` extra-giveaway rows keep their one-per-batch meaning.

**044 stands down.** 044 re-creates the old index on every run. Once rows that only the new index
allows exist, that rebuild would fail and stop every later migration for good. So 044's balance
block wraps its CREATE in
`IF to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NULL THEN … END IF;`, with a header
note saying why.

**115's file order is pinned.** `BEGIN; SET LOCAL lock_timeout = '5s';` are the first lines, so
every lock the file takes is bounded. Then come the table and its indexes (1a), then the swap,
then `COMMIT;`.

```sql
DO $$ BEGIN
  IF to_regclass('public.uq_jcbm_v2_jc_batch_bom_type') IS NOT NULL THEN
    LOCK TABLE job_card_balance_material_v2 IN ACCESS EXCLUSIVE MODE;  -- strongest first: no upgrade,
                                                                      -- no deadlock
    CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_line_type ON … ;
    DROP INDEX uq_jcbm_v2_jc_batch_bom_type;
  ELSIF to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NULL THEN
    CREATE UNIQUE INDEX uq_jcbm_v2_jc_batch_line_type ON … ;          -- a fresh database
  END IF;
END $$;
```

**Re-runs.** On a re-run, neither branch fires, so no table lock is taken. The header notes the
lock in 092's style: the table is small, and CONCURRENTLY is impossible under this runner.

**One constant.** The record screen's `ON CONFLICT` inference and the migration's static test
share one constant:
`(job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0), (CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END), balance_type)`.
The CASE sits in its own parentheses, and it must match the index exactly.

### 1c. Deploy order: code first, then 115

The new server works **before and after** 115:
- **Reads** check `to_regclass('job_card_bom_change')`. With no table there are no changes, and
  every path behaves as today.
- **Write endpoints** answer 503 `bom_changes_not_available` until 115 is applied.
- **The record screen** switches its returns key **and** `ON CONFLICT` clause **together**, on
  one `to_regclass('public.uq_jcbm_v2_jc_batch_line_type')` check per request.
  - The check is made after the request's transaction has read the table, so its ACCESS SHARE
    lock holds the swap off until the INSERT.
  - Without the new index, the record screen keeps today's key and clause, so `duplicate_line`
    still fires.

**The old server does not work after 115.** Its record-screen `ON CONFLICT` names the dropped
index. So the order is:
1. deploy the new server;
2. then apply 115 and the edited 044, on Supabase first and on RDS after the rollout.

115 is registered in `scripts/migrate.py` after 114, with this ordering note.

## Part 2: server

### 2a. The job card's BOM: one module

New `app/modules/production/services/jc_bom_changes.py`:

- **`scope_of(conn, job_card_id) -> Scope`**
  - Finds the chain's first card by a recursive walk back through `prev_job_card_id` while the
    plan line stays the same.
  - Also gives the chain's live cards: a walk forward through the cards whose `prev_job_card_id`
    is on the chain and whose plan line is the same, `deleted_at IS NULL`.
  - Returns the plan line, the first card, and the cards, with their numbers and statuses.
- **`load_changes(conn, scope_job_card_id) -> Changes`**
  - Returns the live rows, split into `removed` and `added`, each in `changed_at` order.
  - Returns empty when the table is missing.
- **`effective_bom_lines(master_lines, indents, changes) -> (lines, flags)`** (pure):
  - **Base list:** the card's `bom_line` rows.
  - **Empty BOM:** if the card's BOM has no lines and there are changes, the base list is the
    card's indent lines, shaped like BOM lines. This mirrors the client fallback. Not seen live.
  - **Removed articles:** dropped by key, every line with that key. A removed article that is not
    on the card's BOM (the BOM changed later) is flagged `not_on_bom`.
  - **Added articles:** appended after the base list, each shaped like a BOM line:
    ```
    {bom_line_id: None, line_number: None, material_sku_name, item_type: 'rm'|'pm',
     uom: 'KGS' (rm) | 'PCS' (pm), quantity_per_unit: None, loss_pct: None, godown: None,
     added: True, change_id, sku_id, required_qty, required_unit}
    ```
  - **Superseded added articles:** an added article whose key now matches a BOM line (after a
    Tally refresh) is dropped and flagged `superseded`.
  - **No changes:** the result is the card's `bom_line` rows unchanged, so the payload is exactly
    as today.
- **`article_state(conn, job_card) -> resolver`** answers, for any article name, whether it is:
  - on the card's BOM (with its line ids);
  - removed;
  - added (with its change row);
  - not on the card.

  It is used by Save Output, extra giveaway, requisitions and receive-material.
- **`has_records(conn, scope, key, bom_line_ids) -> list[Hit]`** lists the saved figures for the
  article on the chain's live cards, matched by key or by one of its `bom_line_id`s. Each kind
  counts only when its quantity is above 0:
  - consumption: `actual_consumed_qty > 0`;
  - returns / extra giveaway / wastage / control sample: `qty_kg > 0`;
  - off-grade by-products: `quantity > 0`;
  - **issued**: RM/PM indent `issued_qty > 0`. Issued kg is canonical input: removing it would
    leave that kg in input with nowhere to return it.

  Zero rows and soft-deleted rows do not count. Each hit carries the kind, card number, batch id,
  batch number and batch status. A row with no batch is marked "saved without a batch".

### 2b. Job card detail (`get_job_card`)

- **`bom_lines`** becomes the effective list for the card's chain.
- **New `bom_changes`:**
  ```
  bom_changes: {
    scope_job_card_id,
    removed: [{change_id, material_sku_name, item_type, note, changed_by, changed_at,
               made_on_job_card_number, not_on_bom}],
    added:   [{change_id, material_sku_name, item_type, sku_id, required_qty, required_unit,
               note, changed_by, changed_at, made_on_job_card_number, superseded}],
  }
  ```
- **Indent lines are unchanged.** They are the record of what was materialised and issued.
- **`cancelled_snapshot`** stores the detail, so it keeps the effective list.

### 2c. Endpoints (production router)

Both need `require_permission("production", "job_cards", "overview", action="start")`: the job
card edit permission, the same one PATCH `/job-cards-v2/{id}` uses.

**Locks.** Each call runs in one transaction:
1. Read the card without a lock (404 `job_card_not_found`, including cancelled cards).
2. Take the plan line row `FOR UPDATE`.
3. Re-read the card and its chain.

**The lock-order rule** holds for every transaction that locks the plan line row: that lock
comes **before any other row lock** (card, batch, indent, or an FK-checked insert).

**Not lock-gated.** A BOM change is a planning edit, like PATCH and requisitions, so the per-card
`awaiting_previous_stage` lock does not refuse it.

**Job card finished:** refused with 409 `job_card_finished` when no live card of the chain is
still open (every card `completed`, `closed` or `cancelled`).

**`POST /api/v1/production/job-cards-v2/{id}/bom-changes`**

Body: `{action: "remove", material_sku_name, note?}` or `{action: "add", sku_id, required_qty?, note?}`.

**`remove`:**
- The article must be on the card's effective list.
  - Otherwise 422 `article_not_on_job_card`.
- It must be RM or PM.
  - An SFG line (the seam a later stage consumes) answers 422 `not_rm_or_pm`.
- `has_records` must be empty.
  - Otherwise 409 `article_has_records`, with the hits and a message built from them, such as:
    - "Consumption is saved for X on PLAN-7-L1-S1, batch 2. Clear it in Accounting (clearing saves
      0), then remove it."
    - "…batch 3 (closed: an admin must re-open it with the override)."
    - "…issued 12.000 kg on PLAN-7-L1-S1: return it first."
- **A BOM line:** a `removed` row.
- **An added article:** its `added` row is undone.
- **Both a BOM line and a live add** (superseded): the add is undone and a `removed` row is
  inserted, in that order.
- Open raised requests are untouched. The response carries `open_requisition_ids`.

**`add`:**
- **SKU lookup:**
  - `sku_id` must be in `all_sku`: otherwise 404 `sku_not_found`.
  - `LOWER(BTRIM(item_type))` must be `rm` or `pm`: otherwise 422 `not_rm_or_pm`.
  - The name is `particulars`.
- **Required qty:**
  - Optional.
  - When given it must be above 0, with at most 3 decimals, and whole for PM.
  - Otherwise 422 `required_qty_invalid`.
  - The unit is `kg` for RM and `pcs` for PM.
- **Already on the list:**
  - The article is on the card's effective list (by key): 409 `already_on_job_card`.
  - A BOM line whose name differs only in repeated or non-breaking spaces also counts. The
    response gives the BOM's spelling.
- **A BOM line that was removed:** the removal is undone (restore). A required qty or note is
  ignored, and the response says `restored: true`.
- **Otherwise:** an `added` row.

**`DELETE /api/v1/production/job-cards-v2/{id}/bom-changes/{change_id}`** undoes one live change
of the card's chain:
- a `removed` row: restores the article;
- an `added` row: removes it, after the `has_records` check.
- An unknown id or one from another job card: 404 `change_not_found`.
- An id already undone: 409 `change_already_undone`.

**Races.** A clash on `uq_job_card_bom_change_live` answers 409 `bom_changed_concurrently`
("reload and try again").

**Response.** Both endpoints answer with the card's new `bom_changes` and `bom_lines`, plus the
flags above.

### 2d. Save Output (`POST /job-cards-v2/{id}/outputs`)

**Line lock.** Right after `assert_not_locked` (`router.py` ~6523), which takes no lock, and
before the admin-override batch updates:
`SELECT j.plan_line_id FROM job_card_v2 j JOIN production_plan_line_v2 l USING (plan_line_id) WHERE j.job_card_id = $1 FOR KEY SHARE OF l`.
- This doesn't conflict with other saves, or with their non-key updates of the line.
- It waits for a BOM-change call on the same line.
- After it, the card is re-read with `deleted_at IS NULL`.

**Id coercion.** `bom_line_id` becomes `int | None = None` on `ConsumedLineV2`. On
`ConsumedLineV2`, `BalanceMaterialV2` and `ByproductLineV2`, an id of 0 or below becomes `None`.
Whatever Android sends for a missing id, the save then does not fail the FK.

**All article checks run before any write**, so a refusal leaves nothing half-saved.

**Consumption:**
- The gate becomes `if rm_rows or pm_rows`, not "some id was submitted". A save whose only lines
  are added articles must still be stored.
- **Each line is checked through `article_state`:**
  - **A line with an id** must be one of the card's BOM lines (400 `invalid_bom_line`, as today),
    and its article must not be removed (400 `article_removed_from_job_card`).
  - **A line without an id** must name an article on the card's effective list: a non-removed BOM
    line or an added article. Otherwise 400 `article_not_on_job_card`.
- **Stored name:** a line without an id is stored under the matched line's or change row's
  spelling. A line with an id keeps today's behaviour.
- **A qty of 0** is accepted and stored. This is how a cleared figure clears (3b).

**Adopting existing rows.** Both /outputs writers (`upsert_consumption_lines`, `save_byproducts`)
adopt an existing row before upserting, when batch X has no row with the exact incoming spelling:
- **Another spelling:** a row for the same article key in batch X under another spelling or with
  a null `bom_line_id` (an added article that was then superseded) is re-tagged with the incoming
  spelling and `bom_line_id`.
- **No batch:** failing that, a row for the same article key with `batch_id NULL` is moved into
  batch X.
- **Why:** this is what the existing "promote batch_id" comments intended. It lets the 188 legacy
  no-batch rows, and the rows of superseded articles, be cleared and corrected from the web.

**Other rows naming a removed article:** returns, wastage, control sample and off-grade rows with
a quantity above 0 answer 400 `article_removed_from_job_card`. Rows at 0 are accepted.

**Extra giveaway** (`replace_balance_materials`):
- a non-`CONSOLIDATED` row naming a removed article answers `ega_material_not_in_bom`;
- a row naming an added article takes that article's `item_type`, so added RM passes and added PM
  answers `ega_non_rm_material`.

**Variance.** The consumption variance keeps today's rule for articles with no BOM line: the
prescribed qty is 0.

### 2e. Accounting record screen (`jc_accounting_crud`)

- **Returns key,** once the new index exists: `(bom_line_id, balance_type, k)`, where `k` is
  `UPPER(BTRIM(material_name))` when `bom_line_id` is null, else `''`.
  - `k` is computed in the key function. `spec["key"]` stays a column list, so the INSERT column
    list doesn't change.
  - Leaving the name out when there is a BOM line keeps a rename of a BOM-line row an update.
- **Key and clause switch together** (1c).
- **No gating:** the record screen and PUT `/accounting/consumption` don't check the job card's
  list. They are admin correction tools.

### 2f. Floor requisitions (`raise_requisition`)

- **Line lock:** the plan line row `FOR KEY SHARE`, taken after the card read (with
  `plan_line_id` added to it) and before the BOM/indent lookups.
- **Removed article:** 422 `article_removed_from_job_card` ("X was removed from this job card's
  BOM").
- **Added article:**
  - accepted;
  - `item_type` from the change row;
  - unit `kg`/`pcs` by type;
  - the shortage snapshot uses its `required_qty` as the required figure, or none when it has no
    required qty.
- Otherwise unchanged. The one-open-request rule still applies.

### 2g. Receive material (`router.py` ~8690)

- **Line lock:** the plan line row `FOR KEY SHARE`, taken once after the card read and before the
  per-box loop, outside the savepoints.
- **Removed article:** a box whose matched indent row's article is removed is refused per box, in
  the existing shape next to `no_matching_indent`:
  `{"box_id": …, "error": "article_removed_from_job_card", "material_sku_name": …}`.
  Nothing is added to `issued_qty`.
- **The other boxes** in the scan are received as usual.

### 2h. Job card PDF (`job_card_pdf.py`)

The "Bill Of Material" section:
- leaves out RM indent rows whose article is removed;
- then lists added RM articles, with the required qty as Required Qty, Issued blank and the UOM
  `Kgs`.

A removed article never has issued qty, because removal is refused when it does. PM lines are not
printed today.

### 2i. Rebuilt and merged job cards

**`replace_job_cards_for_line`** (a quantity/steps edit):
- **Line lock:** it now takes the plan line row `FOR UPDATE` as its **first** statement (lock-order
  rule, 2c).
- **Carry-over:** after rebuilding, it re-points the live changes of the line's deleted chains to
  the new chain's first card.
  - When two old chains both hold a live change for one article, the earliest is kept and the
    others are undone with `undo_reason = 'rebuild'`.
  - A change that no longer fits the rebuilt card is kept and flagged (`not_on_bom` /
    `superseded`).

**`create_merged_process_run`** is **refused** with 409 `bom_changes_on_merged_lines` while any of
the lines it would rebuild has live changes. The message names the job cards, so the changes can
be undone and made again on the merged run.

This is a deliberate exception to "carry over". A removal carried into a shared process run would
apply to every member's share: an article removed for one product would then be refused for
another product that needs it. No merged runs exist on RDS today.

## Part 3: web

### 3a. API and pure helpers: new `src/lib/job-card-bom.ts`

**Types:**
- `BomChange`
- `BomChanges`
- `BomLine` fields `added?`, `change_id?`, `sku_id?`, `required_qty?`, `required_unit?`

**Calls:**
- `removeBomArticle(jobCardId, {material_sku_name, note?})`
- `addBomArticle(jobCardId, {sku_id, required_qty?, note?})`
- `undoBomChange(jobCardId, changeId)`

**Pure helpers**, with node tests in `job-card-bom.test.ts`:
- **`hasBomChanges(changes)`.** The indent fallback applies only when this is false. The server
  has already done the fallback. When every article is removed, the list must stay empty.
- **`requirementIndents(indents, changes)`.** Gives the lines `requirementsByArticle` reads:
  - The indent lines, minus any whose key matches a live added article.
  - Plus each added article's required qty as an indent-like line.
  - So an added figure replaces an indent figure rather than being summed with it.
  - The figure counts on every stage card of the chain.

### 3b. Job card page (`page.tsx`) and `outputAccounting.ts`

**Types and props:**
- `JobCardDetail` gets `bom_changes?`.
- `computeArticles` uses the `hasBomChanges` guard.
- `TabPanel` passes `bomChanges={detail.bom_changes}` and `onReload={onReload}` to
  `MaterialAllocationTab`.

**Saved rows are matched to articles by key.** In `consumptionStateFromDetail`,
`balanceStateFromDetail`, the zeroing helpers and both RM/PM maps (`isRmKey` in
`computeBatchSummary` and at save time), a saved row with a null `bom_line_id` resolves to the
article with the same `UPPER(BTRIM(name))`, preferring a `b<id>` article. Without this, a
superseded added article's saved figures would be hidden, and a PM's pieces would count as RM kg.

**Clearing saves 0 (decision 6),** in the W3-CRIT-2 pattern.

The baseline is **what the inputs were last seeded with**, not the live server memo, which
changes every 60 s while the form is dirty. It is kept in a ref (`seededConsumptionRef`,
`seededRejectionsRef`) written wherever the inputs are seeded from the server. Otherwise a save
could zero a figure another user saved meanwhile.

**The zeroing helpers** are pure, in `outputAccounting.ts`, with node tests:
- **Consumption:** a 0 line for each article whose seeded figure was above 0 and whose input now
  reads empty or 0. It is sent under the saved row's own spelling and the article's
  `bom_line_id`, so the upsert or adoption hits that row.
- **Off-grade:** a 0 row for each seeded (category, article) row that the operator removed or set
  to 0. There is no (category, no-article) zero when the payload has an attributed row in that
  category, because `save_byproducts` clears those itself.

**Seeding skips zero rows.** A cleared consumption shows empty, and a cleared off-grade row does
not come back as a "0" row.

**Closed batches** still need the admin override to re-save, as today.

**Accounting needs nothing else.** Because a change covers the whole chain:
- an added RM shows on the first stage and an added PM on the PM-bearing stage, the same as BOM
  articles (`keepArticle` unchanged);
- added lines key as `n<name>`;
- `bomPrescribedByKey` gives them no expected qty, so they show "no BOM variance available".

### 3c. Material allocation tab (`_MaterialAllocationTab.tsx`)

**Layout.** The BOM articles card no longer depends on floor stock.
- The article list, the Added tags, the ✕, **+ Add article** and **Changes on this job card**
  render from `bomLines` / `bomChanges`.
- Only the Stock type, Available and Against requirement cells wait for floor stock. They show
  "—" (with the reason) when the card has no plant or floor, the viewer lacks Stock Take view, or
  the load failed.
- The no-plant/floor early return moves below the BOM controls.
- Request still needs a plant and floor.

**Permission.** `canEditBom` = `useHasPermission("production","job_cards","overview","start")`.

**Actions column.** Headed **Actions**, it shows when the viewer may raise requests or edit the
BOM. It holds the Request cell as today, and a **✕** on RM and PM rows (none on SFG rows).

**✕ confirmation:**
- "Remove <article> from this job card's BOM (all its stages)? The BOM in the BOM module is not
  changed."
- With raised requests, it adds: "Request #N stays open."
- It has an optional note.
- A refusal (for example `article_has_records`) shows in the dialog with the server's message,
  which names the card and batch.

**Added rows** show an **Added** tag. Their ✕ undoes the add, through the same confirmation.

**+ Add article** sits below the table. It opens a new `_AddBomArticleDialog.tsx`:
- the RM/PM catalogue picker `ArticlePicker` (`app/modules/sample/_form.tsx`), with
  `restrictItemType={["rm","pm"]}`. `ArticlePicker` gains one fix: its Search tab keeps each
  result's item type (it already queries once per allowed type) and passes it when resolving the
  pick. A name that also exists as FG can then no longer resolve to the wrong SKU. NPD, its other
  user, gets the same fix.
- The dialog then shows the picked article and its type. It refuses a pick that is not RM/PM.
- An optional **Required qty** in kg (RM) or pcs (PM), validated with the requisition form's
  quantity rules (`lib/floor-requisition-form.ts`).
- An optional note.
- An **Add** button.

**Changes on this job card (n)** is a collapsed list under the table:
- removed articles, each with who, when, the card it was made on and the note, and **Restore**;
- added articles flagged `superseded`, marked "now on the BOM", with **Undo**;
- removed articles flagged `not_on_bom`, marked "no longer on the BOM".

**Required and requests:**
- **Required** comes from `requirementIndents`.
- The Request dialog's unit follows the article type.

**After any change** the tab calls `onReload()`: the page re-fetches the job card, so every tab
sees the new list.

**Empty list:** when the list is empty the table says "No BOM articles on this job card", and
**+ Add article** still shows.

## Behaviour notes

- **Scans stay.** Raw Material scans (`jc_box_scan`) of a removed article are kept. Accounting
  lists them under "Scanned but not on this BOM", as for any off-BOM article.
- **Removing with a raised request:**
  - The request stays in the requisitions list.
  - Stores can issue it, and the floor can receive it.
  - A new request for the article is refused.
- **Added RM and the input figure.** Material issued for an added article never reaches
  `rm_indents.issued_qty`: it has no indent line, and requisitions are tracking only. On a card
  whose input is the indent or carried-in basis, an added RM therefore counts in the input only
  through the consumption fallback. That is the same limit that already applies to any
  requisition-issued material.
- **Merged process runs** keep today's list: the primary line's BOM. RM that only another
  member's BOM has is not on the process cards' list, so it can't be removed there.
- **Soft-deleted accounting rows** (092) are still returned by `get_job_card` and are not revived
  by Save Output. That is existing behaviour and out of scope. `has_records` ignores them.
- **Android** reads the same `bom_lines`:
  - Added lines arrive with `bom_line_id: null`, and an id of 0 or below is treated as none.
  - Android's own "empty BOM means use the indents" fallback would still show the removed articles
    if every article is removed.

## Testing

**Server (pytest, fake connections as in the existing tests):**
- **Migrations 115 and 044:**
  - static checks on the table, every CHECK, and the indexes;
  - the pinned file order (`BEGIN`/`lock_timeout` first, `COMMIT` last);
  - the swap's `IF`/`ELSIF` block, with no unguarded CREATE INDEX on the returns table;
  - 044's guard;
  - the ON CONFLICT constant equals the index expression;
  - registration after 114.
- **`scope_of`:**
  - a one-chain line;
  - two partial chains on one line, kept apart;
  - a merged run's process chain and member packing card.
- **`effective_bom_lines`:**
  - no changes gives the master list unchanged;
  - removed by key, including duplicates;
  - added appended and shaped;
  - `superseded` and `not_on_bom`;
  - an empty BOM with changes uses the indents.
- **Endpoints:**
  - remove, add, restore by add, undo, and remove of a superseded add;
  - `has_records` per kind, with issued included, zero and soft-deleted rows ignored, across the
    chain's cards, and with the batch and closed-batch wording;
  - SFG refused;
  - `job_card_finished`;
  - not refused by `awaiting_previous_stage`;
  - the SKU checks, the required-qty rules, and `already_on_job_card`, including the
    whitespace/NBSP variant;
  - the race gives 409;
  - table missing gives 503;
  - lock order: line first, then card re-read;
  - permissions.
- **Save Output:**
  - a save with only added-article lines is stored under the change row's spelling;
  - removed refused by id and by name, and in the returns and off-grade rows (qty > 0);
  - unknown names refused;
  - ids ≤ 0 coerced;
  - a 0 is stored;
  - adoption of a no-batch row and of a superseded added row;
  - no write before a refusal;
  - `FOR KEY SHARE` taken before the batch updates.
- **Extra giveaway:** added RM passes, added PM refused, removed refused.
- **Requisitions:** removed refused; added raised with its unit and required qty; lock position.
- **Receive material:** a removed article is refused per box in a 200 response, with the other
  boxes received.
- **Rebuild:** changes re-pointed to the new chain; duplicate changes undone with
  `undo_reason 'rebuild'`; the line lock taken first.
- **Merge:** refused while a line has live changes.
- **Accounting record screen:**
  - key and clause before and after 115 (fake conn);
  - two cases in the live-Postgres module `test_accounting_crud.py`, which runs inside its
    rolled-back transaction:
    - one skipped unless the new index exists: two returns rows with no BOM line in one batch,
      plus a rename of a BOM-line row;
    - a companion, skipped once the index exists, asserting `duplicate_line` before 115.
- **Job card PDF:** removed RM rows left out, added RM rows listed.
- **Full suite:** runs with only the two known `test_sku_lookup_permission.py` failures.

**Web:**
- node tests for `job-card-bom.ts`, the `outputAccounting.ts` zeroing, seeding and key-matching
  helpers, and the stale-baseline case (baseline {} with a live figure of 5 emits no zero);
- `tsc`;
- eslint on the changed files.

No browser or RDS test is possible from here. The user deploys the server, then applies 044 and
115 on Supabase to try it.

## Not in scope

- The master BOM, the BOM module and the Tally refresh.
- The Amendments tab's one-off add/remove (`jc_material_exception_v2`).
- Rescaling indent lines.
- A prescribed qty for an added article in the consumption variance, which stays 0.
- The merged process card's member-only RM.
- Soft-deleted rows in the job card detail.
- Android code.

## Addendum A — "Use" on other floor stock; FG/SFG articles; stock in the Add dialog

Approved in chat, 2026-09-21. Decisions: Use is offered on RM, PM, FG and SFG floor rows; Use opens
the Add dialog prefilled.

**A1. Migration 116** widens `job_card_bom_change`: `chk_jcbc_item_type` allows `('rm','pm','fg','sfg')`;
`chk_jcbc_required` puts FG/SFG in kg (`CASE item_type WHEN 'pm' THEN 'pcs' ELSE 'kg' END`). One
bounded transaction; a guarded block (checks `pg_get_constraintdef`) so a re-run takes no lock.
Before 116, adding an FG/SFG article answers 503 `bom_changes_need_116`; RM/PM work as before.

**A2. FG/SFG added articles are RM for accounting.** Accounting treats every SFG article as the
seam carried in from the previous stage, and consumption accepts only RM/PM/SFG/WIP. So an added
FG/SFG article appears in `bom_lines` with `item_type 'rm'` (its accounting kind) and its real type
in a new `article_type` field. Accounting, Save Output (stored as `input_kind 'RM'`), extra
giveaway and Android treat it exactly like an added RM (stage 1, pullable into later stages, kg
side). The Material allocation tab, `bom_changes` and requisitions show the real type; requests go
out in kg. The job card PDF lists added FG/SFG with the added RM (every added type but PM).

**A3. Add by name.** `POST …/bom-changes` with `action "add"` accepts `material_sku_name` (+ optional
`item_type` hint) instead of `sku_id`. The server finds the SKU by `UPPER(BTRIM(particulars))`; when
the name has several SKUs (five floor items are both FG and SFG) the hint picks, else RM, PM, SFG,
FG in that order. Not in the SKU master: 404 `sku_not_found`. A type outside RM/PM/FG/SFG: 422
`type_not_addable`. **+ Add article** also offers FG/SFG.

**A4. Remove.** An added article of any type can be removed (undo add). BOM-module lines stay RM/PM
only (the SFG seam line gets no ✕).

**A5. Other stock on this floor** gets an **Actions** column (users with the job card edit
permission) with **Use** on RM/PM/FG/SFG rows. Use opens the Add dialog with the item picked; after
Add the item moves into BOM articles with its stock. Using an article removed from this job card
restores it.

**A6. Stock in the Add dialog.** After a pick (or with Use), the dialog shows the article's stock on
this floor: "On A185 · Mezzanine: Fresh Stock 836.660 kg · Off Grade/Rejection 0.500 kg", or "None on
this floor", or "Floor stock not loaded".
