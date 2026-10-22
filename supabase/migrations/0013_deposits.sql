-- Deposits: one row per deposit announced to its owner on Telegram, pUSD
-- that Polymarket's CollateralOnramp minted into a custodial deposit wallet
-- (backend/core/deposits.py). The alert goes out only from the insert that
-- creates the row, so UNIQUE(user_id, tx_hash) keeps it to one message per
-- deposit across rescans, restarts and processes. Rows are never deleted.
--
-- chain_cursors: the last Polygon block each chain scan finished, so the
-- deposit scan resumes where it stopped after a restart.
--
-- Additive and replay-safe. Also created by the application's boot schema
-- (backend/db/models.py PG_SCHEMA_SQL) on the next start; this file adds the
-- Supabase-role lockdown the boot schema cannot name.
CREATE TABLE IF NOT EXISTS public.deposits (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES public.users(id),
    tx_hash       TEXT NOT NULL,
    amount_units  BIGINT NOT NULL CHECK(amount_units > 0),
    block_number  BIGINT NOT NULL,
    landed_at     TEXT NOT NULL,
    detected_at   TEXT NOT NULL,
    UNIQUE(user_id, tx_hash)
);
CREATE INDEX IF NOT EXISTS idx_deposits_user_landed
    ON public.deposits(user_id, landed_at);

CREATE TABLE IF NOT EXISTS public.chain_cursors (
    name        TEXT PRIMARY KEY,
    last_block  BIGINT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Wallets and amounts are user data: backend only.
ALTER TABLE public.deposits ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.deposits FROM anon, authenticated;
DROP POLICY IF EXISTS no_api_access ON public.deposits;
CREATE POLICY no_api_access ON public.deposits
    FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

ALTER TABLE public.chain_cursors ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.chain_cursors FROM anon, authenticated;
DROP POLICY IF EXISTS no_api_access ON public.chain_cursors;
CREATE POLICY no_api_access ON public.chain_cursors
    FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
