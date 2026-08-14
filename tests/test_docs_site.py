from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app


client = TestClient(app)


def test_docs_home_serves_polytrade_documentation_site():
    response = client.get("/docs")

    assert response.status_code == 200
    assert "PolyTrade Documentation" in response.text
    assert "swagger-ui" not in response.text.lower()
    assert 'href="https://t.me/cpolytrade_bot">Open Telegram bot</a>' in response.text
    assert "risk-banner" not in response.text
    assert 'href="/">Open app</a>' not in response.text
    assert 'href="https://polytradebot.live/">Open app</a>' not in response.text


def test_interactive_api_reference_lives_under_api_namespace():
    response = client.get("/api/docs")

    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower()
    assert "API Reference" in response.text
    assert "/docs/assets/styles.css" in response.text
    assert "/docs/assets/api-docs.css" in response.text
    assert "/docs/assets/api-docs.js" in response.text
    assert 'href="https://t.me/cpolytrade_bot">Open Telegram bot</a>' in response.text
    assert "risk-banner" not in response.text
    assert 'href="/">Open app</a>' not in response.text


def test_docs_markdown_is_available_to_the_documentation_site():
    response = client.get("/docs/content/getting-started.md")

    assert response.status_code == 200
    assert response.text.startswith("# Getting Started")


def test_consumer_docs_are_telegram_first_and_keep_material_facts():
    overview = client.get("/docs/content/README.md").text
    getting_started = client.get("/docs/content/getting-started.md").text
    wallet = client.get("/docs/content/wallet-and-funding.md").text
    risk = client.get("/docs/content/risk-and-security.md").text
    consumer_docs = "\n".join((overview, getting_started, wallet, risk)).lower()

    assert "https://t.me/cpolytrade_bot" in overview
    assert "@cpolytrade_bot" in getting_started
    assert "plain-browser creation" not in consumer_docs
    assert "custodial" in consumer_docs
    assert "lose" in consumer_docs and "full" in consumer_docs
    assert "eligib" in consumer_docs
    assert "does not expose a withdrawal" in consumer_docs
    assert "does not automatically redeem" in consumer_docs
    assert "pause" in consumer_docs and "does not sell" in consumer_docs


def test_audience_hubs_and_official_links_are_allowlisted_and_navigable():
    expected_pages = {
        "developers": "# Developers",
        "operators": "# Operators",
        "links": "# Official Links",
    }
    for slug, heading in expected_pages.items():
        assert client.get(f"/docs/{slug}").status_code == 200
        content = client.get(f"/docs/content/{slug}.md")
        assert content.status_code == 200
        assert content.text.startswith(heading)

    navigation = client.get("/docs/assets/app.js").text
    for slug in expected_pages:
        assert f"['{slug}'" in navigation

    links = client.get("/docs/content/links.md").text
    assert "https://t.me/cpolytrade_bot" in links
    assert "https://polytradebot.live/docs/developers" in links
    assert "https://polytradebot.live/docs/operators" in links
    assert "https://github.com/sxwrpv/polytrade-bot" in links
    assert "52.51.200.58" not in links


def test_official_links_inventory_includes_all_core_consumer_guides():
    links = client.get("/docs/content/links.md").text

    for slug in ("core-concepts", "copy-trading", "glossary"):
        assert f"https://polytradebot.live/docs/{slug}" in links


def test_root_readme_uses_calm_before_funding_disclosure():
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    disclosure = readme.lower()

    assert "[!warning]" not in disclosure
    assert "before you fund" in disclosure
    assert "custodial" in disclosure
    assert "lose all funds" in disclosure


def test_docs_assets_are_available_from_a_dedicated_mount():
    response = client.get("/docs/assets/app.js")

    assert response.status_code == 200
    assert "normalizeLink" in response.text

    styles = client.get("/docs/assets/styles.css")
    assert styles.status_code == 200
    assert "risk-banner" not in styles.text
    assert "--banner-h" not in styles.text


def test_existing_documentation_urls_remain_available():
    existing_slugs = {
        "overview",
        "getting-started",
        "core-concepts",
        "copy-trading",
        "wallet-and-funding",
        "risk-and-security",
        "api-reference",
        "configuration",
        "deployment",
        "troubleshooting",
        "glossary",
    }
    for slug in existing_slugs:
        assert client.get(f"/docs/{slug}").status_code == 200


def test_unknown_documentation_page_returns_not_found():
    assert client.get("/docs/not-a-real-page").status_code == 404
    assert client.get("/docs/README").status_code == 404
    assert client.get("/docs/content/.env").status_code == 404
    assert client.get("/docs/content/not-a-real-page.md").status_code == 404


def test_openapi_routes_live_under_api_namespace_only():
    assert client.get("/api/redoc").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_create_wallet_openapi_does_not_promise_immediate_readiness():
    schema = client.get("/api/openapi.json").json()
    description = " ".join(
        schema["paths"]["/api/user/create-wallet"]["post"]["description"].lower().split()
    )

    assert "ready to fund and trade immediately" not in description
    assert "readiness" in description
    assert "approvals" in description
    assert "may still" in description
