import json
import os

from app.ai.base import AIProviderAdapter
from app.ai.anthropic import AnthropicProvider
from app.ai.custom_http import CustomHttpProvider
from app.ai.gemini import GeminiProvider
from app.ai.mock import MockProvider
from app.ai.ollama import OllamaProvider
from app.ai.responses import ResponsesProvider
from app.ai.deepseek import DeepSeekProvider
from app.ai.glm import GLMProvider
from app.ai.openai_compatible import OpenAICompatibleProvider
from app.config import Settings
from app.models import AIProvider


def create_provider(settings: Settings, name: str | None = None) -> AIProviderAdapter:
    provider_name = name or settings.ai["default_provider"]
    config = settings.ai["providers"].get(provider_name)
    if config is None:
        raise ValueError(f"Unknown AI provider: {provider_name}")
    if provider_name == "mock":
        return MockProvider(settings.interaction.get("sensitive_keywords", []))
    provider_types = {"deepseek": DeepSeekProvider, "glm": GLMProvider, "openai_compatible": OpenAICompatibleProvider}
    provider_type = provider_types.get(provider_name, OpenAICompatibleProvider)
    return provider_type(
        config["base_url"], settings.provider_api_key(provider_name), config["model"],
        timeout_seconds=float(config.get("timeout_seconds", 60)),
        supports_json_mode=bool(config.get("supports_json_mode", True)),
    )


def create_provider_from_profile(profile: AIProvider, settings: Settings) -> AIProviderAdapter:
    if not profile.enabled:
        raise ValueError("Provider is disabled")
    provider_type = profile.provider_type
    if provider_type == "mock":
        return MockProvider(settings.interaction.get("sensitive_keywords", []))
    provider_types = {
        "openai_compatible": OpenAICompatibleProvider,
        "chat_completions": OpenAICompatibleProvider,
        "responses": ResponsesProvider,
        "openai_responses": ResponsesProvider,
        "lm_studio": OpenAICompatibleProvider,
        "anthropic_messages": AnthropicProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
        "custom_http": CustomHttpProvider,
    }
    adapter = provider_types.get(provider_type)
    if not adapter:
        raise ValueError(f"Unsupported provider_type: {provider_type}")
    api_key = os.getenv(profile.api_key_env, "") if profile.api_key_env else ""
    requires_key = provider_type not in {"ollama", "lm_studio", "custom_http"}
    if requires_key and not api_key:
        raise ValueError(f"API key environment variable is not configured: {profile.api_key_env or '(empty)'}")
    kwargs = {
        "timeout_seconds": profile.timeout_seconds,
        "supports_json_mode": profile.supports_json_mode,
        "extra_headers": json.loads(profile.extra_headers_json or "{}"),
        "extra_body": json.loads(profile.extra_body_json or "{}"),
    }
    if provider_type == "anthropic_messages":
        kwargs["auth_scheme"] = profile.auth_scheme
    if provider_type in {"lm_studio", "custom_http"}:
        kwargs["requires_api_key"] = False
    return adapter(profile.base_url, api_key, profile.model_id, **kwargs)
