-- The Performance chart and the snapshot writer read equity_snapshots by user
-- and time (backend/core/equity.py: get_series, take_snapshot), and compaction
-- walks it in (user_id, ts) order. Postgres had no index on the table beyond
-- its primary key, so each of those reads scanned every user's snapshots.
--
-- Additive and replay-safe. Also created by the application's boot schema
-- (backend/db/models.py PG_SCHEMA_SQL) on the next start.
CREATE INDEX IF NOT EXISTS idx_equity_snapshots_user_ts
    ON public.equity_snapshots(user_id, ts);
