# PolyTrade Close UX & Wallet Screener Improvement Plan

> **For Hermes:** Use `subagent-driven-development` to implement this plan task-by-task, with specification review followed by code-quality review after each phase.

**Goal:** Repair the position-close UI, make wallet-screener statistics accurate and understandable, and create a clearer discovery-to-analysis-to-copy workflow while preserving PolyTrade’s fully original visual identity.

**Architecture:** Keep the existing React/FastAPI/Postgres application and visual system. Move close-operation state out of individual position cards into one page-level controller and body-level modal. Establish a tested metric contract between `trader_stats.py`, the leaderboard API, and screener UI before simplifying the discovery cards and deep profile. Do not copy PolyBot’s colors, typography, card layouts, animations, wording, or brand elements; competitor research informs only workflow and information hierarchy.

**Tech Stack:** React 18, existing CSS system (`brutalism.css`), Node built-in test runner, FastAPI, Pydantic, pytest, SQLite/Postgres-compatible schema, Polymarket public APIs.

---

## Non-negotiable constraints

1. PolyTrade design remains original.
2. No competitor assets, copy, color tokens, layouts, or animations are reproduced.
3. Do not deploy frontend or backend changes until the complete suite, browser QA, backup, and production approval gates pass.
4. Do not enable a local copy engine while the cloud engine is active.
5. Do not modify `/opt/polytrade/.env`, print secrets, or inspect private keys.
6. Preserve access for existing funded users.
7. A missing metric must render as unavailable/partial—not as zero.
8. Do not introduce a “copyability score” until every component is supported by data and documented.
9. Do not claim lifetime/all-time coverage when the source window is limited.
10. Separate the already completed homepage/docs/auth work from this new change set.

---

## Target user flow

```text
Screener discovery
  → choose period
  → compare a small set of trustworthy metrics
  → open wallet analysis
  → inspect evidence, freshness, positions, activity, and limitations
  → configure copy limits
  → copy wallet
  → monitor positions
  → close with one stable operation state
```

The screener answers **“Which wallets deserve inspection?”**

The wallet profile answers **“What evidence supports copying this wallet?”**

Copy settings answer **“How much risk am I willing to take?”**

---

# Phase 0 — Establish a safe baseline

### Task 0.1: Separate current completed work from new work

**Objective:** Prevent the homepage/docs/auth changes and workflow diagram files from being mixed with the close/screener implementation.

**Files:**
- Review only: entire working tree
- Preserve untouched: `docs/polytrade-workflow-diagram.html`
- Preserve untouched: `docs/polytrade-workflow-diagram.png`

**Steps:**

1. Run `git status --short` and classify every modified/untracked file.
2. Confirm the homepage/docs/auth/consent changes still pass:
   - `.venv/bin/python -m pytest -q`
   - `cd frontend && npm test && npm run build`
3. Review the existing change set independently.
4. Create a dedicated baseline commit only after user approval; exclude the two pre-existing workflow diagram files unless explicitly requested.
5. Create a fresh feature branch for this plan, for example `fix/close-ux-screener-clarity`.

**Acceptance:** The new work begins from a known reviewed revision, with no accidental secret, generated file, or unrelated diagram included.

---

### Task 0.2: Capture current visual and API behavior

**Objective:** Create comparison evidence before changing behavior.

**Files:**
- Test: `frontend/tests/launchRouting.test.js`
- Test: `tests/test_trader_stats.py` or the existing closest trader-stats test file
- Test: `tests/test_positions_api.py` or the existing closest position-route test file

**Steps:**

1. Capture desktop and narrow/mobile screenshots of:
   - Positions → Open
   - close confirmation
   - submission state
   - wallet screener 7D/30D/90D
   - wallet profile
   - copy settings
2. Save test-only fixtures for:
   - a successful close;
   - an exchange-rejected close;
   - `reconciliation_required=true`;
   - stale/partial trader statistics;
   - a wallet with fewer than seven closing days.
3. Record current API payloads without secrets or private user data.

