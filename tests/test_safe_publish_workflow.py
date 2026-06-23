from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.ai.mock import MockProvider
from app.browser import xhs as xhs_module
from app.database import get_db
from app.models import AuditLog, CommandEvent, ContentPlanTopic, NoteStatus
from app.repositories import NoteRepository
from app.schemas import GenerateNoteRequest
from app.services.commands import CommandExecutor
from app.services.content_plans import ContentPlanService
from app.services.materials import MaterialService
from app.services.notifications import NullNotifier
from app.services.publish import PublishService
from app.services.review import ReviewService
from app.services.state_machine import transition_note


class RecordingLocator:
    def __init__(self, page):
        self.page = page

    def fill(self, value):
        self.page.filled.append(value)

    def set_input_files(self, paths):
        self.page.files = list(paths)

    def click(self):
        self.page.clicked = True


class RecordingPage:
    def __init__(self):
        self.clicked = False
        self.filled = []
        self.files = []

    def goto(self, url):
        self.url = url

    def wait_for_selector(self, selector, timeout):
        return None

    def locator(self, selector):
        return RecordingLocator(self)

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
    def __init__(self, pages):
        self.pages = pages

    def launch(self, **kwargs):
        return FakeBrowser(self.pages.pop(0))


class FakePlaywright:
    def __init__(self, pages):
        self.chromium = FakeChromium(pages)

    def start(self):
        return self

    def stop(self):
        return None


def client_for(db):
    def override_db():
        yield db

    main.app.dependency_overrides[get_db] = override_db
    return TestClient(main.app)


def approved_note(db):
    request = GenerateNoteRequest(topic="AI workflow")
    note = NoteRepository(db).create(request, MockProvider().generate_note(request))
    review = ReviewService(db, NullNotifier())
    review.submit(note.id)
    review.approve(note.id)
    return note


def test_waiting_final_confirm_is_required_for_final_publish(db):
    note = approved_note(db)
    with pytest.raises(ValueError):
        transition_note(note, NoteStatus.PUBLISHED)
    note.status = NoteStatus.WAITING_FINAL_CONFIRM
    transition_note(note, NoteStatus.PUBLISH_UNCERTAIN)
    assert note.status == NoteStatus.PUBLISH_UNCERTAIN


def test_fill_only_fills_without_click_and_final_confirm_clicks(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    asset = tmp_path / "cover.png"
    asset.write_bytes(b"png")
    MaterialService(db).set_note_assets(note.id, [str(asset)])
    settings.browser["screenshots_dir"] = str(tmp_path)
    fill_page, final_page = RecordingPage(), RecordingPage()
    pages = [fill_page, final_page]
    monkeypatch.setattr(xhs_module, "sync_playwright", lambda: FakePlaywright(pages))

    PublishService(db, settings, NullNotifier()).fill(note.id, mode="fill_only")
    assert not fill_page.clicked
    assert note.status == NoteStatus.WAITING_FINAL_CONFIRM
    assert note.publish_screenshot_path

    PublishService(db, settings, NullNotifier()).final_confirm(note.id)
    assert final_page.clicked
    assert note.status == NoteStatus.PUBLISH_UNCERTAIN


def test_invalid_and_unsupported_assets_block_publish(db, tmp_path):
    note = approved_note(db)
    with pytest.raises(ValueError, match="does not exist"):
        MaterialService(db).set_note_assets(note.id, [str(tmp_path / "missing.png")])
    bad = tmp_path / "bad.gif"
    bad.write_bytes(b"gif")
    with pytest.raises(ValueError, match="Unsupported"):
        MaterialService(db).set_note_assets(note.id, [str(bad)])


def test_content_plan_creates_topics_and_generates_drafts(db):
    service = ContentPlanService(db, MockProvider())
    plan = service.create_plan(
        name="June plan",
        audience="builders",
        style="tutorial",
        goal="growth",
        topics_text="topic one\ntopic two",
    )
    created = service.generate_drafts(plan.id)
    assert len(created) == 2
    topics = list(db.scalars(select(ContentPlanTopic).where(ContentPlanTopic.plan_id == plan.id)))
    assert all(topic.status == "generated" and topic.note_id for topic in topics)
    assert all(note.status == NoteStatus.DRAFT for note in created)


def test_command_approve_and_final_confirm_guards(db):
    note = approved_note(db)
    note.status = NoteStatus.PENDING_REVIEW
    db.commit()
    response = CommandExecutor(db, {}, NullNotifier()).execute(f"/approve {note.id}")
    assert "not published" in response
    assert note.status == NoteStatus.APPROVED
    with pytest.raises(ValueError, match="waiting_final_confirm"):
        CommandExecutor(db, {}, NullNotifier()).execute(f"/final_confirm {note.id}")
    event = db.scalar(select(CommandEvent).where(CommandEvent.command == "final_confirm", CommandEvent.status == "failed"))
    assert event is not None


def test_dashboard_counts_waiting_final_confirm(db):
    note = approved_note(db)
    note.status = NoteStatus.WAITING_FINAL_CONFIRM
    db.commit()
    with client_for(db) as client:
        response = client.get("/")
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "waiting_final_confirm" in response.text
    assert str(note.id) in response.text


def test_no_real_interaction_logic_added():
    source = Path("app/browser/xhs.py").read_text(encoding="utf-8")
    assert "add_cookies" not in source
    assert "storage_state" not in source
    assert "comment" not in source.casefold()
    assert "private_message" not in source.casefold()
