"""SQLite schema as SQL strings — run at startup (idempotent, IF NOT EXISTS).

Deltas from the original spec (see BUILD_PLAN.md §4):
  - users.id           = the deposit/funder wallet address (the proxyWallet that
                         appears in positions), NOT the signer EOA.
  - users.signer_address = the EOA derived from the encrypted signer key.
  - users.private_key_enc = AES-256-GCM(signer key, ENCRYPTION_SECRET) — at rest;
                         the engine must decrypt autonomously, so it is NOT a
                         passphrase. Passphrase is an export-only second factor.
  - collateral is pUSD, not USDC (notional_usd / amount_usd / volume_usd are pUSD).
  - trader_cache.open_positions surfaces "N open positions" so the UI/ranking can
    avoid flat market-makers (phase-2 product insight).

Outcome strings are stored normalized as 'YES'/'NO' (the API returns 'Yes'/'No').
"""
from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id                      TEXT PRIMARY KEY,          -- deposit/funder wallet (0x..., = proxyWallet)
    signer_address          TEXT,                      -- EOA derived from the signer key
    api_token               TEXT,                      -- secret session token (Bearer auth; the address is public!)
    telegram_user_id        INTEGER,                   -- linked Telegram account (Mini App login)
    display_name            TEXT,
    private_key_enc         TEXT NOT NULL,             -- AES-256-GCM(signer key, ENCRYPTION_SECRET)
    export_blob             TEXT,                      -- AES-256-GCM(signer key, passphrase) for /export-key
    deposit_wallet_deployed INTEGER NOT NULL DEFAULT 0,
    referral_code           TEXT UNIQUE,
    referred_by             TEXT,
    -- risk settings (engine-enforced; NULL = use global default)
    paused                  INTEGER NOT NULL DEFAULT 0,   -- master kill-switch
    copy_multiplier         REAL NOT NULL DEFAULT 1.0,    -- 0.1x..5x scaling of every copy
    max_slippage_pct        REAL,                          -- per-user cap vs leader price
    max_total_exposure_usd  REAL,                          -- cap on total open notional
    daily_loss_limit_usd    REAL,                          -- block opens after today's loss hits this
    default_allocation_pct  REAL NOT NULL DEFAULT 10.0,    -- prefill for follow modal
    default_max_position_usd REAL NOT NULL DEFAULT 50.0,
    created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followed_traders (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(id),
    trader_address   TEXT NOT NULL,                    -- proxyWallet of the copied trader
    -- per-wallet risk settings (each copied trader is configured independently)
    allocation_pct   REAL NOT NULL DEFAULT 10.0,       -- % of user capital to copy
    max_position_usd REAL NOT NULL DEFAULT 50.0,       -- pUSD cap per position
    paused           INTEGER NOT NULL DEFAULT 0,       -- pause copying this wallet
    max_slippage_pct REAL,                              -- vs leader price (NULL = global)
    max_total_exposure_usd REAL,                        -- cap total open notional for this trader
    daily_loss_limit_usd REAL,                          -- block opens after today's loss on this trader
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    UNIQUE(user_id, trader_address)
);

CREATE TABLE IF NOT EXISTS copy_positions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id),
    trader_address TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    token_id      TEXT NOT NULL,                       -- = position.asset
    market_slug   TEXT,
    market_title  TEXT,
    outcome       TEXT NOT NULL,                       -- 'YES' | 'NO'
    shares        REAL NOT NULL,                       -- our copied share count
    trader_shares REAL,                                -- trader's share count we are mirroring
    entry_price   REAL NOT NULL,                       -- = position.avgPrice
    notional_usd  REAL NOT NULL,                       -- pUSD
    status        TEXT NOT NULL DEFAULT 'open',        -- 'open' | 'closed' | 'resolved'
    exit_price    REAL,
    realized_pnl  REAL,
    opened_at     TEXT NOT NULL,
    closed_at     TEXT
);