**Acceptance:** Later visual and behavioral comparisons do not rely on memory.

---

# Phase 1 — Repair close-position UI and state handling

### Task 1.1: Define the close-operation state machine

**Objective:** Make every close state explicit and testable outside React rendering.

**Files:**
- Create: `frontend/src/closePositionState.js`
- Create or modify: `frontend/tests/closePositionState.test.js`

**States:**

```text
idle
confirming
submitting
confirmed
reconciliation_required
rejected
failed
```

**Required transitions:**

- request close: `idle → confirming`
- confirm: `confirming → submitting`
- verified success: `submitting → confirmed`
- uncertain execution: `submitting → reconciliation_required`
- known exchange rejection: `submitting → rejected`
- transport/server failure: `submitting → failed`
- retry only when safe: `rejected|failed → submitting`
- uncertain operations cannot be manually retried until reconciliation resolves.

**TDD steps:**

1. Write failing Node tests for every transition.
2. Add tests proving backdrop/Escape close is ignored during `submitting` and `reconciliation_required`.
3. Run `cd frontend && node --test tests/closePositionState.test.js`; expect failure.
4. Implement the minimal reducer/helpers.
5. Run the targeted test; expect pass.

**Acceptance:** Financial request state is not represented by multiple loosely coupled booleans.

---

### Task 1.2: Lift close state out of `PositionCard`

**Objective:** Prevent polling and card re-rendering from leaving an old modal over new card content.

**Files:**
- Modify: `frontend/src/pages/Positions.jsx`
- Modify: `frontend/src/components/PositionCard.jsx`
- Modify: `frontend/src/closePositionState.js`
- Test: `frontend/tests/closePositionState.test.js`

**Approach:**

- `PositionCard` becomes presentational and emits `onRequestClose(position)`.
- `Positions` owns one immutable `closeTarget` snapshot and one operation state.
- Only one close dialog exists per page.
- Polling may update the list, but it cannot replace the active close target’s confirmation content mid-operation.
- If the position disappears after a verified close, the operation result remains visible until acknowledged.

**TDD steps:**

1. Test that a polling payload update does not mutate the active close target snapshot.
2. Test that only one position can enter `submitting`.
3. Test that a successful close triggers one refresh and no fixed delay.
4. Remove the current 800 ms artificial wait.
5. Replace the card-local `confirm`/`busy` handling with the page controller.

**Acceptance:** Opening one close flow never leaves stale content from another card or polling refresh.

---

### Task 1.3: Render one hardened modal at the document root

**Objective:** Eliminate the stacking-context bug caused by fixed overlays nested inside backdrop-filter cards.

**Files:**
- Modify: `frontend/src/components/Modal.jsx`
- Modify: `frontend/src/styles/brutalism.css`
- Modify: `frontend/src/pages/Positions.jsx`
- Test: `frontend/tests/launchRouting.test.js` or create `frontend/tests/modalContract.test.js`

**Implementation requirements:**

- Use `createPortal(..., document.body)`.
- Add `role="dialog"`, `aria-modal="true"`, and an accessible title relationship.
- Lock body scrolling while open and restore it on cleanup.
- Close on Escape only when operation state permits.
- Ignore backdrop close during submission or reconciliation.
- Place overlay above app header, tab bar, cards, tooltips, and sticky elements.
- Keep focus inside the dialog and restore focus to the originating close button.
- Respect `prefers-reduced-motion`.

**Verification:**

1. Static/contract tests fail before implementation.
2. Implement portal and accessibility behavior.
3. Run frontend tests.
4. Verify in Chromium and Telegram-like narrow WebKit/mobile viewport.

**Acceptance:** The overlay always covers the viewport; no card, header, tab, or old element appears above it.

---

### Task 1.4: Handle uncertain close execution correctly

**Objective:** Make `reconciliation_required` a visible non-retryable state instead of reopening the confirm action.

**Files:**
- Modify: `frontend/src/pages/Positions.jsx`
- Modify: `frontend/src/components/PositionCard.jsx`
- Modify: `frontend/src/closePositionState.js`
- Test: `frontend/tests/closePositionState.test.js`
- Verify: `backend/api/routes_positions.py`

