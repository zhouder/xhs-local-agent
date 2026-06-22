from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.ai.base import AIProviderAdapter, coerce_generation_request
from app.ai.endpoints import build_endpoint_url
from app.ai.safety import validate_generation_request, validate_note_content
from app.schemas import GenerateNoteRequest, NoteContent, SafetyResult


T = TypeVar("T", bound=BaseModel)


class AIProviderError(RuntimeError):
    pass


class AIOutputError(AIProviderError):
    pass


class ReplyContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str


class CoverPromptContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str


def parse_json_object(content: str) -> dict:
    candidate = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("AI output must be a JSON object")
    return parsed


class OpenAICompatibleProvider(AIProviderAdapter):
    """Strict adapter for OpenAI-compatible chat-completions APIs."""

    provider_name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout_seconds: float = 60, supports_json_mode: bool = True, client: httpx.Client | None = None, extra_headers: dict | None = None, extra_body: dict | None = None, requires_api_key: bool = True):
        if requires_api_key and not api_key:
            raise ValueError(f"API key is required for provider {self.provider_name}")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.supports_json_mode = supports_json_mode
        self._client = client
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}

    def _raw_completion(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.6,
        }
        if self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        for key, value in self.extra_body.items():
            if key not in {"model", "messages"}:
                payload[key] = value
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            sender = self._client.post if self._client else httpx.post
            response = sender(
                build_endpoint_url(self.base_url, "chat_completions"),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise AIProviderError(f"{self.provider_name} request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise AIProviderError(f"{self.provider_name} request failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise AIProviderError(f"{self.provider_name} request failed due to a network error") from None
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise AIProviderError(f"{self.provider_name} returned an invalid response envelope") from None
        if not isinstance(content, str):
            raise AIProviderError(f"{self.provider_name} returned non-text content")
        return content

    def _structured_completion(self, system: str, user: str, schema: type[T], validator: Callable[[T], None] | None = None) -> T:
        content = self._raw_completion(system, user)
        try:
            result = schema.model_validate(parse_json_object(content))
            if validator:
                validator(result)
            return result
        except (json.JSONDecodeError, ValidationError, ValueError) as first_error:
            repair_system = (
                "你是 JSON 修复器。根据 JSON Schema 修复或重写输入，只输出一个 JSON 对象，不要使用 Markdown。"
                "不得添加 Schema 之外的字段，并继续遵守原始安全要求。"
            )
            repair_user = json.dumps({
                "schema": schema.model_json_schema(),
                "validation_error": str(first_error)[:1000],
                "invalid_output": content[:6000],
                "original_task": user[:3000],
            }, ensure_ascii=False)
            repaired = self._raw_completion(repair_system, repair_user)
            try:
                result = schema.model_validate(parse_json_object(repaired))
                if validator:
                    validator(result)
                return result
            except (json.JSONDecodeError, ValidationError, ValueError):
                raise AIOutputError(f"{self.provider_name} returned invalid structured output after one repair attempt") from None

    def generate_note(self, topic: str | GenerateNoteRequest, style: str = "实用、自然", audience: str = "科技爱好者", **options) -> NoteContent:
        request = coerce_generation_request(topic, style, audience, **options)
        validate_generation_request(request)
        system = (
            "你是合规的小红书图文编辑。只输出符合给定 JSON Schema 的对象。"
            "不得提供医疗、法律、金融或政治敏感建议；不得诱导关注、虚假承诺、夸大收益；"
            "不得生成互关、刷量、私信领取、评论区口令等平台违规互动话术。"
            "争议性标题仅可做理性观点讨论，不得制造对立、恐慌或误导。"
        )
        user = json.dumps({
            "task": "生成小红书图文笔记",
            "parameters": request.model_dump(),
            "schema": NoteContent.model_json_schema(),
        }, ensure_ascii=False)
        return self._structured_completion(system, user, NoteContent, lambda note: validate_note_content(note, request))

    def generate_reply(self, message: str, context: str = "") -> str:
        result = self._structured_completion(
            "生成简短、自然、安全的普通互动回复，只输出 JSON。不得生成诱导互动或敏感建议。",
            json.dumps({"message": message, "context": context}, ensure_ascii=False),
            ReplyContent,
        )
        return result.reply

    def classify_safety(self, text: str) -> SafetyResult:
        return self._structured_completion(
            "判断文本是否涉及敏感建议、虚假承诺、夸大收益或平台违规互动，只输出 JSON。",
            json.dumps({"text": text, "schema": SafetyResult.model_json_schema()}, ensure_ascii=False),
            SafetyResult,
        )

    def generate_cover_prompt(self, note: NoteContent) -> str:
        result = self._structured_completion(
            "生成合规的图片提示词，只输出 JSON。",
            json.dumps({"note": note.model_dump(), "schema": CoverPromptContent.model_json_schema()}, ensure_ascii=False),
            CoverPromptContent,
        )
        return result.prompt

    def chat_text(self, prompt: str) -> str:
        return self._raw_completion("直接完成用户请求。", prompt)

    def chat_json(self, prompt: str) -> dict:
        content = self._raw_completion("只输出一个 JSON 对象，不要使用 Markdown。", prompt)
        try:
            return parse_json_object(content)
        except (json.JSONDecodeError, ValueError):
            repaired = self._raw_completion("修复输入并只输出一个合法 JSON 对象。", content[:6000])
            try:
                return parse_json_object(repaired)
            except (json.JSONDecodeError, ValueError):
                raise AIOutputError(f"{self.provider_name} test output was not valid JSON after one repair attempt") from None
