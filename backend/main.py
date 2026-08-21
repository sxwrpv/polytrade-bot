"""FastAPI entrypoint — lifespan starts the DB + CopyEngine, mounts the API and SPA.

Env toggles (useful for tests/dev):
  SEED_ON_START=0              skip the startup leaderboard seed (no network on boot)
  COPY_ENGINE_AUTOSTART=0      don't start the background copy engine
  STATS_REFRESH_AUTOSTART=0    don't start the windowed wallet-screener stats refresh loop
  TRADER_STATS_REFRESH_SECONDS interval between refresh passes (default 900 = 15min)
  TRADER_STATS_REFRESH_LIMIT   how many cached traders to refresh per pass (default 200)
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.config import CORS_ALLOW_ORIGINS, DB_PATH, ENCRYPTION_SECRET, TELEGRAM_BOT_TOKEN
from backend.core import auth, equity, runtime_security, telemetry, trader_stats, wallet
from backend.core.copy_engine import CopyEngine
from backend.core.polymarket import PolymarketClient
from backend.core.telegram_alerts import TelegramPositionNotifier
from backend.db.database import Database
from backend.api import (
    routes_auth, routes_positions, routes_public_screener, routes_telemetry,
    routes_traders, routes_user,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

# httpx logs a line per request at INFO. The engine polls Polymarket constantly,
# so this alone was ~86% of the log volume and grew server.log to 1.5 GB with no
# rotation. Warnings and errors still come through; set HTTP_LOG_LEVEL=INFO to
# get the per-request trace back when debugging.
for _noisy in ("httpx", "httpcore", "urllib3", "web3", "websockets"):
    logging.getLogger(_noisy).setLevel(
        os.environ.get("HTTP_LOG_LEVEL", "WARNING").upper())

log = logging.getLogger("main")
_FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
_DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
_DOCS_INDEX = os.path.join(_DOCS_DIR, "site", "index.html")
_DOCS_PAGES = {
    "README.md",
    "getting-started.md",
    "core-concepts.md",
    "copy-trading.md",
    "wallet-and-funding.md",
    "risk-and-security.md",
    "api-reference.md",
    "configuration.md",
    "deployment.md",
    "troubleshooting.md",
    "glossary.md",
    "developers.md",
    "operators.md",
    "links.md",
}
_DOCS_SLUGS = {
    "overview",
    # Diagram collection. Its content is an HTML fragment under docs/site (the
    # explicitly-public asset directory), not an allowlisted Markdown file, so
    # it needs a slug here but no entry in _DOCS_PAGES.
    "system-design",
    *(name.removesuffix(".md") for name in _DOCS_PAGES if name != "README.md"),
}
# Russian translations sit beside the English files. Only pages that actually
# have one are served; the docs site falls back to English for the rest.
_DOCS_PAGES |= {
    name.removesuffix(".md") + ".ru.md"
    for name in _DOCS_PAGES
    if os.path.isfile(os.path.join(_DOCS_DIR, name.removesuffix(".md") + ".ru.md"))
}


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


async def _telemetry_retention_loop(db, stop: asyncio.Event) -> None:
    """Enforce the 90-day privacy window throughout long-running deployments."""
    interval = 60 * 60
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            pruned = await telemetry.prune_product_events(db, retention_days=90)
            if pruned:
                log.info("product telemetry: pruned %d expired event(s)", pruned)
        except Exception:
            log.exception("product telemetry retention prune failed (continuing)")


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
    # Before anything opens a file: make new DB/WAL/log files owner-only, then
    # tighten whatever already exists. .env holds ENCRYPTION_SECRET (which
    # decrypts every user's wallet key), the database password, and the builder
    # credentials — it must never be group/world readable.
    runtime_security.secure_process_umask()
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        runtime_security.harden_runtime_files(_root, db_path=DB_PATH)
    except OSError:
        log.exception("could not tighten runtime file permissions")
    try:
        trimmed = runtime_security.trim_oversized_logs(_root)
        if trimmed:
            log.warning("trimmed oversized log file(s): %s", ", ".join(trimmed))
    except OSError:
        log.exception("could not trim oversized logs")

    db = Database()
    await db.connect()
    await db.init()
    # Fail closed before accepting requests: a restarted deployment must not
    # serve while expired privacy telemetry is still present. The hourly loop
    # below bounds cleanup drift during normal uptime.
    await telemetry.prune_product_events(db, retention_days=90)
    # One-way cutover to hashed, expiring sessions: any surviving plaintext or
    # non-expiring token is a credential that a DB leak could replay, so it is
    # destroyed rather than migrated. Everyone signs in again — Telegram users
    # re-auth automatically from initData on next launch.
    n = await auth.invalidate_legacy_sessions(db)
    if n:
        log.info("invalidated %d legacy (plaintext/non-expiring) sessions", n)
    pm = PolymarketClient()
    app.state.db = db
    app.state.pm = pm
    app.state.clients = {}            # user_id -> cached CLOB client
    app.state.copy_risk_lock = asyncio.Lock()  # shared by BUYs and settings writes
    app.state.position_notifier = TelegramPositionNotifier(db, TELEGRAM_BOT_TOKEN)

    if os.environ.get("SEED_ON_START", "1") == "1":
        try:
            n = await trader_stats.seed_from_leaderboard(db, pm, limit=25)
            log.info("seeded %d traders", n)
        except Exception:
            log.exception("leaderboard seed failed (continuing)")

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(_telemetry_retention_loop(db, stop)))
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
        engine = CopyEngine(db, pm, detector=detector,
                            risk_lock=app.state.copy_risk_lock,
                            position_notifier=app.state.position_notifier)
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
        await app.state.position_notifier.aclose()
        await pm.aclose()
        await db.close()


app = FastAPI(
    title="PolyTrade API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
# The SPA is served same-origin by this app, so cross-origin access stays OFF
# unless explicitly configured (CORS_ALLOW_ORIGINS, e.g. a Vite dev server).
# Never wildcard-with-credentials: that reflected any Origin back as allowed.
if CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=list(CORS_ALLOW_ORIGINS),
        allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )
app.include_router(routes_auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(routes_user.router, prefix="/api/user", tags=["user"])
app.include_router(routes_traders.router, prefix="/api/traders", tags=["traders"])
app.include_router(routes_positions.router, prefix="/api/positions", tags=["positions"])
app.include_router(routes_telemetry.router, prefix="/api/telemetry", tags=["telemetry"])
# Anonymous, read-only, rate-limited wallet research for the standalone
# screener. Separate from /api/traders/* precisely so that router's session
# gate and its upstream-spending routes stay exactly as they are.
app.include_router(routes_public_screener.router, prefix="/api/public/screener",
                   tags=["public-screener"])


@app.get("/api/docs", include_in_schema=False)
async def api_documentation():
    """Serve FastAPI's live schema in a PolyTrade-branded Swagger shell."""
    response = get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="PolyTrade API — Developer Reference",
        swagger_ui_parameters={
            "deepLinking": True,
            "displayRequestDuration": True,
            "docExpansion": "list",
            "filter": True,
            "operationsSorter": "method",
            "persistAuthorization": True,
            "syntaxHighlight.theme": "arta",
            "tagsSorter": "alpha",
            "tryItOutEnabled": True,
        },
    )
    html = bytes(response.body).decode("utf-8").replace(
        "</head>",
        '<meta name="theme-color" content="#eef2ef">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">\n'
        '<link rel="stylesheet" href="/docs/assets/styles.css">\n'
        '<link rel="stylesheet" href="/docs/assets/api-docs.css">\n'
        "</head>",
    ).replace(
        '<body>\n    <div id="swagger-ui">',
        '<body>\n'
        '<header class="topbar api-topbar">\n'
        '  <a class="brand" href="/docs"><img class="brand-logo" src="/docs/assets/polytrade-mark.png" alt=""><span>PolyTrade</span><span class="brand-divider"></span><span class="brand-docs">API Reference</span></a>\n'
        '  <div class="top-actions"><nav class="header-links" aria-label="Header links"><a href="https://t.me/cpolytrade_bot">Open Telegram bot</a><a href="/docs/developers">Developers</a><a href="/api/openapi.json">OpenAPI JSON</a><a href="https://github.com/sxwrpv/polytrade-bot">GitHub</a></nav>'
        '  <button class="theme-toggle" id="api-theme-toggle" aria-label="Toggle color theme"><svg class="sun" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v2m0 14v2M3 12h2m14 0h2M5.64 5.64l1.42 1.42m9.88 9.88 1.42 1.42m0-12.72-1.42 1.42M7.06 16.94l-1.42 1.42M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z"/></svg><svg class="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 15.5A8.5 8.5 0 0 1 8.5 4 8.5 8.5 0 1 0 20 15.5Z"/></svg></button></div>\n'
        '</header>\n'
        '<main class="api-reference">\n'
        '  <section class="api-intro"><div class="api-eyebrow">Developers</div><h1>API Reference</h1><p>Explore PolyTrade’s live OpenAPI schema and test requests directly from your browser.</p><p class="api-risk-note">PolyTrade operates custodial wallets for real-money prediction-market trading. Integrations should treat wallet actions as financially consequential and preserve explicit user consent.</p><div class="api-quicklinks"><a href="/docs/api-reference">Read the integration guide</a><span>·</span><a href="/api/openapi.json">Download OpenAPI JSON</a></div></section>\n'
        '  <div id="swagger-ui">',
    ).replace(
        "</body>",
        '<footer class="api-footer"><span><a href="/docs/developers">Developer documentation</a> · <a href="/docs/links">Official links</a></span><span>Live schema · OpenAPI 3.1</span></footer></main>'
        '<script src="/docs/assets/api-docs.js"></script></body>',
    )
    return HTMLResponse(html)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Product documentation. Only allowlisted Markdown and dedicated site assets
