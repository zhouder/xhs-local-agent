from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.anthropic import build_auth_headers, extract_anthropic_text
from app.ai.endpoints import build_endpoint_url
from app.ai.openai_compatible import parse_json_object
from app.browser.vision.prompts import VISION_SYSTEM_PROMPT, build_vision_user_prompt
from app.browser.vision.types import VisionObservation, VisionPlanResult
from app.config import Settings
from app.models import AIProvider, Setting


class VisionProviderError(RuntimeError):
    pass


CHAT_COMPLETIONS_PROVIDER_TYPES = {"openai_compatible", "chat_completions", "lm_studio"}
ANTHROPIC_MESSAGES_PROVIDER_TYPES = {"anthropic_messages"}
RESPONSES_PROVIDER_TYPES = {"responses", "openai_responses"}


def parse_vision_plan(content: str) -> VisionPlanResult:
    try:
        return VisionPlanResult.model_validate(parse_json_object(content))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise VisionProviderError("AI 已响应，但没有按页面视觉控制要求返回 JSON。可以换模型，或降低视觉模式依赖。") from exc


def _image_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _request_error(exc: httpx.HTTPStatusError) -> VisionProviderError:
    status_code = exc.response.status_code
    if status_code in {401, 403}:
        return VisionProviderError("AI Provider 鉴权失败，请检查 API Key。")
    if status_code in {400, 415, 422}:
        return VisionProviderError("当前 Provider 已尝试发送截图，但接口拒绝了图片输入。请确认该模型/API 支持图片输入，或在高级设置里覆盖 Provider/模型。")
    return VisionProviderError(f"页面视觉控制请求 AI 失败：HTTP {status_code}")


class BaseVisionProvider:
    payload_type = ""

    def _send(self, request_url: str, headers: dict, payload: dict) -> dict:
        try:
            sender = self._client.post if self._client else httpx.post
            response = sender(request_url, headers=headers, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            raise VisionProviderError("页面视觉控制请求 AI 超时。") from None
        except httpx.HTTPStatusError as exc:
            raise _request_error(exc) from None
        except httpx.HTTPError:
            raise VisionProviderError("页面视觉控制请求 AI 失败，请检查 Base URL 和网络。") from None
        except ValueError:
            raise VisionProviderError("页面视觉控制收到无法解析的 AI 响应。") from None


class ChatCompletionsVisionProvider(BaseVisionProvider):
    payload_type = "chat_completions_image_url"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
        extra_headers: dict | None = None,
        extra_body: dict | None = None,
        client: httpx.Client | None = None,
    ):
        if not base_url:
            raise VisionProviderError("页面视觉控制使用的 AI Provider 缺少 Base URL。")
        if not model:
            raise VisionProviderError("页面视觉控制使用的 AI Provider 缺少默认模型。")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self._client = client

    def plan(self, observation: VisionObservation, goal: str, forbidden_click_texts: list[str]) -> VisionPlanResult:
        image_data = _image_base64(observation.screenshot_path)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_vision_user_prompt(observation, goal, forbidden_click_texts)},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
                    ],
                },
            ],
            "temperature": 0,
        }
        for key, value in self.extra_body.items():
            if key not in {"model", "messages"}:
                payload[key] = value
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            content = self._send(build_endpoint_url(self.base_url, "chat_completions"), headers, payload)["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise VisionProviderError("页面视觉控制收到无法解析的 AI 响应。") from None
        if not isinstance(content, str):
            raise VisionProviderError("页面视觉控制收到非文本 AI 响应。")
        return parse_vision_plan(content)


class AnthropicMessagesVisionProvider(BaseVisionProvider):
    payload_type = "anthropic_messages_base64"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
        extra_headers: dict | None = None,
        extra_body: dict | None = None,
        auth_scheme: str = "auto",
        client: httpx.Client | None = None,
    ):
        if not base_url:
            raise VisionProviderError("页面视觉控制使用的 AI Provider 缺少 Base URL。")
        if not model:
            raise VisionProviderError("页面视觉控制使用的 AI Provider 缺少默认模型。")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self.auth_scheme = auth_scheme
        self._client = client

    def plan(self, observation: VisionObservation, goal: str, forbidden_click_texts: list[str]) -> VisionPlanResult:
        payload = {
            "model": self.model,
            "max_tokens": 1000,
            "temperature": 0,
            "system": VISION_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_vision_user_prompt(observation, goal, forbidden_click_texts)},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _image_base64(observation.screenshot_path)}},
                    ],
                }
            ],
        }
        for key, value in self.extra_body.items():
            if key not in {"model", "messages", "system"}:
                payload[key] = value
        headers = {**build_auth_headers(self._api_key, self.auth_scheme, self.base_url), **self.extra_headers}
        data = self._send(build_endpoint_url(self.base_url, "anthropic_messages"), headers, payload)
        return parse_vision_plan(extract_anthropic_text(data))


