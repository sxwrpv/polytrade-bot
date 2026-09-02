"""/api/user/* — wallet onboarding, profile, PnL, settings, key export."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import time

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.config import CREATE_WALLET_RATE_LIMIT, ENCRYPTION_SECRET, TELEGRAM_BOT_TOKEN
from backend.core.client_identity import client_identity
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
    """Real client IP for rate limiting — see backend.core.client_identity.

    This used to trust X-Forwarded-For only from loopback, which was right for
    the retired tunnel and wrong for the Docker/Caddy topology that replaced
    it: Caddy has a 172.x address, so the branch never fired and every caller
    shared one bucket.
    """
    return client_identity(request)

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

# Consent is deliberately versioned. Changing material onboarding terms
# requires a new value and a matching frontend acknowledgement.
CURRENT_TERMS_VERSION = "2026-08-14"
CURRENT_FUNDING_ACK_VERSION = "2026-08-14"
WALLET_CLAIM_STALE_SECONDS = 300
WALLET_LEASE_SECONDS = 300


class CreateWallet(BaseModel):
    display_name: str | None = None
    init_data: str | None = None      # required signed Telegram initData
    terms_accepted: bool = False
    terms_version: str | None = None


class FundingAcknowledgement(BaseModel):
    accepted: bool = False
    version: str | None = None


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
async def create_wallet(body: CreateWallet, request: Request, response: Response,
                        db=Depends(get_db)):
    """Generate a signer and build its client, deriving and deploying the
    gasless Deposit Wallet when a Builder key is configured (or falling back
    to an EOA). Wallet setup attempts backend-readiness checks and trading
    approvals, but readiness and approvals may still be incomplete or
    completing when this response returns; do not assume the wallet can be
    funded or traded immediately. In Telegram, the account is linked to the
    Telegram user. If that user already has a wallet, return the existing
    session instead of minting an orphan."""
    # A valid legacy session identifies an existing wallet even though this
    # endpoint does not require authentication. Never let its owner accidentally
    # mint a second wallet. Missing, invalid, and expired cookies are ignored so
    # normal first-time Telegram onboarding remains available.
    raw_cookie = getattr(request, "cookies", {}).get(auth.SESSION_COOKIE)
    if raw_cookie:
        session_user = await db.fetchone(
            "SELECT id,telegram_user_id,api_token_expires_at FROM users WHERE api_token=?",
            (auth.hash_session_token(raw_cookie),),
        )
        if (session_user and
                auth.parse_session_expiry(session_user.get("api_token_expires_at")) > time.time() and
                session_user.get("telegram_user_id") is None):
            raise HTTPException(
                409, "this session already owns a legacy wallet; use /api/auth/link-telegram instead")

    # Identity and consent gates run before rate-limit accounting, signer
    # generation, relayer calls, or any other irreversible wallet work.
    if not body.init_data:
        raise HTTPException(401, "wallet creation requires verified Telegram init data")
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(503, "Telegram wallet creation is not configured")
    tg_user = auth.validate_init_data(body.init_data, TELEGRAM_BOT_TOKEN)
    if not tg_user:
        raise HTTPException(401, "invalid or expired Telegram init data")
    if not body.terms_accepted or body.terms_version != CURRENT_TERMS_VERSION:
        raise HTTPException(
            400, f"current terms must be accepted (terms_version={CURRENT_TERMS_VERSION})")
    if not ENCRYPTION_SECRET:
        raise HTTPException(500, "ENCRYPTION_SECRET not configured")

    telegram_user_id = int(tg_user["id"])

    async def restore_existing_wallet(existing_user: dict) -> dict:
        """Record current consent and restore the existing wallet session."""
        await db.execute(
            "INSERT INTO user_consents(user_id,terms_version,telegram_user_id,accepted_at) "
            "VALUES(?,?,?,?) ON CONFLICT(user_id,terms_version) DO NOTHING",
            (existing_user["id"], CURRENT_TERMS_VERSION, telegram_user_id, now_iso()))
        raw = await auth.issue_session(db, existing_user["id"])
        auth.set_session_cookie(response, raw)
        return {
            "address": existing_user["id"],
            "signer_address": existing_user["signer_address"],
            "gasless": existing_user["id"] != existing_user["signer_address"],
            "created": False,
        }

    existing = await db.fetchone(
        "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,))
    if existing:
        return await restore_existing_wallet(existing)

    ip = _client_ip(request)
    if _create_rate_limited(ip):
        raise HTTPException(429, "too many wallets created from this address — try again later")

    # Acquire durable cross-process ownership. A prepared signer is resumable
    # only after its prior SDK call returned a caught failure and explicitly
    # released ownership. Expiry is informational and never permits takeover.
    claim_token = secrets.token_urlsafe(32)  # opaque lease owner, never a credential
    stale_before = (dt.datetime.now(dt.timezone.utc) -
                    dt.timedelta(seconds=WALLET_CLAIM_STALE_SECONDS)).isoformat()
    lease_expires_at = (dt.datetime.now(dt.timezone.utc) +
                        dt.timedelta(seconds=WALLET_LEASE_SECONDS)).isoformat()
    claim = await db.acquire_wallet_creation_lease(
        telegram_user_id, claim_token, stale_before=stale_before,
        lease_expires_at=lease_expires_at)
    if not claim:
        # A legacy-link transaction may have committed while the first owner
        # lookup still saw no binding. Return that durable owner and do no work.
        existing = await db.fetchone(
            "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,))
        if existing:
            return await restore_existing_wallet(existing)
        raise HTTPException(409, "wallet creation or reconciliation is already in progress")

    try:
        if claim["state"] == "claimed":
            # Persist the sole signer before entering the opaque SDK call.
            kp = wallet.create_signer()
            signer, pk = kp["address"], kp["private_key"]
            enc = wallet.encrypt_private_key(pk, ENCRYPTION_SECRET)
            claim = await db.prepare_wallet_creation_signer(
                telegram_user_id, claim_token, signer, enc)
            if not claim:
                raise RuntimeError("durable signer preparation lost its lease")
        else:
            signer = claim["signer_address"]
            enc = claim["private_key_enc"]
            pk = wallet.decrypt_private_key(enc, ENCRYPTION_SECRET)
            if wallet.address_for_key(pk).lower() != signer.lower():
                raise RuntimeError("persisted wallet signer does not match ciphertext")

        # Session material is retry-local; only its digest is persisted.
        raw, stored, expires_at = auth.new_session()
        display_name = body.display_name or tg_user.get("username") or tg_user.get("first_name")
        created_at = now_iso()
    except Exception as exc:
        log.error("local wallet preparation failed (%s)", type(exc).__name__)
        pre_signer_released = await db.abandon_wallet_preparation(
            telegram_user_id, claim_token)
        if pre_signer_released:
            raise HTTPException(503, "wallet preparation could not continue safely; retry is safe")
        raise HTTPException(
            503, "wallet preparation could not continue safely; reconciliation may be required")

    async def retain_failure(stage: str, exc: Exception) -> None:
        safe_error = f"{stage}: {type(exc).__name__}"
        try:
            recorded = await db.record_wallet_creation_error(
                telegram_user_id, claim_token, safe_error)
            if not recorded:
                log.error("retained wallet creation fence could not be found for annotation")
        except Exception as record_exc:
            log.error("could not annotate retained wallet creation fence (%s)",
                      type(record_exc).__name__)

    async def finish_failed_attempt(stage: str, exc: Exception) -> None:
        await retain_failure(stage, exc)
        released = await db.release_wallet_creation_after_sdk_failure(
            telegram_user_id, claim_token)
        if not released:
            log.error("wallet creation owner could not be explicitly released")

    async def close_client_safely(client, context: str) -> None:
        """Finish client cleanup without leaving a shield-created task detached."""
        close_task = asyncio.create_task(client.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            close_task.cancel()
            try:
                await close_task
            except BaseException:
                pass
            raise
        except Exception as close_exc:
            log.error("wallet client close failed %s (%s)", context,
                      type(close_exc).__name__)

    try:
        client = await wallet.make_clob_client(pk)
    except asyncio.CancelledError:
        # The opaque call may continue beyond request cancellation. Retain the
        # owner so another process cannot overlap work with an unknown outcome.
        raise
    except Exception as exc:
        log.error("wallet client creation failed after external boundary (%s)",
                  type(exc).__name__)
        await finish_failed_attempt("make_clob_client", exc)
        raise HTTPException(
            503, "an external wallet operation may have started; retry will resume the same wallet identity")
    if client is None:
        exc = RuntimeError("wallet client creation returned no client")
        await finish_failed_attempt("make_clob_client", exc)
        raise HTTPException(
            503, "an external wallet operation may have started; retry will resume the same wallet identity")

    try:
        funder = client.wallet
    except Exception as exc:
        log.error("wallet client returned no usable wallet after external boundary (%s)",
                  type(exc).__name__)
        await close_client_safely(client, "after unusable wallet response")
        await finish_failed_attempt("read_funder", exc)
        raise HTTPException(
            503, "an external wallet operation may have started; retry will resume the same wallet identity")
    try:
        try:
            await wallet.wait_wallet_ready(client)
            await wallet.ensure_allowances(client)
        except Exception as setup_exc:
            log.error("wallet setup (readiness/approvals) failed for %s (%s) — "
                      "wallet exists but may need funding/approval retried later",
                      funder, type(setup_exc).__name__)
    finally:
        await close_client_safely(client, f"for {funder}")

    try:
        # User, consent, and claim completion are one transaction. The temporary
        # claim ciphertext is cleared only after the users row owns its copy.
        async with db.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO users(id, signer_address, api_token, api_token_expires_at, "
                "telegram_user_id, display_name, private_key_enc, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (funder, signer, stored, expires_at, telegram_user_id,
                 display_name, enc, created_at))
            await tx.execute(
                "INSERT INTO user_consents(user_id,terms_version,telegram_user_id,accepted_at) "
                "VALUES(?,?,?,?)",
                (funder, CURRENT_TERMS_VERSION, telegram_user_id, created_at))
            completed = await tx.execute(
                "UPDATE wallet_creation_claims SET state='complete',updated_at=?,"
                "private_key_enc=NULL,lease_owner=NULL,lease_expires_at=NULL,last_error=NULL "
                "WHERE telegram_user_id=? AND lease_owner=? AND state='side_effect_started'",
                (created_at, telegram_user_id, claim_token))
            if completed != 1:
                raise aiosqlite.IntegrityError("wallet creation claim completion failed")
    except Exception as exc:
        log.error("wallet persistence failed after external boundary (%s)",
                  type(exc).__name__)
        # The SDK work completed but its durable result is unknown. This is not
        # a returned SDK failure, so retain ownership for operator reconciliation.
        await retain_failure("persist_wallet", exc)
        raise HTTPException(
            503, "wallet setup may have completed externally but could not be persisted; "
                 "operator reconciliation is required")
    auth.set_session_cookie(response, raw)
    return {"address": funder, "signer_address": signer,
            "gasless": funder != signer, "created": True}


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
            "gasless": user["id"] != user["signer_address"],
            "telegram_linked": user.get("telegram_user_id") is not None}


@router.post("/funding-acknowledgement")
async def acknowledge_funding(body: FundingAcknowledgement,
                              user=Depends(get_current_user), db=Depends(get_db)):
    """Persist the exact funding-risk disclosure version before address access."""
    if not body.accepted or body.version != CURRENT_FUNDING_ACK_VERSION:
        raise HTTPException(
            400, f"current funding disclosure must be accepted "
                 f"(version={CURRENT_FUNDING_ACK_VERSION})")
    await db.execute(
        "INSERT INTO funding_acknowledgements(user_id,version,accepted_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id,version) DO NOTHING",
        (user["id"], CURRENT_FUNDING_ACK_VERSION, now_iso()),
    )
    return {"accepted": True, "version": CURRENT_FUNDING_ACK_VERSION}


@router.get("/deposit-address")
async def deposit_address(user=Depends(get_current_user), db=Depends(get_db),
                          pmc=Depends(get_pm)):
    """Bridge deposit addresses so the user can fund their wallet from any
    supported chain in USDC/USDT/etc — arrives as pUSD automatically. This is
    Polymarket's own bridge, not something we run; see BUILD_PLAN §wallet model
    for why the one-time allowance approval (separate from funding) still
    needs a little MATIC on this EOA wallet model."""
    accepted = await db.fetchone(
        "SELECT accepted_at FROM funding_acknowledgements WHERE user_id=? AND version=?",
        (user["id"], CURRENT_FUNDING_ACK_VERSION),
    )
    if not accepted:
        raise HTTPException(403, "accept the current funding disclosure before revealing addresses")
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


class ExportKeyBody(BaseModel):
    # Fresh Telegram initData proving the caller controls the linked account.
    init_data: str


# Exporting the signer key hands over irreversible control of the wallet, so it
# is the one action a stolen session token must NOT be able to perform on its
# own. The session proves "some client holds the token"; this proves "the human
# who owns the linked Telegram account is here right now".
EXPORT_STEP_UP_MAX_AGE = 300     # seconds — initData must be minutes old, not a day


@router.post("/export-key")
async def export_key(body: ExportKeyBody, user=Depends(get_current_user)):
    """Reveal the signer private key, gated by a fresh Telegram step-up.

    Requires initData that (a) carries Telegram's valid HMAC, (b) is at most
    EXPORT_STEP_UP_MAX_AGE old, and (c) belongs to the SAME Telegram account
    linked to this wallet. A leaked session token alone can no longer drain a
    user, and a replayed old initData is rejected by the freshness window.
    """
    linked = user.get("telegram_user_id")
    if not linked or not TELEGRAM_BOT_TOKEN:
        # No second factor available: refuse rather than silently falling back
        # to session-only auth on the most dangerous endpoint in the app.
        raise HTTPException(
            403, "key export requires a linked Telegram account — open the app "
                 "from the Telegram bot and try again")
    tg_user = auth.validate_init_data(
        body.init_data, TELEGRAM_BOT_TOKEN, max_age=EXPORT_STEP_UP_MAX_AGE)
    if not tg_user or int(tg_user.get("id", 0)) != int(linked):
        log.warning("export-key step-up REJECTED for %s", str(user.get("id"))[:10])
        raise HTTPException(403, "telegram verification failed or expired — reopen "
                                 "the app from Telegram and retry")
    log.info("export-key step-up ok for %s", str(user.get("id"))[:10])
    pk = wallet.decrypt_private_key(user["private_key_enc"], ENCRYPTION_SECRET)
    return {"private_key": pk}
