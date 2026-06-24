from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.browser.vision.executor import VisionExecutor
from app.browser.vision.providers import AnthropicMessagesVisionProvider, ChatCompletionsVisionProvider, ResponsesVisionProvider, create_vision_provider, parse_vision_plan, selected_visual_model, selected_visual_provider, visual_provider_source
from app.browser.vision.safety import VisionSafetyError, validate_vision_action
from app.browser.vision.types import VisionAction, VisionObservation, VisionPlanResult
from app.models import AIProvider, AuditLog, BrowserError, Setting
from app.repositories import AuditRepository


def observation(**kwargs) -> VisionObservation:
    data = {
        "screenshot_path": "screen.png",
        "url": "https://creator.xiaohongshu.com/publish/publish?target=image",
        "title": "xhs",
        "viewport_width": 1200,
        "viewport_height": 800,
        "page_text_summary": "",
        "step": "click_text_to_image_entry",
    }
    data.update(kwargs)
    return VisionObservation(**data)


def action(**kwargs) -> VisionAction:
    data = {
        "type": "click",
        "target_label": "文字配图",
        "x": 100,
        "y": 200,
        "confidence": 0.9,
        "reason": "visible button",
        "visible_text": "文字配图",
    }
    data.update(kwargs)
    return VisionAction(**data)


class FakeHttpClient:
    def __init__(self, data=None, status_code=200):
        self.data = data or {}
        self.status_code = status_code
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "json": json or {}, "timeout": timeout})
        response = httpx.Response(self.status_code, json=self.data, request=httpx.Request("POST", url))
        if self.status_code >= 400:
            response.raise_for_status()
        return response


def write_png(tmp_path: Path) -> str:
    path = tmp_path / "screen.png"
    path.write_bytes(b"fake-png")
    return str(path)


def add_provider(db, *, name="default", provider_type="chat_completions", model="model-a", is_default=True, api_key_env=""):
    row = AIProvider(
        name=name,
        display_name=name.title(),
        provider_type=provider_type,
        base_url="https://api.example.com/v1",
        model=model,
        model_id=model,
        default_model_id=model,
        api_key_env=api_key_env,
        enabled=True,
        is_default=is_default,
        extra_headers_json="{}",
        extra_body_json="{}",
    )
    db.add(row)
    db.commit()
    return row


def set_local_setting(db, key: str, value: str):
    db.add(Setting(key=key, value_json=value))
    db.commit()


def test_parse_vision_provider_json():
    plan = parse_vision_plan(json.dumps({
        "ok": True,
        "action": {"type": "click", "target_label": "文字配图", "x": 10, "y": 20, "confidence": 0.91, "reason": "seen", "visible_text": "文字配图"},
        "targets": [],
        "refusal_reason": None,
    }))
    assert plan.ok is True
    assert plan.action.target_label == "文字配图"


def test_parse_vision_provider_rejects_non_json():
    with pytest.raises(RuntimeError, match="没有按页面视觉控制要求返回 JSON"):
        parse_vision_plan("not json")


def test_visual_provider_defaults_to_default_ai_provider(db, settings):
    provider = add_provider(db, name="main", model="text-model")

    selected = selected_visual_provider(db, settings)

    assert selected.id == provider.id
    assert selected_visual_model(db, settings, selected) == "text-model"
    assert visual_provider_source(db, settings) == "default_ai_provider"


def test_visual_provider_override_and_model_override(db, settings):
    add_provider(db, name="main", model="main-model")
    override = add_provider(db, name="override", model="override-default", is_default=False)
    set_local_setting(db, "browser_visual_mode_provider_id", str(override.id))
    set_local_setting(db, "browser_visual_mode_model", "override-model")

    selected = selected_visual_provider(db, settings)

    assert selected.id == override.id
    assert selected_visual_model(db, settings, selected) == "override-model"
    assert visual_provider_source(db, settings) == "override"


def test_visual_provider_without_default_has_clear_error(db, settings):
    with pytest.raises(RuntimeError, match="未配置默认 AI Provider"):
        create_vision_provider(db, settings)


def test_visual_provider_missing_api_key_has_clear_error(db, settings, monkeypatch):
    add_provider(db, api_key_env="MISSING_VISUAL_KEY")
    monkeypatch.delenv("MISSING_VISUAL_KEY", raising=False)

    with pytest.raises(RuntimeError, match="默认 AI Provider 的 API Key 未配置"):
        create_vision_provider(db, settings)


