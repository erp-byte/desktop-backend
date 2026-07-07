# Remaining anomalies after option (a)

84 Rishi Cold rows are now INCLUDED in savla_articles_backfill.sql with synthetic
transaction_no = 'RISHI-LEGACY-{lot_no}' (see Part 3 of the SQL file).

## Still excluded -- needs your decision: 1 row

- Row 487: transaction_no='23007', unit='Supreme', item='WET DATE BOX 10KG', lot=35038, mark='KS SUFRI', remarks='Sufri'

Inward No is a 5-digit number `23007` with no GR prefix, from a `Supreme`
warehouse. Treat as typo for `GR23007` and manually add, or leave excluded.