from __future__ import annotations


def build_endpoint_url(base_url: str, api_format: str) -> str:
    base = base_url.strip().rstrip("/")
    normalized = {
        "openai_compatible": "chat_completions", "lm_studio": "chat_completions",
        "openai_responses": "responses",
    }.get(api_format, api_format)
    if normalized == "chat_completions":
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    if normalized == "anthropic_messages":
        if base.endswith("/v1/messages"):
            return base
        return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    if normalized == "responses":
        return base if base.endswith("/responses") else f"{base}/responses"
    return base


def normalize_ui_api_format(provider_type: str) -> str | None:
    if provider_type in {"openai_compatible", "lm_studio", "chat_completions"}:
        return "chat_completions"
    if provider_type in {"openai_responses", "responses"}:
        return "responses"
    if provider_type == "anthropic_messages":
        return "anthropic_messages"
    return None