**Behavior:**

- For `{ok:false, reconciliation_required:true}` show:
  - “Execution status is being reconciled.”
  - no retry/confirm button;
  - a safe dismiss button only if the state remains visible in the underlying position list;
  - an automatic targeted refresh.
- Do not submit another SELL.
- Known rejection may offer retry after a fresh position read.
- Unknown transport failure must not claim that no order was submitted.

**Acceptance:** An uncertain order cannot be duplicated from the UI.

---

### Task 1.5: Browser regression QA for closing

**Objective:** Reproduce and prove removal of the reported lag/overlap.

**Verification matrix:**

- long market title;
- multiple cards;
- narrow Telegram viewport;
- page scrolled before modal opens;
- modal open during the 30-second polling tick;
- successful close;
- known rejection;
- server error;
- uncertain execution;
- Escape/backdrop during idle and submitting;
- double-click on confirm;
- reduced-motion mode.

**Acceptance:** No overlap, duplicate submit, stale modal, hidden operation, or 800 ms artificial delay remains.

---

# Phase 2 — Establish a truthful screener metric contract

### Task 2.1: Document every current metric before changing UI

**Objective:** Make metric meaning and provenance explicit in code and tests.

**Files:**
- Create: `docs/screener-metric-contract.md`
- Modify: `backend/core/trader_stats.py`
- Modify: `backend/db/models.py`
- Test: `tests/test_trader_stats.py`

**Document for each metric:**

- exact formula;
- source endpoint;
- time window;
- row/page limits;
- refresh cadence;
- null/partial behavior;
- whether official or reconstructed;
- whether safe for sorting/filtering;
- user-facing label and tooltip.

**Metrics:**

- `total_pnl`
- `win_rate`
- `open_positions`
- `consistency_score`
- `pnl_quality`
- windowed PnL/WR/volume
- green/red days
- fill/exit counts and ratio
- daily PnL sparkline
- `history_days`
- `stats_refreshed_at`

**Acceptance:** No user-facing metric exists without a documented contract.

---

### Task 2.2: Add deterministic metric fixtures

**Objective:** Protect calculations from plausible-looking but incorrect output.

**Files:**
- Create or modify: `tests/fixtures/trader_activity.py`
- Modify: `tests/test_trader_stats.py`

**Fixture cases:**

- one large BUY with several small SELLs;
- old BUY basis outside fetched history;
- partial SELL;
- redeem event;
- resolved holding with missing true resolution date;
- six profitable closing days;
- seven closing days;
- flat days;
- partial 30D and 90D coverage;
- official leaderboard PnL present;
- official PnL absent and local fallback used.

**Acceptance:** Tests expose why event-count ratios, reconstructed PnL, and limited windows cannot be mislabeled as capital close rate or all-time statistics.

---

### Task 2.3: Fix selected-period sorting

**Objective:** Make 7D/30D/90D selection affect both displayed data and sorting.

**Files:**
- Modify: `frontend/src/components/WalletScreener.jsx`
- Modify: `frontend/src/api.js`
- Modify: `backend/api/routes_traders.py`
- Modify: `backend/core/trader_stats.py`
- Test: frontend query-construction test
- Test: backend leaderboard sorting test

**Required mapping:**

```text
7D + PNL    → pnl_7d
30D + PNL   → pnl_30d
90D + PNL   → pnl_90d
7D + WR     → winrate_7d
...
7D + VOLUME → volume_7d
...
```

Non-windowed sorting must be explicitly named `LIFETIME PNL` or `OVERALL`, not silently reused.

**Acceptance:** A 7D selection cannot be sorted by lifetime PnL unless the user explicitly chooses lifetime.

---

### Task 2.4: Correct or remove misleading default metrics

**Objective:** Prefer fewer truthful metrics to many ambiguous ones.

