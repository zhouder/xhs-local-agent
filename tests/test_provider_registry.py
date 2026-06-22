from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.ai.factory import create_provider_from_profile
from app.ai.mock import MockProvider
from app.models import AIProvider, AuditLog
from app.services.provider_registry import ProviderInput, ProviderRegistry


def input_profile(name="Custom OpenAI", provider_type="openai_compatible", base_url="https://example.test/v1", model_id="free-model-id", api_key_env="CUSTOM_API_KEY"):
    return ProviderInput(name, provider_type, base_url, model_id, api_key_env)


def test_empty_registry_initializes_mock_and_legacy_profiles(db, settings):
    registry = ProviderRegistry(db, settings)
    registry.initialize()
    names = {row.name for row in registry.list_all()}
    assert {"mock", "deepseek", "glm"} <= names
    assert registry.get_default() is not None


def test_default_provider_persists_across_registry_instances(db, settings):
    registry = ProviderRegistry(db, settings)
    registry.initialize()
    row = registry.create(input_profile())
    registry.set_default(row.id)
    refreshed = ProviderRegistry(db, settings).get_default()
    assert refreshed.id == row.id
    assert sum(item.is_default for item in registry.list_all()) == 1


def test_add_openai_compatible_provider_with_free_model_id(db, settings):
    row = ProviderRegistry(db, settings).create(input_profile(model_id="vendor/arbitrary-model-2026"))
    assert row.provider_type == "openai_compatible"
    assert row.model_id == "vendor/arbitrary-model-2026"


@pytest.mark.parametrize("preset", ["qwen_dashscope", "moonshot_kimi", "openrouter", "siliconflow"])
def test_required_presets_create_profiles(db, settings, preset):
    row = ProviderRegistry(db, settings).create_from_preset(preset)
    assert row.provider_type == "chat_completions"
    assert row.base_url.startswith("https://")
    assert row.model_id


def test_api_key_value_is_never_written_to_database(db, settings, monkeypatch):
    secret = "sk-database-secret-value"
    monkeypatch.setenv("CUSTOM_API_KEY", secret)
    row = ProviderRegistry(db, settings).create(input_profile())
    serialized = " ".join(str(value) for value in row.__dict__.values())
    assert row.api_key_env == "CUSTOM_API_KEY"
    assert row.api_key_configured_status is True
    assert secret not in serialized


def test_authentication_header_cannot_be_saved(db, settings):
    data = ProviderInput("Unsafe", "openai_compatible", "https://example.test/v1", "model", "", extra_headers_json=json.dumps({"Authorization": "Bearer secret"}))
    with pytest.raises(ValueError, match="api_key_env"):
        ProviderRegistry(db, settings).create(data)


def test_api_key_cannot_be_hidden_in_extra_body(db, settings):
    data = ProviderInput("Unsafe body", "openai_compatible", "https://example.test/v1", "model", "", extra_body_json=json.dumps({"nested": {"api_key": "secret"}}))
    with pytest.raises(ValueError, match="cannot be stored"):
        ProviderRegistry(db, settings).create(data)


def test_mock_connection_succeeds(db, settings):
    registry = ProviderRegistry(db, settings)
    registry.initialize()
    adapter = create_provider_from_profile(registry.get_by_name("mock"), settings)
    assert isinstance(adapter, MockProvider)
    assert adapter.test_connection() is True


def test_legacy_model_id_migrates_to_provider_models(db, settings):
    row = AIProvider(
        name="legacy", display_name="Legacy", provider_type="openai_compatible",
        base_url="https://example.test/v1", model="legacy-model", model_id="legacy-model",
        api_key_env="LEGACY_API_KEY", enabled=True,
    )
    db.add(row)
    db.commit()
    registry = ProviderRegistry(db, settings)
    registry.initialize()
    models = registry.models_for(row.id)
    assert [(model.model_id, model.is_default) for model in models] == [("legacy-model", True)]
    assert row.default_model_id == "legacy-model"


def test_unsupported_provider_type_is_friendly(db, settings):
    with pytest.raises(ValueError, match="Unsupported provider_type"):
        ProviderRegistry(db, settings).create(input_profile(provider_type="unknown"))


def test_failed_connection_audit_redacts_secret(db, settings, monkeypatch):
    from app import main

    secret = "sk-test-connection-secret"
    monkeypatch.setenv("CUSTOM_API_KEY", secret)
    row = ProviderRegistry(db, settings).create(input_profile())

    def fail(*args, **kwargs):
        raise RuntimeError(f"connection failed {secret}")

    monkeypatch.setattr(main, "create_provider_from_profile", lambda profile, config: type("Bad", (), {"test_connection": fail})())
    main.test_provider_connection(row.id, db)
    audit = db.scalar(select(AuditLog).where(AuditLog.action_type == "ai_provider.test_connection"))
    assert audit.status == "failed"
    assert secret not in audit.error_message
    assert "[REDACTED]" in audit.error_message
