-- Durable onboarding invariants. This migration is a complete additive upgrade
-- from an already-applied 0006 database; application boot schema is not needed.
-- All objects are schema-qualified and every operation is safe to replay.

CREATE TABLE IF NOT EXISTS public.user_consents (
    user_id          TEXT NOT NULL REFERENCES public.users(id),
    terms_version    TEXT NOT NULL,
    telegram_user_id BIGINT NOT NULL,
    accepted_at      TEXT NOT NULL,
    PRIMARY KEY(user_id, terms_version)
);
CREATE INDEX IF NOT EXISTS idx_user_consents_telegram
    ON public.user_consents(telegram_user_id);

CREATE TABLE IF NOT EXISTS public.funding_acknowledgements (
    user_id     TEXT NOT NULL REFERENCES public.users(id),
    version     TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(user_id, version)
);

CREATE TABLE IF NOT EXISTS public.wallet_creation_claims (
    telegram_user_id BIGINT PRIMARY KEY,
    claim_token      TEXT NOT NULL,
    state            TEXT NOT NULL CHECK(state IN ('claimed','side_effect_started','complete')),
    claimed_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_error       TEXT
);

-- The backend connects as the table owner. Browser-facing Supabase roles receive
-- no grants and are also denied by RLS as a defense-in-depth backstop.
ALTER TABLE public.user_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.funding_acknowledgements ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wallet_creation_claims ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.user_consents FROM anon, authenticated;
REVOKE ALL ON TABLE public.funding_acknowledgements FROM anon, authenticated;
REVOKE ALL ON TABLE public.wallet_creation_claims FROM anon, authenticated;

DROP POLICY IF EXISTS no_api_access ON public.user_consents;
CREATE POLICY no_api_access ON public.user_consents
    FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
DROP POLICY IF EXISTS no_api_access ON public.funding_acknowledgements;
CREATE POLICY no_api_access ON public.funding_acknowledgements
    FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
DROP POLICY IF EXISTS no_api_access ON public.wallet_creation_claims;
CREATE POLICY no_api_access ON public.wallet_creation_claims
    FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
