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


class AnthropicProvider(OpenAICompatibleProvider):
    provider_name = "anthropic_messages"

    def __init__(self, *args, auth_scheme: str = "auto", **kwargs):
        super().__init__(*args, **kwargs)
        resolve_auth_scheme(auth_scheme, self.base_url)
        self.auth_scheme = auth_scheme

    def _diagnostic(self, response: httpx.Response, request_url: str, *, invalid_json: bool) -> str:
        content_type = response.headers.get("Content-Type", "未提供")
        summary = str(redact_secrets(response.text))
        safe_url = str(redact_secrets(request_url))
        if self._api_key:
            summary = summary.replace(self._api_key, "[REDACTED]")
            safe_url = safe_url.replace(self._api_key, "[REDACTED]")
        summary = " ".join(summary.split())[:300] or "（空响应）"
        reason = "返回内容不是 JSON" if invalid_json else "请求失败"
        resolved = resolve_auth_scheme(self.auth_scheme, self.base_url)
        auth = self.auth_scheme if self.auth_scheme != "auto" else f"auto（实际 {resolved}）"
        return (
            f"{reason}。请求 URL：{safe_url}；状态码：{response.status_code}；"
            f"Content-Type：{content_type}；认证方式：{auth}；响应摘要：{summary}"
        )

    def _raw_completion(
        self, system: str, user: str, *, max_tokens: int | None = None, temperature: float | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens if max_tokens is not None else int(self.extra_body.get("max_tokens", 2048)),
            "temperature": temperature if temperature is not None else float(self.extra_body.get("temperature", 0.6)),
            "system": system, "messages": [{"role": "user", "content": user}],
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
            envelope = response.json()
        except ValueError:
            raise AIProviderError(self._diagnostic(response, request_url, invalid_json=True)) from None
        if response.is_error:
            raise AIProviderError(self._diagnostic(response, request_url, invalid_json=False)) from None
        try:
            content = envelope["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise AIProviderError(self._diagnostic(response, request_url, invalid_json=False)) from None
        if not isinstance(content, str):
            raise AIProviderError(self._diagnostic(response, request_url, invalid_json=False))
        return content

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
            raise AIProviderError("Anthropic Messages 返回内容不是可解析的测试 JSON。") from None
