"""/api/user/* — wallet onboarding, profile, PnL, settings, key export."""
from __future__ import annotations

import datetime as dt
import logging
import time

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.config import CREATE_WALLET_RATE_LIMIT, ENCRYPTION_SECRET, TELEGRAM_BOT_TOKEN
from backend.core import auth, equity as equity_mod, pnl as pnl_mod, wallet
from backend.api.deps import get_current_user, get_db, get_pm, get_user_client
from backend.db.database import now_iso

log = logging.getLogger("routes_user")

# Wallet creation hits Polymarket's shared relayer (deploy + approvals), which
# rate-limits by builder key — one abusive IP must not exhaust it for every
# user. In-memory per-IP sliding window; sufficient for a single process.
_create_hits: dict[str, list[float]] = {}


def _create_rate_limited(ip: str) -> bool:
    limit_s, _, window_s = CREATE_WALLET_RATE_LIMIT.partition("/")
    limit, window = int(limit_s), float(window_s or 3600)
    now = time.time()
    hits = [t for t in _create_hits.get(ip, []) if now - t < window]
    limited = len(hits) >= limit
    if not limited:
        hits.append(now)
    _create_hits[ip] = hits
    return limited


def _client_ip(request: Request) -> str:
    """Real client IP for rate limiting. Behind the tunnel (Tailscale Funnel /
    localhost.run) every request reaches uvicorn from loopback, so keying on
    request.client.host put ALL users in one shared bucket; the tunnel's
    X-Forwarded-For carries the real address. Only trusted from loopback —
    a direct remote caller can't spoof its way into someone else's bucket."""
    host = request.client.host if request.client else "unknown"
    if host in ("127.0.0.1", "::1"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return host

# Bridge response keys verified live against bridge.polymarket.com/deposit
# (2026-07-01) — one address per chain family; whatever arrives is converted
# to pUSD at the destination wallet by Polymarket's own Collateral Onramp.
_DEPOSIT_CHAIN_LABELS = {
    "evm": "ETHEREUM / POLYGON / ARBITRUM / BASE / OPTIMISM / BNB (USDC or USDT)",
    "svm": "SOLANA (USDC or USDT)",
    "btc": "BITCOIN (BTC)",
    "tron": "TRON (USDT)",
}

router = APIRouter()


class CreateWallet(BaseModel):
    display_name: str | None = None
    init_data: str | None = None      # Telegram initData — links the account


class SettingsBody(BaseModel):
    # Only settings the engine actually reads: display_name (UI), paused (the
    # account-level kill switch), max_total_exposure_usd (account-wide cap
    # across every copied wallet). Slippage and daily-loss limits are PER
    # COPIED WALLET (followed_traders) — the user-level twins were dead
    # columns the engine never consulted, removed 2026-07-12.
    display_name: str | None = Field(None, max_length=80)
    paused: bool | None = None
    max_total_exposure_usd: float | None = Field(None, ge=0, le=100000)


_SETTINGS_KEYS = ("display_name", "paused", "max_total_exposure_usd")


@router.post("/create-wallet")
async def create_wallet(body: CreateWallet, request: Request, db=Depends(get_db)):
    """Generate a signer, build its client (derives + deploys the gasless
    Deposit Wallet automatically when a Builder key is configured; falls back
    to EOA otherwise), wait for backend indexing, then set up trading
    approvals — all before the user ever sees the wallet, so it's ready to
    fund and trade immediately. Launched inside Telegram, the account is also
    linked to the Telegram user — and if that Telegram user already has a
    wallet, their existing session is returned instead of minting an orphan."""
    if not ENCRYPTION_SECRET:
        raise HTTPException(500, "ENCRYPTION_SECRET not configured")

    tg_user = None
    if body.init_data and TELEGRAM_BOT_TOKEN:
        tg_user = auth.validate_init_data(body.init_data, TELEGRAM_BOT_TOKEN)
        if tg_user:
            existing = await db.fetchone(
                "SELECT * FROM users WHERE telegram_user_id = ?", (int(tg_user["id"]),))
            if existing:
                return {"address": existing["id"], "signer_address": existing["signer_address"],
                        "api_token": existing["api_token"],
                        "gasless": existing["id"] != existing["signer_address"]}

    ip = _client_ip(request)
    if _create_rate_limited(ip):
        raise HTTPException(429, "too many wallets created from this address — try again later")

    kp = wallet.create_signer()
    signer, pk = kp["address"], kp["private_key"]

    try:
        client = await wallet.make_clob_client(pk)
    except Exception as e:
        log.exception("wallet client creation failed")
        raise HTTPException(503, f"wallet creation temporarily unavailable, try again shortly: {e}")

    funder = client.wallet
    try:
        await wallet.wait_wallet_ready(client)
        await wallet.ensure_allowances(client)
    except Exception:
        log.exception("wallet setup (readiness/approvals) failed for %s — "
                      "wallet exists but may need funding/approval retried later", funder)
    finally:
        await client.close()

    enc = wallet.encrypt_private_key(pk, ENCRYPTION_SECRET)
    token = auth.new_api_token()
    display_name = body.display_name
    if not display_name and tg_user:
        display_name = tg_user.get("username") or tg_user.get("first_name")
    try:
        await db.execute(
            "INSERT INTO users(id, signer_address, api_token, telegram_user_id, "
            "display_name, private_key_enc, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (funder, signer, token, int(tg_user["id"]) if tg_user else None,
             display_name, enc, now_iso()))
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "wallet already exists")
    return {"address": funder, "signer_address": signer,
            "api_token": token, "gasless": funder != signer}


