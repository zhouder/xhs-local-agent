from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.browser.vision.executor import VisionExecutor
from app.browser.vision.providers import parse_vision_plan
from app.browser.vision.safety import VisionSafetyError, validate_vision_action
from app.browser.vision.types import VisionAction, VisionObservation, VisionPlanResult
from app.models import AuditLog, BrowserError
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
    with pytest.raises(RuntimeError, match="合法 JSON"):
        parse_vision_plan("not json")


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