**Files:**
- Modify: `frontend/src/components/TraderCard.jsx`
- Modify: `frontend/src/components/WalletScreener.jsx`
- Modify: `backend/core/trader_stats.py`
- Modify: `backend/db/models.py` comments
- Test: frontend wording/contract test
- Test: metric calculation tests

**Decisions:**

1. `EXIT/FILL`
   - Do not describe event-count ratio as “how much was closed”.
   - Remove it from the primary card.
   - If retained under Advanced, label exactly as `SELL / BUY EVENT COUNT` with formula explanation.

2. `PNL QUALITY`
   - Remove from primary cards and default sorting until horizons are made comparable.
   - Do not describe limited reconstructed realized PnL as all-time.

3. `WR`
   - Use selected-period WR in the discovery card.
   - Label reconstructed/partial coverage explicitly.

4. `GREEN DAYS`
   - Rename to `POSITIVE CLOSE DAYS`.
   - Explain denominator: positive ÷ positive-plus-negative realized close days; flat/no-close days excluded.

5. `CONS` and tier
   - Remove from the default discovery surface until it is period-aware and validated.
   - If retained in deep analysis, call it `CLOSE-DAY CONSISTENCY` and state the seven-day minimum.

**Acceptance:** Static copy tests fail if removed misleading phrases return.

---

### Task 2.5: Expose freshness and source quality

**Objective:** Let users judge whether a metric is current and complete.

**Files:**
- Modify: `backend/api/routes_traders.py`
- Modify: `backend/core/trader_stats.py`
- Modify: `frontend/src/components/TraderCard.jsx`
- Modify: `frontend/src/components/TraderProfile.jsx`
- Test: backend response tests
- Test: frontend freshness-label tests

**Required fields:**

- `stats_refreshed_at`
- `history_days`
- `history_partial`
- PnL source where known: `official_leaderboard` or `reconstructed_estimate`

**UI states:**

- Fresh
- Aging
- Stale
- Partial history
- Unavailable

Thresholds must be derived from actual configured refresh behavior, not arbitrary marketing language.

**Acceptance:** Users can see data age and limitations without opening documentation.

---

# Phase 3 — Simplify screener information architecture in PolyTrade’s own design

### Task 3.1: Define the original PolyTrade discovery card

**Objective:** Reduce cognitive load without imitating PolyBot’s visual design.

**Files:**
- Modify: `frontend/src/components/TraderCard.jsx`
- Modify: `frontend/src/styles/brutalism.css`
- Test: frontend content contract tests

**Primary discovery information:**

- trader name/address;
- selected period;
- selected-period realized PnL;
- selected-period WR;
- selected-period gross activity volume;
- active positions;
- freshness/coverage state;
- one primary `ANALYZE` action.

**Secondary information:**

- positions and latest activity preview;
- advanced reconstructed metrics;
- exact source explanation.

**Design constraints:**

- Keep PolyTrade’s light technical brutalism.
- Keep its own spacing, typography, borders, and motion.
- Do not reproduce PolyBot’s dark blue table, ranking badges, or card hierarchy.
- Avoid fake precision and unsupported ranking numbers.

**Acceptance:** A user can scan the primary evidence without decoding acronyms.

---

### Task 3.2: Separate discovery from deep wallet analysis

**Objective:** Stop forcing all analytics into the discovery card.

**Files:**
- Modify: `frontend/src/components/WalletScreener.jsx`
- Modify: `frontend/src/components/TraderCard.jsx`
- Modify: `frontend/src/components/TraderProfile.jsx`
- Possibly create: `frontend/src/components/TraderAnalysis.jsx`
- Modify: `frontend/src/styles/brutalism.css`

**Deep-analysis sections:**

1. Identity and freshness
2. Selected-period summary
3. PnL and close-day history
4. Current positions
5. Recent activity
6. Concentration/sample limitations, only where supported
7. Data-source notes
8. Copy settings entry point

Do not add a recommended setup engine in this phase unless its rule set is separately specified and tested.

**Acceptance:** Discovery stays concise; deep evidence is available without being hidden.

---

### Task 3.3: Improve filters and progressive disclosure

