from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.ai.endpoints import build_endpoint_url
from app.ai.openai_compatible import AIProviderError
from app.ai.responses import ResponsesProvider
from app.database import get_db
from app.models import AIProvider, AuditLog
from app.services.provider_registry import ProviderInput, ProviderRegistry


def client_for(db):
    def override_db():
        yield db
    main.app.dependency_overrides[get_db] = override_db
    return TestClient(main.app)


def profile(registry, provider_type="openai_compatible", env_name="FORMAT_TEST_API_KEY"):
    return registry.create(ProviderInput(
        "Format Test", provider_type, "https://api.example.test/v1",
        api_key_env=env_name, models_text="model-one\nmodel-two", default_model_id="model-one",
    ))


def api_format_select(html: str) -> str:
    return re.search(r'<select name="provider_type".*?</select>', html, flags=re.DOTALL).group(0)


def test_new_form_shows_exactly_three_api_formats_with_chat_default(db):
    with client_for(db) as client:
        response = client.get("/providers/new")
    main.app.dependency_overrides.clear()
    select_html = api_format_select(response.text)
    assert re.findall(r'<option value="([^"]+)"', select_html) == ["chat_completions", "anthropic_messages", "responses"]
    assert '<option value="chat_completions" selected>' in select_html
    assert "OpenAI 兼容格式" not in select_html
    assert "Google Gemini 格式" not in select_html
    assert "Ollama 本地格式" not in select_html
    assert "Mock 测试" not in select_html


@pytest.mark.parametrize("stored,label,value", [
    ("openai_compatible", "Chat Completions (/chat/completions)", "chat_completions"),
    ("anthropic_messages", "Anthropic Messages (/v1/messages)", "anthropic_messages"),
    ("openai_responses", "Responses (/responses)", "responses"),
])
def test_legacy_mainstream_formats_map_to_three_ui_options(db, settings, stored, label, value):
    row = profile(ProviderRegistry(db, settings), stored)
    with client_for(db) as client:
        response = client.get(f"/providers/{row.id}/edit")
    main.app.dependency_overrides.clear()
    select_html = api_format_select(response.text)
    assert label in select_html
    assert f'<option value="{value}" selected>' in select_html


def test_configured_key_uses_masked_placeholder_and_never_renders_secret(db, settings, monkeypatch):
    secret = "sk-ui-never-render-this"
    monkeypatch.setenv("FORMAT_TEST_API_KEY", secret)
    row = profile(ProviderRegistry(db, settings))
    with client_for(db) as client:
        response = client.get(f"/providers/{row.id}/edit")
    main.app.dependency_overrides.clear()
    assert "******** 已配置，留空则不修改" in response.text
    assert "当前状态：<strong>已配置</strong>" in response.text
    assert secret not in response.text


def test_save_and_test_first_persists_then_tests_and_stays_on_edit_page(db, settings, monkeypatch):
    monkeypatch.setenv("FORMAT_TEST_API_KEY", "valid-format-test-secret")
    row = profile(ProviderRegistry(db, settings))
    observed = {}

    class Adapter:
        model = ""
        def test_connection(self):
            observed["model"] = self.model
            return True

    def factory(saved, config):
        observed["display_name"] = saved.display_name
        return Adapter()

    monkeypatch.setattr(main, "create_provider_from_profile", factory)
    with client_for(db) as client:
        response = client.post(f"/providers/{row.id}", data={
            "display_name": "Saved Before Test", "provider_type": "chat_completions",
            "base_url": "https://api.example.test/v1", "models_text": "new-model",
            "default_model_id": "new-model", "action": "test", "supports_json_mode": "true",
        })
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.url.path == f"/providers/{row.id}"
    assert "连接成功，模型 new-model 可用。" in response.text
    assert observed == {"display_name": "Saved Before Test", "model": "new-model"}
    assert db.get(AIProvider, row.id).default_model_id == "new-model"