class ResponsesVisionProvider(BaseVisionProvider):
    payload_type = "responses_input_image"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
        extra_headers: dict | None = None,
        extra_body: dict | None = None,
        client: httpx.Client | None = None,
    ):
        if not base_url:
            raise VisionProviderError("页面视觉控制使用的 AI Provider 缺少 Base URL。")
        if not model:
            raise VisionProviderError("页面视觉控制使用的 AI Provider 缺少默认模型。")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self._client = client

    def plan(self, observation: VisionObservation, goal: str, forbidden_click_texts: list[str]) -> VisionPlanResult:
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"{VISION_SYSTEM_PROMPT}\n\n{build_vision_user_prompt(observation, goal, forbidden_click_texts)}"},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{_image_base64(observation.screenshot_path)}"},
                    ],
                }
            ],
            "temperature": 0,
        }
        for key, value in self.extra_body.items():
            if key not in {"model", "input"}:
                payload[key] = value
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = self._send(build_endpoint_url(self.base_url, "responses"), headers, payload)
        content = extract_responses_text(data)
        return parse_vision_plan(content)


def extract_responses_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    output = data.get("output")
    if isinstance(output, list):
        texts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for block in item.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
        if texts:
            return "\n".join(texts)
    raise VisionProviderError("页面视觉控制收到无法解析的 AI 响应。")


def setting_value(db: Session, key: str, default: str = "") -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value_json if row else default


def vision_setting_bool(db: Session, settings: Settings, key: str, default: bool = False) -> bool:
    value = setting_value(db, key, "")
    if value:
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(settings.browser.get(key.removeprefix("browser_"), default))


def visual_mode_enabled(db: Session, settings: Settings) -> bool:
    return vision_setting_bool(db, settings, "browser_visual_mode_enabled", bool(settings.browser.get("visual_mode_enabled", False)))


def selected_visual_provider(db: Session, settings: Settings) -> AIProvider | None:
    provider_id_text = setting_value(db, "browser_visual_mode_provider_id", "")
    provider_id = int(provider_id_text) if provider_id_text.isdigit() else settings.browser.get("visual_mode_provider_id")
    if provider_id:
        return db.get(AIProvider, int(provider_id))
    return db.query(AIProvider).filter(AIProvider.is_default.is_(True), AIProvider.enabled.is_(True)).first()


def selected_visual_model(db: Session, settings: Settings, provider: AIProvider) -> str:
    return setting_value(db, "browser_visual_mode_model", "") or settings.browser.get("visual_mode_model") or provider.default_model_id or provider.model_id


def selected_vision_provider(db: Session, settings: Settings) -> AIProvider | None:
    return selected_visual_provider(db, settings)


def selected_vision_model(db: Session, settings: Settings, provider: AIProvider) -> str:
    return selected_visual_model(db, settings, provider)


def visual_provider_source(db: Session, settings: Settings) -> str:
    provider_id_text = setting_value(db, "browser_visual_mode_provider_id", "")
    return "override" if provider_id_text or settings.browser.get("visual_mode_provider_id") else "default_ai_provider"


def create_vision_provider(db: Session, settings: Settings) -> BaseVisionProvider:
    provider = selected_visual_provider(db, settings)
    if not provider:
        raise VisionProviderError("未配置默认 AI Provider，无法使用页面视觉控制。")
    api_key = os.getenv(provider.api_key_env, "") if provider.api_key_env else ""
    if provider.provider_type != "lm_studio" and provider.api_key_env and not api_key:
        raise VisionProviderError("默认 AI Provider 的 API Key 未配置，无法使用页面视觉控制。")
    kwargs = {
        "base_url": provider.base_url,
        "api_key": api_key,
        "model": selected_visual_model(db, settings, provider),
        "timeout_seconds": provider.timeout_seconds,
        "extra_headers": json.loads(provider.extra_headers_json or "{}"),
        "extra_body": json.loads(provider.extra_body_json or "{}"),
    }
    if provider.provider_type in CHAT_COMPLETIONS_PROVIDER_TYPES:
        return ChatCompletionsVisionProvider(**kwargs)
    if provider.provider_type in ANTHROPIC_MESSAGES_PROVIDER_TYPES:
        return AnthropicMessagesVisionProvider(**kwargs, auth_scheme=provider.auth_scheme)
    if provider.provider_type in RESPONSES_PROVIDER_TYPES:
        return ResponsesVisionProvider(**kwargs)
    raise VisionProviderError(f"页面视觉控制暂不支持当前 API 格式：{provider.provider_type}。")
