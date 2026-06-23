from __future__ import annotations

import base64
import json
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session

from app.config import ROOT, Settings
from app.database import utcnow
from app.models import BrowserError, NoteStatus
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
        screenshot = self._run_browser_fill(note, assets, mode=mode, click_publish=False)
        transition_note(note, NoteStatus.PUBLISHING)
        transition_note(note, NoteStatus.WAITING_FINAL_CONFIRM)
        note.publish_mode = mode
        note.publish_screenshot_path = screenshot
        note.publish_error_message = ""
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
        return note

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
            browser = playwright.chromium.launch(
                channel=self.settings.browser.get("channel"),
                headless=False,
                slow_mo=self.settings.browser.get("slow_mo_ms", 300),
            )
            context = browser.new_context()
            page = context.new_page()
            step = "open_publish_page"
            page.goto(self.settings.browser["publish_url"])
            print("Please complete XHS login manually in the opened browser. This app does not read or save cookies.")
            step, selector_name = "wait_title", "title"
            page.wait_for_selector(self.selectors["title"], timeout=180_000)
            step, selector_name = "fill_title", "title"
            page.locator(self.selectors["title"]).fill(note.title)
            hashtags = " ".join(f"#{tag}" for tag in json.loads(note.hashtags_json))
            step, selector_name = "fill_body", "body"
            page.locator(self.selectors["body"]).fill(f"{note.body}\n{hashtags}".strip())
            if self.selectors.get("topic_input"):
                with suppress(Exception):
                    step, selector_name = "fill_topics", "topic_input"
                    page.locator(self.selectors["topic_input"]).fill(hashtags)
            if assets:
                step, selector_name = "upload_assets", "file_input"
                page.locator(self.selectors["file_input"]).set_input_files(assets)
            page.wait_for_timeout(1500)
            screenshot = self._screenshot(page, note.id, "published-clicked" if click_publish else mode)
            if click_publish:
                step, selector_name = "click_publish", "submit_button"
                page.locator(self.selectors["submit_button"]).click()
                page.wait_for_timeout(3000)
                screenshot = self._screenshot(page, note.id, "publish-uncertain")
            return screenshot
        except Exception as exc:
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
                error_message=str(exc),
                screenshot_path=screenshot,
                metadata_json=json.dumps({"page_screenshot_available": page is not None}, ensure_ascii=False),
            ))
            self.db.commit()
            self.audit.record(
                "browser.final_confirm" if click_publish else "browser.fill_publish",
                "failed",
                target_type="note",
                target_id=note.id,
                error_message=str(exc),
                screenshot_path=screenshot,
                metadata={"mode": mode, "step": step, "selector_name": selector_name},
            )
            note = self.notes.get(note.id)
            if note and note.status == NoteStatus.PUBLISHING:
                transition_note(note, NoteStatus.FAILED)
                note.publish_error_message = str(exc)
                note.publish_screenshot_path = screenshot
                self.db.commit()
            self._notify("Browser publish flow failed", f"note_id={note.id}; step={step}; {exc}", note.id)
            raise
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