def test_connection_failure_stays_on_edit_and_shows_specific_redacted_reason(db, settings, monkeypatch):
    secret = "sk-failure-secret-value"
    monkeypatch.setenv("FORMAT_TEST_API_KEY", secret)
    row = profile(ProviderRegistry(db, settings))

    class Adapter:
        model = ""
        def test_connection(self):
            raise AIProviderError(f"request failed with HTTP 401 {secret}")

    monkeypatch.setattr(main, "create_provider_from_profile", lambda saved, config: Adapter())
    with client_for(db) as client:
        response = client.post(f"/providers/{row.id}", data={
            "display_name": row.display_name, "provider_type": "chat_completions",
            "base_url": row.base_url, "models_text": "model-one", "default_model_id": "model-one",
            "action": "test",
        })
    main.app.dependency_overrides.clear()
    assert response.status_code == 400
    assert response.url.path == f"/providers/{row.id}"
    assert "连接失败：401 Unauthorized，请检查 API Key。" in response.text
    assert secret not in response.text
    audit = db.scalar(select(AuditLog).where(AuditLog.action_type == "ai_provider.test_connection", AuditLog.status == "failed"))
    assert secret not in audit.error_message


@pytest.mark.parametrize("base,api_format,expected", [
    ("https://api.example.com", "chat_completions", "https://api.example.com/chat/completions"),
    ("https://api.example.com/v1", "chat_completions", "https://api.example.com/v1/chat/completions"),
    ("https://api.example.com/v1/chat/completions", "chat_completions", "https://api.example.com/v1/chat/completions"),
    ("https://api.example.com", "anthropic_messages", "https://api.example.com/v1/messages"),
    ("https://api.example.com/v1", "anthropic_messages", "https://api.example.com/v1/messages"),
    ("https://api.example.com/v1/messages", "anthropic_messages", "https://api.example.com/v1/messages"),
    ("https://api.example.com/v1", "responses", "https://api.example.com/v1/responses"),
    ("https://api.example.com/v1/responses", "responses", "https://api.example.com/v1/responses"),
])
def test_endpoint_url_joining(base, api_format, expected):
    assert build_endpoint_url(base, api_format) == expected


def test_presets_use_only_user_visible_formats(settings):
    presets = settings.ai["presets"]
    assert presets["deepseek"]["provider_type"] == "chat_completions"
    assert presets["qwen_dashscope"]["provider_type"] == "chat_completions"
    assert presets["anthropic"]["provider_type"] == "anthropic_messages"


def test_edit_page_has_request_preview_and_collapsed_advanced_settings(db, settings):
    row = profile(ProviderRegistry(db, settings))
    with client_for(db) as client:
        response = client.get(f"/providers/{row.id}/edit")
    main.app.dependency_overrides.clear()
    assert "当前请求预览" in response.text
    assert "将请求的 URL" in response.text
    assert "<details class=\"advanced-settings\">" in response.text
    assert "<details class=\"advanced-settings\" open" not in response.text


def test_legacy_advanced_rows_are_kept_and_marked(db, settings):
    registry = ProviderRegistry(db, settings)
    legacy = [
        registry.create(ProviderInput("Legacy Mock", "mock", "", models_text="mock-v1")),
        registry.create(ProviderInput("Legacy Ollama", "ollama", "http://127.0.0.1:11434", models_text="qwen")),
        registry.create(ProviderInput("Legacy Custom", "custom_http", "https://example.test", models_text="")),
    ]
    with client_for(db) as client:
        response = client.get("/settings")
    main.app.dependency_overrides.clear()
    assert all(db.get(AIProvider, item.id) is not None for item in legacy)
    assert response.text.count("旧版 / 高级 Provider") >= 3


class ResponseClient:
    def __init__(self):
        self.url = ""
    def post(self, url, **kwargs):
        self.url = url
        return httpx.Response(200, request=httpx.Request("POST", url), json={"output_text": '{"ok": true}'})


def test_responses_provider_minimal_connection():
    client = ResponseClient()
    provider = ResponsesProvider("https://api.openai.com/v1", "test-secret", "model", client=client)
    assert provider.test_connection()
    assert client.url == "https://api.openai.com/v1/responses"