**Objective:** Make useful filters obvious and experimental metrics secondary.

**Files:**
- Modify: `frontend/src/components/WalletScreener.jsx`
- Modify: `frontend/src/styles/brutalism.css`
- Test: query construction and filter-label tests

**Default controls:**

- period;
- PnL;
- WR;
- volume;
- freshness/coverage;
- search.

**Advanced controls:**

- positive close-day ratio;
- event-count ratio, only if retained;
- experimental consistency, only if retained;
- partial-history inclusion.

**Acceptance:** Default screener is usable without understanding internal statistical reconstruction.

---

# Phase 4 — Data-source and performance hardening

### Task 4.1: Remove duplicate live wallet fetches

**Objective:** Reduce lag when analyzing an arbitrary pasted wallet.

**Files:**
- Modify: `backend/api/routes_traders.py`
- Modify: `backend/core/trader_stats.py`
- Test: upstream-call-count tests

**Approach:**

- Reuse enrichment results for positions/recent activity instead of immediately fetching them again.
- Return a structured analysis result from one backend orchestration path.
- Cache only successful, provenance-tagged calculations.
- Do not cache transient failures as zero values.

**Acceptance:** One analysis request has a bounded, tested number of upstream calls.

---

### Task 4.2: Make refresh cadence visible and operationally realistic

**Objective:** Align UI freshness claims with a population that may take roughly 150 minutes to rotate under defaults.

**Files:**
- Modify: `backend/main.py` comments
- Modify: `backend/core/trader_stats.py`
- Modify: `docs/screener-metric-contract.md`
- Test: stale-first rotation tests

**Steps:**

1. Fix the stale comment claiming a default refresh limit of 100 when code uses 200.
2. Measure actual pass duration under test/staging conditions.
3. Define aging/stale thresholds from configured cadence, target population, and batch size.
4. Ensure never-enriched/stalest wallets continue to be prioritized.
5. Add structured refresh metrics/logs without sensitive data.

**Acceptance:** Freshness labels reflect the real refresh system.

---

### Task 4.3: Reconcile user PnL series definitions

**Objective:** Prevent headline and chart totals from diverging on partial exits.

**Files:**
- Modify: `backend/core/pnl.py`
- Modify: `backend/core/equity.py`
- Test: PnL/equity partial-exit fixtures

**Steps:**

1. Create a shared realized-PnL aggregation helper.
2. Test full close, partial close, resize, resolve, and legacy fallback.
3. Use the same definition for headline PnL and equity snapshot cumulative PnL.
4. Document any intentional difference.

**Acceptance:** The same account/time produces reconcilable headline and chart values.

---

# Phase 5 — Correct redemption messaging adjacent to positions

### Task 5.1: Remove the false automatic-redemption statement

**Objective:** Stop telling users that winnings redeem automatically when no automatic redemption exists.

**Files:**
- Modify: `frontend/src/components/PositionCard.jsx`
- Modify: `backend/api/routes_positions.py`
- Modify: relevant frontend copy test
- Modify: relevant backend route test

**Correct behavior:**

- Resolved market: nothing remains to sell.
- If claimable: user must redeem through the currently supported external path.
- Do not claim automatic redemption.

**Acceptance:** Frontend, backend errors, homepage, funding docs, and wallet docs agree.

---

### Task 5.2: Fix claimable status for tracked resolved positions

**Objective:** Ensure tracked resolved rows can display claimable status when the corresponding live holding remains redeemable.

**Files:**
- Modify: `backend/api/routes_positions.py`
- Test: position-list merge/dedupe tests
- Modify: `frontend/src/components/PositionCard.jsx`

**Acceptance:** Dedupe does not discard live claimable metadata for a DB-tracked resolved position.

---

# Phase 6 — Documentation, analytics, and release preparation

### Task 6.1: Update consumer metric explanations

**Objective:** Explain what screener data means without overwhelming the main interface.

