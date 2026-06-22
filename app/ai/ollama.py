from __future__ import annotations

import httpx

from app.ai.openai_compatible import AIProviderError, OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    provider_name = "ollama"

    def __init__(self, base_url: str, api_key: str, model: str, **kwargs):
        super().__init__(base_url, "", model, requires_api_key=False, **kwargs)

    def _raw_completion(self, system: str, user: str) -> str:
        payload = {
            "model": self.model, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": float(self.extra_body.get("temperature", 0.6))},
        }
        try:
            sender = self._client.post if self._client else httpx.post
            response = sender(f"{self.base_url}/api/chat", headers=self.extra_headers, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AIProviderError(f"ollama request failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise AIProviderError("ollama request failed; verify that the local Ollama service is running") from None
        try:
            return response.json()["message"]["content"]
        except (KeyError, TypeError, ValueError):
            raise AIProviderError("ollama returned an invalid response envelope") from None
