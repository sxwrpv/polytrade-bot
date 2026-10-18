import base64
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
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


def _site_block(address: str) -> str:
    """One top-level site block of the Caddyfile, comment lines stripped."""
    lines = _caddy_directives().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(address))
    depth, block = 0, []
    for line in lines[start:]:
        block.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return "\n".join(block)


def _parse_policy(policy: str) -> dict[str, list[str]]:
    directives = {}
    for part in policy.split(";"):
        if part.strip():
            name, *sources = part.split()
            directives[name] = sources
    return directives


def _csp_policies() -> dict[str, dict[str, list[str]]]:
    """{matcher: {directive: sources}} for every CSP the domain block sets."""
    found = re.findall(r'header (@\w+) \{\s*Content-Security-Policy "([^"]*)"',
                       _site_block("polytradebot.live"))
    return {matcher: _parse_policy(policy) for matcher, policy in found}


def _allows(sources: list[str], url: str) -> bool:
    """CSP source matching, reduced to the forms these policies use: 'self'
    for same-origin paths, data:, and https:// sources naming an origin, a
    directory (trailing slash) or one exact file."""
    if url.startswith("/") and not url.startswith("//"):
        return "'self'" in sources
    if url.startswith("data:"):
        return "data:" in sources
    resource = url.split("#")[0].split("?")[0]
    for source in sources:
        if not source.startswith("https://"):
            continue
        if "/" not in source[len("https://"):]:
            if resource == source or resource.startswith(source + "/"):
                return True
        elif resource == source or (source.endswith("/") and resource.startswith(source)):
            return True
    return False


