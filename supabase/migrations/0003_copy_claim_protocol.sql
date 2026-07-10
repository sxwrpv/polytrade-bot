-- Durable BUY claim protocol and cross-process risk revision.
-- Additive/idempotent: safe after the historical 0001 + 0002 migrations.
ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS risk_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.users
  ALTER COLUMN default_max_position_usd SET DEFAULT 15.0;
ALTER TABLE public.followed_traders
  ALTER COLUMN max_position_usd SET DEFAULT 15.0;

CREATE TABLE IF NOT EXISTS public.copy_open_claims (
    user_id        TEXT NOT NULL REFERENCES public.users(id),
    token_id       TEXT NOT NULL,
    trader_address TEXT NOT NULL,
    claim_id       TEXT,
    action         TEXT NOT NULL DEFAULT 'open',
    state          TEXT NOT NULL DEFAULT 'reserved',
    reserved_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    risk_revision  INTEGER NOT NULL DEFAULT 0,
    claimed_at     TEXT NOT NULL,
    updated_at     TEXT,
    last_error     TEXT,
    PRIMARY KEY(user_id, token_id)
);
ALTER TABLE public.copy_open_claims ADD COLUMN IF NOT EXISTS claim_id TEXT;
ALTER TABLE public.copy_open_claims ADD COLUMN IF NOT EXISTS action TEXT NOT NULL DEFAULT 'open';
ALTER TABLE public.copy_open_claims ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'reserved';
ALTER TABLE public.copy_open_claims ADD COLUMN IF NOT EXISTS reserved_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE public.copy_open_claims ADD COLUMN IF NOT EXISTS risk_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.copy_open_claims ADD COLUMN IF NOT EXISTS updated_at TEXT;
ALTER TABLE public.copy_open_claims ADD COLUMN IF NOT EXISTS last_error TEXT;
UPDATE public.copy_open_claims
SET claim_id = COALESCE(claim_id, md5(user_id || ':' || token_id)),
    updated_at = COALESCE(updated_at, claimed_at);
ALTER TABLE public.copy_open_claims ALTER COLUMN claim_id SET NOT NULL;
ALTER TABLE public.copy_open_claims ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE public.copy_open_claims ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.copy_open_claims FROM anon, authenticated;
DROP POLICY IF EXISTS no_api_access ON public.copy_open_claims;
CREATE POLICY no_api_access ON public.copy_open_claims
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

-- Preserve all legacy active rows while deterministically selecting one fence:
-- closing first, then latest opened_at (id breaks timestamp ties).
WITH ranked AS (
  SELECT id, ROW_NUMBER() OVER (
    PARTITION BY user_id, token_id
    ORDER BY CASE status WHEN 'closing' THEN 0 ELSE 1 END, opened_at DESC, id DESC
  ) AS rn
  FROM public.copy_positions
  WHERE status IN ('open', 'closing')
)
UPDATE public.copy_positions AS p
SET status = 'reconciliation_required'
FROM ranked
WHERE p.id = ranked.id AND ranked.rn > 1;

-- OPEN and CLOSING are both active ownership/fencing states. This partial
-- unique index makes untracked close claims race-safe across API workers.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_position_per_token
  ON public.copy_positions(user_id, token_id)
  WHERE status IN ('open', 'closing');
