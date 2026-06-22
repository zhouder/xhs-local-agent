from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIProvider, ProviderModel


SUPPORTED_PROVIDER_TYPES = {
    "mock", "openai_compatible", "anthropic_messages", "gemini",
    "ollama", "lm_studio", "custom_http", "chat_completions", "responses", "openai_responses",
}
SUPPORTED_AUTH_SCHEMES = {"auto", "bearer", "x_api_key", "both"}


@dataclass(frozen=True)
class ProviderInput:
    display_name: str
    provider_type: str
    base_url: str
    model_id: str = ""
    api_key_env: str = ""
    models_text: str = ""
    default_model_id: str = ""
    supports_json_mode: bool = True
    supports_streaming: bool = False
    supports_vision: bool = False
    supports_tools: bool = False
    extra_headers_json: str = "{}"
    extra_body_json: str = "{}"
    notes: str = ""
    timeout_seconds: int = 60
    max_output_tokens: int | None = None
    temperature_default: str = "0.6"
    auth_scheme: str = "auto"


class ProviderRegistry:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def initialize(self) -> None:
        existing = self.list_all()
        if not existing:
            self._seed_legacy_config()
            existing = self.list_all()
        if not any(row.name == "mock" for row in existing):
            self.create(ProviderInput("Mock Provider", "mock", "", "mock-v1"), name="mock")
        self._migrate_legacy_models()
        default = self.get_default()
        if default is None:
            preferred = self.db.scalar(select(AIProvider).where(AIProvider.name == self.settings.ai.get("default_provider", "mock"), AIProvider.enabled.is_(True)))
            self.set_default((preferred or self.get_by_name("mock")).id)
        self.refresh_key_statuses()

    def _seed_legacy_config(self) -> None:
        default_name = self.settings.ai.get("default_provider", "mock")
        for name, config in self.settings.ai.get("providers", {}).items():
            provider_type = "mock" if name == "mock" else "openai_compatible"
            self.create(ProviderInput(
                display_name={"mock": "Mock Provider", "deepseek": "DeepSeek", "glm": "智谱 GLM"}.get(name, name),
                provider_type=provider_type,
                base_url=config.get("base_url", ""), model_id=config.get("model", ""),
                api_key_env=config.get("api_key_env", ""),
                supports_json_mode=bool(config.get("supports_json_mode", True)),
                timeout_seconds=int(config.get("timeout_seconds", 60)),
            ), name=name, is_default=name == default_name)

    def list_all(self) -> list[AIProvider]:
        return list(self.db.scalars(select(AIProvider).order_by(AIProvider.id)))

    def list_enabled(self) -> list[AIProvider]:
        return list(self.db.scalars(select(AIProvider).where(AIProvider.enabled.is_(True)).order_by(AIProvider.id)))

    def get(self, provider_id: int) -> AIProvider | None:
        return self.db.get(AIProvider, provider_id)

    def get_by_name(self, name: str) -> AIProvider | None:
        return self.db.scalar(select(AIProvider).where(AIProvider.name == name))

    def get_default(self) -> AIProvider | None:
        return self.db.scalar(select(AIProvider).where(AIProvider.is_default.is_(True), AIProvider.enabled.is_(True)))

    def create(self, data: ProviderInput, *, name: str | None = None, is_default: bool = False) -> AIProvider:
        self._validate(data)
        models, default_model = self._models_from_input(data)
        slug = name or self._unique_name(data.display_name)
        row = AIProvider(
            name=slug, display_name=data.display_name.strip(), provider_type=data.provider_type,
            base_url=data.base_url.strip().rstrip("/"), model=default_model, model_id=default_model, default_model_id=default_model,
            api_key_env=data.api_key_env.strip(), enabled=True, is_default=False,
            supports_json_mode=data.supports_json_mode, supports_streaming=data.supports_streaming,
            supports_vision=data.supports_vision, supports_tools=data.supports_tools,
            extra_headers_json=self._validated_json(data.extra_headers_json, headers=True),
            extra_body_json=self._validated_json(data.extra_body_json), notes=data.notes,
            timeout_seconds=max(1, min(data.timeout_seconds, 300)),
            max_output_tokens=data.max_output_tokens,
            temperature_default=data.temperature_default,
            auth_scheme=data.auth_scheme,
            api_key_configured_status=self._key_configured(data.provider_type, data.api_key_env),
        )
        self.db.add(row)
        self.db.flush()
        self._replace_models(row.id, models, default_model)
        self.db.commit()
        if is_default:
            self.set_default(row.id)
        return row

    def update(self, provider_id: int, data: ProviderInput) -> AIProvider:
        self._validate(data)
        models, default_model = self._models_from_input(data)
        row = self._required(provider_id)
        row.display_name, row.provider_type = data.display_name.strip(), data.provider_type
        row.base_url = data.base_url.strip().rstrip("/")
        row.model_id = row.model = row.default_model_id = default_model
        row.api_key_env = data.api_key_env.strip()
        row.supports_json_mode, row.supports_streaming = data.supports_json_mode, data.supports_streaming
        row.supports_vision, row.supports_tools = data.supports_vision, data.supports_tools
        row.extra_headers_json, row.extra_body_json = self._validated_json(data.extra_headers_json, headers=True), self._validated_json(data.extra_body_json)
        row.notes, row.timeout_seconds = data.notes, max(1, min(data.timeout_seconds, 300))
        row.max_output_tokens, row.temperature_default = data.max_output_tokens, data.temperature_default
        row.auth_scheme = data.auth_scheme
        row.api_key_configured_status = self._key_configured(row.provider_type, row.api_key_env)
        self._replace_models(row.id, models, default_model)
        self.db.commit()
        return row

    def delete(self, provider_id: int) -> None:
        row = self._required(provider_id)
        if row.is_default:
            raise ValueError("默认 Provider 不能删除，请先设置新的默认 Provider")
        if row.name == "mock":
            raise ValueError("Mock Provider 不能删除")
        self.db.execute(delete(ProviderModel).where(ProviderModel.provider_id == row.id))
        self.db.delete(row)
        self.db.commit()

    def set_default(self, provider_id: int) -> AIProvider:
        row = self._required(provider_id)
        if not row.enabled:
            raise ValueError("Disabled provider cannot be the default")
        for provider in self.list_all():
            provider.is_default = provider.id == row.id
        self.db.commit()
        return row

    def set_enabled(self, provider_id: int, enabled: bool) -> AIProvider:
        row = self._required(provider_id)
        if row.is_default and not enabled:
            raise ValueError("Default provider cannot be disabled")
        if row.name == "mock" and not enabled:
            raise ValueError("Mock provider cannot be disabled")
        row.enabled = enabled
        self.db.commit()
        return row

    def create_from_preset(self, preset_name: str, *, model_id: str = "", api_key_env: str = "", base_url: str = "", display_name: str = "") -> AIProvider:
        preset = self.settings.ai.get("presets", {}).get(preset_name)
        if not preset:
            raise ValueError(f"Unknown provider preset: {preset_name}")
        examples = preset.get("example_models", [])
        return self.create(ProviderInput(
            display_name=display_name or preset.get("display_name", preset_name),
            provider_type=preset["provider_type"],
            base_url=base_url or preset.get("default_base_url", ""),
            model_id=model_id or (examples[0] if examples else ""),
            models_text="\n".join(examples),
            api_key_env=api_key_env or preset.get("api_key_env", ""),
            supports_json_mode=bool(preset.get("supports_json_mode", True)),
            auth_scheme=preset.get("auth_scheme", "auto"),
            notes=f"Created from preset: {preset_name}",
        ))

    def refresh_key_statuses(self) -> None:
        changed = False
        for row in self.list_all():
            configured = self._key_configured(row.provider_type, row.api_key_env)
            if row.api_key_configured_status != configured:
                row.api_key_configured_status = configured
                changed = True
        if changed:
            self.db.commit()

    def _validate(self, data: ProviderInput) -> None:
        if data.provider_type not in SUPPORTED_PROVIDER_TYPES:
            raise ValueError(f"Unsupported provider_type: {data.provider_type}")
        if not data.display_name.strip():
            raise ValueError("display_name is required")
        if data.provider_type != "mock" and not data.base_url.strip():
            raise ValueError("base_url is required")
        if data.provider_type != "custom_http" and not data.model_id.strip():
            if not data.models_text.strip() and data.provider_type != "mock":
                raise ValueError("请至少填写一个模型 ID")
        if data.api_key_env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", data.api_key_env):
            raise ValueError("api_key_env must be an uppercase environment variable name")
        if data.auth_scheme not in SUPPORTED_AUTH_SCHEMES:
            raise ValueError("不支持的认证方式")
        self._validated_json(data.extra_headers_json, headers=True)
        self._validated_json(data.extra_body_json)

    def validate_input(self, data: ProviderInput) -> None:
        self._validate(data)
        self._models_from_input(data)

    def models_for(self, provider_id: int) -> list[ProviderModel]:
        return list(self.db.scalars(select(ProviderModel).where(ProviderModel.provider_id == provider_id, ProviderModel.enabled.is_(True)).order_by(ProviderModel.id)))

    def _models_from_input(self, data: ProviderInput) -> tuple[list[str], str]:
        source = data.models_text or data.model_id
        models: list[str] = []
        for line in source.splitlines():
            model_id = line.strip()
            if model_id and model_id not in models:
                models.append(model_id)
        if not models and data.provider_type == "mock":
            models = ["mock-v1"]
        if not models and data.provider_type != "custom_http":
            raise ValueError("请至少填写一个模型 ID")
        requested = data.default_model_id.strip()
        if requested and requested not in models:
            raise ValueError("默认模型必须来自模型列表")
        default_model = requested or (models[0] if models else "")
        return models, default_model

    def _replace_models(self, provider_id: int, models: list[str], default_model: str) -> None:
        self.db.execute(delete(ProviderModel).where(ProviderModel.provider_id == provider_id))
        for model_id in models:
            self.db.add(ProviderModel(provider_id=provider_id, model_id=model_id, display_name=model_id, is_default=model_id == default_model, enabled=True))

    def _migrate_legacy_models(self) -> None:
        changed = False
        for row in self.list_all():
            models = self.models_for(row.id)
            legacy_model = row.default_model_id or row.model_id or row.model
            if not models and legacy_model:
                self.db.add(ProviderModel(provider_id=row.id, model_id=legacy_model, display_name=legacy_model, is_default=True, enabled=True))
                changed = True
            if legacy_model and not row.default_model_id:
                row.default_model_id = row.model_id = row.model = legacy_model
                changed = True
        if changed:
            self.db.commit()

    def _validated_json(self, value: str, *, headers: bool = False) -> str:
        parsed = json.loads(value or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("Extra headers/body must be JSON objects")
        if headers:
            sensitive_headers = {"authorization", "proxy-authorization", "x-api-key", "api-key"}
            if any(str(key).casefold() in sensitive_headers for key in parsed):
                raise ValueError("Authentication headers must use api_key_env and cannot be stored in SQLite")
        if self._contains_secret_field(parsed):
            raise ValueError("Secret values must use api_key_env and cannot be stored in SQLite JSON")
        return json.dumps(parsed, ensure_ascii=False)

    def _contains_secret_field(self, value: Any) -> bool:
        secret_names = {"api_key", "apikey", "access_token", "auth_token", "secret", "password", "credential"}
        if isinstance(value, dict):
            return any(str(key).casefold().replace("-", "_") in secret_names or self._contains_secret_field(item) for key, item in value.items())
        if isinstance(value, list):
            return any(self._contains_secret_field(item) for item in value)
        return False

    def _unique_name(self, display_name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", display_name.casefold()).strip("_") or "provider"
        candidate, suffix = base, 2
        while self.get_by_name(candidate):
            candidate, suffix = f"{base}_{suffix}", suffix + 1
        return candidate

    def _required(self, provider_id: int) -> AIProvider:
        row = self.get(provider_id)
        if not row:
            raise LookupError("Provider not found")
        return row

    @staticmethod
    def _key_configured(provider_type: str, env_name: str) -> bool:
        return provider_type in {"mock", "ollama", "lm_studio"} or bool(env_name and os.getenv(env_name))


def provider_view(row: AIProvider, models: list[ProviderModel] | None = None) -> dict[str, Any]:
    result = {
        "id": row.id, "name": row.name, "display_name": row.display_name,
        "provider_type": row.provider_type, "base_url": row.base_url, "model_id": row.model_id,
        "default_model_id": row.default_model_id or row.model_id,
        "api_key_env": row.api_key_env, "key_configured": row.api_key_configured_status,
        "enabled": row.enabled, "is_default": row.is_default,
        "supports_json_mode": row.supports_json_mode, "supports_streaming": row.supports_streaming,
        "supports_vision": row.supports_vision, "supports_tools": row.supports_tools,
        "extra_headers_json": row.extra_headers_json, "extra_body_json": row.extra_body_json,
        "notes": row.notes, "timeout_seconds": row.timeout_seconds,
        "max_output_tokens": row.max_output_tokens, "temperature_default": row.temperature_default,
        "auth_scheme": row.auth_scheme,
    }
    if models is not None:
        result["models"] = [model.model_id for model in models]
        result["models_text"] = "\n".join(result["models"])
    return result


def provider_requires_api_key(provider_type: str, base_url: str) -> bool:
    if provider_type in {"mock", "ollama", "lm_studio", "custom_http"}:
        return False
    if provider_type == "openai_compatible" and ("127.0.0.1" in base_url or "localhost" in base_url):
        return False
    return provider_type in {"openai_compatible", "chat_completions", "responses", "openai_responses", "anthropic_messages", "gemini"}
