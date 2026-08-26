# Handoff → Hermes: the trader screener

You have no memory of the session that built this. Everything you need is below.
Start with your normal opening checks (`mc board`, `mc mine Hermes`, `mc alerts`,
`mc get kill_switch`) before acting on any of it.

---

## What exists

A working Polymarket trader screener at `~/polycopy-clone`, port 4310, launch
config `polytrade-screener`. Node 20+, no dependencies, no build step.

```bash
cd ~/polycopy-clone && node scripts/ingest.mjs && node server.mjs
node --test tests/*.test.mjs     # 23 pass
```

It ranks wallets by **what survives being copied** — the trader-level question
polytrade's existing screener does not ask (that one ranks by PnL, win rate and
volume only). Two data layers behind one board:

- **the board** reads a cached snapshot of a 3,766-wallet ranked cohort (3,565 with sparklines), pulled
  from polycopy.app's unauthenticated `/api/v2/discover/dataset`. This carries
  the derived signals that need a full-chain indexer we do not have: Copy Score,
  market-maker / arbitrage / frequency classifiers, weekly sparklines, and 235
  per-slice "who actually wins NBA spreads" boards;
- **every wallet page** is computed at request time from Polymarket's own public
  read APIs — no key, no session, read only.

It is already styled in polytrade's design language and shaped to **merge**
into `~/polytrade/frontend/src/screener` rather than be ported: same token
layer as `brutalism.css`, same shell class names, pure-model split matching the
existing `screenerModel.js` convention, and a `/api/public/screener/*` surface
that mirrors `backend/api/routes_public_screener.py` field for field, including
its allowlist projection and per-client rate limit.

Full detail, including the merge checklist, is in `~/polycopy-clone/README.md`.
Read it before dispatching anything.

## What is verified

- With the same snapshot, the board reproduces polycopy's live top ten **row for
  row** — same order, same Copy Score, same ROI / PnL / open value / volume, same
  week-over-week deltas.
- 23 tests cover the board rules, episode reconstruction, the score model and
  the public endpoint (including that its rate limit is per-client and that its
  projection is an allowlist).
- Two Polymarket API limits were found the hard way and are encoded in
  `lib/polymarket.mjs`. They matter for **any** polytrade work touching wallet
  history, not just this surface:
  1. `/positions?closed=true` returns the *identical* rows as open. Redeemed
     positions leave the endpoint entirely, so it cannot find finished trades.
     They must be reconstructed from `/activity`.
  2. `/activity` refuses any offset past 5000 — **5,500 rows is the ceiling on
     public per-wallet history.** A busy wallet's totals are totals over that
     window, not lifetime ones. The wallet page says so in a banner when the
     tape is truncated.

## What is deliberately not done

- **Maker/taker split, Sharpe/Sortino, net deposited, true ROI.** These need
  order-level chain logs the public API does not expose. They render as
  unavailable rather than being estimated from fills. Do not let anyone "fix"
  these by inferring them from the tape.
- **No composite score on the polytrade data source.** When pointed at
  polytrade's own cache, `copyClass`/`copyNet` come back null and the board
  falls back to ordering by money — matching the FastAPI router's deliberate
  choice not to publish a copyability number over partial inputs.
- **Telegram deep linking.** `SUPPORTS_WALLET_DEEP_LINK` is `false` because
  nothing in the polytrade repo reads a `start` payload; emitting one would
  produce a link that silently drops the wallet. A test pins this.

## Constants: what is inherited vs derived

The README table is the authority. The short version: volume floors, staleness
cut, ordering, class taxonomy, display formats and chart geometry are
**VERBATIM** from polycopy's shipped bundle. The class cut-points, spread/fee
split and shrinkage prior are **INFERRED** by fitting their published dataset —
the shrinkage prior in particular rests on a single published example. Both
labels appear in the source and on screen. Reject any result packet that blurs
that distinction.

---

## Open decisions — these are Arsen's, not yours

Prepare decision memos. Do not resolve these by dispatching work.

**1. Do we ship a surface that depends on a third party's endpoint?**
The Copy Score signal, the classifiers and the angle boards all come from
polycopy.app's unauthenticated dataset endpoint. It is the same request their
own browser makes, but it is their computed output, it can change or close
without notice, and this would carry polytrade branding. Options are roughly:
keep the dependency and label the provenance; drop those columns until we can
compute them ourselves; or build the indexer. Each has a very different cost.
This is the blocking question — most other work is wasted until it is answered.

**2. If we keep it, what is the refresh and failure story?**
Right now `scripts/ingest.mjs` is manual and the snapshot is a file. A shipped
version needs a schedule, a staleness signal on the page, and a defined
behaviour when the upstream shape changes.

**3. Does this jump the queue?**
Per your fixed priority order it should not. This is polytrade tooling, not
revenue-blocking G7 work and not live-bot safety. MC-01, MC-02, MC-07 and MC-08
are all still assigned and unstarted. Unless Arsen overrides, file this behind
them and say so in the briefing rather than quietly reordering.

---

## Suggested shape of the work, once decision 1 is answered

Yours to route — this is context, not instruction. Note the two-agent rule is
`on`, so nothing here ships vetted only by its author.

- The **numbers** need an independent check before anyone trusts them: episode
  reconstruction, the Copy Score decomposition, and whether the inferred
  constants hold across more than the one wallet they were fitted on. That is
  Atlas's kind of work, and Atlas is the vet for numbers regardless of who does it.
- The **merge** into `polytrade/frontend/src/screener` is Vanta's: four steps,
  listed at the end of the README, and step 4 is decision 1 above so it cannot
  start until that is resolved.
- **Sentinel must vet** anything that reaches a deploy, and separately should
  look at the ingest dependency itself — an unauthenticated third-party endpoint
  in our build path is a supply-chain question as much as a product one.

Budget each packet. This is a research surface, not revenue, and the ceremony
should not cost more than the work — most of the above is fast-lane sized
except the merge itself.

## Filing

Nothing is on the board yet. Project is `polytrade`. Highest existing ID is
MC-08. If Arsen greenlights it, file the decision memo first and let his answer
determine what gets packeted after it.
