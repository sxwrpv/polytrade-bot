"""FastAPI entrypoint — lifespan starts the DB + CopyEngine, mounts the API and SPA.

Env toggles (useful for tests/dev):
  SEED_ON_START=0              skip the startup leaderboard seed (no network on boot)
  COPY_ENGINE_AUTOSTART=0      don't start the background copy engine
  STATS_REFRESH_AUTOSTART=0    don't start the windowed wallet-screener stats refresh loop
  TRADER_STATS_REFRESH_SECONDS interval between refresh passes (default 900 = 15min)
  TRADER_STATS_REFRESH_LIMIT   how many cached traders to refresh per pass (default 100)
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import ENCRYPTION_SECRET
from backend.core import auth, equity, trader_stats, wallet
from backend.core.copy_engine import CopyEngine
from backend.core.polymarket import PolymarketClient
from backend.db.database import Database
from backend.api import routes_auth, routes_positions, routes_traders, routes_user

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

log = logging.getLogger("main")
_FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")


async def _stats_refresh_loop(db, pm, stop: asyncio.Event) -> None:
    """Background loop for the wallet screener. Each pass: (1) crawl the public
    leaderboard feeds to discover the active-trader population (cheap,
    ~target/50 calls — keeps the screener covering ALL active wallets, not one
    top-25 page), then (2) recompute the windowed (7d/30d/90d) stats for the
    stalest batch, rotating through the whole cache over successive passes.
    The screener endpoint only ever reads precomputed columns, so filters stay
    instant regardless of population size. Runs once immediately on boot
    (best-effort — early minutes may show wallets whose windowed stats haven't
    been computed yet), then on the configured interval."""
    interval = float(os.environ.get("TRADER_STATS_REFRESH_SECONDS", "900"))
    limit = int(os.environ.get("TRADER_STATS_REFRESH_LIMIT", "200"))
    # 4 by default — 8 drew data-api 429s in production; the copy engine
    # shares those hosts, so the crawler must stay under the radar.
    concurrency = int(os.environ.get("TRADER_STATS_REFRESH_CONCURRENCY", "4"))
    target = int(os.environ.get("DISCOVER_WALLETS_TARGET", "2000"))
    while not stop.is_set():
        try:
            found = await trader_stats.discover_active_wallets(db, pm, target=target)
            log.info("wallet screener: discovery pass saw %d active wallets", found)
        except Exception:
            log.exception("wallet discovery pass failed (continuing)")
        try:
            n = await trader_stats.refresh_all(db, pm, limit=limit, concurrency=concurrency)
            log.info("wallet screener: refreshed windowed stats for %d traders", n)
        except Exception:
            log.exception("wallet screener stats refresh pass failed (continuing)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _equity_snapshot_loop(app, stop: asyncio.Event) -> None:
    """Snapshot every user's equity on a fixed cadence (default 5 min) so the
    Performance chart has a dense, market-moving time series. Reuses the API's
    per-user CLOB client cache (app.state.clients) so it doesn't rebuild creds.
    Runs once on boot so a fresh chart has a first point quickly."""
    interval = float(os.environ.get("EQUITY_SNAPSHOT_SECONDS", "300"))
    db, pm = app.state.db, app.state.pm

    async def client_for(user):
        cache = app.state.clients
        cid = user["id"]
        if cid not in cache:
            pk = wallet.decrypt_private_key(user["private_key_enc"], ENCRYPTION_SECRET)
            cache[cid] = await wallet.make_clob_client(pk, funder=cid)
        return cache[cid]

    while not stop.is_set():
        try:
            n = await equity.snapshot_all(db, pm, client_for)
            log.info("equity snapshot: recorded %d users", n)
        except Exception:
            log.exception("equity snapshot pass failed (continuing)")
        try:
            # thin old snapshots to the resolution the charts render (keeps
            # storage bounded; never changes a chart)
            pruned = await equity.prune_snapshots(db)
            if pruned:
                log.info("equity snapshot: pruned %d redundant rows", pruned)
        except Exception:
            log.exception("equity snapshot prune failed (continuing)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database()
    await db.connect()
    await db.init()
    n = await auth.ensure_api_tokens(db)   # rows that predate token auth
    if n:
        log.info("backfilled session tokens for %d users", n)
    pm = PolymarketClient()
    app.state.db = db
    app.state.pm = pm
    app.state.clients = {}            # user_id -> cached CLOB client

    if os.environ.get("SEED_ON_START", "1") == "1":
        try:
            n = await trader_stats.seed_from_leaderboard(db, pm, limit=25)
            log.info("seeded %d traders", n)
        except Exception:
            log.exception("leaderboard seed failed (continuing)")

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    if os.environ.get("COPY_ENGINE_AUTOSTART", "1") == "1":
        from backend.config import POLYGON_RPC_URL
        from backend.core import detection
        detector = None
        if POLYGON_RPC_URL:
            try:
                detector = detection.OnChainDetector(POLYGON_RPC_URL)
                log.info("using on-chain OrderFilled detector")
            except Exception:
                log.exception("on-chain detector init failed; falling back to activity poll")
        engine = CopyEngine(db, pm, detector=detector)   # None -> ActivityPollDetector
        app.state.engine = engine
        tasks.append(asyncio.create_task(engine.run(stop)))

    if os.environ.get("STATS_REFRESH_AUTOSTART", "1") == "1":
        tasks.append(asyncio.create_task(_stats_refresh_loop(db, pm, stop)))

    if os.environ.get("EQUITY_SNAPSHOT_AUTOSTART", "1") == "1":
        tasks.append(asyncio.create_task(_equity_snapshot_loop(app, stop)))

    try:
        yield
    finally:
        stop.set()
        for task in tasks:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        # close cached per-user CLOB clients (API cache + engine cache)
        for client in app.state.clients.values():
            try:
                await client.close()
            except Exception:
                pass
        engine = getattr(app.state, "engine", None)
        if engine is not None:
            await engine.aclose()
        await pm.aclose()
        await db.close()


app = FastAPI(title="polymarket-copybot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(routes_auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(routes_user.router, prefix="/api/user", tags=["user"])
app.include_router(routes_traders.router, prefix="/api/traders", tags=["traders"])
app.include_router(routes_positions.router, prefix="/api/positions", tags=["positions"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def public_config():
    """Non-secret config the frontend needs: the bot's username builds the
    t.me referral/share deep link (empty = share the web URL instead)."""
    return {"telegram_bot_username": os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()}


# SPA (built in phase 10) — mount last so it doesn't shadow /api.
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="spa")
