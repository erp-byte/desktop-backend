"""The calendar day a stock-take timestamp belongs to.

WHY THIS EXISTS AS ONE DEFINITION
Every date in this module — the baseline count date, the netting window, the
ledger's date filters, and the date encoded in a txn_code — has to mean the same
"day" or the figures stop agreeing with each other. They are computed in four
different queries across two services, so the expression lives here once.

THE DAY IS IST, NOT THE SERVER'S
The database server runs on UTC. Truncating with a bare `created_at::date`
therefore cuts the day at 05:30 IST, so a count or an adjustment made in the
first half of a night shift lands on the previous day from the one the operator
sees on screen. The warehouse works to Asia/Kolkata, so that is the day.

THE TABLES NEED DIFFERENT SQL — THIS IS THE TRAP
    new_stock_entries.created_at       timestamp WITH time zone
    stocktake_transactions.created_at  timestamp WITH time zone
    stocktake_entries.created_at       timestamp WITHOUT time zone, holding UTC

For the tz-aware column, `AT TIME ZONE 'Asia/Kolkata'` converts an absolute
instant to IST wall-clock. For the NAIVE column the same expression means the
opposite — "read this value as if it were already IST" — and would shift the day
by -5:30 instead of +5:30. The naive column must first be told what it is
(`AT TIME ZONE 'UTC'`) and only then converted.

That the naive column holds UTC is not documented anywhere; it is established
from the data. Its hour-of-day histogram runs 02:00-19:00 and peaks at 11:00-12:00.
Read as UTC that is 07:30-00:30 IST peaking 16:30-17:30 — a warehouse day. Read
as IST it would mean roughly 950 counting entries were made between 2am and 5am,
which is not what happens on a shop floor.

WHY THE TABLE NAME LIVES HERE TOO
This console reads `new_stock_entries`, not the `stocktake_entries` the floor app
writes. The two carry the same 8,590 rows under the same ids, but the new one
stores its timestamps as timestamptz and its floor names canonicalised. So the
table and the day expression are not independent choices: pairing either one with
the other table's expression is silently wrong rather than an error — the -5:30
form still parses, still returns dates, and just puts every count on the wrong
day. Importing both from one module is what keeps them in step.

Readers still on the old table (legacy_backend/services/ims_service, and the
floor app itself) must keep using LEGACY_ENTRY_DAY.
"""
from __future__ import annotations

BUSINESS_TZ = "Asia/Kolkata"

#: The entries table this console reads and writes. See "WHY THE TABLE NAME
#: LIVES HERE TOO" above — it must be imported alongside ENTRY_DAY, never
#: hard-coded next to a day expression that came from somewhere else.
ENTRIES_TABLE = "new_stock_entries"

#: IST calendar day of a `new_stock_entries` row (timestamptz).
ENTRY_DAY = "(created_at AT TIME ZONE 'Asia/Kolkata')::date"

#: IST calendar day of a `stocktake_entries` row (naive column holding UTC).
#: Kept for the readers that still point at the floor app's table.
LEGACY_ENTRY_DAY = "((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date"

#: IST calendar day of a `stocktake_transactions` row (timestamptz).
TXN_DAY = "(created_at AT TIME ZONE 'Asia/Kolkata')::date"
