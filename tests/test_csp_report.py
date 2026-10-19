"""POST /api/csp-report turns each CSP refusal into one sanitized log line.

Production had no other trace of a refused asset: the browser drops it and
the page degrades, which inside Telegram is a blank Mini App. The endpoint is
anonymous by necessity (browsers send reports on their own), so these tests
pin what it accepts, what it refuses, and above all what it never logs.
"""
from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from backend.api import routes_csp_report
from backend.main import app

# The Caddy container hop: a trusted peer, so X-Forwarded-For names the caller.
PROXY = ("172.20.0.3", 50000)
WALLET = "0x" + "ab" * 20
TELEGRAM_ID = "5123456789"


def _report_uri_body(**overrides) -> str:
    """A report-uri (application/csp-report) body shaped like Chromium's."""
    report = {
        "document-uri": "https://polytradebot.live/docs",
        "referrer": "",
        "violated-directive": "img-src",
        "effective-directive": "img-src",
        "original-policy": "default-src 'self'; img-src 'self'",
        "disposition": "enforce",
        "blocked-uri": "https://cdn.example.net/pixel.png",
        "line-number": 12,
        "column-number": 7,
        "source-file": "https://polytradebot.live/docs/assets/app.js",
        "status-code": 200,
        "script-sample": "",
    }
    report.update(overrides)
    return json.dumps({"csp-report": report})


def _reporting_api_body(*bodies, extra=()) -> str:
    """A Reporting API (application/reports+json) batch."""
    return json.dumps([
        *({"age": 10, "type": "csp-violation", "url": body.get("documentURL", ""),
           "user_agent": "Mozilla/5.0 (Linux; Android 14) Telegram-Android/11.2", "body": body}
          for body in bodies),
        *extra,
    ])


