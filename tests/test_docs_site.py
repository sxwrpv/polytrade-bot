import re

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


def test_public_screener_pagination_and_range_validation_are_documented():
    reference = client.get("/docs/content/api-reference.md").text

    for field in ("`total`", "`count`", "`limit`", "`offset`", "`has_more`"):
        assert field in reference
    assert "`consistency_ratio_min`" in reference
    assert "`consistency_min`" not in reference
    assert "`positive_close_day_ratio_min`" not in reference
    assert "minimum cannot exceed the maximum" in reference.lower()


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


def test_brand_mark_and_typography_are_consistent_across_docs_surfaces():
    docs = client.get("/docs")
    api_docs = client.get("/api/docs")
    mark = client.get("/docs/assets/polytrade-mark.png")
    navigation = client.get("/docs/assets/app.js").text

    for response in (docs, api_docs):
        assert 'src="/docs/assets/polytrade-mark.png"' in response.text
        assert 'class="brand-mark">P<' not in response.text

    assert "family=Instrument+Serif" in api_docs.text
    assert mark.status_code == 200
    assert mark.headers["content-type"] == "image/png"
    assert len(mark.content) > 1_000

    # Glossary remains useful reference material in the sidebar/search corpus;
    # it does not need a competing top-header tab.
    assert "['glossary', 'Glossary', 'glossary.md']" in navigation
    assert '>Glossary</a>' not in docs.text


def test_public_headers_share_home_screener_docs_liquid_glass_navigation():
    docs = client.get("/docs")
    api_docs = client.get("/api/docs")
    redoc = client.get("/api/redoc")
    styles = client.get("/docs/assets/styles.css").text

    for response in (docs, api_docs, redoc):
        assert response.status_code == 200
        assert 'src="/docs/assets/polytrade-mark.png"' in response.text
        assert 'class="site-switcher"' in response.text
        assert 'href="/">Home</a>' in response.text
        assert 'href="/screener">Screener</a>' in response.text
        assert 'href="/docs"' in response.text
        assert '>Docs</a>' in response.text

    assert 'aria-current="page">Docs</a>' in docs.text
    assert "backdrop-filter: blur(" in styles
    assert ".site-switcher" in styles
    assert "border-radius: 999px" in styles


def test_docs_header_compacts_search_before_it_can_overlap_center_navigation():
    styles = client.get("/docs/assets/styles.css").text
    responsive = styles[styles.index("@media (max-width: 1380px)") :]

    assert ".search-trigger span" in responsive
    assert ".search-trigger kbd" in responsive
    assert "display: none" in responsive
    assert ".search-trigger { width: 36px" in responsive


def test_docs_and_api_headers_collapse_long_brand_labels_before_narrow_overlap():
    styles = client.get("/docs/assets/styles.css").text
    narrow = styles[styles.index("@media (max-width: 680px)") :]
    api_styles = client.get("/docs/assets/api-docs.css").text
    api_tablet = api_styles[api_styles.index("@media (max-width: 820px)") :]

    assert ".brand > span" in narrow
    assert "display: none" in narrow
    assert ".api-topbar .brand > span" in api_tablet
    assert "display: none" in api_tablet


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


# ---------------------------------------------------------------------------
# System Design — the diagram collection restored into the documentation site.
# ---------------------------------------------------------------------------

def _system_design_fragment() -> str:
    response = client.get("/docs/assets/system-design.html")
    assert response.status_code == 200
    return response.text


def test_system_design_has_a_clean_public_url_inside_the_docs_shell():
    page = client.get("/docs/system-design")

    assert page.status_code == 200
    # Shares the ordinary documentation shell: same header, logo, sidebar,
    # language toggle and footer as every other docs page.
    assert "PolyTrade Documentation" in page.text
    assert 'src="/docs/assets/polytrade-mark.png"' in page.text
    assert 'class="doc-footer"' in page.text


def test_system_design_is_a_reference_entry_not_a_consumer_top_tab():
    navigation = client.get("/docs/assets/app.js").text
    docs = client.get("/docs").text

    assert "'system-design'" in navigation
    # It belongs to the Developers/Reference end of the sidebar, never to the
    # consumer-facing header links.
    assert ">System Design</a>" not in docs
    assert '<nav class="header-links"' in docs


def test_system_design_publishes_every_diagram_from_the_local_sources():
    fragment = _system_design_fragment()

    for heading in (
        "System topology",
        "Onboarding and wallet creation",
        "The copy cycle",
        "Risk surfaces",
        "Lifecycle",
        "Wallet screener",
        "Production workflow",
    ):
        assert heading in fragment, heading


def test_system_design_diagrams_are_accessible_without_colour_or_a_mouse():
    fragment = _system_design_fragment()
    diagrams = re.findall(r"<svg\b[^>]*class=\"[^\"]*diagram[^\"]*\"[^>]*>", fragment)

    assert len(diagrams) >= 7
    for opening_tag in diagrams:
        assert 'role="img"' in opening_tag
        assert "aria-labelledby=" in opening_tag
    # Every diagram carries a title and a prose equivalent, and the legends
    # name each class rather than relying on the swatch colour alone.
    assert fragment.count("<title id=") >= 7
    assert fragment.count("<desc id=") >= 7
    assert "LEGEND" in fragment


def test_system_design_diagram_labels_match_the_current_implementation():
    fragment = _system_design_fragment()

    # The claims table is copy_open_claims; an unqualified "claims" table label
    # does not exist in backend/db/models.py.
    assert "copy_open_claims" in fragment
    assert "copy_positions · claims" not in fragment
    # The reconcile cadence is configurable (COPY_ENGINE_POLL_SECONDS): 5s as
    # deployed, 30s as the code default. Do not publish one as the only truth.
    assert "COPY_ENGINE_POLL_SECONDS" in fragment
    assert "detect 2s · reconcile 5s" not in fragment
    # Storage is SQLite or Postgres via backend/db/database.py — not Supabase.
    assert "Supabase" not in fragment


def test_restyled_workflow_diagram_drops_the_dark_operator_visual_system():
    fragment = _system_design_fragment()
    styles = client.get("/docs/assets/system-design.css").text

    for dark_token in ("#020617", "#0f172a", "#22d3ee", "#a78bfa", "#fb7185", "#34d399"):
        assert dark_token not in fragment, dark_token
        assert dark_token not in styles, dark_token
    # It uses the documentation palette instead.
    assert "#0b9e63" in fragment
    # No pulsing dot implying live system status.
    assert "pulse" not in fragment


def test_system_design_motion_is_subtle_and_respects_reduced_motion():
    styles = client.get("/docs/assets/system-design.css").text
    script = client.get("/docs/assets/system-design.js").text

    assert "prefers-reduced-motion: reduce" in styles
    assert "prefers-reduced-motion" in script
    assert "IntersectionObserver" in script
    # Reveal animations must run once, not loop forever.
    assert "infinite" not in styles


def test_system_design_diagrams_stay_usable_on_a_narrow_screen():
    styles = client.get("/docs/assets/system-design.css").text

    # Wide diagrams scroll inside their own figure rather than forcing the page
    # to scroll horizontally, and the scroller is reachable from the keyboard.
    assert "overflow-x: auto" in styles
    assert "tabindex" in _system_design_fragment()
    assert "@media (max-width: 820px)" in styles
