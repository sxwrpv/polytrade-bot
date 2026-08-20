-- Privacy-minimized Release C product telemetry.
-- Authentication gates ingestion in the API, but no account or wallet identity
-- is retained. Runtime pruning enforces a rolling 90-day window.
CREATE TABLE IF NOT EXISTS public.product_events (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    event_name      TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    ts              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_events_name_ts
    ON public.product_events(event_name, ts);
CREATE INDEX IF NOT EXISTS idx_product_events_ts
    ON public.product_events(ts);
CREATE INDEX IF NOT EXISTS idx_product_events_session_ts
    ON public.product_events(session_id, ts);

-- The backend's database owner writes events. Supabase browser roles get no
-- direct table access even if API grants or default privileges change later.
ALTER TABLE public.product_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.product_events FROM anon, authenticated;
DROP POLICY IF EXISTS no_api_access ON public.product_events;
CREATE POLICY no_api_access ON public.product_events
    FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
