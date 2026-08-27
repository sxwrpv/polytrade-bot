from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from backend.api.routes_traders import FollowBody, FollowSettings
from backend.config import DEFAULT_MAX_POSITION_USD


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
        self.assertIn("'MAX / POSITION'", source)
        self.assertIn("'max_total_exposure_usd', 'MAX EXPOSURE', 0, 5000, 5", source)
        self.assertIn("'daily_loss_limit_usd', 'DAILY LOSS LIMIT', 0, 1000, 1", source)
        self.assertIn("Risk warning:", source)

    def test_exchange_sdk_is_exactly_pinned(self):
        requirements = (Path(__file__).parents[1] / "requirements.txt").read_text()
        self.assertIn("polymarket-client==0.6.0", requirements)
        self.assertNotIn("polymarket-client>=", requirements)


if __name__ == "__main__":
    unittest.main()
