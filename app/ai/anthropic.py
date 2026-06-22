from __future__ import annotations

import httpx

from app.ai.endpoints import build_endpoint_url
from app.ai.openai_compatible import AIProviderError, OpenAICompatibleProvider


class AnthropicProvider(OpenAICompatibleProvider):
    provider_name = "anthropic_messages"

    def _raw_completion(self, system: str, user: str) -> str:
        payload = {
            "model": self.model, "max_tokens": int(self.extra_body.get("max_tokens", 2048)),
            "temperature": float(self.extra_body.get("temperature", 0.6)),
            "system": system, "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self._api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json", **self.extra_headers}
        try:
            sender = self._client.post if self._client else httpx.post
            response = sender(build_endpoint_url(self.base_url, "anthropic_messages"), headers=headers, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise AIProviderError("anthropic_messages request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise AIProviderError(f"anthropic_messages request failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise AIProviderError("anthropic_messages request failed due to a network error") from None
        try:
            return response.json()["content"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise AIProviderError("anthropic_messages returned an invalid response envelope") from None
