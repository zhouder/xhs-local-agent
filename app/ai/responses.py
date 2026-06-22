from __future__ import annotations

import httpx

from app.ai.endpoints import build_endpoint_url
from app.ai.openai_compatible import AIProviderError, OpenAICompatibleProvider


class ResponsesProvider(OpenAICompatibleProvider):
    provider_name = "responses"

    def _raw_completion(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": user}]}],
            "temperature": 0.6,
        }
        for key, value in self.extra_body.items():
            if key not in {"model", "input", "instructions"}:
                payload[key] = value
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            sender = self._client.post if self._client else httpx.post
            response = sender(build_endpoint_url(self.base_url, "responses"), headers=headers, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise AIProviderError("responses request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise AIProviderError(f"responses request failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise AIProviderError("responses request failed due to a network error") from None
        try:
            data = response.json()
            if isinstance(data.get("output_text"), str):
                return data["output_text"]
            return data["output"][0]["content"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise AIProviderError("responses returned an invalid response envelope") from None
