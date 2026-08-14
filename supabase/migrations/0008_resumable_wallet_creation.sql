-- Preserve one signer identity across ambiguous SDK wallet creation and lease
-- opaque external work across backend processes. Additive and replay-safe.
ALTER TABLE public.wallet_creation_claims
    ADD COLUMN IF NOT EXISTS signer_address TEXT;
ALTER TABLE public.wallet_creation_claims
    ADD COLUMN IF NOT EXISTS private_key_enc TEXT;
ALTER TABLE public.wallet_creation_claims
    ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE public.wallet_creation_claims
    ADD COLUMN IF NOT EXISTS lease_expires_at TEXT;

COMMENT ON COLUMN public.wallet_creation_claims.private_key_enc IS
    'Temporary at-rest ciphertext for the single durable signer; cleared on completion';
COMMENT ON COLUMN public.wallet_creation_claims.lease_owner IS
    'Durable exclusive SDK-work owner; clear only after a caught, returned SDK failure';
COMMENT ON COLUMN public.wallet_creation_claims.lease_expires_at IS
    'Informational only; expiry never authorizes side-effect ownership takeover';