def _sha256_source(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


class _PageResources(HTMLParser):
    """What an HTML document makes the browser load or apply."""

    def __init__(self, html: str):
        super().__init__()
        self.scripts, self.inline_scripts = [], []
        self.stylesheets, self.inline_styles, self.style_attributes = [], [], []
        self.images = []
        self._inline = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "style" in attrs:
            self.style_attributes.append(attrs["style"])
        rel = set((attrs.get("rel") or "").split())
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        elif tag in ("script", "style"):
            self._inline = (tag, [])
        elif tag == "link" and "stylesheet" in rel:
            self.stylesheets.append(attrs["href"])
        elif tag == "link" and rel & {"icon", "apple-touch-icon"}:
            self.images.append(attrs["href"])
        elif tag == "img" and attrs.get("src"):
            self.images.append(attrs["src"])

    def handle_data(self, data):
        if self._inline:
            self._inline[1].append(data)

    def handle_endtag(self, tag):
        if self._inline and tag == self._inline[0]:
            target = self.inline_scripts if tag == "script" else self.inline_styles
            target.append("".join(self._inline[1]))
            self._inline = None

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


class ContentSecurityPolicyTests(unittest.TestCase):
    """The session cookie is SameSite=Strict, which does nothing against
    script on this origin, and the screener renders third-party Polymarket
    data on it. A policy that refuses a page's own assets fails silently in
    production -- a blank Mini App inside Telegram -- so every shipped page
    is checked against the policy it is served with."""

    SURFACES = ("@csp_app", "@csp_swagger", "@csp_redoc", "@csp_screener")

    def setUp(self):
        self.domain = _site_block("polytradebot.live")
        self.policies = _csp_policies()

    def test_every_path_gets_exactly_one_policy(self):
        self.assertEqual(sorted(self.policies), sorted(self.SURFACES))
        # The site-wide header block must not add a fifth, stacked policy.
        self.assertEqual(_caddy_directives().count("Content-Security-Policy"),
                         len(self.SURFACES))
        matchers = {
            name: (negated == "not ", patterns.split())
            for name, negated, patterns in re.findall(
                r"^\s*(@csp_\w+) (not )?path (.+)$", self.domain, re.M)
        }
        self.assertEqual(sorted(matchers), sorted(self.SURFACES))

        def matches(name, path):
            negated, patterns = matchers[name]
            hit = any(path.startswith(p[:-1]) if p.endswith("*") else path == p
                      for p in patterns)
            return hit != negated

        expected = {
            "/": "@csp_app",
            "/assets/index-3f9a1c.js": "@csp_app",
            "/docs": "@csp_app",
            "/docs/system-design": "@csp_app",
            "/api/user/me": "@csp_app",
            "/api/openapi.json": "@csp_app",
            "/api/docs": "@csp_swagger",
            "/api/redoc": "@csp_redoc",
            "/screener": "@csp_screener",
            "/screener/": "@csp_screener",
            "/screener/trader/0x" + "ab" * 20: "@csp_screener",
            "/screener/api/health": "@csp_screener",
            "/screenerish": "@csp_app",
        }
        for path, surface in expected.items():
            self.assertEqual([n for n in matchers if matches(n, path)], [surface], path)

    def test_policies_replace_rather_than_stack_on_upstream_headers(self):
        for name in self.SURFACES:
            block = re.search(rf"header {name} \{{([^}}]*)\}}", self.domain)
            self.assertIsNotNone(block, name)
            self.assertIn("defer", block.group(1), name)

    def test_scripts_are_limited_to_self_and_the_telegram_sdk(self):
        self.assertEqual(self.policies["@csp_app"]["script-src"],
                         ["'self'", "https://telegram.org/js/telegram-web-app.js"])
        self.assertEqual(self.policies["@csp_screener"]["script-src"], ["'self'"])
        for name, policy in self.policies.items():
            for loose in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'",
                          "*", "https:", "data:", "blob:"):
                self.assertNotIn(loose, policy["script-src"], f"{name} {loose}")

    def test_jsdelivr_is_allowed_only_per_file_on_the_api_reference(self):
        """jsDelivr serves any npm package. Allowing the whole origin anywhere
        on this site would let injected markup load attacker-published code."""
        for name, policy in self.policies.items():
            for directive, sources in policy.items():
                for source in sources:
                    if source.startswith("https://cdn.jsdelivr.net"):
                        self.assertIn(name, ("@csp_swagger", "@csp_redoc"), directive)
                        self.assertTrue(source.endswith((".js", ".css")), source)

    def test_every_policy_keeps_telegram_framing_and_the_baseline(self):
        for name, policy in self.policies.items():
            self.assertEqual(policy["frame-ancestors"],
                             ["'self'", "https://web.telegram.org", "https://*.telegram.org"],
                             name)
            self.assertEqual(policy["default-src"], ["'self'"], name)
            self.assertEqual(policy["object-src"], ["'none'"], name)
            self.assertEqual(policy["base-uri"], ["'none'"], name)
            self.assertEqual(policy["form-action"], ["'self'"], name)
            if name != "@csp_redoc":
                self.assertNotIn("'unsafe-inline'", policy["style-src"], name)

    def test_shipped_pages_load_only_what_their_policy_allows(self):
        pages = {
            "frontend/index.html": "@csp_app",
            "docs/site/index.html": "@csp_app",
            # Injected into the docs shell with innerHTML.
            "docs/site/system-design.html": "@csp_app",
            "trader-screener/public/index.html": "@csp_screener",
            "trader-screener/public/trader.html": "@csp_screener",
        }
        for path, surface in pages.items():
            self.assertPageAllowed(path, (ROOT / path).read_text(), self.policies[surface])

    def test_api_reference_shells_load_only_what_their_policy_allows(self):
        """Also the guard on the Swagger hash: FastAPI renders the inline
        SwaggerUIBundle({...}) bootstrap, and requirements.txt does not pin
        FastAPI, so a template change shows up here first."""
        from fastapi.testclient import TestClient
        from backend.main import app

        client = TestClient(app)
        for route, surface in (("/api/docs", "@csp_swagger"), ("/api/redoc", "@csp_redoc")):
            response = client.get(route)
            self.assertEqual(response.status_code, 200, route)
            self.assertPageAllowed(route, response.text, self.policies[surface])

    def test_screener_images_from_its_data_are_allowed(self):
        """Avatars and market icons arrive in Polymarket data at runtime, so no
        HTML names them, and a refused image degrades quietly to initials."""
        snapshot = (ROOT / "trader-screener" / "data" / "dataset.json").read_text()
        images = set(re.findall(r'"img":"([^"]+)"', snapshot))
        self.assertTrue(images)
        for url in images:
            self.assertTrue(_allows(self.policies["@csp_screener"]["img-src"], url), url)

    def test_docs_and_screener_scripts_write_no_style_attributes(self):
        """style-src has no 'unsafe-inline' there, so a style="" attribute
        built in JavaScript is dropped without an error. Use a class, or
        element.style (the CSSOM), which CSP does not restrict."""
        scripts = [*sorted((ROOT / "docs" / "site").glob("*.js")),
                   *sorted((ROOT / "trader-screener" / "public").rglob("*.js"))]
        self.assertTrue(scripts)
        for script in scripts:
            self.assertNotRegex(script.read_text(),
                                r"""style=["'\\]|setAttribute\(\s*['"]style['"]""",
                                script.name)

    def assertPageAllowed(self, name, html, policy):
        page = _PageResources(html)
        fallback = policy["default-src"]
        scripts = policy.get("script-src", fallback)
        styles = policy.get("style-src", fallback)
        images = policy.get("img-src", fallback)
        for url in page.scripts:
            self.assertTrue(_allows(scripts, url), f"{name}: script {url}")
        for body in page.inline_scripts:
            self.assertIn(_sha256_source(body), scripts, f"{name}: inline script")
        for url in page.stylesheets:
            self.assertTrue(_allows(styles, url), f"{name}: stylesheet {url}")
            if url.startswith("https://fonts.googleapis.com/"):
                self.assertIn("https://fonts.gstatic.com", policy.get("font-src", fallback), name)
        if "'unsafe-inline'" not in styles:
            self.assertEqual(page.inline_styles, [], f"{name}: inline <style>")
            self.assertEqual(page.style_attributes, [], f"{name}: style= attributes")
        for url in page.images:
            self.assertTrue(_allows(images, url), f"{name}: image {url}")


