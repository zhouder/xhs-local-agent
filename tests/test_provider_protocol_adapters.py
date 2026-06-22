import httpx
import pytest

from app.ai.anthropic import AnthropicProvider
from app.ai.custom_http import CustomHttpProvider
from app.ai.gemini import GeminiProvider
from app.ai.ollama import OllamaProvider
from app.ai.openai_compatible import AIProviderError


class ResponseClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", url), json=self.payload)


def test_anthropic_messages_adapter():
    client = ResponseClient({"content": [{"type": "text", "text": '{"ok": true}'}]})
    provider = AnthropicProvider("https://api.anthropic.com", "secret", "claude-sonnet", client=client)
    assert provider.test_connection()
    url, kwargs = client.calls[0]
    assert url.endswith("/v1/messages")
    assert kwargs["headers"]["x-api-key"] == "secret"
    assert kwargs["json"]["model"] == "claude-sonnet"


def test_gemini_adapter():
    client = ResponseClient({"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})
    provider = GeminiProvider("https://generativelanguage.googleapis.com", "secret", "gemini-flash", client=client)
    assert provider.test_connection()
    assert "/v1beta/models/gemini-flash:generateContent" in client.calls[0][0]


def test_ollama_adapter_without_api_key():
    client = ResponseClient({"message": {"content": '{"ok": true}'}})
    provider = OllamaProvider("http://127.0.0.1:11434", "", "qwen", client=client)
    assert provider.test_connection()
    assert client.calls[0][0] == "http://127.0.0.1:11434/api/chat"


def test_custom_http_is_friendly_placeholder():
    provider = CustomHttpProvider("https://example.test/generate", "", "", requires_api_key=False)
    with pytest.raises(AIProviderError, match="not implemented"):
        provider.test_connection()
