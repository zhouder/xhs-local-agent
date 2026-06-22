from __future__ import annotations

import json
import base64
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session

from app.config import ROOT, Settings
from app.models import BrowserError
from app.repositories import AuditRepository, NoteRepository
from app.services.notifications import Notifier
from app.services.policy import PolicyEngine


class XHSBrowser:
    def __init__(self, db: Session, settings: Settings, notifier: Notifier):
        self.db, self.settings, self.notifier = db, settings, notifier
        self.audit = AuditRepository(db)
        with (ROOT / "app/browser/selectors/xhs.yaml").open(encoding="utf-8") as stream:
            self.selectors = yaml.safe_load(stream)["publish"]

    def fill_approved_note(self, note_id: int, *, dry_run: bool = True) -> None:
        note = NoteRepository(self.db).get(note_id)
        if not note:
            raise LookupError("Note not found")
        decision = PolicyEngine(self.db, self.settings).check("publish", text=f"{note.title}\n{note.body}", note_status=note.status)
        if not decision.allowed:
            self.audit.record("browser.fill_publish", "blocked", target_type="note", target_id=note_id, output_summary=decision.reason)
            raise PermissionError(decision.reason)
        if not dry_run:
            self.audit.record("browser.real_publish", "blocked", target_type="note", target_id=note_id, output_summary="disabled_in_phase_1")
            raise PermissionError("Real publish is disabled in phase 1")
        screenshot = ""
        page = None
        browser = None
        context = None
        playwright = None
        try:
            assets = NoteRepository(self.db).media_paths(note.id)
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(channel=self.settings.browser.get("channel"), headless=False, slow_mo=self.settings.browser.get("slow_mo_ms", 300))
            context = browser.new_context()
            page = context.new_page()
            page.goto(self.settings.browser["publish_url"])
            print("请在打开的浏览器中手动登录。程序不会读取或保存 cookie。")
            page.wait_for_selector(self.selectors["title"], timeout=180_000)
            page.locator(self.selectors["title"]).fill(note.title)
            page.locator(self.selectors["body"]).fill(note.body + "\n" + " ".join(f"#{x}" for x in json.loads(note.hashtags_json)))
            if assets:
                page.locator(self.selectors["file_input"]).set_input_files(assets)
            page.wait_for_timeout(1500)
            screenshot = self._screenshot(page, note.id, "dry-run")
            self.audit.record("browser.fill_publish", "success", target_type="note", target_id=note.id, screenshot_path=screenshot, metadata={"dry_run": True})
            self._notify("Dry-run 已完成", f"草稿 {note.id} 已填表，未点击发布", note.id)
            page.wait_for_timeout(5000)
        except Exception as exc:
            if page is not None and not screenshot:
                try:
                    screenshot = self._screenshot(page, note.id, "failed")
                except Exception:
                    screenshot = ""
            if not screenshot:
                screenshot = self._unavailable_screenshot(note.id)
            self.db.rollback()
            self.db.add(BrowserError(action_type="fill_publish", error_message=str(exc), screenshot_path=screenshot, metadata_json=json.dumps({"note_id": note_id, "page_screenshot_available": page is not None})))
            self.db.commit()
            self.audit.record("browser.fill_publish", "failed", target_type="note", target_id=note_id, error_message=str(exc), screenshot_path=screenshot)
            self._notify("Dry-run 失败", f"草稿 {note.id}: {exc}", note.id)
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
        """Write a valid placeholder PNG when Playwright failed before a page existed."""
        directory = ROOT / self.settings.browser["screenshots_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"note-{note_id}-{datetime.now():%Y%m%d-%H%M%S}-page-unavailable.png"
        pixel_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        path.write_bytes(pixel_png)
        return str(path)