@router.get("/me")
async def me(request: Request, balance: bool = False,
             user=Depends(get_current_user), pmc=Depends(get_pm)):
    """Profile. With ?balance=true it also computes the account's money split:
      balance       = free cash collateral (spendable pUSD)
      positions_val = live market value of open positions
      claimable     = value of resolved-but-unredeemed winnings (redeem on
                      polymarket.com to turn into cash; not auto-claimed)
      equity        = balance + positions_val + claimable (total account value)
    Splitting these is why the single 'balance' looked wrong: money sitting in
    open positions or unclaimed wins was invisible."""
    bal = positions_val = claimable = equity = None
    if balance:   # live reads are expensive (derive creds) — opt-in
        try:
            client = await get_user_client(request, user)
            r = await client.get_balance_allowance(asset_type="COLLATERAL")
            bal = r.balance / 1e6
        except Exception:
            bal = None
        try:
            positions = await pmc.get_positions(user["id"], size_threshold=0)
            positions_val = round(sum(p.current_value for p in positions
                                      if p.size > 0 and not p.redeemable), 2)
            claimable = round(sum(p.current_value for p in positions
                                  if p.size > 0 and p.redeemable), 2)
        except Exception:
            positions_val = claimable = None
        # Equity only when every component was actually read — a failed
        # positions read must show '—', not cash silently presented as the
        # whole account value.
        if bal is not None and positions_val is not None and claimable is not None:
            equity = round(bal + positions_val + claimable, 2)
    return {"address": user["id"], "signer_address": user["signer_address"],
            "display_name": user["display_name"], "balance": bal,
            "positions_value": positions_val, "claimable": claimable, "equity": equity,
            # deposit wallet (gasless) vs EOA fallback — cheap DB-only check,
            # no client build needed: a deposit wallet's funder != its signer.
            "gasless": user["id"] != user["signer_address"]}


@router.get("/deposit-address")
async def deposit_address(user=Depends(get_current_user), pmc=Depends(get_pm)):
    """Bridge deposit addresses so the user can fund their wallet from any
    supported chain in USDC/USDT/etc — arrives as pUSD automatically. This is
    Polymarket's own bridge, not something we run; see BUILD_PLAN §wallet model
    for why the one-time allowance approval (separate from funding) still
    needs a little MATIC on this EOA wallet model."""
    r = await pmc.create_bridge_address(user["id"])
    addresses = r.get("address", {})
    return {
        "addresses": [
            {"chain": chain, "label": _DEPOSIT_CHAIN_LABELS.get(chain, chain.upper()),
             "address": addr}
            for chain, addr in addresses.items()
        ],
    }


ACTIVITY_WINDOW_HOURS = 12


