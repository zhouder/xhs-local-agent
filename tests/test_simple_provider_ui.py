from __future__ import annotations

import os

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.database import get_db
from app.models import AIProvider, AuditLog, ProviderModel
from app.security import write_api_key as real_write_api_key
from app.services.provider_registry import ProviderInput, ProviderRegistry


def client_for(db):
    def override_db():
        yield db
    main.app.dependency_overrides[get_db] = override_db
    return TestClient(main.app)


def patch_env_writer(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(main, "write_api_key", lambda env_name, key, ignored_path: real_write_api_key(env_name, key, env_path))
    return env_path


def create_profile(registry, *, name="Editable", provider_type="openai_compatible", base_url="https://example.test/v1", env_name="EDITABLE_API_KEY"):
    return registry.create(ProviderInput(name, provider_type, base_url, api_key_env=env_name, models_text="model-a\nmodel-b", default_model_id="model-b"))


def test_new_provider_basic_fields_save_key_and_models(db, settings, monkeypatch, tmp_path):
    env_path = patch_env_writer(monkeypatch, tmp_path)
    monkeypatch.delenv("MY_PROVIDER_API_KEY", raising=False)
    with client_for(db) as client:
        response = client.post("/providers", data={
            "display_name": "My Provider", "provider_type": "openai_compatible",
            "base_url": "https://example.test/v1", "api_key": "sk-real-secret-value",
            "models_text": "model-a\nmodel-b", "default_model_id": "model-b",
        }, follow_redirects=False)
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "保存成功" in response.text
    row = db.scalar(select(AIProvider).where(AIProvider.display_name == "My Provider"))
    assert row.api_key_env == "MY_PROVIDER_API_KEY"
    assert row.default_model_id == "model-b"
    assert [m.model_id for m in ProviderRegistry(db, settings).models_for(row.id)] == ["model-a", "model-b"]
    assert "sk-real-secret-value" not in str(row.__dict__)
    assert "sk-real-secret-value" in env_path.read_text(encoding="utf-8")
    assert "sk-real-secret-value" not in " ".join(log.error_message + log.input_summary for log in db.scalars(select(AuditLog)))


def test_edit_page_is_simple_and_advanced_is_collapsed(db, settings):
    row = create_profile(ProviderRegistry(db, settings))
    with client_for(db) as client:
        response = client.get(f"/providers/{row.id}/edit")
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert 'type="password"' in response.text
    assert "请输入 API Key" in response.text
    assert "模型列表" in response.text
    assert "<details class=\"advanced-settings\">" in response.text
    assert "<details class=\"advanced-settings\" open" not in response.text
    assert "api_key_env" not in response.text


def test_edit_blank_key_preserves_existing_secret(db, settings, monkeypatch, tmp_path):
    env_path = patch_env_writer(monkeypatch, tmp_path)
    monkeypatch.setenv("EDITABLE_API_KEY", "existing-secret-value")
    env_path.write_text("EDITABLE_API_KEY=existing-secret-value\nOTHER=keep\n", encoding="utf-8")
    row = create_profile(ProviderRegistry(db, settings))
    with client_for(db) as client:
        response = client.post(f"/providers/{row.id}", data={
            "display_name": "Editable", "provider_type": "openai_compatible", "base_url": row.base_url,
            "api_key": "", "models_text": "model-a\nmodel-b", "default_model_id": "model-a",
        }, follow_redirects=False)
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "保存成功" in response.text
    assert "existing-secret-value" in env_path.read_text(encoding="utf-8")
    assert db.get(AIProvider, row.id).default_model_id == "model-a"


def test_edit_new_key_updates_env_and_creates_backup(db, settings, monkeypatch, tmp_path):
    env_path = patch_env_writer(monkeypatch, tmp_path)
    env_path.write_text("EDITABLE_API_KEY=old-secret-value\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setenv("EDITABLE_API_KEY", "old-secret-value")
    row = create_profile(ProviderRegistry(db, settings))
    with client_for(db) as client:
        response = client.post(f"/providers/{row.id}", data={
            "display_name": "Editable", "provider_type": "openai_compatible", "base_url": row.base_url,
            "api_key": "new-secret-value", "models_text": "model-a", "default_model_id": "model-a",
        }, follow_redirects=False)
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "保存成功" in response.text
    assert "new-secret-value" in env_path.read_text(encoding="utf-8")
    assert "old-secret-value" in (tmp_path / ".env.bak").read_text(encoding="utf-8")


def test_save_errors_stay_on_html_form(db, monkeypatch, tmp_path):
    patch_env_writer(monkeypatch, tmp_path)
    with client_for(db) as client:
        response = client.post("/providers", data={"display_name": "Broken", "provider_type": "openai_compatible", "base_url": "", "models_text": ""})
    main.app.dependency_overrides.clear()
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "请填写请求地址" in response.text
    assert "Internal Server Error" not in response.text


def test_invalid_advanced_json_stays_on_form(db, monkeypatch, tmp_path):
    patch_env_writer(monkeypatch, tmp_path)
    monkeypatch.setenv("JSON_TEST_API_KEY", "existing-valid-secret")
    with client_for(db) as client:
        response = client.post("/providers", data={
            "display_name": "JSON Test", "provider_type": "openai_compatible",
            "base_url": "https://example.test/v1", "models_text": "model",
            "extra_headers_json": "not-json",
        })
    main.app.dependency_overrides.clear()
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "新增 AI 供应商" in response.text


def test_env_write_failure_stays_on_form_without_exposing_key(db, monkeypatch):
    secret = "never-log-this-secret"
    monkeypatch.setattr(main, "write_api_key", lambda *args: (_ for _ in ()).throw(OSError("本地凭据文件不可写")))
    with client_for(db) as client:
        response = client.post("/providers", data={
            "display_name": "Write Failure", "provider_type": "openai_compatible",
            "base_url": "https://example.test/v1", "models_text": "model", "api_key": secret,
        })
    main.app.dependency_overrides.clear()
    assert response.status_code == 400
    assert "本地凭据文件不可写" in response.text
    assert secret not in response.text
    logs = " ".join(log.error_message + log.input_summary for log in db.scalars(select(AuditLog)))
    assert secret not in logs


def test_invalid_advanced_number_stays_on_html_form(db):
    with client_for(db) as client:
        response = client.post("/providers", data={
            "display_name": "Bad Timeout", "provider_type": "mock",
            "models_text": "mock-v1", "timeout_seconds": "not-a-number",
        })
    main.app.dependency_overrides.clear()
    assert response.status_code == 400
    assert "请求超时必须是数字" in response.text


def test_mock_and_ollama_allow_blank_api_key(db, monkeypatch, tmp_path):
    patch_env_writer(monkeypatch, tmp_path)
    with client_for(db) as client:
        mock_response = client.post("/providers", data={"display_name": "Extra Mock", "provider_type": "mock", "models_text": ""}, follow_redirects=False)
        ollama_response = client.post("/providers", data={"display_name": "Local Ollama", "provider_type": "ollama", "base_url": "http://127.0.0.1:11434", "models_text": "qwen"}, follow_redirects=False)
    main.app.dependency_overrides.clear()
    assert mock_response.status_code == 200
    assert ollama_response.status_code == 200


def test_remote_formats_require_api_key(db, monkeypatch, tmp_path):
    patch_env_writer(monkeypatch, tmp_path)
    for env_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env_name, raising=False)
    with client_for(db) as client:
        for name, provider_type in (("No Key OpenAI", "openai_compatible"), ("No Key Claude", "anthropic_messages"), ("No Key Gemini", "gemini")):
            response = client.post("/providers", data={"display_name": name, "provider_type": provider_type, "base_url": "https://example.test", "models_text": "model"})
            assert response.status_code == 400
            assert "需要填写 API Key" in response.text
    main.app.dependency_overrides.clear()


def test_provider_list_shows_default_model_but_never_secret(db, settings, monkeypatch):
    secret = "page-secret-value"
    monkeypatch.setenv("EDITABLE_API_KEY", secret)
    create_profile(ProviderRegistry(db, settings))
    with client_for(db) as client:
        response = client.get("/settings")
    main.app.dependency_overrides.clear()
    assert "model-b" in response.text
    assert secret not in response.text
    assert "api_key_env" not in response.text


def test_delete_default_provider_returns_friendly_page(db, settings):
    registry = ProviderRegistry(db, settings)
    registry.initialize()
    default = registry.get_default()
    with client_for(db) as client:
        response = client.post(f"/providers/{default.id}/delete", follow_redirects=True)
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "默认 Provider 不能删除" in response.text
    assert db.get(AIProvider, default.id) is not None


def test_connection_uses_default_model(db, settings, monkeypatch):
    registry = ProviderRegistry(db, settings)
    row = create_profile(registry)
    observed = {}

    class FakeAdapter:
        model = ""
        def test_connection(self):
            observed["model"] = self.model
            return True

    monkeypatch.setattr(main, "create_provider_from_profile", lambda profile, config: FakeAdapter())
    main.test_provider_connection(row.id, db)
    assert observed["model"] == "model-b"
