-- Refresh health and backoff state for trader_cache (2026-09-02).
--
-- refresh_all ordered candidates by "stats_refreshed_at IS NULL DESC", so a
-- wallet whose upstream data consistently 500s never set that column and
-- therefore sorted FIRST in every batch, forever. Two addresses produced 381
-- of 388 refresh failures over 3.4 days of production logs.
--
-- next_retry_at is the cooldown gate. Cached stats are never deleted: a
-- quarantined wallet keeps showing its last good numbers, labelled stale.

ALTER TABLE trader_cache ADD COLUMN IF NOT EXISTS last_refresh_attempt_at TEXT;
ALTER TABLE trader_cache ADD COLUMN IF NOT EXISTS last_refresh_success_at TEXT;
ALTER TABLE trader_cache ADD COLUMN IF NOT EXISTS refresh_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE trader_cache ADD COLUMN IF NOT EXISTS next_retry_at TEXT;
ALTER TABLE trader_cache ADD COLUMN IF NOT EXISTS last_refresh_error TEXT;
ALTER TABLE trader_cache ADD COLUMN IF NOT EXISTS data_completeness TEXT;

CREATE INDEX IF NOT EXISTS idx_trader_cache_next_retry ON trader_cache(next_retry_at);
