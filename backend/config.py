"""Configuration — Polymarket only. No other venues, no paper/mock trading.

Values come from the environment (.env loaded via python-dotenv). See .env.example.
"""
from __future__ import annotations

import math
import os

from dotenv import load_dotenv

load_dotenv()


def validate_slippage_pct(value: float | str, name: str = "slippage") -> float:
    """Validate every slippage source, including env/DB values that bypass API models."""
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 10:
        raise ValueError(f"{name} must be a finite number from 0 to 10")
    return value

# --- Polymarket hosts ---
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BRIDGE_API = "https://bridge.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# --- HTTP ---
# data-api 403s library/default user-agents (see API_RECON.md gotcha #1); send a
# browser-like UA on every request.
HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT", "Mozilla/5.0 (compatible; polymarket-copybot/0.1)"
)
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "15"))

# --- On-chain detection (optional, faster tier) ---
# Polygon JSON-RPC HTTP URL. If set, the engine uses the on-chain OrderFilled
# detector (~2-4s, attributed) instead of activity polling (~3-8s). Use your own
# Alchemy/Infura endpoint for reliability; a public RPC works for light use.
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "").strip()

# --- Referral ---
REFERRAL_CODE = os.environ.get("REFERRAL_CODE", "").strip()

# --- Database ---
# When DATABASE_URL is set (a Postgres/Supabase DSN) the app uses Postgres via
# asyncpg; otherwise it falls back to local SQLite at DB_PATH. Supabase: use the
# direct connection string, e.g.
#   postgresql://postgres:<PW>@db.<ref>.supabase.co:5432/postgres
# (or the session pooler on :5432). The app sets statement_cache_size=0 so the
# transaction pooler on :6543 also works.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "copybot.db")

# --- Copy engine ---
# Fast trade detection (per leader) — shrinks the leader->copy latency window.
DETECTION_POLL_SECONDS = float(os.environ.get("DETECTION_POLL_SECONDS", "2"))
# Slow reconciliation diff — catches missed trades, drift, and resolutions.
COPY_ENGINE_POLL_SECONDS = float(os.environ.get("COPY_ENGINE_POLL_SECONDS", "30"))
DEFAULT_ALLOCATION_PCT = float(os.environ.get("DEFAULT_ALLOCATION_PCT", "10.0"))
DEFAULT_MAX_POSITION_USD = float(os.environ.get("DEFAULT_MAX_POSITION_USD", "15.0"))
# Per-wallet sizing model: each copy = the LEADER's position value × this %,
# then capped by MAX/TRADE (max_position_usd), available collateral, and the
# per-trader exposure cap. 1.0 = copy 1% of the leader's dollar position.
DEFAULT_COPY_RATIO_PCT = float(os.environ.get("DEFAULT_COPY_RATIO_PCT", "1.0"))
# Price band a leader position must sit in to be copied (skip longshot dust and
# near-resolved markets). Per-wallet overridable.
# NOTE: these MUST match the WalletRiskCard UI defaults (frontend) — when a
# per-wallet column is NULL the engine falls back to these, and the slider shows
# the same number, so "what you see is what the engine enforces".
DEFAULT_MIN_PRICE = float(os.environ.get("DEFAULT_MIN_PRICE", "0.1"))
DEFAULT_MAX_PRICE = float(os.environ.get("DEFAULT_MAX_PRICE", "0.98"))
# Skip a copy whose notional would be below this (dust). Matches UI default.
DEFAULT_IGNORE_BELOW_USD = float(os.environ.get("DEFAULT_IGNORE_BELOW_USD", "2.0"))
# Max adverse price vs the LEADER's fill price. Copies execute as market (FOK)
# orders — owner's call: filling beats strictly bounding price — but the
# pre-flight quote is still checked against leader_price*(1+this) and the copy
# is skipped if the market ran further than that. Per-wallet override exists
# (followed_traders.max_slippage_pct).
MAX_COPY_SLIPPAGE_PCT = validate_slippage_pct(
    os.environ.get("MAX_COPY_SLIPPAGE_PCT", "2.0"), "MAX_COPY_SLIPPAGE_PCT")

# --- Server ---
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

# --- Wallet / signing ---
# Trading goes through polymarket-client's AsyncSecureClient (see BUILD_PLAN.md
# §wallet model — py-clob-client and py-clob-client-v2 both have real, unfixed
# upstream bugs for anything beyond plain EOA reads). Builder API credentials
# (from polymarket.com/settings?tab=builder) enable gasless deposit-wallet
# trading: wallet creation/deployment, and — via ensure_allowances' individual
# per-operator approval calls — trading approvals, all with no MATIC needed.
# Without these set, wallet.py falls back to plain EOA mode (needs a little
# MATIC for the one-time allowance; real order placement is unverified there).
POLYMARKET_BUILDER_API_KEY = os.environ.get("POLYMARKET_BUILDER_API_KEY", "").strip()
POLYMARKET_BUILDER_SECRET = os.environ.get("POLYMARKET_BUILDER_SECRET", "").strip()
POLYMARKET_BUILDER_PASSPHRASE = os.environ.get("POLYMARKET_BUILDER_PASSPHRASE", "").strip()
# Builder code (0x… hex from the builder dashboard): stamped on every order the
# bot places, attributing routed volume to the owner and carrying whatever
# builder fee rates are configured on Polymarket's side. This is the API-native
# monetization — website ?r= referral links cannot attach to API-created
# wallets (verified: no referral parameter exists anywhere in the SDK/relayer
# wallet-creation path).
POLYMARKET_BUILDER_CODE = os.environ.get("POLYMARKET_BUILDER_CODE", "").strip()

# --- Security ---
# Encrypts signer private keys at rest (AES-256-GCM). Must be set in production.
ENCRYPTION_SECRET = os.environ.get("ENCRYPTION_SECRET", "")

# --- Telegram Mini App ---
# Bot token from @BotFather. Enables Telegram login (signed initData) and
# account linking, so a Telegram user can never lose access to their wallet by
# clearing storage. Without it, the app still works as a plain web app.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# Wallet-creation abuse guard: "N/seconds" per client IP. Creating a wallet
# calls Polymarket's shared relayer (deploy + approvals), which rate-limits by
# builder key — one abusive IP must not exhaust it for everyone.
CREATE_WALLET_RATE_LIMIT = os.environ.get("CREATE_WALLET_RATE_LIMIT", "3/3600")
