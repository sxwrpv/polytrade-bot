"""Deterministic activity fixtures for trader metric contract tests.

The fixture clock is fixed so rolling-window assertions never depend on the day
or timezone in which the suite runs.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
TODAY = NOW.strftime("%Y-%m-%d")


def days_ago(days: int, *, hour: int = 12) -> int:
    value = NOW - dt.timedelta(days=days)
    return int(value.replace(hour=hour).timestamp())


def day_ago(days: int) -> str:
    return (NOW - dt.timedelta(days=days)).strftime("%Y-%m-%d")


def trade(*, days: int, side: str, size: float, price: float,
          asset: str = "asset-a", condition: str = "condition-a"):
    return SimpleNamespace(
        timestamp=days_ago(days), side=side, size=size, price=price,
        usd_size=round(size * price, 8), asset=asset,
        condition_id=condition,
    )


def position(*, asset: str, condition: str, size: float, cash_pnl: float,
             cur_price: float, redeemable: bool):
    return SimpleNamespace(
        asset=asset, condition_id=condition, size=size, cash_pnl=cash_pnl,
        cur_price=cur_price, redeemable=redeemable,
    )


# A row-count ratio that looks like 400% despite only 0.4% of bought shares
# being sold. This deliberately proves the metric is not capital close rate.
ONE_LARGE_BUY_SEVERAL_SMALL_SELLS = [
    trade(days=2, side="BUY", size=1000, price=0.40, asset="whale"),
    *[
        trade(days=1, side="SELL", size=1, price=0.60, asset="whale")
        for _ in range(4)
    ],
]

# The BUY is outside fetched history, so this visible SELL has no known basis.
OLD_BUY_BASIS_OUTSIDE_FETCH = [
    trade(days=3, side="SELL", size=5, price=0.80, asset="old-basis"),
]

PARTIAL_SELL_TRADES = [
    trade(days=5, side="BUY", size=10, price=0.40, asset="partial"),
    trade(days=4, side="SELL", size=4, price=0.60, asset="partial"),
]
PARTIAL_SELL_REMAINDER = position(
    asset="partial", condition="condition-a", size=6, cash_pnl=0,
    cur_price=0.50, redeemable=False,
)

REDEEM_TRADES = [
    trade(days=8, side="BUY", size=10, price=0.30,
          asset="redeem", condition="redeem-condition"),
]
REDEEM_EVENTS = [{
    "timestamp": days_ago(6), "conditionId": "redeem-condition", "usdcSize": 10,
}]

# No fetched trade supplies a defensible resolution date. It is an observed
# realized result, but it must not be invented into a rolling UTC window.
RESOLVED_HOLDING_WITHOUT_RESOLUTION_DATE = position(
    asset="resolved-before-fetch", condition="resolved-condition", size=20,
    cash_pnl=12, cur_price=1.0, redeemable=True,
)

SIX_PROFITABLE_CLOSING_DAYS = [
    (day_ago(days), 10.0, True) for days in range(1, 7)
]
SEVEN_PROFITABLE_CLOSING_DAYS = [
    *SIX_PROFITABLE_CLOSING_DAYS,
    (day_ago(7), 10.0, True),
]
FLAT_AND_DIRECTIONAL_DAYS = [
    (day_ago(1), 5.0, True),
    (day_ago(2), 0.0, False),
    (day_ago(3), -2.0, False),
]

# A page-budget-limited activity walk reaching only 20 days: 7d is covered,
# while 30d and 90d necessarily have partial source coverage.
PARTIAL_30D_90D_TRADES = [
    trade(days=1, side="BUY", size=2, price=0.40, asset="coverage"),
    trade(days=20, side="BUY", size=2, price=0.40, asset="coverage-oldest"),
]

# Five round trips straddle the exact 30d and 90d cutoffs. Boundary outcomes
# are wins and next-older outcomes are losses, making both PnL and win-rate
# leakage observable. The boundary SELL rows are exactly NOW_TS - N*86400 and
# must be included; next-older SELL rows must be excluded from that window. The
# API can return
# the older rows on the same final fetched page, so fetch-bounded aggregates
# still include all five round trips and all ten activity rows.
OLDER_AND_BOUNDARY_TRADES = [
    trade(days=1, side="BUY", size=10, price=0.40, asset="recent"),
    trade(days=0, side="SELL", size=10, price=0.60, asset="recent"),
    trade(days=31, side="BUY", size=10, price=0.40, asset="boundary-30"),
    trade(days=30, side="SELL", size=10, price=0.60, asset="boundary-30"),
    trade(days=32, side="BUY", size=10, price=0.60, asset="older-30"),
    trade(days=31, side="SELL", size=10, price=0.40, asset="older-30"),
    trade(days=91, side="BUY", size=10, price=0.40, asset="boundary-90"),
    trade(days=90, side="SELL", size=10, price=0.60, asset="boundary-90"),
    trade(days=92, side="BUY", size=10, price=0.60, asset="older-90"),
    trade(days=91, side="SELL", size=10, price=0.40, asset="older-90"),
]

OFFICIAL_LEADERBOARD_PNL = SimpleNamespace(
    pnl=9876.54, vol=54321.0, user_name="fixture trader", x_username="fixture_x",
)
OFFICIAL_PNL_ABSENT = None
