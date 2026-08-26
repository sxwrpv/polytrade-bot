from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCREENER = ROOT / "trader-screener"


class TraderScreenerProductionContractTests(unittest.TestCase):
    def test_standalone_screener_is_part_of_the_polytrade_repository(self):
        self.assertTrue((SCREENER / "server.mjs").is_file())
        self.assertTrue((SCREENER / "data" / "dataset.json").is_file())
        self.assertTrue((SCREENER / "public" / "index.html").is_file())

    def test_compose_runs_the_screener_as_a_non_trading_service(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("trader-screener:", compose)
        self.assertIn("polytrade-trader-screener:local", compose)
        block = self._service_block(compose, "trader-screener")
        self.assertIn("context: ./trader-screener", block)
        self.assertIn("dockerfile: Dockerfile", block)
        self.assertNotIn("COPY_ENGINE_AUTOSTART", block)

    def test_services_are_network_isolated_behind_caddy(self):
        compose = (ROOT / "compose.yaml").read_text()
        app = self._service_block(compose, "app")
        screener = self._service_block(compose, "trader-screener")
        caddy = self._service_block(compose, "caddy")
        self.assertIn("- backend", app)
        self.assertNotIn("- screener", app)
        self.assertIn("- screener", screener)
        self.assertNotIn("- backend", screener)
        self.assertNotIn("- edge", screener)
        self.assertIn("- backend", caddy)
        self.assertIn("- screener", caddy)
        self.assertIn("- edge", caddy)

    def test_caddy_replaces_the_old_screener_route_with_the_new_service(self):
        caddy = (ROOT / "Caddyfile").read_text()
        self.assertIn("handle_path /screener/*", caddy)
        self.assertIn("reverse_proxy trader-screener:4310", caddy)
        self.assertIn("redir /screener /screener/", caddy)

    def test_public_assets_and_api_are_scoped_under_screener(self):
        index = (SCREENER / "public" / "index.html").read_text()
        wallet = (SCREENER / "public" / "trader.html").read_text()
        data_source = (SCREENER / "public" / "lib" / "dataSource.js").read_text()
        server = (SCREENER / "server.mjs").read_text()
        board = (SCREENER / "public" / "board.js").read_text()
        self.assertIn('href="/screener/tokens.css"', index)
        self.assertIn('src="/screener/board.js"', index)
        self.assertIn('href="/screener/wallet.css"', wallet)
        self.assertIn("'/screener/api'", data_source)
        self.assertIn("startsWith('/screener/')", server)
        self.assertNotIn("`/trader/${", board)
        self.assertIn("location.href = M.traderPath(direct)", board)

    def test_public_service_has_bounded_abuse_guards_and_static_containment(self):
        server = (SCREENER / "server.mjs").read_text()
        self.assertIn("canonicalClientIdentity(req)", server)
        self.assertIn("new TtlLruCache({ max: 16", server)
        self.assertIn("new ConcurrencyGate(2)", server)
        self.assertIn("profileInflight.run", server)
        self.assertIn("LIVE_PROFILE_DEADLINE_MS = 30_000", server)
        self.assertIn("pm.activity(wallet, options)", server)
        self.assertIn("datasetLimiter.enforce", server)
        self.assertIn("path.startsWith(`${PUBLIC_ROOT}${sep}`)", server)

    def test_saved_drawer_hidden_state_survives_author_display_rules(self):
        css = (SCREENER / "public" / "glass.css").read_text()
        self.assertIn(".drawer[hidden], .drawer-scrim[hidden] { display: none !important; }", css)

    def test_frozen_category_boards_are_labelled_as_archived(self):
        board = (SCREENER / "public" / "board.js").read_text()
        self.assertIn("ds.meta.boardsFrozen", board)
        self.assertIn("ds.meta.boardsAsOf", board)
        self.assertIn("archived snapshot frozen as of", board)
        self.assertIn("they are historical research, not current rankings", board)

    @staticmethod
    def _service_block(compose: str, name: str) -> str:
        lines = compose.splitlines()
        marker = f"  {name}:"
        start = lines.index(marker)
        end = len(lines)
        for index in range(start + 1, len(lines)):
            line = lines[index]
            if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
                end = index
                break
        return "\n".join(lines[start:end])


if __name__ == "__main__":
    unittest.main()