def test_visual_provider_does_not_gate_by_model_name(db, settings):
    add_provider(db, model="plain-text-looking-model")

    provider = create_vision_provider(db, settings)

    assert provider.model == "plain-text-looking-model"


def test_visual_provider_supports_anthropic_messages(db, settings):
    add_provider(db, provider_type="anthropic_messages")

    provider = create_vision_provider(db, settings)

    assert isinstance(provider, AnthropicMessagesVisionProvider)
    assert provider.payload_type == "anthropic_messages_base64"


def test_visual_provider_supports_responses(db, settings):
    add_provider(db, provider_type="responses")

    provider = create_vision_provider(db, settings)

    assert isinstance(provider, ResponsesVisionProvider)
    assert provider.payload_type == "responses_input_image"


def test_chat_completions_vision_payload(tmp_path):
    client = FakeHttpClient({"choices": [{"message": {"content": '{"ok": false, "action": null, "targets": [], "refusal_reason": "no"}'}}]})
    provider = ChatCompletionsVisionProvider(base_url="https://api.example.com/v1", api_key="key", model="plain-model", client=client)

    provider.plan(observation(screenshot_path=write_png(tmp_path)), "找文字配图", [])

    payload = client.requests[0]["json"]
    assert payload["messages"][1]["content"][1]["type"] == "image_url"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_anthropic_messages_vision_payload_and_bearer_auth(tmp_path):
    client = FakeHttpClient({
        "content": [
            {"type": "thinking", "thinking": "private reasoning should not leak"},
            {"type": "text", "text": '{"ok": false, "action": null, "targets": [], "refusal_reason": "no"}'},
        ]
    })
    provider = AnthropicMessagesVisionProvider(base_url="https://api.openmodel.ai", api_key="secret", model="deepseek-v4-flash", auth_scheme="auto", client=client)

    plan = provider.plan(observation(screenshot_path=write_png(tmp_path)), "找文字配图", [])

    request = client.requests[0]
    assert plan.refusal_reason == "no"
    assert request["headers"]["Authorization"] == "Bearer secret"
    assert "x-api-key" not in request["headers"]
    assert request["json"]["messages"][0]["content"][1]["type"] == "image"
    assert request["json"]["messages"][0]["content"][1]["source"]["type"] == "base64"


def test_anthropic_messages_official_auto_uses_x_api_key(tmp_path):
    client = FakeHttpClient({"content": [{"type": "text", "text": '{"ok": false, "action": null, "targets": [], "refusal_reason": "no"}'}]})
    provider = AnthropicMessagesVisionProvider(base_url="https://api.anthropic.com", api_key="secret", model="claude", auth_scheme="auto", client=client)

    provider.plan(observation(screenshot_path=write_png(tmp_path)), "找文字配图", [])

    assert client.requests[0]["headers"]["x-api-key"] == "secret"
    assert "Authorization" not in client.requests[0]["headers"]


def test_responses_vision_payload_and_output_text(tmp_path):
    client = FakeHttpClient({"output_text": '{"ok": false, "action": null, "targets": [], "refusal_reason": "no"}'})
    provider = ResponsesVisionProvider(base_url="https://api.example.com/v1", api_key="key", model="model", client=client)

    provider.plan(observation(screenshot_path=write_png(tmp_path)), "找文字配图", [])

    payload = client.requests[0]["json"]
    assert payload["input"][0]["content"][1]["type"] == "input_image"
    assert payload["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("status_code, message", [
    (401, "鉴权失败"),
    (403, "鉴权失败"),
    (400, "接口拒绝了图片输入"),
    (415, "接口拒绝了图片输入"),
    (422, "接口拒绝了图片输入"),
])
def test_vision_http_errors_are_friendly(tmp_path, status_code, message):
    client = FakeHttpClient({"error": "bad"}, status_code=status_code)
    provider = ChatCompletionsVisionProvider(base_url="https://api.example.com/v1", api_key="key", model="model", client=client)

    with pytest.raises(RuntimeError, match=message):
        provider.plan(observation(screenshot_path=write_png(tmp_path)), "找文字配图", [])


def test_vision_diagnostic_prints_provider_type_and_payload():
    source = Path("scripts/check_xhs_selectors.py").read_text(encoding="utf-8")
    assert "provider_type:" in source
    assert "vision payload:" in source


