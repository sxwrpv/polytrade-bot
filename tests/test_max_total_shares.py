import unittest
from types import SimpleNamespace

from backend.api.routes_traders import FollowSettings
from backend.core.copy_engine import plan_actions


def position(*, size=100.0, price=0.5):
    return SimpleNamespace(
        asset="token", condition_id="condition", size=size,
        current_value=size * price, cur_price=price, avg_price=price,
        redeemable=False, outcome="YES",
    )


class MaxTotalSharesPlanningTests(unittest.TestCase):
    def test_open_is_limited_to_configured_share_cap(self):
        actions = plan_actions(
            [position()], [], {"max_position_usd": 100.0}, 100.0, 100.0,
            ratio_pct=100.0, max_total_shares=10.0,
        )

        self.assertEqual(1, len(actions))
        self.assertEqual("open", actions[0].kind)
        self.assertAlmostEqual(5.0, actions[0].amount)

    def test_resize_increase_only_fills_remaining_share_capacity(self):
        row = {
            "id": "position", "token_id": "token", "shares": 8.0,
            "trader_shares": 50.0, "notional_usd": 4.0,
        }
        actions = plan_actions(
            [position(size=100.0)], [row], {"max_position_usd": 100.0},
            100.0, 100.0, ratio_pct=100.0, max_total_shares=10.0,
            min_notional=0.1,
        )

        self.assertEqual(1, len(actions))
        self.assertEqual("resize", actions[0].kind)
        self.assertAlmostEqual(1.0, actions[0].amount)

    def test_no_buy_is_planned_after_share_cap_is_reached(self):
        row = {
            "id": "position", "token_id": "token", "shares": 10.0,
            "trader_shares": 50.0, "notional_usd": 5.0,
        }
        actions = plan_actions(
            [position(size=100.0)], [row], {"max_position_usd": 100.0},
            100.0, 100.0, ratio_pct=100.0, max_total_shares=10.0,
            min_notional=0.1,
        )

        self.assertEqual([], actions)


class MaxTotalSharesSettingsTests(unittest.TestCase):
    def test_setting_accepts_zero_as_unlimited(self):
        self.assertEqual(0.0, FollowSettings(max_total_shares=0).max_total_shares)

    def test_setting_rejects_negative_cap(self):
        with self.assertRaises(ValueError):
            FollowSettings(max_total_shares=-1)


if __name__ == "__main__":
    unittest.main()
