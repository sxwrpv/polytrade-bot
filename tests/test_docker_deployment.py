from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]



def _caddy_directives() -> str:
    """Caddyfile with comment lines stripped.

    Assertions about what Caddy will DO must not trip over comments that
    legitimately name the thing being avoided (e.g. explaining why
    X-Frame-Options and HSTS preload are deliberately absent).
    """
    raw = (ROOT / "Caddyfile").read_text().splitlines()
    return "\n".join(l for l in raw if not l.strip().startswith("#"))

class DockerDeploymentContractTests(unittest.TestCase):
    def test_backend_runs_exactly_one_uvicorn_worker(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn('"--workers", "1"', dockerfile)

    def test_backend_port_is_not_published_by_compose(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertNotIn("8080:8080", compose)
        self.assertNotIn("8123:8080", compose)

    def test_services_restart_and_backend_has_healthcheck(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertGreaterEqual(compose.count("restart: unless-stopped"), 2)
        self.assertIn("healthcheck:", compose)

    def test_caddy_is_the_only_ingress(self):
        compose = (ROOT / "compose.yaml").read_text()
        caddyfile = (ROOT / "Caddyfile").read_text()
        self.assertIn('"80:80"', compose)
        self.assertIn('"443:443"', compose)
        self.assertIn("reverse_proxy app:8080", caddyfile)

    def test_caddy_allows_telegram_to_frame_the_mini_app(self):
        """Telegram Web/Desktop render Mini Apps in an iframe.

        X-Frame-Options: DENY blocks that with no visible error — the app just
        renders as a blank frame. frame-ancestors must permit Telegram and
        nobody else, and XFO must not reappear alongside it.
        """
        caddyfile = _caddy_directives()
        self.assertIn("frame-ancestors", caddyfile)
        self.assertIn("https://web.telegram.org", caddyfile)
        self.assertNotIn("X-Frame-Options", caddyfile)

    def test_caddy_serves_the_registered_domain_for_tls(self):
        """Telegram requires a trusted HTTPS URL; a bare IP cannot get a cert."""
        caddyfile = _caddy_directives()
        self.assertIn("polytradebot.live", caddyfile)
        self.assertIn("Strict-Transport-Security", caddyfile)
        # preload is effectively irreversible — keep it out
        self.assertNotIn("preload", caddyfile)

    def test_build_does_not_enable_global_prereleases(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("pip install --no-cache-dir --pre", dockerfile)

    def test_copy_engine_defaults_to_disabled(self):
        """An unconfigured deploy must not start trading.

        The value is interpolated so production can opt in via its own .env
        instead of hand-editing this file (a divergent compose.yaml meant a
        redeploy from git would silently stop the live engine). The default
        must still be off.
        """
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn('COPY_ENGINE_AUTOSTART: "${COPY_ENGINE_AUTOSTART:-0}"', compose)
        # ":-0" is the whole safety property — it is what makes an absent or
        # empty setting resolve to "engine off" rather than the app's own
        # autostart-on default.
        self.assertNotIn('COPY_ENGINE_AUTOSTART: "${COPY_ENGINE_AUTOSTART}"', compose)

    def test_build_context_excludes_secrets(self):
        dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertIn(".env", dockerignore)
        self.assertIn("*.pem", dockerignore)
        self.assertIn("*.db", dockerignore)


if __name__ == "__main__":
    unittest.main()


class ProxyHeaderContractTests(unittest.TestCase):
    """Caddy is the only peer the app ever sees, so without --proxy-headers
    every per-client rate limit collapses into one bucket and the access log
    records the proxy instead of the caller. Measured 2026-09-02: 4,882 of
    4,882 external requests logged as 172.20.0.3."""

    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text()

    def test_proxy_headers_are_enabled(self):
        self.assertIn("--proxy-headers", self.dockerfile)

    def test_forwarded_allow_ips_is_configurable(self):
        self.assertIn("FORWARDED_ALLOW_IPS", self.dockerfile)


class DeployVerificationTests(unittest.TestCase):
    """The screener commit reached GitHub on 27 Aug at 11:25 UTC while the
    running container had started at 10:02. Nothing could report that, so the
    deploy path has to prove the revision it landed."""

    def setUp(self):
        self.script = (ROOT / "scripts" / "deploy.sh").read_text()
        self.dockerfile = (ROOT / "Dockerfile").read_text()
        self.screener_dockerfile = (ROOT / "trader-screener" / "Dockerfile").read_text()

    def test_both_images_are_stamped_with_the_revision(self):
        for name, text in (("app", self.dockerfile),
                           ("screener", self.screener_dockerfile)):
            self.assertIn("ARG GIT_REVISION", text, name)
            self.assertIn("org.opencontainers.image.revision", text, name)

    def test_deploy_builds_from_an_explicit_commit(self):
        self.assertIn("git rev-parse --verify", self.script)
        self.assertIn("--build-arg", self.script)
        self.assertIn("GIT_REVISION=$REVISION", self.script)

    def test_the_caddyfile_is_validated_before_anything_changes(self):
        """A bad matcher is a total outage, and `caddy reload` reports success
        on a stale inode — so this gate must precede the recreate."""
        validate_at = self.script.index("caddy validate")
        recreate_at = self.script.index("up -d --force-recreate")
        self.assertLess(validate_at, recreate_at)

    def test_deploy_refuses_to_silently_disable_the_engine(self):
        self.assertIn("COPY_ENGINE_AUTOSTART=1", self.script)

    def test_verification_checks_readiness_not_just_liveness(self):
        self.assertIn("/api/ready", self.script)
        self.assertIn("/api/version", self.script)
        self.assertIn("/screener/api/health", self.script)

    def test_verification_requires_live_data_mode(self):
        self.assertIn("dataMode", self.script)
        self.assertIn("expected live", self.script)

    def test_a_failed_verification_rolls_back(self):
        self.assertIn("rolling back", self.script)
        self.assertIn("PREVIOUS_IMAGE", self.script)

    def test_caddy_access_log_is_rotated(self):
        caddyfile = (ROOT / "Caddyfile").read_text()
        self.assertIn("roll_size", caddyfile)
        self.assertIn("roll_keep", caddyfile)

    def test_scanner_paths_are_refused_at_the_edge(self):
        caddyfile = (ROOT / "Caddyfile").read_text()
        self.assertIn("@hidden", caddyfile)
        for probe in ("/.env", "/.git", "/wp-admin", "/config.php"):
            self.assertIn(probe, caddyfile, probe)
        self.assertIn("respond 404", caddyfile)