def test_vision_safety_rejects_low_confidence(settings):
    with pytest.raises(VisionSafetyError, match="置信度低于阈值"):
        validate_vision_action(observation(), action(confidence=0.2), settings, mode="fill_only")


def test_vision_safety_rejects_forbidden_publish(settings):
    with pytest.raises(VisionSafetyError, match="目标文字包含"):
        validate_vision_action(observation(), action(target_label="立即发布", visible_text="立即发布"), settings, mode="fill_only")


def test_vision_safety_rejects_wrong_domain(settings):
    with pytest.raises(VisionSafetyError, match="域名不在允许列表"):
        validate_vision_action(observation(url="https://example.com"), action(), settings, mode="fill_only")


def test_vision_safety_rejects_out_of_viewport(settings):
    with pytest.raises(VisionSafetyError, match="坐标超出"):
        validate_vision_action(observation(), action(x=1300), settings, mode="fill_only")


class FakeMouse:
    def __init__(self, page):
        self.page = page

    async def click(self, x, y):
        self.page.clicks.append((x, y))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.keys.append(key)

    async def insert_text(self, text):
        self.page.typed.append(text)


class FakeLocator:
    async def inner_text(self, timeout=0):
        return "文字配图 生成图片"


class FakeVisionPage:
    def __init__(self, tmp_path: Path):
        self.url = "https://creator.xiaohongshu.com/publish/publish?target=image"
        self.viewport_size = {"width": 1200, "height": 800}
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard(self)
        self.clicks = []
        self.keys = []
        self.typed = []
        self.tmp_path = tmp_path

    async def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"png")

    async def title(self):
        return "creator"

    def locator(self, selector):
        return FakeLocator()

    async def wait_for_timeout(self, milliseconds):
        return None


def test_visual_click_writes_audit_log(db, settings, tmp_path, monkeypatch):
    settings.browser["screenshots_dir"] = str(tmp_path)
    page = FakeVisionPage(tmp_path)

    def fake_plan(db_arg, settings_arg, obs, goal):
        return VisionPlanResult(ok=True, action=action(), targets=[], refusal_reason=None)

    monkeypatch.setattr("app.browser.vision.executor.plan_vision_action", fake_plan)
    result = asyncio.run(VisionExecutor(db, settings, AuditRepository(db)).visual_click(page, goal="点击文字配图", step="click_text_to_image_entry", mode="fill_only", target_id=1))

    assert page.clicks == [(100, 200)]
    assert result.after_screenshot_path
    log = db.scalar(select(AuditLog).where(AuditLog.action_type == "browser.vision_action", AuditLog.status == "success"))
    assert log is not None
    assert "点击文字配图" in log.metadata_json


def test_visual_type_text_inserts_text_and_screenshots(db, settings, tmp_path, monkeypatch):
    settings.browser["screenshots_dir"] = str(tmp_path)
    page = FakeVisionPage(tmp_path)
    monkeypatch.setattr(
        "app.browser.vision.executor.plan_vision_action",
        lambda db_arg, settings_arg, obs, goal: VisionPlanResult(ok=True, action=action(target_label="输入区"), targets=[], refusal_reason=None),
    )

    asyncio.run(VisionExecutor(db, settings, AuditRepository(db)).visual_type_text(page, goal="标题输入框", text="hello", step="fill_title", mode="fill_only"))

    assert page.typed == ["hello"]
    assert "Control+A" in page.keys


def test_visual_failure_writes_browser_error(db, settings, tmp_path, monkeypatch):
    settings.browser["screenshots_dir"] = str(tmp_path)
    page = FakeVisionPage(tmp_path)
    monkeypatch.setattr(
        "app.browser.vision.executor.plan_vision_action",
        lambda db_arg, settings_arg, obs, goal: VisionPlanResult(ok=False, action=None, targets=[], refusal_reason="没有看到文字配图按钮"),
    )

    with pytest.raises(RuntimeError, match="没有看到文字配图按钮"):
        asyncio.run(VisionExecutor(db, settings, AuditRepository(db)).visual_click(page, goal="点击文字配图", step="click_text_to_image_entry", mode="fill_only"))

    assert db.scalar(select(BrowserError).where(BrowserError.action_type == "browser.vision_action")) is not None
