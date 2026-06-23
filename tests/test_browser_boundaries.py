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
    def __init__(self, page=None, selector=""):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        if self.page and self.page.fail:
            raise RuntimeError("selector changed")

    async def count(self):
        if "active" in self.selector or "aria-selected" in self.selector:
            if "上传视频" in self.selector and self.page.current_tab == "upload_video":
                return 1
            if "上传图文" in self.selector and self.page.current_tab == "upload_image":
                return 1
            if "写长文" in self.selector and self.page.current_tab == "long_text":
                return 1
            return 0
        return 1

    async def fill(self, value):
        return None

    async def set_input_files(self, paths):
        return None

    async def click(self):
        if "上传图文" in self.selector:
            self.page.current_tab = "upload_image"
        elif "写长文" in self.selector:
            self.page.current_tab = "long_text"


class FakePage:
    def __init__(self, fail=False):
        self.fail = fail
        self.url = ""
        self.current_tab = "upload_video"

    async def goto(self, url):
        self.url = url
        return None

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def wait_for_timeout(self, milliseconds):
        return None

    async def screenshot(self, path, full_page):
        Path(path).write_bytes(b"fake-png")

    async def title(self):
        return "fake title"


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [page]
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True
        return None


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_context(self):
        return FakeContext(self.page)

    async def close(self):
        self.closed = True
        return None


class FakeChromium:
    def __init__(self, page):
        self.page = page

    async def launch(self, **kwargs):
        return FakeBrowser(self.page)

    async def launch_persistent_context(self, *args, **kwargs):
        return FakeContext(self.page)


class FakePlaywright:
    def __init__(self, page):
        self.chromium = FakeChromium(page)
        self.stopped = False

    async def start(self):
        return self

    async def stop(self):
        self.stopped = True
        return None


class FailingPlaywright:
    async def start(self):
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
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: (_ for _ in ()).throw(AssertionError("dry_run should not open browser")))
    XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=True)
    row = db.scalar(select(AuditLog).where(AuditLog.action_type == "browser.dry_run_preview", AuditLog.status == "success"))
    assert row is not None
    assert Path(row.screenshot_path).exists()


def test_fill_only_browser_failure_saves_screenshot_error_and_audit(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: FakePlaywright(FakePage(fail=True)))
    with pytest.raises(RuntimeError, match="没有找到|已登录"):
        XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=False, mode="fill_only")
    error = db.scalar(select(BrowserError))
    audit = db.scalar(select(AuditLog).where(AuditLog.action_type == "browser.fill_publish", AuditLog.status == "failed"))
    assert error and audit
    assert error.screenshot_path == audit.screenshot_path
    assert Path(error.screenshot_path).exists()
    assert "selector_candidates" in error.metadata_json
    assert "current_tab" in error.metadata_json


def test_keep_open_on_error_leaves_browser_for_debugging(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)
    settings.browser["keep_open_on_error"] = True
    fake = FakePlaywright(FakePage(fail=True))
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: fake)
    with pytest.raises(RuntimeError):
        XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=False, mode="fill_only")
    assert fake.stopped is False


def test_real_publish_is_blocked_and_audited(db, settings):
    note = approved_note(db)
    with pytest.raises(PermissionError, match="disabled"):
        XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=False)
    audit = db.scalar(select(AuditLog).where(AuditLog.action_type == "browser.real_publish"))
    assert audit.status == "blocked"


def test_launch_failure_still_writes_failure_artifact(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: FailingPlaywright())
    with pytest.raises(RuntimeError, match="browser unavailable"):
        XHSBrowser(db, settings, NullNotifier()).fill_approved_note(note.id, dry_run=False, mode="fill_only")
    error = db.scalar(select(BrowserError))
    assert error.screenshot_path.endswith("page-unavailable.png")
    assert Path(error.screenshot_path).read_bytes().startswith(b"\x89PNG")