CREATE TABLE IF NOT EXISTS trade_events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    position_id TEXT REFERENCES copy_positions(id),
    event_type  TEXT NOT NULL,                         -- 'open' | 'close' | 'partial' | 'resolve'
    amount_usd  REAL,                                  -- pUSD
    pnl         REAL,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trader_cache (
    address           TEXT PRIMARY KEY,                -- proxyWallet
    display_name      TEXT,                            -- userName
    profile_image     TEXT,
    x_username        TEXT,
    verified          INTEGER NOT NULL DEFAULT 0,
    total_pnl         REAL,
    win_rate          REAL,
    consistency_score REAL,
    total_trades      INTEGER,
    open_positions    INTEGER NOT NULL DEFAULT 0,      -- avoid following flat market-makers
    volume_usd        REAL,
    unrealized_pnl    REAL,                            -- mark-to-market on currently open positions (snapshot)
    pnl_quality       REAL,                            -- realized_pnl_all_time - unrealized_pnl_now: banked vs paper gains
    -- windowed screener metrics (7d/30d/90d) — see backend/core/trader_stats.py _period_metrics.
    -- winrate_Xd / pnl_Xd: closing-trade win rate and realized pnl within the window (avg-cost basis).
    -- volume_Xd: sum of trade usd_size within the window.
    -- green_days_Xd/red_days_Xd/consistency_ratio_Xd: count of days with positive/negative realized pnl
    --   within the window, and green/(green+red).
    -- fills_Xd/exits_Xd/fill_exit_ratio_Xd: BUY count, SELL count, and exits/fills*100 (%) within the
    --   window — how much of what they open they actually close out, vs. hold to resolution.
    winrate_7d              REAL,
    winrate_30d             REAL,
    winrate_90d             REAL,
    pnl_7d                  REAL,
    pnl_30d                 REAL,
    pnl_90d                 REAL,
    volume_7d               REAL,
    volume_30d              REAL,
    volume_90d              REAL,
    green_days_7d           INTEGER,
    red_days_7d             INTEGER,
    consistency_ratio_7d    REAL,
    green_days_30d          INTEGER,
    red_days_30d            INTEGER,
    consistency_ratio_30d   REAL,
    green_days_90d          INTEGER,
    red_days_90d            INTEGER,
    consistency_ratio_90d   REAL,
    fills_7d                INTEGER,
    exits_7d                INTEGER,
    fill_exit_ratio_7d      REAL,
    fills_30d               INTEGER,
    exits_30d               INTEGER,
    fill_exit_ratio_30d     REAL,
    fills_90d               INTEGER,
    exits_90d               INTEGER,
    fill_exit_ratio_90d     REAL,
    daily_pnl_90d           TEXT,   -- JSON {"YYYY-MM-DD": realized_pnl} last 90d
    history_days            REAL,   -- how far back fetched trade history reaches (90 = full window; less = page budget hit, windowed stats are partial)
    stats_refreshed_at      TEXT,   -- when windowed stats were last computed
    last_refreshed    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_copy_positions_user_status
    ON copy_positions(user_id, status);
-- at most one OPEN position per (user, token): lets the fast detection path and
-- the slow reconciler both attempt an open; the loser hits this and is skipped.
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_position_per_token
    ON copy_positions(user_id, token_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_copy_positions_user_trader
    ON copy_positions(user_id, trader_address);
CREATE INDEX IF NOT EXISTS idx_followed_active
    ON followed_traders(is_active);
CREATE INDEX IF NOT EXISTS idx_trade_events_user_ts
    ON trade_events(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_trader_cache_consistency
    ON trader_cache(consistency_score);
CREATE INDEX IF NOT EXISTS idx_trader_cache_winrate_30d ON trader_cache(winrate_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_pnl_30d ON trader_cache(pnl_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_volume_30d ON trader_cache(volume_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_consistency_ratio_30d ON trader_cache(consistency_ratio_30d);
CREATE INDEX IF NOT EXISTS idx_trader_cache_fill_exit_ratio_30d ON trader_cache(fill_exit_ratio_30d);
"""

TABLES = ("users", "followed_traders", "copy_positions", "trade_events", "trader_cache")

# Idempotent ALTERs for DBs created before a column existed (CREATE TABLE IF NOT
# EXISTS won't add columns to an existing table). Applied at startup; "duplicate
# column" errors are ignored.
MIGRATIONS = (
    "ALTER TABLE users ADD COLUMN paused INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN copy_multiplier REAL NOT NULL DEFAULT 1.0",
    "ALTER TABLE users ADD COLUMN max_slippage_pct REAL",
    "ALTER TABLE users ADD COLUMN max_total_exposure_usd REAL",
    "ALTER TABLE users ADD COLUMN daily_loss_limit_usd REAL",
    "ALTER TABLE users ADD COLUMN default_allocation_pct REAL NOT NULL DEFAULT 10.0",
    "ALTER TABLE users ADD COLUMN default_max_position_usd REAL NOT NULL DEFAULT 50.0",
    "ALTER TABLE copy_positions ADD COLUMN trader_shares REAL",
    "ALTER TABLE users ADD COLUMN export_blob TEXT",
    "ALTER TABLE users ADD COLUMN signer_address TEXT",
    "ALTER TABLE followed_traders ADD COLUMN paused INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE followed_traders ADD COLUMN max_slippage_pct REAL",
    "ALTER TABLE followed_traders ADD COLUMN max_total_exposure_usd REAL",
    "ALTER TABLE followed_traders ADD COLUMN daily_loss_limit_usd REAL",
    # windowed wallet-screener metrics (7d/30d/90d) + pnl quality — see trader_cache above.
    "ALTER TABLE trader_cache ADD COLUMN unrealized_pnl REAL",
    "ALTER TABLE trader_cache ADD COLUMN pnl_quality REAL",
    "ALTER TABLE trader_cache ADD COLUMN winrate_7d REAL",
    "ALTER TABLE trader_cache ADD COLUMN winrate_30d REAL",
    "ALTER TABLE trader_cache ADD COLUMN winrate_90d REAL",
    "ALTER TABLE trader_cache ADD COLUMN pnl_7d REAL",
    "ALTER TABLE trader_cache ADD COLUMN pnl_30d REAL",
    "ALTER TABLE trader_cache ADD COLUMN pnl_90d REAL",
    "ALTER TABLE trader_cache ADD COLUMN volume_7d REAL",
    "ALTER TABLE trader_cache ADD COLUMN volume_30d REAL",
    "ALTER TABLE trader_cache ADD COLUMN volume_90d REAL",
    "ALTER TABLE trader_cache ADD COLUMN green_days_7d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN red_days_7d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN consistency_ratio_7d REAL",
    "ALTER TABLE trader_cache ADD COLUMN green_days_30d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN red_days_30d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN consistency_ratio_30d REAL",
    "ALTER TABLE trader_cache ADD COLUMN green_days_90d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN red_days_90d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN consistency_ratio_90d REAL",
    "ALTER TABLE trader_cache ADD COLUMN fills_7d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN exits_7d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN fill_exit_ratio_7d REAL",
    "ALTER TABLE trader_cache ADD COLUMN fills_30d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN exits_30d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN fill_exit_ratio_30d REAL",
    "ALTER TABLE trader_cache ADD COLUMN fills_90d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN exits_90d INTEGER",
    "ALTER TABLE trader_cache ADD COLUMN fill_exit_ratio_90d REAL",
    # token auth + Telegram Mini App login (tokens are backfilled at startup
    # by auth.ensure_api_tokens for rows that predate these columns). The
    # indexes live here, not in SCHEMA_SQL, because on a pre-existing DB the
    # columns only exist after the ALTERs above have run.
    "ALTER TABLE users ADD COLUMN api_token TEXT",
    "ALTER TABLE users ADD COLUMN telegram_user_id INTEGER",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_api_token ON users(api_token)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_telegram "
    "ON users(telegram_user_id) WHERE telegram_user_id IS NOT NULL",
    # per-day realized PnL for the last 90d as a JSON object {"YYYY-MM-DD": pnl}
    # — powers the per-card 7/30/90d equity sparkline without extra API calls.
    "ALTER TABLE trader_cache ADD COLUMN daily_pnl_90d TEXT",
    # trade-history coverage in days (90 = the whole 90d window was fetched;
    # less = pagination budget ran out — the UI marks wider periods as partial)
    "ALTER TABLE trader_cache ADD COLUMN history_days REAL",
    # when the windowed screener stats were last computed (last_refreshed is
    # bumped by every upsert incl. cheap discovery, so it can't drive the
    # stale-first refresh rotation).
    "ALTER TABLE trader_cache ADD COLUMN stats_refreshed_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_trader_cache_stats_refreshed "
    "ON trader_cache(stats_refreshed_at)",
)
