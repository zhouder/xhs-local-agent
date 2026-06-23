from __future__ import annotations

import httpx
from urllib.parse import urlparse

from app.ai.endpoints import build_endpoint_url
from app.ai.openai_compatible import AIProviderError, OpenAICompatibleProvider, parse_json_object
from app.security import redact_secrets


AUTH_SCHEMES = {"auto", "bearer", "x_api_key", "both"}


def resolve_auth_scheme(auth_scheme: str, base_url: str) -> str:
    if auth_scheme not in AUTH_SCHEMES:
        raise ValueError(f"Unsupported Anthropic auth scheme: {auth_scheme}")
    if auth_scheme != "auto":
        return auth_scheme
    hostname = (urlparse(base_url).hostname or "").casefold()
    return "x_api_key" if hostname == "anthropic.com" or hostname.endswith(".anthropic.com") else "bearer"


def build_auth_headers(api_key: str, auth_scheme: str, base_url: str) -> dict[str, str]:
    resolved = resolve_auth_scheme(auth_scheme, base_url)
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if resolved in {"bearer", "both"}:
        headers["Authorization"] = f"Bearer {api_key}"
    if resolved in {"x_api_key", "both"}:
        headers["x-api-key"] = api_key
    return headers


def _content_block_types(envelope: dict) -> list[str]:
    content = envelope.get("content")
    if not isinstance(content, list):
        return []
    block_types: list[str] = []
    for block in content:
        if isinstance(block, dict):
            block_types.append(str(block.get("type") or "unknown"))
        else:
            block_types.append(type(block).__name__)
    return block_types


def extract_anthropic_text(envelope: dict) -> str:
    content = envelope.get("content")
    if not isinstance(content, list):
        raise AIProviderError("Anthropic Messages 返回中没有 text 内容块。content block types: none")

    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    if texts:
        return "\n".join(texts)

    block_types = ", ".join(_content_block_types(envelope)) or "none"
    raise AIProviderError(f"Anthropic Messages 返回中没有 text 内容块。content block types: {block_types}")


def _safe_text_summary(text: str, limit: int = 200) -> str:
    return " ".join(str(redact_secrets(text)).split())[:limit]


class AnthropicProvider(OpenAICompatibleProvider):
    provider_name = "anthropic_messages"

    def __init__(self, *args, auth_scheme: str = "auto", **kwargs):
        super().__init__(*args, **kwargs)
        resolve_auth_scheme(auth_scheme, self.base_url)
        self.auth_scheme = auth_scheme

    def _diagnostic(
        self,
        response: httpx.Response,
        request_url: str,
        *,
        invalid_json: bool,
        envelope: dict | None = None,
    ) -> str:
        content_type = response.headers.get("Content-Type", "未提供")
        safe_url = str(redact_secrets(request_url))

        if invalid_json:
            summary = str(redact_secrets(response.text))
            summary = " ".join(summary.split())[:300] or "（空响应）"
        else:
            if envelope is None:
                try:
                    parsed = response.json()
                    envelope = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    envelope = None
            if envelope is None:
                summary = str(redact_secrets(response.text))
                summary = " ".join(summary.split())[:300] or "（空响应）"
            else:
                text_preview = ""
                try:
                    text_preview = _safe_text_summary(extract_anthropic_text(envelope), 200)
                except AIProviderError:
                    text_preview = ""
                summary = (
                    f"id={envelope.get('id', '')}; model={envelope.get('model', '')}; "
                    f"role={envelope.get('role', '')}; content block types: "
                    f"{', '.join(_content_block_types(envelope)) or 'none'}; "
                    f"has text block: {'yes' if text_preview else 'no'}; "
                    f"text摘要: {text_preview}"
                )

        if self._api_key:
            summary = summary.replace(self._api_key, "[REDACTED]")
            safe_url = safe_url.replace(self._api_key, "[REDACTED]")
        reason = "返回内容不是 JSON" if invalid_json else f"HTTP {response.status_code}" if response.is_error else "响应解析失败"
        resolved = resolve_auth_scheme(self.auth_scheme, self.base_url)
        auth = self.auth_scheme if self.auth_scheme != "auto" else f"auto（实际 {resolved}）"
        return (
            f"{reason}。请求 URL：{safe_url}；状态码：{response.status_code}；"
            f"Content-Type：{content_type}；认证方式：{auth}；响应摘要：{summary}"
        )

    def _raw_completion(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens if max_tokens is not None else int(self.extra_body.get("max_tokens", 2048)),
            "temperature": temperature if temperature is not None else float(self.extra_body.get("temperature", 0.6)),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {**build_auth_headers(self._api_key, self.auth_scheme, self.base_url), **self.extra_headers}
        request_url = build_endpoint_url(self.base_url, "anthropic_messages")
        try:
            sender = self._client.post if self._client else httpx.post
            response = sender(request_url, headers=headers, json=payload, timeout=self.timeout_seconds)
        except httpx.TimeoutException:
            raise AIProviderError("anthropic_messages request timed out") from None
        except httpx.HTTPError:
            raise AIProviderError("anthropic_messages request failed due to a network error") from None

        try:
            parsed = response.json()
        except ValueError:
            raise AIProviderError(self._diagnostic(response, request_url, invalid_json=True)) from None
        envelope = parsed if isinstance(parsed, dict) else {}
        if response.is_error:
            raise AIProviderError(self._diagnostic(response, request_url, invalid_json=False, envelope=envelope)) from None
        if not isinstance(parsed, dict):
            raise AIProviderError(self._diagnostic(response, request_url, invalid_json=False)) from None
        return extract_anthropic_text(parsed)

    def test_connection(self) -> bool:
        content = self._raw_completion(
            "只输出一个 JSON 对象，不要使用 Markdown。",
            '请只返回 JSON：{"ok": true}',
            max_tokens=128,
            temperature=0,
        )
        try:
            return parse_json_object(content).get("ok") is True
        except (ValueError, TypeError):
            summary = _safe_text_summary(content, 200)
            if self._api_key:
                summary = summary.replace(self._api_key, "[REDACTED]")
            raise AIProviderError(f'模型返回了文本，但不是 {{"ok": true}} 测试 JSON。文本摘要：{summary}') from None
