from __future__ import annotations

import json

import httpx
import pytest

from app.ai.deepseek import DeepSeekProvider
from app.ai.glm import GLMProvider
from app.ai.openai_compatible import AIOutputError, parse_json_object
from app.ai.safety import UnsafeAIContentError
from app.ai.mock import MockProvider
from app.schemas import GenerateNoteRequest


class SequenceClient:
    def __init__(self, contents: list[str]):
        self.contents = iter(contents)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        request = httpx.Request("POST", url, headers=kwargs.get("headers"))
        body = {"choices": [{"message": {"content": next(self.contents)}}]}
        return httpx.Response(200, request=request, json=body)


def valid_note_json(request: GenerateNoteRequest) -> str:
    return MockProvider().generate_note(request).model_dump_json()


def test_deepseek_provider_sends_strict_json_request():
    request = GenerateNoteRequest(topic="AI 编程", min_length=200, max_length=300, educational=True)
    client = SequenceClient([valid_note_json(request)])
    provider = DeepSeekProvider("https://api.deepseek.com", "test-secret-key", "deepseek-chat", client=client)
    note = provider.generate_note(request)
    assert note.title
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer test-secret-key"
    assert kwargs["json"]["response_format"] == {"type": "json_object"}
    assert "test-secret-key" not in json.dumps(kwargs["json"], ensure_ascii=False)


def test_glm_provider_uses_configured_v4_endpoint():
    request = GenerateNoteRequest(topic="AI 编程")
    client = SequenceClient([valid_note_json(request)])
    provider = GLMProvider("https://open.bigmodel.cn/api/paas/v4", "glm-test-secret", "glm-4", client=client)
    assert provider.generate_note(request).title
    assert client.calls[0][0] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def test_json_fence_is_repaired_locally_without_second_call():
    request = GenerateNoteRequest(topic="编程")
    content = f"```json\n{valid_note_json(request)}\n```"
    client = SequenceClient([content])
    note = DeepSeekProvider("https://api.deepseek.com", "test-secret-key", "deepseek-chat", client=client).generate_note(request)
    assert note.title
    assert len(client.calls) == 1


def test_invalid_json_triggers_exactly_one_repair_call():
    request = GenerateNoteRequest(topic="编程")
    client = SequenceClient(["not-json", valid_note_json(request)])
    note = DeepSeekProvider("https://api.deepseek.com", "test-secret-key", "deepseek-chat", client=client).generate_note(request)
    assert note.title
    assert len(client.calls) == 2
    assert "JSON 修复器" in client.calls[1][1]["json"]["messages"][0]["content"]


def test_schema_extra_field_triggers_repair():
    request = GenerateNoteRequest(topic="编程")
    invalid = json.loads(valid_note_json(request))
    invalid["unexpected"] = "must be rejected"
    client = SequenceClient([json.dumps(invalid, ensure_ascii=False), valid_note_json(request)])
    note = DeepSeekProvider("https://api.deepseek.com", "test-secret-key", "deepseek-chat", client=client).generate_note(request)
    assert note.title
    assert len(client.calls) == 2


def test_locally_unsafe_output_triggers_safe_rewrite():
    request = GenerateNoteRequest(topic="编程")
    unsafe = json.loads(valid_note_json(request))
    unsafe["body"] = "关注我" + unsafe["body"]
    client = SequenceClient([json.dumps(unsafe, ensure_ascii=False), valid_note_json(request)])
    note = DeepSeekProvider("https://api.deepseek.com", "test-secret-key", "deepseek-chat", client=client).generate_note(request)
    assert "关注我" not in note.body
    assert len(client.calls) == 2


def test_invalid_json_after_repair_fails():
    client = SequenceClient(["not-json", "still-not-json"])
    provider = DeepSeekProvider("https://api.deepseek.com", "test-secret-key", "deepseek-chat", client=client)
    with pytest.raises(AIOutputError, match="after one repair attempt"):
        provider.generate_note(GenerateNoteRequest(topic="编程"))
    assert len(client.calls) == 2


def test_sensitive_advice_request_is_blocked_before_network():
    client = SequenceClient([])
    provider = DeepSeekProvider("https://api.deepseek.com", "test-secret-key", "deepseek-chat", client=client)
    with pytest.raises(UnsafeAIContentError):
        provider.generate_note(GenerateNoteRequest(topic="给我投资建议"))
    assert not client.calls


def test_parse_json_object_rejects_non_object():
    with pytest.raises(ValueError):
        parse_json_object("[1, 2, 3]")