class PlainHttpIpBlockTests(unittest.TestCase):
    """The IP block exists so deploy verification does not depend on DNS or
    a certificate. It used to proxy the whole site, Telegram login and
    authenticated /api calls included, over plain HTTP."""

    ALLOWED = {"/api/health", "/api/ready", "/api/version", "/screener/api/health"}

    def setUp(self):
        self.block = _site_block("http://52.51.200.58")

    def test_only_the_health_endpoints_are_proxied(self):
        self.assertIn("@health path /api/health /api/ready /api/version", self.block)
        self.assertIn("handle /screener/api/health {", self.block)
        self.assertIn("uri strip_prefix /screener", self.block)
        self.assertEqual(self.block.count("reverse_proxy"), 2)
        self.assertIn("respond 404", self.block)
        for catch_all in ("handle_path", "redir", "/screener/*"):
            self.assertNotIn(catch_all, self.block)

    def test_every_path_the_scripts_verify_through_it_is_allowed(self):
        for script in ("deploy.sh", "refresh-screener-data.sh"):
            text = (ROOT / "scripts" / script).read_text()
            self.assertIn('BASE_URL="${BASE_URL:-http://52.51.200.58}"', text, script)
            paths = set(re.findall(r"\$BASE_URL(/[\w/.-]*)", text))
            self.assertTrue(paths, script)
            self.assertLessEqual(paths, self.ALLOWED, script)


class RequestBodyLimitTests(unittest.TestCase):
    def test_api_request_bodies_are_capped_at_the_edge(self):
        """Nothing upstream bounds a request body, and every /api write is a
        small JSON object; signed Telegram initData, the largest, is a few KB."""
        domain = _site_block("polytradebot.live")
        self.assertIn("@api path /api/*", domain)
        size = re.search(r"request_body @api \{\s*max_size (\d+)KB\s*\}", domain)
        self.assertIsNotNone(size)
        self.assertTrue(16 <= int(size.group(1)) <= 1024, size.group(0))
