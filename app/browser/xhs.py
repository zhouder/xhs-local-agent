from __future__ import annotations

import asyncio
import html
import json
import re
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
from app.services.materials import validate_image_assets, validate_video_asset
from app.services.notifications import Notifier
from app.services.policy import PolicyEngine
from app.services.publish_kinds import (
    PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE,
    PUBLISH_KIND_IMAGE_UPLOAD,
    PUBLISH_KIND_VIDEO_UPLOAD,
    PUBLISH_KINDS,
    normalize_publish_kind,
    publish_kind_label,
    publish_target_for_kind,
)
from app.services.state_machine import transition_note


PUBLISH_MODES = {"dry_run", "fill_only", "publish_after_final_confirm"}
PREVIEW_SIZE = (1080, 1440)
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
PUBLISH_TARGETS = {"video", "image"}


@dataclass
class SelectorHit:
    locator: object
    selector: str


@dataclass
class TextToImageCandidate:
    locator: object
    selector: str
    tag: str = ""
    text: str = ""
    box: dict | None = None
    visible: bool | None = None
    upload_like: bool = False
    reason_skipped: str = ""


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
        publish_kind = resolve_note_publish_kind(note)
        image_assets = self.notes.media_paths_by_type(note.id, "image")
        video_assets = self.notes.media_paths_by_type(note.id, "video")
        assets = video_assets if publish_kind == PUBLISH_KIND_VIDEO_UPLOAD else image_assets
        self._validate_publish_assets(note.id, publish_kind, image_assets, video_assets, mode)
        if mode == "dry_run":
            screenshot, preview_html = self._dry_run_preview(note, assets)
        else:
            screenshot, preview_html = await self._run_browser_fill_async(note, image_assets=image_assets, video_assets=video_assets, mode=mode, click_publish=False), ""
        transition_note(note, NoteStatus.PUBLISHING)
        transition_note(note, NoteStatus.WAITING_FINAL_CONFIRM)
        note.publish_mode = mode
        note.publish_screenshot_path = screenshot
        note.publish_preview_html_path = preview_html
        note.publish_error_message = "dry_run_preview: 本地模拟预览，未打开小红书，未上传素材，未发布。" if mode == "dry_run" else ""
        self.db.commit()
        self.audit.record("browser.fill_publish", "success", target_type="note", target_id=note.id, screenshot_path=screenshot, metadata={"mode": mode, "publish_kind": publish_kind, "asset_count": len(assets), "preview_html": preview_html})
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
        publish_kind = resolve_note_publish_kind(note)
        image_assets = self.notes.media_paths_by_type(note.id, "image")
        video_assets = self.notes.media_paths_by_type(note.id, "video")
        self._validate_publish_assets(note.id, publish_kind, image_assets, video_assets, note.publish_mode or "publish_after_final_confirm", action_type="browser.final_confirm")
        screenshot = await self._run_browser_fill_async(note, image_assets=image_assets, video_assets=video_assets, mode=note.publish_mode or "publish_after_final_confirm", click_publish=True)
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

    def _validate_publish_assets(self, note_id: int, publish_kind: str, image_assets: list[str], video_assets: list[str], mode: str, *, action_type: str = "browser.fill_publish") -> None:
        if publish_kind == PUBLISH_KIND_VIDEO_UPLOAD:
            ok, reason = validate_video_asset(video_assets[0] if video_assets else "")
        elif publish_kind == PUBLISH_KIND_IMAGE_UPLOAD:
            ok, reason = validate_image_assets(image_assets)
        else:
            ok, reason = True, ""
        if not ok:
            self.audit.record(action_type, "blocked", target_type="note", target_id=note_id, error_message=reason, metadata={"mode": mode, "publish_kind": publish_kind})
            raise ValueError(reason)

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

    async def _run_browser_fill_async(self, note, *, image_assets: list[str], video_assets: list[str], mode: str, click_publish: bool) -> str:
        screenshot = ""
        page = None
        browser = None
        context = None
        playwright = None
        step = "launch"
        selector_name = ""
        publish_kind = resolve_note_publish_kind(note)
        requested_target = publish_target_for_kind(publish_kind)
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

            publish_url = resolve_publish_url(self.settings, publish_kind)
            step = "open_publish_target_url"
            await page.goto(publish_url)
            await self._wait_for_login_and_target_page_async(page, requested_target, publish_url, publish_kind)
            actual_target = detect_publish_target_from_url(getattr(page, "url", "") or "")
            if actual_target in PUBLISH_TARGETS and actual_target != requested_target:
                message = f"请求{publish_target_label(requested_target)}发布页 target={requested_target}，但当前页面是 target={actual_target}。请检查小红书跳转或 publish_urls 配置。"
                await self._record_browser_failure_async(note, mode, click_publish, step, selector_name, message, screenshot, page, current_tab, requested_target, actual_target)
                raise RuntimeError(message)

            step, selector_name = f"fill_{publish_kind}", publish_kind
            screenshot = await self.async_fill_note(page, note, publish_kind, image_assets, video_assets, mode=mode, click_publish=click_publish)
            return screenshot
        except SelectorStepError as exc:
            failed = True
            step, selector_name = exc.step, exc.selector_name
            message = exc.message
            await self._record_browser_failure_async(note, mode, click_publish, step, selector_name, message, screenshot, page, current_tab, requested_target, actual_target)
            raise RuntimeError(message) from None
        except PlaywrightClosedByUser as exc:
            failed = True
            await self._record_browser_failure_async(note, mode, click_publish, step, selector_name, str(exc), screenshot, page, current_tab, requested_target, actual_target)
            raise RuntimeError("\u6d4f\u89c8\u5668\u5df2\u88ab\u7528\u6237\u5173\u95ed\uff0c\u6d41\u7a0b\u53d6\u6d88\u3002") from None
        except PlaywrightTimeoutError:
            failed = True
            actual_target = detect_publish_target_from_url(getattr(page, "url", "") or "") if page is not None else actual_target
            if actual_target in PUBLISH_TARGETS and actual_target != requested_target:
                message = f"请求{publish_target_label(requested_target)}发布页 target={requested_target}，但当前页面是 target={actual_target}。请检查小红书跳转或 publish_urls 配置。"
            elif step.startswith("wait_"):
                message = f"已进入{publish_target_label(requested_target)}发布页，但没有找到{publish_kind_label(publish_kind)}需要的编辑器。请运行选择器诊断脚本或检查小红书页面是否改版。"
            else:
                candidates = selector_candidates(self.selectors, publish_kind, selector_name or "title")
                message = f"没有找到发布页编辑器。当前步骤：{step}；选择器候选：{selector_list(candidates)}"
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

    async def async_fill_note(self, page, note, publish_kind: str, image_assets: list[str], video_assets: list[str], *, mode: str, click_publish: bool) -> str:
        if publish_kind == PUBLISH_KIND_VIDEO_UPLOAD:
            return await self.async_fill_video_upload_note(page, note, video_assets, mode=mode, click_publish=click_publish)
        if publish_kind == PUBLISH_KIND_IMAGE_UPLOAD:
            return await self.async_fill_image_upload_note(page, note, image_assets, mode=mode, click_publish=click_publish)
        if publish_kind == PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE:
            return await self.async_fill_image_text_to_image_note(page, note, mode=mode, click_publish=click_publish)
        raise ValueError(f"Unsupported publish_kind: {publish_kind}")

    async def async_fill_video_upload_note(self, page, note, video_assets: list[str], *, mode: str, click_publish: bool) -> str:
        if not video_assets:
            raise SelectorStepError("validate_video", "file_input", "视频笔记需要先添加一个 mp4/mov 视频文件。")
        selectors = selector_group(self.selectors, PUBLISH_KIND_VIDEO_UPLOAD)
        await async_find_first_visible(page, selectors.get("page_ready", []), timeout=60_000)
        await async_upload_files(page, selectors, video_assets, "file_input", "upload_area")
        await async_wait_uploaded_or_editor_ready(page, selectors, timeout=90_000)
        await self._fill_common_fields(page, note, PUBLISH_KIND_VIDEO_UPLOAD)
        return await self._finish_fill(page, note, mode=mode, click_publish=click_publish)

    async def async_fill_image_upload_note(self, page, note, image_assets: list[str], *, mode: str, click_publish: bool) -> str:
        if not image_assets:
            raise SelectorStepError("validate_images", "file_input", "图文笔记需要先添加 1-9 张图片，或切换为文字生图。")
        selectors = selector_group(self.selectors, PUBLISH_KIND_IMAGE_UPLOAD)
        await async_find_first_visible(page, selectors.get("page_ready", []), timeout=60_000)
        await async_upload_files(page, selectors, image_assets, "file_input", "upload_area")
        await async_wait_uploaded_or_editor_ready(page, selectors, timeout=90_000)
        await self._fill_common_fields(page, note, PUBLISH_KIND_IMAGE_UPLOAD)
        return await self._finish_fill(page, note, mode=mode, click_publish=click_publish)

    async def async_fill_image_text_to_image_note(self, page, note, *, mode: str, click_publish: bool) -> str:
        selectors = selector_group(self.selectors, PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE)
        await async_find_first_visible(page, selector_candidates(self.selectors, PUBLISH_KIND_IMAGE_UPLOAD, "page_ready"), timeout=60_000)
        state = await async_detect_text_to_image_state(page, selectors)
        if state == "entry_page":
            try:
                await async_click_text_to_image_entry(page, selectors.get("entry", []))
            except Exception as exc:
                candidates = selector_list(selectors.get("entry", []))
                message = str(exc) or f"没有找到或无法点击【文字配图】入口。请运行选择器诊断脚本。选择器候选：{candidates}"
                raise SelectorStepError("open_text_to_image", "entry", message) from exc
            with suppress(Exception):
                await async_find_first_visible(page, selectors.get("text_editor_page_ready", []), timeout=5_000)
        if state != "generated_page":
            content = build_text_to_image_content(note)
            if not (getattr(note, "text_to_image_prompt", "") or "").strip():
                note.text_to_image_prompt = content
                self.db.commit()
            text_input = await async_find_first_visible(page, selectors.get("text_input", selectors.get("prompt_input", [])), timeout=30_000)
            await async_fill_text_card_input(text_input.locator, content)
            await async_click_generate_image_button(page, selectors)
        with suppress(Exception):
            template = await async_find_first_visible(page, selectors.get("template_option", []), timeout=10_000)
            await template.locator.click()
        next_button = await async_find_first_visible(page, selectors.get("next_button", []), timeout=30_000)
        await next_button.locator.click()
        await async_find_first_visible(page, selector_candidates(self.selectors, PUBLISH_KIND_IMAGE_UPLOAD, "title"), timeout=60_000)
        await self._fill_common_fields(page, note, PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE)
        return await self._finish_fill(page, note, mode=mode, click_publish=click_publish)

    async def _fill_common_fields(self, page, note, publish_kind: str) -> None:
        selectors = selector_group(self.selectors, publish_kind)
        hashtags = " ".join(f"#{tag}" for tag in json.loads(note.hashtags_json))
        title = await async_find_first_visible(page, selectors["title"], timeout=60_000)
        await title.locator.fill(note.title)
        body = await async_find_first_visible(page, selectors["body"], timeout=60_000)
        await body.locator.fill(f"{note.body}\n{hashtags}".strip())
        if selectors.get("topic_input"):
            with suppress(Exception):
                topic = await async_find_first_visible(page, selectors["topic_input"], timeout=5_000)
                await topic.locator.fill(hashtags)

    async def _finish_fill(self, page, note, *, mode: str, click_publish: bool) -> str:
        await page.wait_for_timeout(1500)
        screenshot = await self._screenshot_async(page, note.id, "published-clicked" if click_publish else mode)
        if click_publish:
            selectors = selector_group(self.selectors, resolve_note_publish_kind(note))
            submit = await async_find_first_visible(page, selectors["submit_button"], timeout=30_000)
            await submit.locator.click()
            await page.wait_for_timeout(3000)
            screenshot = await self._screenshot_async(page, note.id, "publish-uncertain")
        return screenshot

    async def _wait_for_login_and_target_page_async(self, page, requested_target: str, publish_url: str, publish_kind: str) -> None:
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
                if publish_kind == PUBLISH_KIND_VIDEO_UPLOAD:
                    await async_find_first_visible(page, selector_candidates(self.selectors, publish_kind, "page_ready"), timeout=5_000)
                elif requested_target == "image":
                    await async_wait_image_editor_ready(page, self.selectors, require_title_body=False, timeout=5_000)
                else:
                    await async_find_first_visible(page, selector_candidates(self.selectors, publish_kind, "page_ready"), timeout=5_000)
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
        metadata_publish_kind = (
            PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE
            if "text_to_image" in step or selector_name in {"entry", "prompt_input", "text_input", "generate_button", "template_option", "next_button"}
            else (PUBLISH_KIND_VIDEO_UPLOAD if requested_target == "video" else PUBLISH_KIND_IMAGE_UPLOAD)
        )
        metadata = {
            "current_url": current_url,
            "page_title": page_title,
            "current_tab": current_tab,
            "requested_target": requested_target,
            "actual_target": actual_target,
            "step": step,
            "selector_candidates": selector_candidates(self.selectors, metadata_publish_kind, selector_name),
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


class SelectorStepError(RuntimeError):
    def __init__(self, step: str, selector_name: str, message: str):
        super().__init__(message)
        self.step = step
        self.selector_name = selector_name
        self.message = message


def _event_loop_is_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False



def resolve_note_publish_kind(note) -> str:
    return normalize_publish_kind(getattr(note, "publish_kind", None))


def resolve_publish_url(settings: Settings, publish_kind: str) -> str:
    target = publish_target_for_kind(publish_kind)
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
    return {"image": "图文", "video": "视频"}.get(target, "未知")


def selector_group(selectors: dict, publish_kind: str) -> dict:
    if "common" not in selectors:
        return selectors
    common = selectors.get("common", {}) or {}
    specific = selectors.get(normalize_publish_kind(publish_kind), {}) or {}
    return {**common, **specific}


def selector_candidates(selectors: dict, publish_kind: str, key: str):
    group = selector_group(selectors, publish_kind)
    if key in group:
        return group.get(key, [])
    return selectors.get(key, [])

def selector_list(selector_candidates) -> list[str]:
    if isinstance(selector_candidates, str):
        return [selector_candidates]
    return [str(item) for item in selector_candidates or []]


UPLOAD_ENTRY_TEXT = ("上传图片", "拖拽图片", "点击上传", "上传图文", "input[type=file]")
UPLOAD_ENTRY_SELECTOR = ("input[type=\"file\"]", "input[type='file']", "[class*=\"upload\"]", "[class*='upload']", "text=上传图片", "text=图片配图")
TEXT_TO_IMAGE_FALLBACK_ENTRY_SELECTORS = [
    'text=文字配图',
    'span:has-text("文字配图")',
    'div:has-text("文字配图")',
    'button:has-text("文字配图")',
    '[role="button"]:has-text("文字配图")',
    '[class*="btn"]:has-text("文字配图")',
    '[class*="button"]:has-text("文字配图")',
    '[class*="card"]:has-text("文字配图")',
    '[class*="option"]:has-text("文字配图")',
    '[class*="item"]:has-text("文字配图")',
    'text=写文字生成图片',
    'button:has-text("写文字生成图片")',
    '[role="button"]:has-text("写文字生成图片")',
    'div:has-text("写文字生成图片")',
]


class TextToImageFileChooserError(RuntimeError):
    pass


def build_text_to_image_content(note) -> str:
    manual = clean_text_card_content(getattr(note, "text_to_image_prompt", "") or "")
    if manual:
        return manual
    title = clean_text_card_content(getattr(note, "title", "") or "")
    body = clean_text_card_content(getattr(note, "body", "") or "")
    lines: list[str] = []
    if title:
        title = re.sub(r"[:：|｜\-—]+", "\n", title)
        for part in title.splitlines():
            clean = clean_text_card_content(part)
            if clean:
                lines.append(clean)
            if len(lines) >= 2:
                break
    if len("".join(lines)) < 12 and body:
        for sentence in re.split(r"[。！？!?；;\n]", body):
            clean = clean_text_card_content(sentence)
            if clean and clean not in lines:
                lines.append(clean)
            if len(lines) >= 4 or len("".join(lines)) >= 36:
                break
    if not lines:
        lines = ["今天这条经验", "值得保存下来"]
    content = "\n".join(lines[:4])
    if len(content) > 120:
        content = content[:120].rstrip()
    return content


def clean_text_card_content(value: str) -> str:
    text = re.sub(r"#\S+", "", value or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    lines = [line.strip(" #，,。；;") for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if len(text) > 120:
        text = text[:120].rstrip()
    return text


def is_upload_like_text_to_image_candidate(selector: str, text: str) -> bool:
    normalized_selector = (selector or "").casefold().replace(" ", "")
    normalized_text = " ".join((text or "").split())
    if normalized_text in {"文字配图", "写文字生成图片"} or selector in {"text=文字配图", "text=写文字生成图片"}:
        return False
    if any(token.casefold().replace(" ", "") in normalized_selector for token in UPLOAD_ENTRY_SELECTOR):
        return True
    return any(token in normalized_text for token in UPLOAD_ENTRY_TEXT)


def text_to_image_candidate_skip_reason(selector: str, text: str, box: dict | None) -> str:
    clean = " ".join((text or "").split())
    if is_upload_like_text_to_image_candidate(selector, clean):
        return "upload_like"
    if clean not in {"文字配图", "写文字生成图片"} and len(clean) > 80:
        return "container_text_too_long"
    if box:
        width = float(box.get("width") or 0)
        height = float(box.get("height") or 0)
        if width > 900 or height > 500:
            return "container_box_too_large"
    return ""


async def async_locator_count(locator) -> int:
    count = locator.count() if hasattr(locator, "count") else 1
    return await count if hasattr(count, "__await__") else int(count)


async def async_locator_text(locator) -> str:
    try:
        text = locator.inner_text(timeout=1000)
        return (await text if hasattr(text, "__await__") else text or "").strip().replace("\n", " ")[:200]
    except Exception:
        return ""


async def async_locator_tag(locator) -> str:
    try:
        tag = locator.evaluate("node => node.tagName ? node.tagName.toLowerCase() : ''")
        return str(await tag if hasattr(tag, "__await__") else tag or "")
    except Exception:
        return ""


async def async_locator_visible(locator) -> bool | None:
    if not hasattr(locator, "is_visible"):
        return None
    try:
        visible = locator.is_visible()
        return bool(await visible if hasattr(visible, "__await__") else visible)
    except Exception:
        return None


async def async_locator_box(locator) -> dict | None:
    if not hasattr(locator, "bounding_box"):
        return None
    try:
        box = locator.bounding_box()
        return await box if hasattr(box, "__await__") else box
    except Exception:
        return None


async def async_text_to_image_candidates(page, selector_candidates) -> list[TextToImageCandidate]:
    selectors = []
    for selector in selector_list(selector_candidates) + TEXT_TO_IMAGE_FALLBACK_ENTRY_SELECTORS:
        if selector not in selectors:
            selectors.append(selector)
    results: list[TextToImageCandidate] = []
    if hasattr(page, "get_by_text"):
        try:
            locator = page.get_by_text("文字配图", exact=True)
            count = await async_locator_count(locator)
            if count:
                if hasattr(locator, "first"):
                    locator = locator.first
                text = await async_locator_text(locator) or "文字配图"
                box = await async_locator_box(locator)
                results.append(TextToImageCandidate(
                    locator=locator,
                    selector="get_by_text:文字配图",
                    tag=await async_locator_tag(locator),
                    text=text,
                    box=box,
                    visible=await async_locator_visible(locator),
                    upload_like=False,
                    reason_skipped=text_to_image_candidate_skip_reason("get_by_text:文字配图", text, box),
                ))
        except Exception:
            pass
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await async_locator_count(locator)
            if count <= 0:
                continue
            if hasattr(locator, "first"):
                locator = locator.first
            text = await async_locator_text(locator)
            box = await async_locator_box(locator)
            upload_like = is_upload_like_text_to_image_candidate(selector, text)
            candidate = TextToImageCandidate(
                locator=locator,
                selector=selector,
                tag=await async_locator_tag(locator),
                text=text,
                box=box,
                visible=await async_locator_visible(locator),
                upload_like=upload_like,
                reason_skipped=text_to_image_candidate_skip_reason(selector, text, box),
            )
            results.append(candidate)
        except Exception:
            continue
    return results


def summarize_text_to_image_candidates(candidates: list[TextToImageCandidate]) -> list[dict]:
    return [
        {
            "selector": item.selector,
            "tag": item.tag,
            "text": item.text[:120],
            "box": item.box,
            "visible": item.visible,
            "upload_like": item.upload_like,
            "reason_skipped": item.reason_skipped,
        }
        for item in candidates
    ]


async def async_click_text_to_image_entry(page, selector_candidates) -> SelectorHit:
    candidates = await async_text_to_image_candidates(page, selector_candidates)
    if not candidates:
        raise PlaywrightTimeoutError("没有找到小红书【写文字生成图片 / 文字配图】入口候选。")
    skipped_upload = []
    triggered_filechooser = []
    failed = []
    for candidate in candidates:
        if candidate.reason_skipped:
            skipped_upload.append(candidate)
            continue
        try:
            if hasattr(page, "expect_file_chooser"):
                clicked = False
                try:
                    async with page.expect_file_chooser(timeout=800):
                        await candidate.locator.click()
                        clicked = True
                    triggered_filechooser.append(candidate)
                    continue
                except PlaywrightTimeoutError as exc:
                    if clicked:
                        if await async_wait_for_text_editor_after_click(page):
                            return SelectorHit(locator=candidate.locator, selector=candidate.selector)
                        failed.append({"candidate": candidate, "error": "clicked_but_text_editor_not_detected"})
                        continue
                    raise exc
            else:
                await candidate.locator.click()
                if await async_wait_for_text_editor_after_click(page):
                    return SelectorHit(locator=candidate.locator, selector=candidate.selector)
                failed.append({"candidate": candidate, "error": "clicked_but_text_editor_not_detected"})
        except Exception as exc:
            if "file chooser" in str(exc).casefold():
                triggered_filechooser.append(candidate)
            else:
                failed.append({"candidate": candidate, "error": str(exc).splitlines()[0][:160]})
    details = summarize_text_to_image_candidates(candidates)
    if triggered_filechooser and len(triggered_filechooser) + len(skipped_upload) >= len(candidates):
        first = triggered_filechooser[0]
        raise TextToImageFileChooserError(
            "文字配图入口识别失败：当前点击会打开本地文件选择器，已停止以避免误上传。"
            f" wrong_text_to_image_candidate_triggered_filechooser; candidate selector={first.selector}; candidate text={first.text}; candidates={details}"
        )
    raise PlaywrightTimeoutError(f"没有找到或无法点击【文字配图】入口。请运行选择器诊断脚本。candidates={details}; failed={failed}")


async def async_wait_for_text_editor_after_click(page) -> bool:
    checks = [
        'text=写文字',
        'button:has-text("生成图片")',
        '[role="button"]:has-text("生成图片")',
        '[contenteditable="true"]',
        'textarea',
    ]
    try:
        await async_find_first_visible(page, checks, timeout=5_000)
        return True
    except Exception:
        return False


async def async_detect_text_to_image_state(page, selectors: dict) -> str:
    with suppress(Exception):
        await async_find_first_visible(page, selectors.get("text_editor_page_ready", []), timeout=1_000)
        return "already_on_text_editor_page"
    with suppress(Exception):
        await async_find_first_visible(page, selectors.get("next_button", []), timeout=1_000)
        return "generated_page"
    return "entry_page"


async def async_fill_text_card_input(locator, content: str) -> None:
    try:
        await locator.fill(content)
        return
    except Exception:
        pass
    if hasattr(locator, "click"):
        await locator.click()
    if hasattr(locator, "evaluate"):
        await locator.evaluate(
            """(node, value) => {
                if ('value' in node) node.value = value;
                else node.textContent = value;
                node.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
                node.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            content,
        )
        return
    raise PlaywrightTimeoutError("没有找到可填写的文字配图内容输入区。")


async def async_click_generate_image_button(page, selectors: dict) -> None:
    button = await async_find_first_visible(page, selectors.get("generate_button", []), timeout=30_000)
    with suppress(Exception):
        if hasattr(button.locator, "is_enabled"):
            enabled = button.locator.is_enabled()
            enabled = await enabled if hasattr(enabled, "__await__") else enabled
            if enabled is False:
                raise RuntimeError("生成图片按钮不可点击，请检查文字配图内容是否为空。")
    with suppress(Exception):
        if hasattr(button.locator, "scroll_into_view_if_needed"):
            await button.locator.scroll_into_view_if_needed(timeout=5_000)
    try:
        await button.locator.click()
    except Exception:
        if hasattr(button.locator, "evaluate"):
            await button.locator.evaluate("node => node.click()")
        else:
            raise
    try:
        await async_find_first_visible(page, selectors.get("generated_ready", []) + selectors.get("next_button", []), timeout=60_000)
    except PlaywrightTimeoutError:
        raise PlaywrightTimeoutError("已填写文字配图内容，但点击生成图片后没有检测到生成结果。") from None


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


async def async_find_first_locator(page, selector_candidates, timeout: int = 30_000) -> SelectorHit:
    candidates = selector_list(selector_candidates)
    last_error: Exception | None = None
    deadline = datetime.now().timestamp() + (timeout / 1000)
    while datetime.now().timestamp() < deadline:
        for selector in candidates:
            try:
                locator = page.locator(selector)
                count = await locator.count() if hasattr(locator, "count") else 1
                if count:
                    if hasattr(locator, "first"):
                        locator = locator.first
                    return SelectorHit(locator=locator, selector=selector)
            except Exception as exc:
                last_error = exc
        with suppress(Exception):
            await page.wait_for_timeout(250)
    raise PlaywrightTimeoutError(f"No selector found from candidates: {candidates}") from last_error


async def async_upload_files(page, selectors: dict, paths: list[str], file_input_key: str, upload_area_key: str) -> None:
    try:
        file_input = await async_find_first_locator(page, selectors.get(file_input_key, []), timeout=15_000)
        await file_input.locator.set_input_files(paths)
        return
    except Exception as first_error:
        try:
            upload_area = await async_find_first_visible(page, selectors.get(upload_area_key, []), timeout=15_000)
            if hasattr(page, "expect_file_chooser"):
                async with page.expect_file_chooser() as chooser_info:
                    await upload_area.locator.click()
                chooser = await chooser_info.value
                await chooser.set_files(paths)
            else:
                await upload_area.locator.click()
                raise first_error
        except Exception as fallback_error:
            raise PlaywrightTimeoutError(f"上传文件失败；file input 候选：{selector_list(selectors.get(file_input_key, []))}；upload area 候选：{selector_list(selectors.get(upload_area_key, []))}") from fallback_error


async def async_wait_uploaded_or_editor_ready(page, selectors: dict, timeout: int = 90_000) -> None:
    candidates = selector_list(selectors.get("uploaded_ready", [])) + selector_list(selectors.get("title", [])) + selector_list(selectors.get("body", []))
    await async_find_first_visible(page, candidates, timeout=timeout)


async def async_wait_publish_page_ready(page, selectors: dict, timeout: int = 30_000) -> SelectorHit:
    return await async_find_first_visible(page, selector_candidates(selectors, PUBLISH_KIND_IMAGE_UPLOAD, "page_ready"), timeout=timeout)


async def async_detect_active_publish_tab(page, selectors: dict) -> str:
    checks = [
        ("upload_image", selectors.get("active_tab_upload_image", [])),
        ("upload_video", selectors.get("active_tab_upload_video", selectors.get("tab_upload_video", []))),
    ]
    for name, candidates in checks:
        for selector in selector_list(candidates):
            with suppress(Exception):
                if await page.locator(selector).count() > 0:
                    return name
    return "unknown"


async def async_wait_image_editor_ready(page, selectors: dict, require_title_body: bool = False, timeout: int = 30_000) -> None:
    group = selector_group(selectors, PUBLISH_KIND_IMAGE_UPLOAD)
    await async_find_first_visible(page, group.get("page_ready", selectors.get("image_page_ready", group.get("upload_area", []))), timeout=timeout)
    await async_find_first_visible(page, group.get("upload_area", selectors.get("image_upload_area", group.get("file_input", []))), timeout=timeout)
    if require_title_body:
        await async_find_first_visible(page, group.get("title", []), timeout=60_000)
        await async_find_first_visible(page, group.get("body", []), timeout=60_000)


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
