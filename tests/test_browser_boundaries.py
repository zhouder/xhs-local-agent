from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.ai.mock import MockProvider
from app.browser import xhs as xhs_module
from app.browser.xhs import XHSBrowser
from app.models import AuditLog, BrowserError
from app.repositories import NoteRepository
from app.schemas import GenerateNoteRequest
from app.services.notifications import NullNotifier
from app.services.review import ReviewService


class FakeLocator:
    def __init__(self, page=None):
        self.page = page

    def wait_for(self, state="visible", timeout=0):
        if self.page and self.page.fail:
            raise RuntimeError("selector changed")

    def fill(self, value):
        return None

    def set_input_files(self, paths):
        return None


class FakePage:
    def __init__(self, fail=False):
        self.fail = fail

    def goto(self, url):
        return None

    def wait_for_selector(self, selector, timeout):
        if self.fail:
            raise RuntimeError("selector changed")

    def locator(self, selector):
        return FakeLocator(self)

    def wait_for_timeout(self, milliseconds):
        return None

    def screenshot(self, path, full_page):
        Path(path).write_bytes(b"fake-png")


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def close(self):
        return None


class FakeBrowser:
    def __init__(self, page):
        self.page = page

    def new_context(self):
        return FakeContext(self.page)

    def close(self):
        return None


class FakeChromium:
    def __init__(self, page):
        self.page = page

    def launch(self, **kwargs):
        return FakeBrowser(self.page)


class FakePlaywright:
    def __init__(self, page):
        self.chromium = FakeChromium(page)

    def start(self):
        return self

    def stop(self):
        return None


class FailingPlaywright:
    def start(self):
        raise RuntimeError("browser unavailable")


def approved_note(db):
    request = GenerateNoteRequest(topic="AI")
    note = NoteRepository(db).create(request, MockProvider().generate_note(request.topic, request.style, request.audience))
    review = ReviewService(db, NullNotifier())
    review.submit(note.id)
    review.approve(note.id)
    return note


def test_dry_run_creates_local_preview_without_browser(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)
    monkeypatch.setattr(xhs_module, "sync_playwright", lambda: (_ for _ in ()).throw(AssertionError("dry_run should not open browser")))
    XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=True)
    row = db.scalar(select(AuditLog).where(AuditLog.action_type == "browser.dry_run_preview", AuditLog.status == "success"))
    assert row is not None
    assert Path(row.screenshot_path).exists()


def test_fill_only_browser_failure_saves_screenshot_error_and_audit(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)
    monkeypatch.setattr(xhs_module, "sync_playwright", lambda: FakePlaywright(FakePage(fail=True)))
    with pytest.raises(RuntimeError, match="没有找到|等待发布页"):
        XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=False, mode="fill_only")
    error = db.scalar(select(BrowserError))
    audit = db.scalar(select(AuditLog).where(AuditLog.action_type == "browser.fill_publish", AuditLog.status == "failed"))
    assert error and audit
    assert error.screenshot_path == audit.screenshot_path
    assert Path(error.screenshot_path).exists()


def test_real_publish_is_blocked_and_audited(db, settings):
    note = approved_note(db)
    with pytest.raises(PermissionError, match="disabled"):
        XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=False)
    audit = db.scalar(select(AuditLog).where(AuditLog.action_type == "browser.real_publish"))
    assert audit.status == "blocked"


def test_launch_failure_still_writes_failure_artifact(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)
    monkeypatch.setattr(xhs_module, "sync_playwright", lambda: FailingPlaywright())
    with pytest.raises(RuntimeError, match="browser unavailable"):
        XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=False, mode="fill_only")
    error = db.scalar(select(BrowserError))
    assert error.screenshot_path.endswith("page-unavailable.png")
    assert Path(error.screenshot_path).read_bytes().startswith(b"\x89PNG")
