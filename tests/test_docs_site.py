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


def test_docs_markdown_is_available_to_the_documentation_site():
    response = client.get("/docs/content/getting-started.md")

    assert response.status_code == 200
    assert response.text.startswith("# Getting Started")
