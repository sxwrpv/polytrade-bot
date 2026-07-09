-- polytrade initial schema (Postgres/Supabase).
-- Generated from backend/db/models.py PG_SCHEMA_SQL — keep in sync.
CREATE TABLE IF NOT EXISTS users (
    id                       TEXT PRIMARY KEY,
    signer_address           TEXT,
    api_token                TEXT,
    telegram_user_id         BIGINT,
    display_name             TEXT,
    private_key_enc          TEXT NOT NULL,
    export_blob              TEXT,
    deposit_wallet_deployed  INTEGER NOT NULL DEFAULT 0,
    referral_code            TEXT UNIQUE,
    referred_by              TEXT,
    paused                   INTEGER NOT NULL DEFAULT 0,
    copy_multiplier          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    max_slippage_pct         DOUBLE PRECISION,
    max_total_exposure_usd   DOUBLE PRECISION,
    daily_loss_limit_usd     DOUBLE PRECISION,
    default_allocation_pct   DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    default_max_position_usd DOUBLE PRECISION NOT NULL DEFAULT 50.0,
    created_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followed_traders (
    id                     TEXT PRIMARY KEY,
    user_id                TEXT NOT NULL REFERENCES users(id),
    trader_address         TEXT NOT NULL,
    allocation_pct         DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    max_position_usd       DOUBLE PRECISION NOT NULL DEFAULT 50.0,
    paused                 INTEGER NOT NULL DEFAULT 0,
    max_slippage_pct       DOUBLE PRECISION,
    max_total_exposure_usd DOUBLE PRECISION,
    daily_loss_limit_usd   DOUBLE PRECISION,
    copy_ratio_pct         DOUBLE PRECISION,
    min_leader_usd         DOUBLE PRECISION,
    ignore_below_usd       DOUBLE PRECISION,
    max_open_positions     INTEGER,
    min_price              DOUBLE PRECISION,
    max_price              DOUBLE PRECISION,
    is_active              INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT NOT NULL,
    UNIQUE(user_id, trader_address)
);

CREATE TABLE IF NOT EXISTS copy_positions (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id),
    trader_address TEXT NOT NULL,
    condition_id   TEXT NOT NULL,
    token_id       TEXT NOT NULL,
    market_slug    TEXT,
    market_title   TEXT,
    outcome        TEXT NOT NULL,
    shares         DOUBLE PRECISION NOT NULL,
    trader_shares  DOUBLE PRECISION,
    entry_price    DOUBLE PRECISION NOT NULL,
    notional_usd   DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    exit_price     DOUBLE PRECISION,
    realized_pnl   DOUBLE PRECISION,
    opened_at      TEXT NOT NULL,
    closed_at      TEXT
);

CREATE TABLE IF NOT EXISTS trade_events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    position_id TEXT REFERENCES copy_positions(id),
    event_type  TEXT NOT NULL,
    amount_usd  DOUBLE PRECISION,
    pnl         DOUBLE PRECISION,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    ts              TEXT NOT NULL,
    equity          DOUBLE PRECISION,
    balance         DOUBLE PRECISION,
    positions_value DOUBLE PRECISION,
    realized_pnl    DOUBLE PRECISION,
    unrealized_pnl  DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS trader_cache (
    address           TEXT PRIMARY KEY,
    display_name      TEXT,
    profile_image     TEXT,
    x_username        TEXT,
    verified          INTEGER NOT NULL DEFAULT 0,
    total_pnl         DOUBLE PRECISION,
    win_rate          DOUBLE PRECISION,
    consistency_score DOUBLE PRECISION,
    total_trades      INTEGER,
    open_positions    INTEGER NOT NULL DEFAULT 0,
    volume_usd        DOUBLE PRECISION,
    unrealized_pnl    DOUBLE PRECISION,
    pnl_quality       DOUBLE PRECISION,
    winrate_7d              DOUBLE PRECISION,
    winrate_30d             DOUBLE PRECISION,
    winrate_90d             DOUBLE PRECISION,
    pnl_7d                  DOUBLE PRECISION,
    pnl_30d                 DOUBLE PRECISION,
    pnl_90d                 DOUBLE PRECISION,
    volume_7d               DOUBLE PRECISION,
    volume_30d              DOUBLE PRECISION,
    volume_90d              DOUBLE PRECISION,
    green_days_7d           INTEGER,
    red_days_7d             INTEGER,
    consistency_ratio_7d    DOUBLE PRECISION,
    green_days_30d          INTEGER,
    red_days_30d            INTEGER,
    consistency_ratio_30d   DOUBLE PRECISION,
    green_days_90d          INTEGER,
    red_days_90d            INTEGER,
    consistency_ratio_90d   DOUBLE PRECISION,
    fills_7d                INTEGER,
    exits_7d                INTEGER,
    fill_exit_ratio_7d      DOUBLE PRECISION,
    fills_30d               INTEGER,
    exits_30d               INTEGER,
    fill_exit_ratio_30d     DOUBLE PRECISION,
    fills_90d               INTEGER,
    exits_90d               INTEGER,
    fill_exit_ratio_90d     DOUBLE PRECISION,
    daily_pnl_90d           TEXT,
    history_days            DOUBLE PRECISION,
    stats_refreshed_at      TEXT,
    last_refreshed          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_copy_positions_user_status ON copy_positions(user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_position_per_token
    ON copy_positions(user_id, token_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_copy_positions_user_trader ON copy_positions(user_id, trader_address);
CREATE INDEX IF NOT EXISTS idx_followed_active ON followed_traders(is_active);
CREATE INDEX IF NOT EXISTS idx_trade_events_user_ts ON trade_events(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_trader_cache_consistency ON trader_cache(consistency_score);
CREATE INDEX IF NOT EXISTS idx_trader_cache_winrate_30d ON trader_cache(winrate_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_pnl_30d ON trader_cache(pnl_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_volume_30d ON trader_cache(volume_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_consistency_ratio_30d ON trader_cache(consistency_ratio_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_fill_exit_ratio_30d ON trader_cache(fill_exit_ratio_30d);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_api_token ON users(api_token);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_telegram
    ON users(telegram_user_id) WHERE telegram_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trader_cache_stats_refreshed ON trader_cache(stats_refreshed_at);
