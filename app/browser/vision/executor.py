from __future__ import annotations

import json

from app.browser.vision.planner import plan_vision_action
from app.browser.vision.safety import assert_allowed_url, validate_vision_action
from app.browser.vision.screenshot import capture_vision_observation
from app.browser.vision.types import VisionActionResult
from app.config import Settings
from app.models import BrowserError
from app.repositories import AuditRepository


class VisionExecutionError(RuntimeError):
    pass


class VisionExecutor:
    def __init__(self, db, settings: Settings, audit: AuditRepository):
        self.db = db
        self.settings = settings
        self.audit = audit

    async def visual_click(self, page, *, goal: str, step: str, mode: str, target_id: int | None = None, final_confirm: bool = False) -> VisionActionResult:
        before = await capture_vision_observation(page, self.settings, step, suffix="before")
        try:
            plan = plan_vision_action(self.db, self.settings, before, goal)
            if not plan.ok or plan.action is None:
                raise VisionExecutionError(plan.refusal_reason or f"视觉模式没有找到目标：{goal}")
            action = plan.action
            validate_vision_action(before, action, self.settings, mode=mode, final_confirm=final_confirm)
            before_url = getattr(page, "url", "") or ""
            await page.mouse.click(action.x, action.y)
            await page.wait_for_timeout(1000)
            after = await capture_vision_observation(page, self.settings, step, suffix="after")
            assert_allowed_url(after.url, self.settings)
            result = VisionActionResult(
                action=action,
                before_screenshot_path=before.screenshot_path,
                after_screenshot_path=after.screenshot_path,
                before_url=before_url,
                after_url=after.url,
            )
            self._record_audit("success", step, goal, result, target_id=target_id)
            return result
        except Exception as exc:
            after_path = before.screenshot_path
            self._record_error(step, str(exc), before.screenshot_path, target_id=target_id, goal=goal, before_url=before.url)
            self.audit.record(
                "browser.vision_action",
                "failed",
                target_type="note" if target_id else "browser",
                target_id=target_id or "",
                error_message=str(exc),
                screenshot_path=after_path,
                metadata={"requested_goal": goal, "step": step, "before_url": before.url},
            )
            raise

    async def visual_type_text(self, page, *, goal: str, text: str, step: str, mode: str, target_id: int | None = None) -> VisionActionResult:
        result = await self.visual_click(page, goal=goal, step=step, mode=mode, target_id=target_id)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(500)
        after = await capture_vision_observation(page, self.settings, step, suffix="typed")
        typed_result = VisionActionResult(
            action=result.action,
            before_screenshot_path=result.before_screenshot_path,
            after_screenshot_path=after.screenshot_path,
            before_url=result.before_url,
            after_url=after.url,
        )
        self._record_audit("success", step, goal, typed_result, target_id=target_id, typed=True)
        return typed_result

    def _record_audit(self, status: str, step: str, goal: str, result: VisionActionResult, *, target_id: int | None = None, typed: bool = False) -> None:
        action = result.action
        self.audit.record(
            "browser.vision_action",
            status,
            target_type="note" if target_id else "browser",
            target_id=target_id or "",
            screenshot_path=result.after_screenshot_path,
            metadata={
                "screenshot_path": result.after_screenshot_path,
                "before_screenshot_path": result.before_screenshot_path,
                "requested_goal": goal,
                "detected_target": action.target_label,
                "action": "type_text" if typed else action.type,
                "x": action.x,
                "y": action.y,
                "confidence": action.confidence,
                "reason": action.reason,
                "before_url": result.before_url,
                "after_url": result.after_url,
                "step": step,
            },
        )

    def _record_error(self, step: str, message: str, screenshot_path: str, *, target_id: int | None, goal: str, before_url: str) -> None:
        self.db.add(BrowserError(
            note_id=target_id,
            mode="vision",
            step=step,
            selector_name="vision",
            action_type="browser.vision_action",
            error_message=message,
            screenshot_path=screenshot_path,
            metadata_json=json.dumps({
                "requested_goal": goal,
                "before_url": before_url,
                "screenshot_path": screenshot_path,
                "step": step,
            }, ensure_ascii=False),
        ))
        self.db.commit()