**Files:**
- Modify: `docs/core-concepts.md`
- Modify: `docs/copy-trading.md`
- Modify: `docs/getting-started.md`
- Link: `docs/screener-metric-contract.md` from developer/operator documentation, not necessarily the consumer nav.
- Test: `tests/test_docs_site.py`

**Acceptance:** Docs use the same labels, formulas, sources, and limitations as code.

---

### Task 6.2: Add privacy-safe product telemetry

**Objective:** Measure whether the redesign helps users without logging wallet secrets or Telegram init data.

**Events:**

- screener search submitted;
- period changed;
- advanced filters opened;
- wallet analysis opened;
- copy settings opened;
- close modal opened;
- close submitted;
- close confirmed/rejected/reconciliation-required;
- modal dismissed.

**Guardrails:**

- no private keys;
- no cookies;
- no Telegram init data;
- avoid full wallet address when aggregate event data is enough.

**Metrics:**

- analysis-open rate from screener;
- copy-settings-open rate;
- close completion rate;
- close retry rate;
- reconciliation-required rate;
- frontend error rate;
- time from confirm to known outcome.

**Acceptance:** Success is measurable without creating a new custody/privacy risk.

---

### Task 6.3: Complete quality gates

**Objective:** Prove the implementation works before production.

**Commands:**

```bash
cd /Users/mirka001/polytrade
.venv/bin/python -m pytest -q
cd frontend
npm test
npm run build
cd ..
git diff --check
```

**Required reviews:**

1. Specification compliance
2. Code quality
3. Security/custody review
4. Cross-database schema review if any schema changed
5. Browser visual review
6. Telegram mobile review

**Acceptance:** Zero unresolved Important/Critical findings.

---

### Task 6.4: Staged production rollout

**Objective:** Deploy safely without disrupting the cloud copy engine.

**Pre-deployment:**

1. Explicit user approval.
2. Confirm current branch/revision.
3. Back up Supabase/Postgres and deployment files.
4. Confirm local Mac copy engine remains disabled.
5. Record cloud engine state.
6. Build and test target image.

**Rollout order:**

1. Deploy API-compatible backend changes with engine handling reviewed.
2. Verify DB/API and existing user sessions.
3. Deploy frontend.
4. Verify homepage, Telegram launch, screener, profile, positions, close modal, and docs.
5. Perform one controlled small-value close test only with explicit authorization.
6. Verify one cloud engine and fresh reconciliation logs.
7. Monitor errors, uncertain claims, modal failures, and screener latency.

**Rollback trigger:**

- auth/session regression;
- wallet creation failure;
- close-operation duplicate/retry regression;
- schema incompatibility;
- screener API error spike;
- copy-engine freshness failure;
- any second engine detected.

---

# Recommended execution order

## Release A — correctness first

1. Baseline current work.
2. Close state machine.
3. Page-level modal controller and portal.
4. Uncertain execution handling.
5. Redemption copy/status correction.
6. Tests and browser QA.

## Release B — screener truthfulness

1. Metric contract.
2. Deterministic fixtures.
3. Period-aware sorting.
4. Remove/relabel misleading metrics.
5. Freshness and provenance.
6. Docs and tests.

## Release C — screener clarity and performance

1. Original simplified discovery card.
2. Separate deep analysis.
3. Progressive filters.
4. Deduplicate analysis API calls.
5. Add privacy-safe telemetry.
6. Controlled release and measure behavior.

Do not combine all three releases into one production deployment.

---

# Definition of done

- Close modal never overlaps old cards or app chrome.
- Polling cannot mutate or orphan an active financial action.
- Uncertain close cannot be retried from the UI before reconciliation.
- No artificial close-success delay remains.
- Screener sorting follows the selected period.
- Default cards contain only clearly defined metrics.
- Data age, partial history, and unavailable data are visible.
- Event-count ratios are not described as capital/shares closed.
- Limited reconstructed metrics are not labeled all-time.
- Resolved winnings are not described as automatically redeemed.
- PolyTrade retains its original visual identity.
- Full backend/frontend tests and build pass.
- Independent review approves the work.
- Production deployment occurs only after explicit approval and backup.
