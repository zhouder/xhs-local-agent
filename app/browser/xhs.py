from __future__ import annotations

import asyncio
import html
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, async_playwright
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
PREVIEW_SIZE = (1080, 1440)
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
PUBLISH_TARGETS = {"video", "image", "article"}


@dataclass
class SelectorHit:
    locator: object
    selector: str


class XHSBrowser:
    def __init__(self, db: Session, settings: Settings, notifier: Notifier):
        self.db, self.settings, self.notifier = db, settings, notifier
        self.notes = NoteRepository(db)
        self.audit = AuditRepository(db)
        with (ROOT / "app/browser/selectors/xhs.yaml").open(encoding="utf-8") as stream:
            self.selectors = yaml.safe_load(stream)["publish"]

    def fill_approved_note(self, note_id: int, *, dry_run: bool = True, mode: str | None = None) -> str:
        if _event_loop_is_running():
            raise RuntimeError("浏览器自动化运行方式错误，已修复为 async Playwright。如果仍出现此问题，请检查是否还有 sync_playwright 调用。")
        return asyncio.run(self.fill_approved_note_async(note_id, dry_run=dry_run, mode=mode))

    async def fill_approved_note_async(self, note_id: int, *, dry_run: bool = True, mode: str | None = None) -> str:
        if mode is None and not dry_run:
            self.audit.record("browser.real_publish", "blocked", target_type="note", target_id=note_id, output_summary="use_explicit_fill_mode_and_final_confirm")
            raise PermissionError("Real publish is disabled unless explicit safe publish mode and final confirm are used.")
        mode = mode or ("dry_run" if dry_run else "fill_only")
        if mode not in PUBLISH_MODES:
            raise ValueError(f"Unsupported publish mode: {mode}")
        note = self._approved_note(note_id)
        assets = self.notes.media_paths(note.id)
        if any(Path(path).suffix.casefold() in VIDEO_SUFFIXES for path in assets):
            raise ValueError("视频发布暂未实现。")
        ok, reason = validate_image_assets(assets)
        if not ok:
            self.audit.record("browser.fill_publish", "blocked", target_type="note", target_id=note_id, error_message=reason, metadata={"mode": mode})
            raise ValueError(reason)
        if mode == "dry_run":
            screenshot, preview_html = self._dry_run_preview(note, assets)
        else:
            screenshot, preview_html = await self._run_browser_fill_async(note, assets, mode=mode, click_publish=False), ""
        transition_note(note, NoteStatus.PUBLISHING)
        transition_note(note, NoteStatus.WAITING_FINAL_CONFIRM)
        note.publish_mode = mode
        note.publish_screenshot_path = screenshot
        note.publish_preview_html_path = preview_html
        note.publish_error_message = "dry_run_preview: 本地模拟预览，未打开小红书，未上传素材，未发布。" if mode == "dry_run" else ""
        self.db.commit()
        self.audit.record("browser.fill_publish", "success", target_type="note", target_id=note.id, screenshot_path=screenshot, metadata={"mode": mode, "asset_count": len(assets), "preview_html": preview_html})
        self._notify("Publish page filled", f"note_id={note.id}; status={note.status}; review screenshot ready.", note.id)
        return screenshot

    def final_confirm_publish(self, note_id: int) -> str:
        if _event_loop_is_running():
            raise RuntimeError("浏览器自动化运行方式错误，已修复为 async Playwright。如果仍出现此问题，请检查是否还有 sync_playwright 调用。")
        return asyncio.run(self.final_confirm_publish_async(note_id))

    async def final_confirm_publish_async(self, note_id: int) -> str:
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
        if any(Path(path).suffix.casefold() in VIDEO_SUFFIXES for path in assets):
            raise ValueError("视频发布暂未实现。")
        ok, reason = validate_image_assets(assets)
        if not ok:
            self.audit.record("browser.final_confirm", "blocked", target_type="note", target_id=note_id, error_message=reason)
            raise ValueError(reason)
        screenshot = await self._run_browser_fill_async(note, assets, mode=note.publish_mode or "publish_after_final_confirm", click_publish=True)
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

    def _dry_run_preview(self, note, assets: list[str]) -> tuple[str, str]:
        directory = ROOT / self.settings.browser["screenshots_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = directory / f"note-{note.id}-{timestamp}-dry-run-preview.html"
        png_path = directory / f"note-{note.id}-{timestamp}-dry-run-preview.png"
        hashtags = json.loads(note.hashtags_json or "[]")
        _render_preview_png(png_path, note.title, note.body, hashtags, assets)
        html_path.write_text(_preview_html(note.title, note.body, hashtags, assets), encoding="utf-8")
        self.audit.record("browser.dry_run_preview", "success", target_type="note", target_id=note.id, screenshot_path=str(png_path), metadata={"html_preview": str(html_path), "asset_count": len(assets)})
        return str(png_path), str(html_path)

    async def _run_browser_fill_async(self, note, assets: list[str], *, mode: str, click_publish: bool) -> str:
        screenshot = ""
        page = None
        browser = None
        context = None
        playwright = None
        step = "launch"
        selector_name = ""
        requested_target = "image" if assets else "article"
        actual_target = "unknown"
        current_tab = ""
        keep_open = bool(self.settings.browser.get("keep_open_on_error", True))
        failed = False
        try:
            playwright = await async_playwright().start()
            channel = self._browser_channel()
            if hasattr(playwright.chromium, "launch_persistent_context"):
                profile_dir = ROOT / self.settings.browser.get("profile_dir", f"data/browser-profiles/{channel}")
                profile_dir.mkdir(parents=True, exist_ok=True)
                context = await playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel=channel,
                    headless=False,
                    slow_mo=self.settings.browser.get("slow_mo_ms", 300),
                )
                page = context.pages[0] if getattr(context, "pages", []) else await context.new_page()
            else:
                browser = await playwright.chromium.launch(
                    channel=channel,
                    headless=False,
                    slow_mo=self.settings.browser.get("slow_mo_ms", 300),
                )
                context = await browser.new_context()
                page = await context.new_page()

            publish_url = resolve_publish_url(self.settings, requested_target)
            step = "open_publish_target_url"
            await page.goto(publish_url)
            await self._wait_for_login_and_target_page_async(page, requested_target, publish_url)
            actual_target = detect_publish_target_from_url(getattr(page, "url", "") or "")
            if actual_target == "video" and requested_target != "video":
                message = f"\u8bf7\u6c42{publish_target_label(requested_target)}\u53d1\u5e03\u9875 target={requested_target}\uff0c\u4f46\u5f53\u524d\u9875\u9762\u662f target=video\u3002\u8bf7\u68c0\u67e5\u5c0f\u7ea2\u4e66\u8df3\u8f6c\u6216 publish_urls \u914d\u7f6e\u3002"
                await self._record_browser_failure_async(note, mode, click_publish, step, selector_name, message, screenshot, page, current_tab, requested_target, actual_target)
                raise RuntimeError(message)

            hashtags = " ".join(f"#{tag}" for tag in json.loads(note.hashtags_json))
            if requested_target == "image":
                step, selector_name = "wait_image_upload", "image_upload_area"
                await async_wait_image_editor_ready(page, self.selectors, require_title_body=False)
                step, selector_name = "upload_assets", "file_input"
                file_input = await async_find_first_visible(page, self.selectors.get("file_input", self.selectors.get("image_upload_area", [])), timeout=30_000)
                await file_input.locator.set_input_files(assets)
                await page.wait_for_timeout(2500)
                step, selector_name = "wait_image_title_body", "title"
                await async_wait_image_editor_ready(page, self.selectors, require_title_body=True)
            else:
                step, selector_name = "wait_article_editor", "title"
                await async_wait_article_editor_ready(page, self.selectors)

            step, selector_name = "fill_title", "title"
            title = await async_find_first_visible(page, self.selectors["title"], timeout=60_000)
            await title.locator.fill(note.title)
            step, selector_name = "fill_body", "body"
            body = await async_find_first_visible(page, self.selectors["body"], timeout=60_000)
            await body.locator.fill(f"{note.body}\n{hashtags}".strip())
            if self.selectors.get("topic_input"):
                with suppress(Exception):
                    step, selector_name = "fill_topics", "topic_input"
                    topic = await async_find_first_visible(page, self.selectors["topic_input"], timeout=5_000)
                    await topic.locator.fill(hashtags)

            await page.wait_for_timeout(1500)
            screenshot = await self._screenshot_async(page, note.id, "published-clicked" if click_publish else mode)
            if click_publish:
                step, selector_name = "click_publish", "submit_button"
                submit = await async_find_first_visible(page, self.selectors["submit_button"], timeout=30_000)
                await submit.locator.click()
                await page.wait_for_timeout(3000)
                screenshot = await self._screenshot_async(page, note.id, "publish-uncertain")
            return screenshot
        except PlaywrightClosedByUser as exc:
            failed = True
            await self._record_browser_failure_async(note, mode, click_publish, step, selector_name, str(exc), screenshot, page, current_tab, requested_target, actual_target)
            raise RuntimeError("\u6d4f\u89c8\u5668\u5df2\u88ab\u7528\u6237\u5173\u95ed\uff0c\u6d41\u7a0b\u53d6\u6d88\u3002") from None
        except PlaywrightTimeoutError:
            failed = True
            actual_target = detect_publish_target_from_url(getattr(page, "url", "") or "") if page is not None else actual_target
            if actual_target == "video" and requested_target != "video":
                message = f"\u8bf7\u6c42{publish_target_label(requested_target)}\u53d1\u5e03\u9875 target={requested_target}\uff0c\u4f46\u5f53\u524d\u9875\u9762\u662f target=video\u3002\u8bf7\u68c0\u67e5\u5c0f\u7ea2\u4e66\u8df3\u8f6c\u6216 publish_urls \u914d\u7f6e\u3002"
            elif step.startswith("wait_"):
                message = f"\u5df2\u8fdb\u5165{publish_target_label(requested_target)}\u53d1\u5e03\u9875\uff0c\u4f46\u6ca1\u6709\u627e\u5230\u53d1\u5e03\u9875\u7f16\u8f91\u5668\u3002\u8bf7\u8fd0\u884c\u9009\u62e9\u5668\u8bca\u65ad\u811a\u672c\u6216\u68c0\u67e5\u5c0f\u7ea2\u4e66\u9875\u9762\u662f\u5426\u6539\u7248\u3002"
            else:
                candidates = self.selectors.get(selector_name or "title", [])
                message = f"\u6ca1\u6709\u627e\u5230\u53d1\u5e03\u9875\u7f16\u8f91\u5668\u3002\u5f53\u524d\u6b65\u9aa4\uff1a{step}\uff1b\u9009\u62e9\u5668\u5019\u9009\uff1a{selector_list(candidates)}"
            await self._record_browser_failure_async(note, mode, click_publish, step, selector_name or "title", message, screenshot, page, current_tab, requested_target, actual_target)
            raise RuntimeError(message) from None
        except Exception as exc:
            failed = True
            message = friendly_browser_error(exc)
            await self._record_browser_failure_async(note, mode, click_publish, step, selector_name, message, screenshot, page, current_tab, requested_target, actual_target)
            raise RuntimeError(message) from None
        finally:
            should_close = not failed or not keep_open
            if should_close and context is not None:
                with suppress(Exception):
                    await context.close()
            if should_close and browser is not None:
                with suppress(Exception):
                    await browser.close()
            if should_close and playwright is not None:
                with suppress(Exception):
                    await playwright.stop()

    async def _wait_for_login_and_target_page_async(self, page, requested_target: str, publish_url: str) -> None:
        print("\u8bf7\u5728\u6253\u5f00\u7684 Chrome \u4e2d\u626b\u7801\u767b\u5f55\u5c0f\u7ea2\u4e66\u3002\u672c\u7a0b\u5e8f\u4e0d\u4f1a\u8bfb\u53d6\u3001\u5bfc\u51fa\u6216\u4fdd\u5b58 cookie\u3002")
        deadline = datetime.now().timestamp() + 180
        navigated_after_login = False
        last_error: Exception | None = None
        while datetime.now().timestamp() < deadline:
            current_url = getattr(page, "url", "") or ""
            if "/login" in current_url or "login?" in current_url:
                with suppress(Exception):
                    await page.wait_for_timeout(2000)
                continue
            if not navigated_after_login and current_url and detect_publish_target_from_url(current_url) != requested_target:
                with suppress(Exception):
                    await page.goto(publish_url)
                    navigated_after_login = True
            try:
                if requested_target == "image":
                    await async_wait_image_editor_ready(page, self.selectors, require_title_body=False, timeout=5_000)
                else:
                    await async_wait_article_editor_ready(page, self.selectors, timeout=5_000)
                return
            except Exception as exc:
                last_error = exc
                if not current_url or getattr(page, "fail", False):
                    break
                if not navigated_after_login:
                    with suppress(Exception):
                        await page.goto(publish_url)
                        navigated_after_login = True
                with suppress(Exception):
                    await page.wait_for_timeout(2000)
        raise PlaywrightTimeoutError(f"publish target page timeout: {last_error}")


    async def _record_browser_failure_async(
        self,
        note,
        mode: str,
        click_publish: bool,
        step: str,
        selector_name: str,
        message: str,
        screenshot: str,
        page,
        current_tab: str = "",
        requested_target: str = "",
        actual_target: str = "",
    ) -> None:
        current_url = ""
        page_title = ""
        if page is not None:
            current_url = getattr(page, "url", "") or ""
            if not actual_target:
                actual_target = detect_publish_target_from_url(current_url)
            with suppress(Exception):
                page_title = await page.title() if hasattr(page, "title") else ""
            if not current_tab:
                with suppress(Exception):
                    current_tab = await async_detect_active_publish_tab(page, self.selectors)
            if not screenshot:
                with suppress(Exception):
                    screenshot = await self._screenshot_async(page, note.id, "failed")
        if not screenshot:
            screenshot = self._unavailable_screenshot(note.id)
        self.db.rollback()
        metadata = {
            "current_url": current_url,
            "page_title": page_title,
            "current_tab": current_tab,
            "requested_target": requested_target,
            "actual_target": actual_target,
            "step": step,
            "selector_candidates": self.selectors.get(selector_name, []),
            "screenshot_path": screenshot,
            "keep_open_on_error": bool(self.settings.browser.get("keep_open_on_error", True)),
        }
        self.db.add(BrowserError(
            note_id=note.id,
            mode=mode,
            step=step,
            selector_name=selector_name,
            action_type="final_confirm" if click_publish else "fill_publish",
            error_message=message,
            screenshot_path=screenshot,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        ))
        self.db.commit()
        self.audit.record(
            "browser.final_confirm" if click_publish else "browser.fill_publish",
            "failed",
            target_type="note",
            target_id=note.id,
            error_message=message,
            screenshot_path=screenshot,
            metadata=metadata,
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

    async def _screenshot_async(self, page, note_id: int, suffix: str) -> str:
        directory = ROOT / self.settings.browser["screenshots_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"note-{note_id}-{datetime.now():%Y%m%d-%H%M%S}-{suffix}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)

    def _unavailable_screenshot(self, note_id: int) -> str:
        directory = ROOT / self.settings.browser["screenshots_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"note-{note_id}-{datetime.now():%Y%m%d-%H%M%S}-page-unavailable.png"
        _render_preview_png(path, "页面不可用", "浏览器已关闭或启动失败，无法截图。", [], [])
        return str(path)

    def _browser_channel(self) -> str:
        row = self.db.query(Setting).filter(Setting.key == "browser_channel").first()
        return (row.value_json if row else self.settings.browser.get("channel")) or "chrome"


class PlaywrightClosedByUser(RuntimeError):
    pass


def _event_loop_is_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False



def resolve_publish_url(settings: Settings, publish_content_type: str) -> str:
    target = publish_content_type if publish_content_type in PUBLISH_TARGETS else "image"
    urls = settings.browser.get("publish_urls", {}) or {}
    url = urls.get(target)
    if url:
        return str(url)
    fallback = str(settings.browser.get("publish_url", "https://creator.xiaohongshu.com/publish/publish"))
    separator = "&" if "?" in fallback else "?"
    return f"{fallback}{separator}from=menu&target={target}"


def detect_publish_target_from_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
        target = parse_qs(parsed.query).get("target", [""])[0]
    except Exception:
        return "unknown"
    return target if target in PUBLISH_TARGETS else "unknown"


def publish_target_label(target: str) -> str:
    return {"image": "??", "article": "??", "video": "??"}.get(target, "??")

def selector_list(selector_candidates) -> list[str]:
    if isinstance(selector_candidates, str):
        return [selector_candidates]
    return [str(item) for item in selector_candidates or []]


async def async_find_first_visible(page, selector_candidates, timeout: int = 30_000) -> SelectorHit:
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
                await locator.wait_for(state="visible", timeout=per_selector)
            return SelectorHit(locator=locator, selector=selector)
        except PlaywrightError as exc:
            if "Target page, context or browser has been closed" in str(exc):
                raise PlaywrightClosedByUser() from None
            last_error = exc
        except Exception as exc:
            if "Target page, context or browser has been closed" in str(exc):
                raise PlaywrightClosedByUser() from None
            last_error = exc
    raise PlaywrightTimeoutError(f"No visible selector found from candidates: {candidates}") from last_error


async def async_wait_publish_page_ready(page, selectors: dict, timeout: int = 30_000) -> SelectorHit:
    return await async_find_first_visible(page, selectors.get("page_ready", []), timeout=timeout)


async def async_detect_active_publish_tab(page, selectors: dict) -> str:
    checks = [
        ("upload_image", selectors.get("active_tab_upload_image", [])),
        ("long_text", selectors.get("active_tab_long_text", [])),
        ("upload_video", selectors.get("active_tab_upload_video", selectors.get("tab_upload_video", []))),
    ]
    for name, candidates in checks:
        for selector in selector_list(candidates):
            with suppress(Exception):
                if await page.locator(selector).count() > 0:
                    return name
    return "unknown"


async def async_choose_publish_tab(page, selectors: dict, publish_content_type: str) -> str:
    current = await async_detect_active_publish_tab(page, selectors)
    if publish_content_type == "image":
        if current == "upload_image":
            return current
        hit = await async_find_first_visible(page, selectors.get("tab_upload_image", []), timeout=30_000)
        await hit.locator.click()
        with suppress(Exception):
            await async_find_first_visible(page, selectors.get("active_tab_upload_image", []), timeout=5_000)
        return "upload_image"
    if current == "long_text":
        return current
    try:
        hit = await async_find_first_visible(page, selectors.get("tab_long_text", []), timeout=15_000)
        await hit.locator.click()
        with suppress(Exception):
            await async_find_first_visible(page, selectors.get("active_tab_long_text", []), timeout=5_000)
        return "long_text"
    except Exception:
        hit = await async_find_first_visible(page, selectors.get("tab_upload_image", []), timeout=15_000)
        await hit.locator.click()
        return "upload_image"


async def async_wait_image_editor_ready(page, selectors: dict, require_title_body: bool = False, timeout: int = 30_000) -> None:
    await async_find_first_visible(page, selectors.get("image_page_ready", selectors.get("image_upload_area", [])), timeout=timeout)
    await async_find_first_visible(page, selectors.get("image_upload_area", selectors.get("file_input", [])), timeout=timeout)
    if require_title_body:
        await async_find_first_visible(page, selectors.get("title", []), timeout=60_000)
        await async_find_first_visible(page, selectors.get("body", []), timeout=60_000)


async def async_wait_article_editor_ready(page, selectors: dict, timeout: int = 30_000) -> None:
    await async_find_first_visible(page, selectors.get("article_page_ready", selectors.get("title", [])), timeout=timeout)
    await async_find_first_visible(page, selectors.get("title", []), timeout=60_000)
    await async_find_first_visible(page, selectors.get("body", []), timeout=60_000)


async def async_wait_editor_ready(page, selectors: dict, publish_content_type: str) -> None:
    if publish_content_type in {"image", "upload_image"}:
        await async_wait_image_editor_ready(page, selectors, require_title_body=True)
    else:
        await async_wait_article_editor_ready(page, selectors)


def friendly_browser_error(exc: Exception) -> str:
    text = str(exc)
    if "Playwright Sync API inside the asyncio loop" in text or "sync_playwright" in text:
        return "浏览器自动化运行方式错误，已修复为 async Playwright。如果仍出现此问题，请检查是否还有 sync_playwright 调用。"
    if "Target page, context or browser has been closed" in text:
        return "浏览器已被用户关闭，流程取消。"
    if "Executable doesn't exist" in text or ("channel" in text and "chrome" in text.casefold()):
        return "Chrome 启动失败，请确认已安装 Chrome，或在设置里切换为 Edge / Chromium。"
    if "No visible selector" in text or "Timeout" in text:
        return "已登录，但没有找到发布页编辑器。请运行选择器诊断脚本或检查小红书页面是否改版。"
    return text.splitlines()[0][:300] or "浏览器流程失败。"


def _font(size: int, bold: bool = False):
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _short(text: str, limit: int) -> str:
    value = " ".join((text or "").split())
    return value[:limit] + ("..." if len(value) > limit else "")


def _render_preview_png(path: Path, title: str, body: str, hashtags: list[str], assets: list[str]) -> None:
    width, height = PREVIEW_SIZE
    image = Image.new("RGB", PREVIEW_SIZE, "#f6f7fb")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((70, 70, width - 70, height - 70), radius=54, fill="#ffffff", outline="#d0d5dd", width=3)
    draw.rounded_rectangle((110, 110, 390, 166), radius=28, fill="#fff1f3", outline="#ffccd5", width=2)
    draw.text((140, 123), "dry_run 本地预览", font=_font(28, True), fill="#c01048")
    draw.text((110, 220), _short(title, 32), font=_font(58, True), fill="#101828")
    y = 325
    for line in _short(body, 260).split(" "):
        if y > 790:
            break
        draw.text((110, y), line, font=_font(32), fill="#344054")
        y += 48
    tag_text = " ".join(f"#{tag}" for tag in hashtags)
    draw.text((110, 840), _short(tag_text, 80), font=_font(30, True), fill="#3157a4")
    draw.rounded_rectangle((110, 910, width - 110, 1240), radius=30, fill="#f2f4f7", outline="#e4e7ec")
    draw.text((145, 945), f"图片素材：{len(assets)} 张", font=_font(34, True), fill="#344054")
    for index, asset in enumerate(assets[:6], start=1):
        x = 145 + ((index - 1) % 3) * 285
        yy = 1015 + ((index - 1) // 3) * 88
        draw.rounded_rectangle((x, yy, x + 250, yy + 58), radius=18, fill="#ffffff", outline="#d0d5dd")
        draw.text((x + 18, yy + 14), f"{index}. {_short(Path(asset).name, 14)}", font=_font(22), fill="#475467")
    draw.text((110, height - 155), "未打开小红书 | 未上传素材 | 未点击发布", font=_font(30, True), fill="#b42318")
    image.save(path, format="PNG")


def _preview_html(title: str, body: str, hashtags: list[str], assets: list[str]) -> str:
    tags = " ".join(f"#{html.escape(tag)}" for tag in hashtags)
    items = "".join(f"<li>{index}. {html.escape(Path(path).name)}</li>" for index, path in enumerate(assets, start=1))
    if not items:
        items = "<li>无图片素材，纯文本预览。</li>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>
    body{{margin:0;background:#f6f7fb;font-family:Inter,'Microsoft YaHei',sans-serif;color:#182230}}
    .card{{max-width:760px;margin:24px auto;padding:28px;border:1px solid #d0d5dd;border-radius:28px;background:#fff;box-shadow:0 16px 40px #10182814}}
    .badge{{display:inline-block;padding:8px 14px;border-radius:999px;background:#fff1f3;color:#c01048;font-weight:700}}
    h1{{font-size:34px;line-height:1.25;margin:24px 0 16px}}
    article{{font-size:16px;line-height:1.8;white-space:pre-wrap}}
    .tags{{color:#3157a4;font-weight:700}}
    .assets{{background:#f2f4f7;border-radius:18px;padding:16px}}
    .safe{{margin-top:18px;padding:12px;border-radius:14px;background:#fffaeb;color:#93370d}}
  </style>
</head>
<body>
  <main class="card">
    <span class="badge">dry_run 本地模拟预览</span>
    <h1>{html.escape(title)}</h1>
    <article>{html.escape(body)}</article>
    <p class="tags">{tags}</p>
    <section class="assets"><strong>图片素材</strong><ul>{items}</ul></section>
    <p class="safe">未打开小红书，未上传素材，未点击发布。</p>
  </main>
</body>
</html>"""
