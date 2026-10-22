-- Capital-share copy sizing, per followed wallet (backend/core/copy_engine.py,
-- cash_share). The owner's call on 2026-10-22: a copy may take the share of
-- the follower's free cash that the leader's bet took of the leader's.
--
--   sizing_mode         NULL or 'position_share': leader position × copy_ratio_pct
--                       (every follow until now, and the default)
--                       'capital_share': share of the leader's free cash the
--                       bet took × the follower's free cash × capital_multiplier
--   capital_multiplier  NULL = DEFAULT_CAPITAL_MULTIPLIER (1.0)
--
-- Additive and replay-safe: two nullable columns, so every existing row keeps
-- sizing exactly as before. Also applied by the application's boot schema
-- (backend/db/models.py PG_SCHEMA_SQL) on the next start. followed_traders
-- already has RLS and the 0002 lockdown, which cover new columns.
ALTER TABLE public.followed_traders ADD COLUMN IF NOT EXISTS sizing_mode TEXT
    CHECK (sizing_mode IS NULL OR sizing_mode IN ('position_share', 'capital_share'));
ALTER TABLE public.followed_traders ADD COLUMN IF NOT EXISTS capital_multiplier DOUBLE PRECISION;
