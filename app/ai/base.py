from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas import GenerateNoteRequest, NoteContent, SafetyResult


def coerce_generation_request(
    topic: str | GenerateNoteRequest,
    style: str = "实用、自然",
    audience: str = "科技爱好者",
    *,
    min_length: int = 200,
    max_length: int = 600,
    controversial_title: bool = False,
    educational: bool = False,
    growth_oriented: bool = True,
) -> GenerateNoteRequest:
    if isinstance(topic, GenerateNoteRequest):
        return topic
    return GenerateNoteRequest(
        topic=topic, style=style, audience=audience,
        min_length=min_length, max_length=max_length,
        controversial_title=controversial_title,
        educational=educational, growth_oriented=growth_oriented,
    )


class AIProviderAdapter(ABC):
    @abstractmethod
    def generate_note(self, topic: str | GenerateNoteRequest, style: str = "实用、自然", audience: str = "科技爱好者", **options) -> NoteContent: ...

    @abstractmethod
    def generate_reply(self, message: str, context: str = "") -> str: ...

    @abstractmethod
    def classify_safety(self, text: str) -> SafetyResult: ...

    @abstractmethod
    def generate_cover_prompt(self, note: NoteContent) -> str: ...

    def chat_text(self, prompt: str) -> str:
        raise NotImplementedError("chat_text is not implemented for this provider")

    def chat_json(self, prompt: str) -> dict:
        raise NotImplementedError("chat_json is not implemented for this provider")

    def test_connection(self) -> bool:
        result = self.chat_json('请只回复 JSON：{"ok": true}')
        return result.get("ok") is True
