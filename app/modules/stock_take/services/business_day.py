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

THE TWO TABLES NEED DIFFERENT SQL — THIS IS THE TRAP
    stocktake_entries.created_at       timestamp WITHOUT time zone, holding UTC
    stocktake_transactions.created_at  timestamp WITH time zone

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
"""
from __future__ import annotations

BUSINESS_TZ = "Asia/Kolkata"

#: IST calendar day of a `stocktake_entries` row (naive column holding UTC).
ENTRY_DAY = "((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date"

#: IST calendar day of a `stocktake_transactions` row (timestamptz).
TXN_DAY = "(created_at AT TIME ZONE 'Asia/Kolkata')::date"
