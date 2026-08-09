-- Hashed, expiring sessions.
-- users.api_token now stores a 'sha256:<digest>' of the session cookie value,
-- never the raw secret, and every session carries an expiry. Sessions issued
-- before this (plaintext, non-expiring) are destroyed rather than migrated:
-- they are replayable credentials, so the safe move is to force a re-login.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS api_token_expires_at TEXT;

UPDATE public.users
   SET api_token = NULL, api_token_expires_at = NULL
 WHERE api_token IS NOT NULL
   AND (api_token NOT LIKE 'sha256:%' OR api_token_expires_at IS NULL);
