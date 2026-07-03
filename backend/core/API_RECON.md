# Polymarket API — frozen response shapes (recon, verified live 2026-06-30)

Probed against the live public APIs. All endpoints below are **GET, no auth**.
Feeds `polymarket.py` (phase 3). Trading/auth endpoints are covered in BUILD_PLAN §3.

## Cross-cutting gotchas (read first)
1. **User-Agent required.** `data-api` returns **403** to the default urllib UA. Set a
   browser-like `User-Agent` header on every request (also set it for httpx — its default
   `python-httpx/*` UA may be blocked too). Verify in phase 3.
2. **Gamma stringified JSON.** `clobTokenIds`, `outcomes`, `outcomePrices` come back as
   JSON-**encoded strings**, not arrays → `json.loads()` before use.
3. **Activity is mixed.** `type` ∈ {TRADE, TAKER_REBATE, SPLIT, MERGE, REDEEM, REWARD,
   CONVERSION}. Filter `?type=TRADE` for actual trades (stats/history).
4. **`usdcSize` field name persists** despite collateral now being pUSD. Treat as pUSD.
5. **`endDate` formats differ.** Positions: date-only `"2026-07-20"`. Gamma: ISO
   `"2026-07-20T00:00:00Z"`. Parse defensively.
6. **Leaderboard wallets ≠ copyable.** Top PnL/VOL wallets are often market-makers with
   **zero open positions**. Don't assume a leaderboard entry has positions to mirror.
7. `outcomeIndex` 0 = "Yes", 1 = "No"; `outcome` is capitalized. `rank` is a **string**.
8. neg-risk markets exist (`negRisk`/`negativeRisk` true) → affects order placement (neg-risk
   exchange contract) in phase 6.

---

## 1. Leaderboard — seed `trader_cache`
`GET https://data-api.polymarket.com/v1/leaderboard`
params: `category` (OVERALL|POLITICS|SPORTS|…), `timePeriod` (DAY|WEEK|MONTH|ALL),
`orderBy` (PNL|VOL), `limit` (1–50), `offset` (0–1000), optional `user`, `userName`.
→ `list[ {rank:str, proxyWallet:str, userName:str, xUsername:str, verifiedBadge:bool,
vol:float, pnl:float, profileImage:str} ]`
Note: gives pnl/vol only — win_rate, consistency, total_trades are computed locally.

## 2. Positions — copy-engine diff + free unrealized PnL
`GET https://data-api.polymarket.com/positions?user=0x…`
params: `user` (req), `market` (csv conditionIds), `eventId`, `sizeThreshold` (default 1),
`redeemable` (bool), `mergeable` (bool), `limit` (0–500, def 100), `offset`,
`sortBy` (CURRENT|INITIAL|TOKENS|CASHPNL|PERCENTPNL|TITLE|PRICE|AVGPRICE…), `sortDirection`.
→ `list[Position]`, each:
```
proxyWallet:str  asset:str(=token_id)  conditionId:str  size:float(=shares)
avgPrice:float(=entry)  curPrice:float(=current)  initialValue:float  currentValue:float
cashPnl:float(=unrealized)  percentPnl:float  totalBought:float
realizedPnl:float  percentRealizedPnl:float
redeemable:bool(=market resolved→redeem)  mergeable:bool  negativeRisk:bool
outcome:str("Yes"/"No")  outcomeIndex:int(0/1)  oppositeOutcome:str  oppositeAsset:str
title:str  slug:str  icon:str  eventId:str  eventSlug:str  endDate:str(date-only)
```
Mapping to `copy_positions`: trader_address=proxyWallet, token_id=asset, entry_price=avgPrice,
shares=size. Diff source = live positions vs DB `open` rows. `redeemable:true` ⇒ resolved.

## 3. Activity / trades — history & stats (phase 8)
`GET https://data-api.polymarket.com/activity?user=0x…&type=TRADE&limit=N`
TRADE record:
```
proxyWallet:str  timestamp:int(unix s)  conditionId:str  type:"TRADE"  side:"BUY"/"SELL"
asset:str(token_id)  outcome:str  outcomeIndex:int  price:float(0–1)  size:float(shares)
usdcSize:float(pUSD notional)  title:str  slug:str  transactionHash:str  name/pseudonym/…
```

## 4. Order book — pricing & order validation
`GET https://clob.polymarket.com/book?token_id=…`
→ `{ market:str(=conditionId), asset_id:str(=token_id), hash, timestamp,
bids:[{price:str,size:str}], asks:[{price:str,size:str}],
tick_size:str, min_order_size:str, neg_risk:bool, last_trade_price:str }`
bids sorted low→high, asks high→low (prices are **strings**). Use tick_size/min_order_size
for order validation in phase 6.

## 5. Gamma markets — hot markets grid
`GET https://gamma-api.polymarket.com/markets?order=volume24hr&ascending=false&active=true&closed=false&limit=N`
relevant fields: `id, question(=title), slug, conditionId, clobTokenIds(JSON-str [YES,NO]),
outcomes(JSON-str), outcomePrices(JSON-str), volume, volume24hr, liquidity,
bestBid, bestAsk, lastTradePrice, spread, endDate(ISO), active, closed, negRisk,
enableOrderBook, orderMinSize, orderPriceMinTickSize, icon, image`.

## 6. Holders — top holders of a market (bonus, optional feature)
`GET https://data-api.polymarket.com/holders?market=<conditionId>&limit=N`
→ `list[ {token:str, holders:[ {proxyWallet, asset, amount:float, outcomeIndex,
name, pseudonym, verified:bool, profileImage} ]} ]`  (one group per token/outcome)
Useful to find wallets that definitely hold positions; not required for MVP.
