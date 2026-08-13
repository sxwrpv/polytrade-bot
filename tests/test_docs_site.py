from fastapi.testclient import TestClient

from backend.main import app


client = TestClient(app)


def test_docs_home_serves_polytrade_documentation_site():
    response = client.get("/docs")

    assert response.status_code == 200
    assert "PolyTrade Documentation" in response.text
    assert "swagger-ui" not in response.text.lower()


def test_interactive_api_reference_lives_under_api_namespace():
    response = client.get("/api/docs")

    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower()
    assert "API Reference" in response.text
    assert "/docs/assets/styles.css" in response.text
    assert "/docs/assets/api-docs.css" in response.text
    assert "/docs/assets/api-docs.js" in response.text
    assert "Real-money copy trading" in response.text


def test_docs_markdown_is_available_to_the_documentation_site():
    response = client.get("/docs/content/getting-started.md")

    assert response.status_code == 200
    assert response.text.startswith("# Getting Started")


def test_docs_assets_are_available_from_a_dedicated_mount():
    response = client.get("/docs/assets/app.js")

    assert response.status_code == 200
    assert "normalizeLink" in response.text


def test_unknown_documentation_page_returns_not_found():
    assert client.get("/docs/not-a-real-page").status_code == 404
    assert client.get("/docs/README").status_code == 404
    assert client.get("/docs/content/.env").status_code == 404


def test_openapi_routes_live_under_api_namespace_only():
    assert client.get("/api/redoc").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