@router.get("/activity")
async def activity(limit: int = 30, user=Depends(get_current_user), db=Depends(get_db)):
    """The engine's recent actions on this account — the 'it's alive' feed.
    Only the last 12h is shown (a live feed, not a full ledger — closed-position
    history and PnL stats cover the long tail). Resolutions ('resolve') are
    excluded: a market resolving isn't an action the bot took; its realized PnL
    still lands in the PnL stats / closed positions."""
    limit = max(1, min(int(limit), 100))
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(hours=ACTIVITY_WINDOW_HOURS)).isoformat()
    return await db.fetchall(
        "SELECT e.ts, e.event_type, e.amount_usd, e.pnl, "
        "p.market_title, p.market_slug, p.outcome, p.trader_address, "
        "p.entry_price, p.exit_price, c.display_name AS trader_name "
        "FROM trade_events e JOIN copy_positions p ON p.id = e.position_id "
        "LEFT JOIN trader_cache c ON c.address = p.trader_address "
        "WHERE e.user_id = ? AND e.event_type != 'resolve' AND e.ts >= ? "
        "ORDER BY e.ts DESC LIMIT ?",
        (user["id"], cutoff, limit))


@router.get("/pnl")
async def pnl(period: str = "30d", user=Depends(get_current_user),
              db=Depends(get_db), pmc=Depends(get_pm)):
    stats = await pnl_mod.get_pnl_stats(user["id"], db, pmc)
    curve = await pnl_mod.get_equity_curve(user["id"], db, period)
    return {**stats, "equity_curve": curve}


@router.get("/equity-series")
async def equity_series(period: str = "7d", user=Depends(get_current_user), db=Depends(get_db)):
    """Downsampled equity/PnL snapshots for the Performance line chart.
    period=7d (5-min points) | 30d (30-min) | all (4-hour)."""
    return await equity_mod.get_series(db, user["id"], period)


@router.get("/pnl/by-wallet")
async def pnl_by_wallet(user=Depends(get_current_user), db=Depends(get_db)):
    """Realized PnL breakdown per copied wallet, with cached display name/tier
    joined in for the User > Performance > breakdown folder."""
    # display_name is already LEFT JOINed inside get_pnl_by_wallet
    return await pnl_mod.get_pnl_by_wallet(user["id"], db)


@router.get("/settings")
async def get_settings(user=Depends(get_current_user)):
    return {k: user[k] for k in _SETTINGS_KEYS}


@router.post("/settings")
async def update_settings(body: SettingsBody, request: Request,
                          user=Depends(get_current_user), db=Depends(get_db)):
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _SETTINGS_KEYS}
    if "paused" in updates:
        updates["paused"] = int(bool(updates["paused"]))
    if updates.get("max_total_exposure_usd") == 0:   # 0 = no limit
        updates["max_total_exposure_usd"] = None
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        lock = getattr(request.app.state, "copy_risk_lock", None)
        async def apply_update():
            async with db.transaction(write=True) as tx:
                user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if db.is_pg else "")
                await tx.fetchone(user_sql, (user["id"],))
                await tx.execute("UPDATE users SET risk_revision=risk_revision+1 WHERE id=?", (user["id"],))
                await tx.execute(f"UPDATE users SET {cols} WHERE id=?", [*updates.values(), user["id"]])
        if lock is None:
            await apply_update()
        else:
            async with lock:
                await apply_update()
    if updates:
        import asyncio
        for _ in range(50):
            pending = await db.fetchval(
                "SELECT COUNT(*) FROM copy_open_claims WHERE user_id=? AND state='submitting'",
                (user["id"],))
            if not pending:
                break
            await asyncio.sleep(0.1)
        else:
            raise HTTPException(503, "pause persisted; an in-flight order needs reconciliation")
    return {"ok": True, "updated": list(updates)}


@router.post("/export-key")
async def export_key(user=Depends(get_current_user)):
    """Reveal the signer private key. Gated by the Bearer session token (a
    secret only this user's client holds) — no passphrase second factor; see
    BUILD_PLAN.md for that deliberate tradeoff."""
    pk = wallet.decrypt_private_key(user["private_key_enc"], ENCRYPTION_SECRET)
    return {"private_key": pk}