# are public; adding an internal file under docs/ cannot expose it by accident.
if os.path.isfile(_DOCS_INDEX):
    app.mount(
        "/docs/assets", StaticFiles(directory=os.path.join(_DOCS_DIR, "site")),
        name="docs-assets")

    @app.get("/docs/content/{filename}", include_in_schema=False)
    async def documentation_content(filename: str):
        if filename not in _DOCS_PAGES:
            raise HTTPException(status_code=404, detail="Documentation page not found")
        return FileResponse(os.path.join(_DOCS_DIR, filename), media_type="text/markdown")

    @app.get("/docs", include_in_schema=False)
    @app.get("/docs/", include_in_schema=False)
    @app.get("/docs/{page}", include_in_schema=False)
    async def documentation(page: str | None = None):
        if page is not None and page not in _DOCS_SLUGS:
            raise HTTPException(status_code=404, detail="Documentation page not found")
        return FileResponse(_DOCS_INDEX)


# Standalone Wallet Screener. Its own Vite entry, served here same-origin so
# no cross-site cookie or CORS relaxation is needed today; the same built page
# can later be published at screener.polytradebot.live unchanged (see
# docs/deployment.md). Declared before the SPA mount so "/screener" resolves to
# the screener page rather than falling through to the app's index.html.
_SCREENER_PAGE = os.path.join(_FRONTEND_DIST, "screener.html")
if os.path.isfile(_SCREENER_PAGE):

    @app.get("/screener", include_in_schema=False)
    @app.get("/screener/", include_in_schema=False)
    async def wallet_screener():
        return FileResponse(_SCREENER_PAGE)


# SPA — mount last so it doesn't shadow /api, /docs or /screener.
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="spa")
