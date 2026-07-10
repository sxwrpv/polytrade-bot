-- Lock the schema to the backend only. polytrade holds encrypted wallet keys
-- and session tokens; no browser/SDK client ever queries Supabase directly, so
-- the auto-generated REST/GraphQL API must expose nothing. The bot connects as
-- the `postgres` role (BYPASSRLS + table owner), so it is unaffected.
--
-- Defense in depth, three layers:
--   1. ENABLE RLS            -> deny-by-default for every non-owner role
--   2. REVOKE grants         -> anon/authenticated can't even reach the tables
--                               (this is what makes the REST API return 401)
--   3. explicit deny policy  -> self-documenting backstop if a grant is ever
--                               restored by accident; also clears the linter's
--                               "RLS enabled, no policy" notice

ALTER TABLE public.users            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.followed_traders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.copy_positions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trade_events     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trader_cache     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.equity_snapshots ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;

CREATE POLICY no_api_access ON public.users            FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
CREATE POLICY no_api_access ON public.followed_traders FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
CREATE POLICY no_api_access ON public.copy_positions   FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
CREATE POLICY no_api_access ON public.trade_events     FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
CREATE POLICY no_api_access ON public.trader_cache     FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
CREATE POLICY no_api_access ON public.equity_snapshots FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
