def test_settings_page_never_renders_secret(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    secret = "deepseek-ui-secret-value"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    with TestClient(app) as client:
        response = client.get("/settings")
    assert response.status_code == 200
    assert secret not in response.text
    assert "DEEPSEEK_API_KEY" not in response.text
    assert "已配置" in response.text
