-- Per-position PnL lookups (closed history, stats, by-wallet breakdown) filter
-- trade_events by position_id through correlated subqueries. With only
-- (user_id, ts) indexed, each lookup scanned the whole events table, so every
-- dashboard load grew with the platform's total trade history.
--
-- Additive and replay-safe. Also created by the application's boot schema
-- (backend/db/models.py PG_SCHEMA_SQL) on the next start.
CREATE INDEX IF NOT EXISTS idx_trade_events_position
    ON public.trade_events(position_id);
