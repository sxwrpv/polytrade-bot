# Public Launch & X Marketing Plan — Personal Brand Account (Polymarket Copybot + Other Products)

Last updated: 2026-07-06

**Format decision:** this is a personal account, not a product account — your life, your process, your opinions, with the copybot (and other products) as things you build and ship, not the whole identity. This is the right call: on X, "founder posts about their actual life and you can watch them build" converts skepticism into trust far better than a branded product account ever does, especially for something that touches people's money. A faceless bot account promoting a custodial trading product reads as exactly the kind of thing people are trained to distrust; a real person who occasionally posts about a trading product they built, alongside normal life and other work, doesn't trigger the same guard. Everything below is adapted around that — the copybot is one content pillar among several, not the account's entire reason to exist.

## 0. The core mechanic this plan is built around

Every user who signs up through the app gets a **custodial deposit wallet created by the app itself**. That means the app operator (you) is structurally the referrer for 100% of app-originated trading volume — not just people you personally DM. Two separate loops matter here, and they should not be conflated:

1. **Polymarket → you.** You put your own Polymarket referral link/code in the account-creation flow (or generate one server-side per user via the share/profile mechanism) so every wallet the app creates counts as your direct referral. You earn **10% of net trading fees** on each user's trades from their first trade through their **Gold** tier, capped at **30 days per user** (rewards stop at Platinum or 30 days, whichever first). Indirect referrals (someone your referral brings in) pay you 5%.
2. **You → your users.** The schema already has `referral_code` / `referred_by` fields — an internal, app-level referral loop where existing users invite new users to the *app* (not Polymarket directly). This is your growth flywheel and should carry its own incentive (see §4).

Revenue from loop 1 only exists if the app has trading volume. Growth from loop 2 only compounds if the app is public, trusted, and visible. X marketing is what drives both — it's the top of funnel for loop 2, and loop 1 is the reason the funnel is worth building at all.

