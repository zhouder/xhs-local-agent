from __future__ import annotations

import base64
import html
import json
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright
from sqlalchemy.orm import Session

from app.config import ROOT, Settings
from app.database import utcnow
from app.models import BrowserError, NoteStatus, Setting
from app.repositories import AuditRepository, NoteRepository
from app.services.materials import validate_image_assets
from app.services.notifications import Notifier
from app.services.policy import PolicyEngine
from app.services.state_machine import transition_note


PUBLISH_MODES = {"dry_run", "fill_only", "publish_after_final_confirm"}


class XHSBrowser:
    def __init__(self, db: Session, settings: Settings, notifier: Notifier):
        self.db, self.settings, self.notifier = db, settings, notifier
        self.notes = NoteRepository(db)
        self.audit = AuditRepository(db)
        with (ROOT / "app/browser/selectors/xhs.yaml").open(encoding="utf-8") as stream:
            self.selectors = yaml.safe_load(stream)["publish"]

    def fill_approved_note(self, note_id: int, *, dry_run: bool = True, mode: str | None = None) -> str:
        if mode is None and not dry_run:
            self.audit.record("browser.real_publish", "blocked", target_type="note", target_id=note_id, output_summary="use_explicit_fill_mode_and_final_confirm")
            raise PermissionError("Real publish is disabled unless explicit safe publish mode and final confirm are used.")
        mode = mode or ("dry_run" if dry_run else "fill_only")
        if mode not in PUBLISH_MODES:
            raise ValueError(f"Unsupported publish mode: {mode}")
        note = self._approved_note(note_id)
        assets = self.notes.media_paths(note.id)
        ok, reason = validate_image_assets(assets)
        if not ok:
            self.audit.record("browser.fill_publish", "blocked", target_type="note", target_id=note_id, error_message=reason, metadata={"mode": mode})
            raise ValueError(reason)
        screenshot = self._dry_run_preview(note, assets) if mode == "dry_run" else self._run_browser_fill(note, assets, mode=mode, click_publish=False)
        transition_note(note, NoteStatus.PUBLISHING)
        transition_note(note, NoteStatus.WAITING_FINAL_CONFIRM)
        note.publish_mode = mode
        note.publish_screenshot_path = screenshot
        note.publish_error_message = "dry_run_preview: 本地模拟预览，未打开小红书。" if mode == "dry_run" else ""
        self.db.commit()
        self.audit.record("browser.fill_publish", "success", target_type="note", target_id=note.id, screenshot_path=screenshot, metadata={"mode": mode, "asset_count": len(assets)})
        self._notify("Publish page filled", f"note_id={note.id}; status={note.status}; review screenshot ready.", note.id)
        return screenshot

    def final_confirm_publish(self, note_id: int) -> str:
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        if note.status != NoteStatus.WAITING_FINAL_CONFIRM:
            self.audit.record("browser.final_confirm", "blocked", target_type="note", target_id=note_id, output_summary="not_waiting_final_confirm")
            raise PermissionError("Final confirm is only allowed from waiting_final_confirm.")
        if not note.publish_screenshot_path:
            self.audit.record("browser.final_confirm", "blocked", target_type="note", target_id=note_id, output_summary="missing_fill_screenshot")
            raise PermissionError("A fill screenshot is required before final confirm.")
        if note.publish_mode == "dry_run":
            self.audit.record("browser.final_confirm", "blocked", target_type="note", target_id=note_id, output_summary="dry_run_preview")
            raise PermissionError("dry_run 是本地模拟预览，不能用于最终发布。请先使用 fill_only 或 publish_after_final_confirm。")
        assets = self.notes.media_paths(note.id)
        ok, reason = validate_image_assets(assets)
        if not ok:
            self.audit.record("browser.final_confirm", "blocked", target_type="note", target_id=note_id, error_message=reason)
            raise ValueError(reason)
        screenshot = self._run_browser_fill(note, assets, mode=note.publish_mode or "publish_after_final_confirm", click_publish=True)
        transition_note(note, NoteStatus.PUBLISH_UNCERTAIN)
        note.publish_screenshot_path = screenshot
        note.publish_error_message = "Publish button clicked; please verify manually in XHS."
        self.db.commit()
        self.audit.record("browser.final_confirm", "success", target_type="note", target_id=note.id, screenshot_path=screenshot, output_summary="publish_uncertain")
        self._notify("Publish needs verification", f"note_id={note.id}; status=publish_uncertain; please verify manually.", note.id)
        return screenshot

    def _approved_note(self, note_id: int):
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        decision = PolicyEngine(self.db, self.settings).check("publish", text=f"{note.title}\n{note.body}", note_status=note.status)
        if not decision.allowed:
            self.audit.record("browser.fill_publish", "blocked", target_type="note", target_id=note_id, output_summary=decision.reason)
            raise PermissionError(decision.reason)
        hashtags = json.loads(note.hashtags_json or "[]")
        if len(hashtags) < 3:
            self.audit.record("browser.fill_publish", "blocked", target_type="note", target_id=note_id, output_summary="hashtags_required")
            raise ValueError("发布前至少需要 3 个话题。")
        return note

    def _dry_run_preview(self, note, assets: list[str]) -> str:
        directory = ROOT / self.settings.browser["screenshots_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = directory / f"note-{note.id}-{timestamp}-dry-run-preview.html"
        png_path = directory / f"note-{note.id}-{timestamp}-dry-run-preview.png"
        hashtags = " ".join(f"#{tag}" for tag in json.loads(note.hashtags_json or "[]"))
        html_path.write_text(
            "<!doctype html><meta charset='utf-8'><title>dry_run preview</title>"
            "<style>body{font-family:Microsoft YaHei,sans-serif;max-width:760px;margin:40px auto;padding:24px;border:1px solid #ddd}"
            ".badge{background:#fff3cd;padding:8px 12px;border-radius:8px}.asset{font-size:13px;color:#666}</style>"
            f"<p class='badge'>dry_run 本地模拟预览：未打开小红书，未上传素材，未点击发布。</p>"
            f"<h1>{html.escape(note.title)}</h1><article>{html.escape(note.body).replace(chr(10), '<br>')}</article>"
            f"<p>{html.escape(hashtags)}</p>"
            f"<h3>素材</h3>{''.join(f'<p class=asset>{html.escape(path)}</p>' for path in assets) or '<p class=asset>无图片素材，纯文本流程。</p>'}",
            encoding="utf-8",
        )
        png_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
        self.audit.record("browser.dry_run_preview", "success", target_type="note", target_id=note.id, screenshot_path=str(png_path), metadata={"html_preview": str(html_path), "asset_count": len(assets)})
        return str(png_path)

    def _run_browser_fill(self, note, assets: list[str], *, mode: str, click_publish: bool) -> str:
        screenshot = ""
        page = None
        browser = None
        context = None
        playwright = None
        step = "launch"
        selector_name = ""
        try:
            playwright = sync_playwright().start()
            channel = self._browser_channel()
            if hasattr(playwright.chromium, "launch_persistent_context"):
                profile_dir = ROOT / self.settings.browser.get("profile_dir", f"data/browser-profiles/{channel}")
                profile_dir.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel=channel,
                    headless=False,
                    slow_mo=self.settings.browser.get("slow_mo_ms", 300),
                )
                page = context.pages[0] if getattr(context, "pages", []) else context.new_page()
            else:
                browser = playwright.chromium.launch(
                    channel=channel,
                    headless=False,
                    slow_mo=self.settings.browser.get("slow_mo_ms", 300),
                )
                context = browser.new_context()
                page = context.new_page()
            step = "open_publish_page"
            page.goto(self.settings.browser["publish_url"])
            self._wait_for_login_and_editor(page)
            step, selector_name = "wait_title", "title"
            title = find_first_visible(page, self.selectors["title"], timeout=60_000)
            step, selector_name = "fill_title", "title"
            title.fill(note.title)
            hashtags = " ".join(f"#{tag}" for tag in json.loads(note.hashtags_json))
            step, selector_name = "fill_body", "body"
            body = find_first_visible(page, self.selectors["body"], timeout=60_000)
            body.fill(f"{note.body}\n{hashtags}".strip())
            if self.selectors.get("topic_input"):
                with suppress(Exception):
                    step, selector_name = "fill_topics", "topic_input"
                    find_first_visible(page, self.selectors["topic_input"], timeout=5_000).fill(hashtags)
            if assets:
                step, selector_name = "upload_assets", "file_input"
                find_first_visible(page, self.selectors["file_input"], timeout=30_000).set_input_files(assets)
            page.wait_for_timeout(1500)
            screenshot = self._screenshot(page, note.id, "published-clicked" if click_publish else mode)
            if click_publish:
                step, selector_name = "click_publish", "submit_button"
                find_first_visible(page, self.selectors["submit_button"], timeout=30_000).click()
                page.wait_for_timeout(3000)
                screenshot = self._screenshot(page, note.id, "publish-uncertain")
            return screenshot
        except PlaywrightClosedByUser as exc:
            self._record_browser_failure(note, mode, click_publish, step, selector_name, str(exc), screenshot, page)
            raise RuntimeError("浏览器已关闭，发布流程已取消。") from None
        except PlaywrightTimeoutError as exc:
            self._record_browser_failure(note, mode, click_publish, step, selector_name, "等待发布页编辑器超时，请确认已登录小红书并检查页面选择器。", screenshot, page)
            raise RuntimeError("等待发布页编辑器超时，请确认已登录小红书并检查页面选择器。") from None
        except Exception as exc:
            message = friendly_browser_error(exc)
            self._record_browser_failure(note, mode, click_publish, step, selector_name, message, screenshot, page)
            raise RuntimeError(message) from None
        finally:
            if context is not None:
                with suppress(Exception):
                    context.close()
            if browser is not None:
                with suppress(Exception):
                    browser.close()
            if playwright is not None:
                with suppress(Exception):
                    playwright.stop()

    def _wait_for_login_and_editor(self, page) -> None:
        print("请在打开的浏览器中手动登录小红书。本程序不会读取、导出或保存 cookie。")
        deadline = datetime.now().timestamp() + 180
        while datetime.now().timestamp() < deadline:
            current_url = getattr(page, "url", "") or ""
            if "/login" not in current_url and "login?" not in current_url:
                return
            with suppress(Exception):
                page.wait_for_timeout(2000)
        raise PlaywrightTimeoutError("login timeout")

    def _record_browser_failure(self, note, mode: str, click_publish: bool, step: str, selector_name: str, message: str, screenshot: str, page) -> None:
        if page is not None and not screenshot:
            with suppress(Exception):
                screenshot = self._screenshot(page, note.id, "failed")
        if not screenshot:
            screenshot = self._unavailable_screenshot(note.id)
        self.db.rollback()
        self.db.add(BrowserError(
            note_id=note.id,
            mode=mode,
            step=step,
            selector_name=selector_name,
            action_type="final_confirm" if click_publish else "fill_publish",
            error_message=message,
            screenshot_path=screenshot,
            metadata_json=json.dumps({"selector_candidates": self.selectors.get(selector_name, [])}, ensure_ascii=False),
        ))
        self.db.commit()
        self.audit.record(
            "browser.final_confirm" if click_publish else "browser.fill_publish",
            "failed",
            target_type="note",
            target_id=note.id,
            error_message=message,
            screenshot_path=screenshot,
            metadata={"mode": mode, "step": step, "selector_name": selector_name},
        )
        current = self.notes.get(note.id)
        if current and current.status == NoteStatus.PUBLISHING:
            transition_note(current, NoteStatus.FAILED)
            current.publish_error_message = message
            current.publish_screenshot_path = screenshot
            self.db.commit()
        self._notify("Browser publish flow failed", f"note_id={note.id}; step={step}; {message}", note.id)

    def mark_published_manually(self, note_id: int) -> None:
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        if note.status != NoteStatus.PUBLISH_UNCERTAIN:
            raise ValueError("Only publish_uncertain notes can be manually marked as published.")
        transition_note(note, NoteStatus.PUBLISHED)
        note.published_at = utcnow()
        self.db.commit()
        self.audit.record("browser.manual_mark_published", "success", target_type="note", target_id=note.id)

    def _notify(self, title: str, message: str, note_id: int) -> None:
        try:
            self.notifier.send(title, message)
            self.audit.record("notification.browser_result", "success", target_type="note", target_id=note_id, output_summary=title)
        except Exception as exc:
            self.audit.record("notification.browser_result", "failed", target_type="note", target_id=note_id, error_message=str(exc))

    def _screenshot(self, page, note_id: int, suffix: str) -> str:
        directory = ROOT / self.settings.browser["screenshots_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"note-{note_id}-{datetime.now():%Y%m%d-%H%M%S}-{suffix}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)

    def _unavailable_screenshot(self, note_id: int) -> str:
        directory = ROOT / self.settings.browser["screenshots_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"note-{note_id}-{datetime.now():%Y%m%d-%H%M%S}-page-unavailable.png"
        pixel_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        path.write_bytes(pixel_png)
        return str(path)

    def _browser_channel(self) -> str:
        row = self.db.query(Setting).filter(Setting.key == "browser_channel").first()
        return (row.value_json if row else self.settings.browser.get("channel")) or "chrome"


class PlaywrightClosedByUser(RuntimeError):
    pass


def selector_list(selector_candidates) -> list[str]:
    if isinstance(selector_candidates, str):
        return [selector_candidates]
    return [str(item) for item in selector_candidates or []]


def find_first_visible(page, selector_candidates, timeout: int = 30_000):
    candidates = selector_list(selector_candidates)
    last_error: Exception | None = None
    per_selector = max(500, int(timeout / max(len(candidates), 1)))
    for selector in candidates:
        try:
            locator = page.locator(selector)
            with suppress(Exception):
                if hasattr(locator, "first"):
                    locator = locator.first
            if hasattr(locator, "wait_for"):
                locator.wait_for(state="visible", timeout=per_selector)
            return locator
        except PlaywrightError as exc:
            if "Target page, context or browser has been closed" in str(exc):
                raise PlaywrightClosedByUser() from None
            last_error = exc
        except Exception as exc:
            if "Target page, context or browser has been closed" in str(exc):
                raise PlaywrightClosedByUser() from None
            last_error = exc
    raise PlaywrightTimeoutError(f"No visible selector found from candidates: {candidates}") from last_error


def friendly_browser_error(exc: Exception) -> str:
    text = str(exc)
    if "Target page, context or browser has been closed" in text:
        return "浏览器已关闭，发布流程已取消。"
    if "Executable doesn't exist" in text or "channel" in text and "chrome" in text.casefold():
        return "Chrome 启动失败，请确认已安装 Chrome，或在设置里切换为 Edge / Chromium。"
    if "No visible selector" in text or "Timeout" in text:
        return "没有找到小红书发布页编辑器，请先手动登录，或更新选择器配置。"
    return text.splitlines()[0][:300] or "浏览器流程失败。"
