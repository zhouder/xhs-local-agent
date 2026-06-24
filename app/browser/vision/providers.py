from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.endpoints import build_endpoint_url
from app.ai.openai_compatible import parse_json_object
from app.browser.vision.prompts import VISION_SYSTEM_PROMPT, build_vision_user_prompt
from app.browser.vision.types import VisionObservation, VisionPlanResult
from app.config import Settings
from app.models import AIProvider, Setting


class VisionProviderError(RuntimeError):
    pass


def parse_vision_plan(content: str) -> VisionPlanResult:
    try:
        return VisionPlanResult.model_validate(parse_json_object(content))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise VisionProviderError("视觉模型没有返回合法 JSON。") from exc


class OpenAICompatibleVisionProvider:
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
            raise VisionProviderError("视觉 Provider 缺少 Base URL。")
        if not model:
            raise VisionProviderError("视觉 Provider 缺少模型名称。")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self._client = client

    def plan(self, observation: VisionObservation, goal: str, forbidden_click_texts: list[str]) -> VisionPlanResult:
        image_data = base64.b64encode(Path(observation.screenshot_path).read_bytes()).decode("ascii")
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
            sender = self._client.post if self._client else httpx.post
            response = sender(build_endpoint_url(self.base_url, "chat_completions"), headers=headers, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except httpx.TimeoutException:
            raise VisionProviderError("视觉模型请求超时。") from None
        except httpx.HTTPStatusError as exc:
            raise VisionProviderError(f"视觉模型请求失败：HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            raise VisionProviderError("视觉模型返回了无法解析的响应。") from None
        if not isinstance(content, str):
            raise VisionProviderError("视觉模型返回了非文本内容。")
        return parse_vision_plan(content)


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


def selected_vision_provider(db: Session, settings: Settings) -> AIProvider | None:
    provider_id_text = setting_value(db, "browser_visual_mode_provider_id", "")
    provider_id = int(provider_id_text) if provider_id_text.isdigit() else settings.browser.get("visual_mode_provider_id")
    if provider_id:
        return db.get(AIProvider, int(provider_id))
    return db.query(AIProvider).filter(AIProvider.is_default.is_(True), AIProvider.enabled.is_(True)).first()


def selected_vision_model(db: Session, settings: Settings, provider: AIProvider) -> str:
    return setting_value(db, "browser_visual_mode_model", "") or settings.browser.get("visual_mode_model") or provider.default_model_id or provider.model_id


def create_vision_provider(db: Session, settings: Settings) -> OpenAICompatibleVisionProvider:
    provider = selected_vision_provider(db, settings)
    if not provider:
        raise VisionProviderError("未配置视觉模型 Provider。")
    if provider.provider_type not in {"openai_compatible", "chat_completions", "lm_studio"}:
        raise VisionProviderError("视觉模式当前只支持 OpenAI-compatible Chat Completions Provider。")
    api_key = os.getenv(provider.api_key_env, "") if provider.api_key_env else ""
    if provider.provider_type != "lm_studio" and provider.api_key_env and not api_key:
        raise VisionProviderError("视觉 Provider API Key 未配置。")
    return OpenAICompatibleVisionProvider(
        base_url=provider.base_url,
        api_key=api_key,
        model=selected_vision_model(db, settings, provider),
        timeout_seconds=provider.timeout_seconds,
        extra_headers=json.loads(provider.extra_headers_json or "{}"),
        extra_body=json.loads(provider.extra_body_json or "{}"),
    )
