from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services.provider_registry import ProviderInput, ProviderRegistry


def test_settings_dropdown_and_provider_list_are_not_empty(db, settings):
    ProviderRegistry(db, settings).initialize()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.get("/settings")
        assert response.status_code == 200
        assert '<select name="provider"' in response.text
        assert "Mock Provider" in response.text
        assert "DeepSeek" in response.text
        assert "智谱 GLM" in response.text
        assert "当前默认" in response.text
    finally:
        app.dependency_overrides.clear()


def test_setting_default_then_refresh_keeps_selection(db, settings):
    registry = ProviderRegistry(db, settings)
    registry.initialize()
    row = registry.create(ProviderInput("Test Provider", "openai_compatible", "https://example.test/v1", "custom-model", "TEST_API_KEY"))

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post("/settings/ai-provider", data={"provider": row.name}, follow_redirects=True)
            refreshed = client.get("/settings")
        assert response.status_code == 200
        assert registry.get_default().id == row.id
        assert f'<option value="{row.name}" selected>' in refreshed.text
    finally:
        app.dependency_overrides.clear()