**Implementation note:** confirm where in the onboarding flow the Polymarket referral attribution actually gets set — it needs to happen at wallet-creation time (likely alongside the deposit-wallet creation step referenced in `README.md`'s custody section / `core/wallet.py`), not as an afterthought, since Polymarket only attributes a signup to a referrer if it happens within 30 days of the link click and on first trade. If the app's own signup flow doesn't route through a page carrying your referral link/code, loop 1 doesn't fire regardless of how good the marketing is — verify this before investing in content.

**Eligibility gate to flag now:** you need $10,000 in your own lifetime Polymarket trading volume before referral rewards start paying out. Confirm this is cleared (or will be shortly) before you build a launch around this revenue line — rewards accrue and are visible before that, but don't pay out until you cross it.

Source: [Polymarket Referral Program docs](https://docs.polymarket.com/resources/referral-program) — terms current as of the May 28, 2026 update. Note the program's own disclaimer: rates, windows, and caps "can change at any time without notice." Treat every number above as re-verify-before-launch, not load-bearing forever.

---

## 1. Compliance and risk framing — read before writing a single tweet

This is a custodial, real-money trading product, not a game or a signal service. Marketing it carelessly is the single biggest risk to the whole plan — bigger than any growth tactic below.

- **Geoblocking is real and load-bearing.** The bot already checks geoblock before every order (per `BUILD_PLAN.md`). Marketing must not target or actively solicit US persons or residents of other restricted jurisdictions — that includes replying to US-coded accounts with "come try this," ad targeting, or bio claims that imply US availability. Keep this consistent everywhere: profile, pinned tweet, every thread.
- **No "guaranteed returns" language, ever.** Copytrading past performance ("this wallet is up 340%") is a statement about the leader, not a promise about a follower's outcome — slippage, sizing, timing, and market resolution all differ. Every performance post needs a plain-language "copying carries independent risk; past performance isn't predictive" line. This is not boilerplate CYA — prediction-market and copy-trading products draw regulatory attention, and X (like most platforms) will act on financial-product complaints faster than on generic spam reports.
- **Custody disclosure.** Because onboarding is custodial (app generates the keypair, encrypts and holds it), any post that could be read as "we hold your funds, trust us" needs to be paired, at least once per week in rotation, with a post about the actual safeguards (encryption at rest, self-export via `POST /api/user/export-key`, no import path so users know exactly what custody model they're in). Don't let hype content run ahead of the safety story.
- **Influencer/paid promo disclosure.** If you pay or comp anyone to post about the bot, US FTC-style disclosure norms (`#ad`) still apply on X regardless of the poster's location, because the audience is global. Build this into the affiliate brief in §4, not as an afterthought.
- **Referral-program-specific violations to avoid:** self-referral, referring wallets you control, and "inauthentic trading" are explicit grounds for clawback under Polymarket's terms. Don't create test/demo accounts through your own referral link for QA — use a separate, unlinked flow for that.

---

## 2. X platform rules that shape what "semi-automated" can mean

X's current automation policy (verify at [help.x.com/en/rules-and-policies/x-automation](https://help.x.com/en/rules-and-policies/x-automation) before building anything, since these rules move):

| Allowed | Not allowed |
|---|---|
| Scheduling your own original posts via an authorized app/API | Automated follow/unfollow, or the follow-then-unfollow growth trick |
| AI-assisted or AI-drafted post content | Automated likes/retweets/bookmarks ("engagement farming") |
| A clearly-labeled bot account (bio says it's automated) | Bulk or automated DMs for marketing/outreach |
| Manually replying, or replying via an app the original poster explicitly mentions | Automated replies to posts that didn't mention your app first (blocked platform-side since Feb 2026) |

**Implication for this plan:** "semi-automated" should mean *automated drafting and scheduling of original content*, with a human approval gate before anything publishes, and *manual* (not automated) replies/engagement. Trying to automate replies or engagement is both against policy and the highest-risk thing to automate for a financial product anyway — a bad auto-reply on a trading account is a screenshot-and-ratio waiting to happen.

**API cost reality (2026):** the free tier is gone. New developer access is pay-per-use — roughly $0.015 per post created ($0.20 if it contains a link), ~$0.005 per read, capped reads. Legacy flat-rate Basic ($200/mo, 50k posts/app/month) still exists for pre-existing subscribers but is being migrated to pay-per-use after June 2026. Budget accordingly (see §7) — at expected volume (a few posts/day) this is closer to $5–15/month than the old $200/month tier, so don't over-provision.

---

## 3. Positioning and messaging

**Who this account is for:** people who want to follow an indie builder shipping real products — one of which happens to be a Polymarket copy-trading tool. The account's core promise is "watch me build and use real things," not "use my trading bot." That framing is what makes the promotional posts land instead of getting muted — people tolerate (and often want) product talk from someone whose feed isn't only product talk.

**Content pillars — mix across all of these, not just the product ones:**

1. **Personal / life** — the ordinary stuff: what you're working on today, opinions, things you find interesting, occasional non-work posts. This is the majority of volume, not a garnish. It's what makes the account read as a person and not a funnel.
2. **Build-in-public / process** — decisions, tradeoffs, bugs, what broke and how you fixed it, screenshots of the actual dashboard or code. This is where the copybot's technical substance (price-capped entries, on-chain detection, risk controls from `BUILD_PLAN.md`) shows up — framed as "here's what I'm building," not "here's why you should sign up."
3. **Proof** — leaderboard movers, real trader performance, notable copied trades. Still needs the same disclaimer discipline as before (§1) whenever numbers are attached, regardless of how casually it's framed.
4. **Product promotion (explicit)** — direct asks: "I built X, here's what it does, here's the link." Do this occasionally and openly rather than constantly disguising it as something else — an audience that's been getting real personal content trusts a direct pitch when it shows up, and resents a feed that turns out to have been stealth marketing the whole time.
5. **Transparency** — custody model, incident/postmortem posts when something in the bot breaks. Same as before, still non-negotiable for a product that holds people's funds — just delivered in your own voice rather than an official-sounding account voice.

**What to avoid:** don't let every post secretly be about the bot. If someone reads ten posts and eight are disguised funnels, the personal-account trust benefit evaporates and it's worse than just running a product account, because now it also reads as manipulative. Price predictions / "this market is a lock" content is still off-limits per §1 — that risk doesn't change just because the account is personal.

**Multiple products:** since the copybot isn't the only thing you're building, keep a running mental (or literal) list of what you're promoting and rotate — an account that only ever talks about one product, even in a personal voice, still reads as a product account. Variety across your actual work is part of what sells the "real builder" framing.

**Differentiation note (for when you do talk about the bot):** copy-trading tools for Polymarket already exist. The defensible edge per `BUILD_PLAN.md` is price-capped entries anchored to the leader's actual fill (so a slow reaction doesn't mean a worse entry), on-chain detection rather than slow polling, and per-wallet risk controls — a real technical claim, safe to lead with. Don't claim to be "the first" or "the only" without checking current competitors.

**Sample posts (tone: first person, casual, not corporate):**

- *Personal:* "Didn't ship anything today, spent it debugging why [X] instead. some days are just that."
- *Build-in-public:* "Spent the week on detection speed for the copy bot — was polling every few seconds, switched to reading on-chain fill events directly. copies now land in under a second instead of chasing a stale price. small change, big difference in slippage."
- *Proof:* "One of the wallets on the Polymarket leaderboard this week is up [X]% on real volume, not just a lucky market. tempting to copy blindly — worth remembering past performance isn't a promise, and copying has its own slippage/timing risk on top."
- *Product promotion:* "Been building a Polymarket copy-trading tool for a few months — pick a wallet, it mirrors their trades with your own risk settings (position caps, slippage limits, daily loss limit). custodial, real money, non-US only. link if you want to try it: [link]."
- *Transparency:* "Quick note on custody since a few people asked: your key is generated and encrypted per-wallet, you can export it anytime, there's no import path by design — that's the tradeoff of unattended copying, want people to know exactly what they're opting into."

---

## 4. Growth loop: referral incentive design (the app-level one, loop 2 from §0)

You already have `referral_code`/`referred_by` in the schema — use it as the spine of both the product loop and the content loop.

- **Incentive shape:** since your own revenue is 10% of a referred user's net fees for up to 30 days, consider passing a slice of *that* back to the referring user (e.g., a fee rebate or bonus credit funded from your referral earnings, not from thin air) for each person they bring in who trades. This makes "invite a friend" a real, fundable feature instead of a marketing slogan.
- **Make the referral link the unit of content.** Every leaderboard post, every "here's a trader worth watching" thread, every dashboard screenshot should carry a trackable link back to the app with the poster's own `referral_code` baked in — including your own official account's posts, which should run through your top-level link.
- **Affiliate tier for larger accounts:** for crypto/Polymarket-adjacent X accounts with real audiences, offer a fixed referral-code + a simple public leaderboard of "top app referrers" (gamifies it, costs nothing but visibility, and gives you a reason to post about your own affiliates — more content).

---

## 5. X account setup and content operations

**Account basics**
- Bio: about you, not about the bot — name/handle as yourself, one line on what you generally do (e.g., "building things, currently a Polymarket copy-trading tool"), no "Official," no bot-style branding. It should read like a person's bio, because it is one.
- No blanket bot-account label is needed here, because a human is drafting and approving every post (see pipeline below) — X's automated-account labeling rule targets accounts that post without a human in the loop. Keep it that way: the review gate isn't just a compliance nicety, it's also what keeps this from legally/functionally becoming "a bot account" under X's own policy.

**Cadence (realistic, not aspirational) — rough weekly mix across ~1 post/day:**
- ~3–4/week personal or opinion/life content
- ~1–2/week build-in-public/process content (can double as bot-technical content when relevant)
- ~1/week proof content (leaderboard/trader performance, disclaimer attached)
- ~1/week explicit product promotion (rotate which product if there's more than one)
- Transparency/incident posts are unscheduled and manual, same-day, whenever something actually happens — never queued or faked

This ratio matters more than the exact numbers: personal + process should outweigh direct promotion by something like 3:1 or better, or the account drifts back into reading as a product account with a human mask on it.

**Semi-automated production pipeline:**

1. **Capture (manual, low-friction):** jot rough notes/voice memos for personal and build-in-public content as things happen through the day/week — this can't be data-pulled, it has to come from you. For bot-related content, a scheduled job can pull raw material from the app's own DB (`trade_events`, `copy_positions`, `trader_stats`) for leaderboard movers and notable copies — no PII, no individual user's PnL without consent.
2. **Draft generation (automated, LLM-assisted):** feed your rough notes and any pulled bot data into pillar-specific templates to produce a handful of draft posts. Keep a fixed disclaimer block auto-appended to any draft that contains a number, a claim of performance, or an invitation to sign up.
3. **Human review + voice pass (manual — do not skip):** every draft gets reviewed and edited into your actual voice before scheduling. This step does two jobs at once: it catches compliance mistakes (§1) on product-related drafts, and it's what stops the personal content from reading as obviously LLM-drafted — an AI-flavored "personal" post is worse than no post, since it undermines the exact trust the format is supposed to build.
4. **Scheduling (automated):** approved posts go into a scheduling tool at the mix/cadence above, spread across the week rather than batched.
5. **Engagement and replies (fully manual):** per §2, never automate this, and it matters even more here — replies are literally how "this is a real person" gets proven out over time.

**Tooling:** a third-party scheduler (e.g., Typefully, Buffer, Postiz-style tools) authorized via X's official OAuth is the lowest-effort path and avoids managing X API pay-per-use billing directly for scheduling; reserve direct API use for the data-pull step if you want that in-house. Confirm current pricing/limits at signup — this space moved fast in 2026.

---

## 6. Launch sequence

| Phase | Focus | Exit criteria |
|---|---|---|
| **Pre-launch** | If the account is new or has been quiet, spend 2–4 weeks posting personal/build-in-public content *before* the first product mention — an account that starts with a pitch has no trust reserve to spend. In parallel: finish the custody/risk explainer thread, confirm $10k lifetime volume threshold status, set up scheduler + draft pipeline | Account has a believable personal posting history; pipeline producing reviewable drafts; disclaimer template locked |
| **Soft launch** | First explicit product mention, framed as "here's something I built," to whatever audience has accumulated organically; invite direct contacts to test the referral loop end-to-end (signup → trade → reward shows in dashboard) | At least one real referral reward paid out and verified in pUSD |
| **Public launch** | Regular cadence at the §5 mix; pin the custody/risk thread; light outreach to 5–10 relevant crypto/prediction-market accounts for organic RTs (not paid, not automated) | Cadence sustained 2 weeks without a compliance miss, and without the feed tipping into product-account territory |
| **Scale** | Introduce affiliate tier (§4), consider 1–2 paid/comped posts from disclosed accounts, evaluate direct API use if volume justifies it over the scheduler tool | Referral revenue (loop 1) covers X tooling costs |

---

## 7. Budget (rough, verify before committing)

- Scheduling tool: $10–30/month typical for a solo-operator tier (verify current plan at signup).
- X API (if used directly for data pull, not required for scheduling): pay-per-use, ~$0.015/post, ~$0.005/read — at a few posts/day this is single-digit dollars/month, not the old $200 flat tier.
- Affiliate/incentive budget: funded from Polymarket referral revenue (loop 1), not fixed cash — keep it self-funding so growth spend scales with actual revenue.

---

## 8. Metrics to actually track

- Signups attributed via `referred_by` (app-level loop), split by which account/post drove them where trackable.
- Users crossing first trade → 30-day reward window, and how much of that window each actually captures (are people trading enough in the first 30 days to matter?).
- pUSD referral rewards received (loop 1) vs. X tooling spend — this is the number that tells you if the channel pays for itself.
- Compliance incidents: zero is the target, not "low." Track near-misses caught at the human review gate as a leading indicator the process is working.

---

## Open questions to resolve before finalizing

- Do you already have a Polymarket account past the $10k lifetime volume threshold, or does that need to happen first?
- Is there budget/appetite for the app-level referral incentive (§4) to be a real credit/rebate, or should launch content lean on the "proof/mechanism/transparency" pillars only until that's built?
- Who is doing the manual review gate and manual replies day-to-day — is this a one-person operation, and if so, is the daily cadence in §5 actually sustainable, or should it start at half that?
- Is this an existing X account with a history, or a new one? That changes the pre-launch phase a lot — an existing account with real posting history can mention the bot much sooner than a brand-new one with no trust reserve built up yet.
- What are the other products worth rotating into the content mix, so the account doesn't end up being "personal, except every product post is about the same bot"?
