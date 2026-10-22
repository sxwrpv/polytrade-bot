from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from backend.api.routes_traders import FollowBody, FollowSettings
from backend.config import DEFAULT_CAPITAL_MULTIPLIER, DEFAULT_MAX_POSITION_USD


class RiskSettingsContractTests(unittest.TestCase):
    def test_backend_default_matches_visible_wallet_default(self):
        self.assertEqual(15.0, DEFAULT_MAX_POSITION_USD)
        self.assertEqual(15.0, FollowBody().max_position_usd)

    def test_rejects_inverted_price_bracket(self):
        with self.assertRaises(ValidationError):
            FollowSettings(min_price=0.90, max_price=0.10)

    def test_rejects_negative_limits(self):
        invalid = (
            {"copy_ratio_pct": -1},
            {"max_position_usd": -1},
            {"max_total_exposure_usd": -1},
            {"daily_loss_limit_usd": -1},
            {"max_open_positions": -1},
            {"min_price": -0.01},
            {"max_price": 1.01},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                FollowSettings(**values)

    def test_wallet_ui_names_position_cap_and_allows_small_account_limits(self):
        source = (Path(__file__).parents[1] / "frontend" / "src" /
                  "components" / "WalletRiskCard.jsx").read_text()
        # Labels moved to sentence case with the 2026-10-20 design pass (the
        # whole Mini App dropped all-caps controls); the names and the
        # small-account bounds they guard are unchanged.
        self.assertIn("'Max per position'", source)
        self.assertIn("'max_total_exposure_usd', 'Max total exposure', 0, 5000, 5", source)
        self.assertIn("'daily_loss_limit_usd', 'Daily loss limit', 0, 1000, 1", source)
        self.assertIn("Risk warning:", source)

    def test_sizing_mode_takes_the_two_modes_only(self):
        for mode in ("position_share", "capital_share"):
            self.assertEqual(mode, FollowSettings(sizing_mode=mode).sizing_mode)
        for junk in ("equity", "", "CAPITAL_SHARE"):
            with self.subTest(junk=junk), self.assertRaises(ValidationError):
                FollowSettings(sizing_mode=junk)

    def test_capital_multiplier_bounds_match_the_slider(self):
        for ok in (0.1, 1, 100):
            self.assertEqual(ok, FollowSettings(capital_multiplier=ok).capital_multiplier)
        for bad in (0, 0.05, 100.5, -1, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                FollowSettings(capital_multiplier=bad)

    def test_wallet_ui_offers_capital_share_with_the_api_bounds_and_default(self):
        root = Path(__file__).parents[1] / "frontend" / "src"
        card = (root / "components" / "WalletRiskCard.jsx").read_text()
        self.assertIn("'capital_multiplier', 'Multiplier', 0.1, 100", card)
        self.assertIn("'Their cash share'", card)
        self.assertIn("capital_multiplier: 1,", card)
        self.assertEqual(1.0, DEFAULT_CAPITAL_MULTIPLIER)
        sizing = (root / "copySizing.js").read_text()
        stops = sizing.split("MULTIPLIER_STOPS = [", 1)[1].split("]", 1)[0]
        values = [float(v) for v in stops.replace("\n", " ").split(",") if v.strip()]
        self.assertEqual((0.1, 100.0), (values[0], values[-1]))
        self.assertEqual(sorted(values), values)

    def test_exchange_sdk_is_exactly_pinned(self):
        requirements = (Path(__file__).parents[1] / "requirements.txt").read_text()
        self.assertIn("polymarket-client==0.6.0", requirements)
        self.assertNotIn("polymarket-client>=", requirements)


if __name__ == "__main__":
    unittest.main()