class CspReportEndpointTests(unittest.TestCase):
    def setUp(self):
        routes_csp_report.reset_rate_limits()
        self.client = TestClient(app, client=PROXY)

    def tearDown(self):
        routes_csp_report.reset_rate_limits()

    def post(self, body, content_type="application/csp-report", ip="203.0.113.9", headers=None):
        return self.client.post("/api/csp-report", content=body, headers={
            "content-type": content_type, "x-forwarded-for": ip, **(headers or {})})

    def logged(self, body, content_type="application/csp-report", **kwargs):
        """POST, returning the response and every csp WARNING it logged."""
        with self.assertLogs("csp", level="WARNING") as captured:
            response = self.post(body, content_type, **kwargs)
        return response, [record.getMessage() for record in captured.records]

    # --- the two wire formats ---------------------------------------------

    def test_a_report_uri_report_becomes_one_line(self):
        response, lines = self.logged(_report_uri_body())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(lines, [
            "CSP violation directive=img-src blocked=https://cdn.example.net document=/docs "
            "source=https://polytradebot.live disposition=enforce via=report-uri",
        ])

    def test_a_reporting_api_batch_logs_each_csp_report_and_skips_other_types(self):
        body = _reporting_api_body(
            {"documentURL": "https://polytradebot.live/", "referrer": "",
             "effectiveDirective": "script-src-elem", "blockedURL": "https://evil.example.org/x.js",
             "sourceFile": "https://polytradebot.live/assets/index-3f9a1c.js",
             "originalPolicy": "script-src 'self'", "disposition": "enforce", "sample": "",
             "statusCode": 200, "lineNumber": 1, "columnNumber": 2},
            {"documentURL": "https://polytradebot.live/screener/", "effectiveDirective": "style-src-elem",
             "blockedURL": "inline", "disposition": "report"},
            extra=[{"type": "deprecation", "url": "https://polytradebot.live/",
                    "body": {"id": "x", "message": "y"}}],
        )
        response, lines = self.logged(body, "application/reports+json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(lines, [
            "CSP violation directive=script-src-elem blocked=https://evil.example.org document=/ "
            "source=https://polytradebot.live disposition=enforce via=report-to",
            "CSP violation directive=style-src-elem blocked=inline document=/screener/ "
            "source=- disposition=report via=report-to",
        ])

    def test_content_type_parameters_and_plain_json_are_accepted(self):
        response, _ = self.logged(_report_uri_body(), "application/csp-report; charset=utf-8")
        self.assertEqual(response.status_code, 204)
        response, lines = self.logged(_report_uri_body(), "application/json")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(lines[0].endswith("via=report-uri"), lines)
        batch = _reporting_api_body({"effectiveDirective": "img-src", "blockedURL": "data"})
        response, lines = self.logged(batch, "application/json")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(lines[0].endswith("via=report-to"), lines)

    def test_identical_reports_in_a_batch_collapse_into_one_line(self):
        same = {"documentURL": "https://polytradebot.live/docs", "effectiveDirective": "img-src",
                "blockedURL": "data"}
        response, lines = self.logged(_reporting_api_body(same, same, same), "application/reports+json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(lines, [
            "CSP violation directive=img-src blocked=data document=/docs source=- "
            "disposition=- via=report-to count=3",
        ])

    def test_a_large_batch_logs_a_bounded_number_of_lines_and_says_so(self):
        extra = 5
        bodies = [{"documentURL": "https://polytradebot.live/docs", "effectiveDirective": "img-src",
                   "blockedURL": f"https://host{i}.example.net/x.png"}
                  for i in range(routes_csp_report.MAX_LINES_PER_REQUEST + extra)]
        response, lines = self.logged(_reporting_api_body(*bodies), "application/reports+json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(len(lines), routes_csp_report.MAX_LINES_PER_REQUEST + 1)
        self.assertEqual(lines[-1],
                         f"CSP violation: {extra} more distinct report(s) in this request not logged")

    def test_a_batch_without_csp_reports_is_accepted_silently(self):
        body = json.dumps([{"type": "deprecation", "body": {"id": "x"}}])
        with self.assertNoLogs("csp", level="WARNING"):
            self.assertEqual(self.post(body, "application/reports+json").status_code, 204)

    def test_unusable_items_in_a_batch_are_skipped(self):
        body = json.dumps([
            1, "x", None, {"type": "csp-violation", "body": "img-src"}, {"type": "csp-violation"},
            {"type": "csp-violation", "body": {"effectiveDirective": "font-src",
                                               "blockedURL": "https://fonts.example.net/a.woff2",
                                               "documentURL": "https://polytradebot.live/docs"}},
        ])
        response, lines = self.logged(body, "application/reports+json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(lines, [
            "CSP violation directive=font-src blocked=https://fonts.example.net document=/docs "
            "source=- disposition=- via=report-to",
        ])

    def test_fields_of_the_wrong_type_count_as_absent(self):
        body = json.dumps({"csp-report": {
            "document-uri": 42, "effective-directive": ["img-src"], "blocked-uri": None,
            "source-file": {"x": 1}, "disposition": True}})
        response, lines = self.logged(body)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(lines, [
            "CSP violation directive=unknown blocked=- document=- source=- disposition=- via=report-uri",
        ])

    def test_the_endpoint_is_not_advertised_in_the_api_schema(self):
        """A browser-only sink, not an integration API."""
        paths = self.client.get("/api/openapi.json").json()["paths"]
        self.assertNotIn("/api/csp-report", paths)

    # --- refusals -----------------------------------------------------------

    def test_each_client_address_has_its_own_rate_limit(self):
        with self.assertLogs("csp", level="WARNING"):
            for _ in range(routes_csp_report.REPORT_RATE_LIMIT):
                self.assertEqual(self.post(_report_uri_body()).status_code, 204)
        with self.assertNoLogs("csp", level="WARNING"):
            refused = self.post(_report_uri_body())
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.headers["retry-after"],
                         str(int(routes_csp_report.REPORT_RATE_WINDOW_SECONDS)))
        # Another caller behind the same proxy keeps its own budget.
        response, _ = self.logged(_report_uri_body(), ip="198.51.100.4")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(sorted(routes_csp_report._requests), ["198.51.100.4", "203.0.113.9"])

    def test_malformed_bodies_are_refused_without_a_log_line(self):
        cases = {
            "not json": (b"not json", "application/csp-report"),
            "empty": (b"", "application/csp-report"),
            "invalid utf-8": (b'{"csp-report": {"document-uri": "\xff"}}', "application/csp-report"),
            "no csp-report key": (b'{"report": {}}', "application/csp-report"),
            "csp-report not an object": (b'{"csp-report": "img-src"}', "application/csp-report"),
            "a list sent as report-uri": (b"[]", "application/csp-report"),
            "an object sent as reports+json": (_report_uri_body().encode(), "application/reports+json"),
            "a bare scalar": (b"42", "application/json"),
            "nested past the parser's limit": (b"[" * 20000 + b"]" * 20000, "application/reports+json"),
        }
        for name, (body, content_type) in cases.items():
            with self.subTest(name), self.assertNoLogs("csp", level="WARNING"):
                self.assertEqual(self.post(body, content_type).status_code, 400)

    def test_other_media_types_are_refused(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
            with self.subTest(content_type), self.assertNoLogs("csp", level="WARNING"):
                self.assertEqual(self.post(_report_uri_body(), content_type).status_code, 415)

    def test_bodies_over_the_cap_are_refused_with_or_without_a_length(self):
        cap = routes_csp_report.MAX_BODY_BYTES
        padding = cap - len(_report_uri_body(**{"script-sample": ""}).encode())
        exact = _report_uri_body(**{"script-sample": "x" * padding}).encode()
        self.assertEqual(len(exact), cap)
        response, _ = self.logged(exact)
        self.assertEqual(response.status_code, 204)

        oversized = _report_uri_body(**{"script-sample": "x" * (padding + 1)}).encode()

        def chunks():   # no Content-Length: the stream is cut off as it arrives
            for start in range(0, len(oversized), 4096):
                yield oversized[start:start + 4096]

        with self.assertNoLogs("csp", level="WARNING"):
            self.assertEqual(self.post(oversized).status_code, 413)
            self.assertEqual(self.post(chunks()).status_code, 413)

    # --- what reaches the log -----------------------------------------------

    def test_nothing_identifying_reaches_the_log(self):
        """Cookies, query strings, fragments, credentials, user ids and wallet
        addresses stay out; the line still says which page refused what."""
        document = (f"https://polytradebot.live/screener/trader/{WALLET}/{TELEGRAM_ID}"
                    f"?tgWebAppData=user%3D{TELEGRAM_ID}&q=SECRET-QUERY#SECRET-FRAGMENT")
        blocked = "https://user:SECRET-PASSWORD@evil.example.org:8443/steal?c=SECRET-BLOCKED-QUERY"
        source = "https://polytradebot.live/assets/index.js?session=SECRET-SOURCE"
        bodies = {
            "report-uri": (_report_uri_body(**{
                "document-uri": document, "blocked-uri": blocked, "source-file": source,
                "referrer": "https://t.me/cpolytrade_bot?start=SECRET-REFERRER",
                "original-policy": "default-src 'self'; SECRET-POLICY",
                "script-sample": "SECRET-SAMPLE"}), "application/csp-report"),
            "report-to": (_reporting_api_body({
                "documentURL": document, "blockedURL": blocked, "sourceFile": source,
                "effectiveDirective": "img-src", "disposition": "enforce",
                "referrer": "https://t.me/cpolytrade_bot?start=SECRET-REFERRER",
                "originalPolicy": "default-src 'self'; SECRET-POLICY", "sample": "SECRET-SAMPLE"}),
                "application/reports+json"),
        }
        for via, (body, content_type) in bodies.items():
            with self.subTest(via):
                response, lines = self.logged(body, content_type, headers={
                    "cookie": "polytrade_session=SECRET-COOKIE",
                    "user-agent": "Mozilla/5.0 SECRET-UA"})
                self.assertEqual(response.status_code, 204)
                self.assertEqual(lines, [
                    "CSP violation directive=img-src blocked=https://evil.example.org:8443 "
                    "document=/screener/trader/{id}/{id} source=https://polytradebot.live "
                    f"disposition=enforce via={via}",
                ])
                for secret in ("SECRET", WALLET, WALLET[2:], TELEGRAM_ID, "tgWebAppData",
                               "user:", "Mozilla", "Telegram-Android"):
                    self.assertNotIn(secret, lines[0])

    def test_a_forged_report_cannot_split_or_forge_log_lines(self):
        body = _report_uri_body(**{
            "effective-directive": "img-src\n2026-10-19 00:00:00,000 CRITICAL main | forged",
            "blocked-uri": "https://evil.example.org/\r\nCRITICAL forged",
            "document-uri": "https://polytradebot.live/docs/ x\x1b[31m",
            "source-file": "javascript:alert(1)\n",
            "disposition": "enforce\nCRITICAL",
        })
        response, lines = self.logged(body)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(lines, [
            "CSP violation directive=img-src blocked=https://evil.example.org document=/docs/_x__31m "
            "source=javascript disposition=- via=report-uri",
        ])


class ReductionTests(unittest.TestCase):
    def test_resources_are_reduced_to_an_origin_or_a_keyword(self):
        cases = {
            "inline": "inline",
            "eval": "eval",
            "wasm-eval": "wasm-eval",
            "trusted-types-sink": "trusted-types-sink",
            "data": "data",
            "blob": "blob",
            "data:image/png;base64,iVBORw0KGgo=": "data",
            "blob:https://polytradebot.live/1b4f7a2e-0000-4000-8000-000000000000": "blob",
            "chrome-extension://abcdefghijklmnopabcdefghijklmnop/inject.js": "chrome-extension",
            "moz-extension://8c5a0f3e-1111-4222-8333-944444444444/x.js": "moz-extension",
            "HTTPS://Fonts.GStatic.com/s/inter.woff2?v=1": "https://fonts.gstatic.com",
            "https://evil.example.org:8443/x": "https://evil.example.org:8443",
            "https://[2001:db8::1]:8443/x": "https://[2001:db8::1]:8443",
            "wss://stream.example.net/socket": "wss://stream.example.net",
            "": "-",
            "https://": "invalid",
            "https://exa mple.com/": "invalid",
            "https://example.com:99999/": "invalid",
            "%%%": "invalid",
        }
        for value, expected in cases.items():
            with self.subTest(value):
                self.assertEqual(routes_csp_report._resource(value), expected)

    def test_document_paths_lose_queries_fragments_and_identifiers(self):
        cases = {
            "https://polytradebot.live/?tgWebAppStartParam=ref123456#tgWebAppData=x": "/",
            "https://polytradebot.live": "/",
            f"https://polytradebot.live/screener/trader/{WALLET}": "/screener/trader/{id}",
            f"https://polytradebot.live/screener/trader/{WALLET.upper()[2:]}": "/screener/trader/{id}",
            f"https://polytradebot.live/u/{TELEGRAM_ID}/positions": "/u/{id}/positions",
            "https://polytradebot.live/c/1b4f7a2e-0000-4000-8000-000000000000": "/c/{id}",
            "https://polytradebot.live/docs/risk-and-security": "/docs/risk-and-security",
            "about:srcdoc": "about",
            "": "-",
            # Not "a": a long run of hex letters is an identifier and is masked.
            "https://polytradebot.live/" + "z" * 300: "/" + "z" * 119 + "...",
        }
        for value, expected in cases.items():
            with self.subTest(value):
                self.assertEqual(routes_csp_report._document(value), expected)

    def test_directives_are_logged_by_name_only(self):
        self.assertEqual(routes_csp_report._directive("img-src 'self' https://x.example"), "img-src")
        self.assertEqual(routes_csp_report._directive("SCRIPT-SRC-ELEM"), "script-src-elem")
        self.assertEqual(routes_csp_report._directive("img-src;evil"), "unknown")
        self.assertEqual(routes_csp_report._directive(""), "unknown")


if __name__ == "__main__":
    unittest.main()
