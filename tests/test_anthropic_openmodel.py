from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.ai.anthropic import AnthropicProvider, build_auth_headers
from app.ai.endpoints import build_endpoint_url
from app.ai.openai_compatible import AIProviderError
from app.database import get_db
from app.models import AuditLog
from app.services.provider_registry import ProviderInput, ProviderRegistry


class ResponseClient:
    def __init__(self, *, status=200, payload=None, text=None, content_type="application/json"):
        self.status = status
        self.payload = payload
        self.text = text
        self.content_type = content_type
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        request = httpx.Request("POST", url)
        if self.text is not None:
            return httpx.Response(self.status, request=request, text=self.text, headers={"Content-Type": self.content_type})
        return httpx.Response(self.status, request=request, json=self.payload, headers={"Content-Type": self.content_type})


def client_for(db):
    def override_db():
        yield db

    main.app.dependency_overrides[get_db] = override_db
    return TestClient(main.app)


@pytest.mark.parametrize(
    ("base_url", "auth_scheme", "authorization", "x_api_key"),
    [
        ("https://api.anthropic.com", "auto", False, True),
        ("https://api.openmodel.ai", "auto", True, False),
        ("https://api.openmodel.ai", "bearer", True, False),
        ("https://api.openmodel.ai", "x_api_key", False, True),
        ("https://api.openmodel.ai", "both", True, True),
    ],
)
def test_anthropic_auth_header_modes(base_url, auth_scheme, authorization, x_api_key):
    headers = build_auth_headers("test-secret", auth_scheme, base_url)
    assert ("Authorization" in headers) is authorization
    assert ("x-api-key" in headers) is x_api_key
    assert headers["Content-Type"] == "application/json"
    assert headers["anthropic-version"] == "2023-06-01"


def test_openmodel_test_connection_uses_messages_url_and_minimal_payload():
    client = ResponseClient(payload={"content": [{"type": "text", "text": '```json\n{"ok": true}\n```'}]})
    provider = AnthropicProvider(
        "https://api.openmodel.ai", "test-secret", "deepseek-v4-flash",
        auth_scheme="auto", client=client,
    )
    assert provider.test_connection()
    url, request = client.calls[0]
    assert url == "https://api.openmodel.ai/v1/messages"
    assert request["headers"]["Authorization"] == "Bearer test-secret"
    assert "x-api-key" not in request["headers"]
    assert request["json"]["model"] == "deepseek-v4-flash"
    assert request["json"]["max_tokens"] == 128
    assert request["json"]["temperature"] == 0
    assert request["json"]["messages"][0]["content"] == '请只返回 JSON：{"ok": true}'


def test_openmodel_preset_builds_anthropic_request_preview(db, settings):
    row = ProviderRegistry(db, settings).create_from_preset("openmodel")
    assert row.display_name == "OpenModel"
    assert row.provider_type == "anthropic_messages"
    assert row.base_url == "https://api.openmodel.ai"
    assert row.default_model_id == "deepseek-v4-flash"
    assert row.auth_scheme == "auto"
    assert build_endpoint_url(row.base_url, row.provider_type) == "https://api.openmodel.ai/v1/messages"


def test_connection_failure_renders_redacted_openmodel_diagnostics(db, settings, monkeypatch):
    secret = "openmodel-real-test-secret"
    monkeypatch.setenv("OPENMODEL_TEST_API_KEY", secret)
    registry = ProviderRegistry(db, settings)
    row = registry.create(ProviderInput(
        "OpenModel", "anthropic_messages", "https://api.openmodel.ai",
        api_key_env="OPENMODEL_TEST_API_KEY", models_text="deepseek-v4-flash",
        auth_scheme="bearer",
    ))
    response_client = ResponseClient(
        status=401,
        text=f"<html>invalid key {secret}</html>",
        content_type="text/html; charset=utf-8",
    )
    monkeypatch.setattr(
        main,
        "create_provider_from_profile",
        lambda saved, config: AnthropicProvider(
            saved.base_url, secret, saved.default_model_id,
            auth_scheme=saved.auth_scheme, client=response_client,
        ),
    )

    with client_for(db) as client:
        response = client.post(f"/providers/{row.id}", data={
            "display_name": "OpenModel",
            "provider_type": "anthropic_messages",
            "base_url": "https://api.openmodel.ai",
            "models_text": "deepseek-v4-flash",
            "default_model_id": "deepseek-v4-flash",
            "auth_scheme": "bearer",
            "action": "test",
        })
    main.app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "请求 URL：https://api.openmodel.ai/v1/messages" in response.text
    assert "状态码：401" in response.text
    assert "Content-Type：text/html; charset=utf-8" in response.text
    assert "认证方式：bearer" in response.text
    assert secret not in response.text
    audit = db.scalar(select(AuditLog).where(AuditLog.action_type == "ai_provider.test_connection", AuditLog.status == "failed"))
    assert secret not in audit.error_message


def test_provider_page_never_contains_openmodel_api_key(db, settings, monkeypatch):
    secret = "openmodel-page-secret"
    monkeypatch.setenv("OPENMODEL_PAGE_API_KEY", secret)
    row = ProviderRegistry(db, settings).create(ProviderInput(
        "OpenModel", "anthropic_messages", "https://api.openmodel.ai",
        api_key_env="OPENMODEL_PAGE_API_KEY", models_text="deepseek-v4-flash",
        auth_scheme="auto",
    ))
    with client_for(db) as client:
        response = client.get(f"/providers/{row.id}/edit")
    main.app.dependency_overrides.clear()
    assert secret not in response.text
    assert "自动（实际 Bearer Token）" in response.text
    assert "https://api.openmodel.ai" in response.text


def test_non_json_diagnostic_redacts_key_even_when_not_in_environment():
    secret = "unregistered-provider-secret"
    client = ResponseClient(status=502, text=f"upstream rejected {secret}", content_type="text/plain")
    provider = AnthropicProvider(
        "https://api.openmodel.ai", secret, "deepseek-v4-flash",
        auth_scheme="bearer", client=client,
    )
    with pytest.raises(AIProviderError) as caught:
        provider.test_connection()
    message = str(caught.value)
    assert "状态码：502" in message
    assert "Content-Type：text/plain" in message
    assert secret not in message
