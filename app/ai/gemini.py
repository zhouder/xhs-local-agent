from __future__ import annotations

from urllib.parse import quote

import httpx

from app.ai.openai_compatible import AIProviderError, OpenAICompatibleProvider


class GeminiProvider(OpenAICompatibleProvider):
    provider_name = "gemini"

    def _raw_completion(self, system: str, user: str) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": float(self.extra_body.get("temperature", 0.6)), "responseMimeType": "application/json"},
        }
        url = f"{self.base_url}/v1beta/models/{quote(self.model, safe='')}:generateContent?key={quote(self._api_key, safe='')}"
        try:
            sender = self._client.post if self._client else httpx.post
            response = sender(url, headers={"Content-Type": "application/json", **self.extra_headers}, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AIProviderError(f"gemini request failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise AIProviderError("gemini request failed due to a network error") from None
        try:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise AIProviderError("gemini returned an invalid response envelope") from None
