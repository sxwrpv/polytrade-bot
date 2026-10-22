-- Withdrawals: one row per request, the audit trail of pUSD leaving a
-- custodial deposit wallet as native USDC on Polygon through Polymarket's
-- bridge (backend/core/withdraw.py). Status only moves forward, each step
-- stamps its own *_at column, and rows are never deleted.
--
-- Additive and replay-safe. Also created by the application's boot schema
-- (backend/db/models.py PG_SCHEMA_SQL) on the next start; this file adds the
-- Supabase-role lockdown the boot schema cannot name.
CREATE TABLE IF NOT EXISTS public.withdrawals (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES public.users(id),
    idempotency_key  TEXT NOT NULL,
    amount_units     BIGINT NOT NULL CHECK(amount_units > 0),
    destination      TEXT NOT NULL,
    to_chain_id      INTEGER NOT NULL,
    to_token         TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN ('requested','submitting','submitted',
                         'bridging','confirmed','failed','attention','refunded')),
    quote_id         TEXT,
    quoted_out_units BIGINT,
    min_out_units    BIGINT,
    bridge_address   TEXT,
    bridge_baseline  INTEGER,
    relayer_tx_id    TEXT,
    tx_hash          TEXT,
    submit_block     BIGINT,
    bridge_status    TEXT,
    delivery_tx_hash TEXT,
    delivered_units  BIGINT,
    error            TEXT,
    operator_note    TEXT,
    requested_at     TEXT NOT NULL,
    submitting_at    TEXT,
    submitted_at     TEXT,
    bridging_at      TEXT,
    confirmed_at     TEXT,
    failed_at        TEXT,
    attention_at     TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key)
);

-- One open withdrawal per user. 'attention' (funds with the bridge, delivery
-- unconfirmed) stays open until an operator resolves it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_withdrawals_open_per_user
    ON public.withdrawals(user_id)
    WHERE status IN ('requested','submitting','submitted','bridging','attention');
CREATE INDEX IF NOT EXISTS idx_withdrawals_user_requested
    ON public.withdrawals(user_id, requested_at);

-- Destinations and bridge addresses are user data: backend only.
ALTER TABLE public.withdrawals ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.withdrawals FROM anon, authenticated;
DROP POLICY IF EXISTS no_api_access ON public.withdrawals;
CREATE POLICY no_api_access ON public.withdrawals
    FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
