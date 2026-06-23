from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app import main
from app.ai.mock import MockProvider
from app.browser import xhs as xhs_module
from app.browser.xhs import detect_publish_target_from_url, resolve_publish_url
from app.database import get_db
from app.models import AuditLog, BrowserError, CommandEvent, ContentPlanTopic, MediaAsset, NoteStatus
from app.repositories import NoteRepository
from app.schemas import GenerateNoteRequest
from app.services.commands import CommandExecutor
from app.services.content_plans import ContentPlanService
from app.services.materials import MaterialService
from app.services.notifications import NullNotifier
from app.services.publish import PublishService
from app.services.review import ReviewService
from app.services.state_machine import transition_note
from app.services.hashtags import ensure_hashtags


class RecordingLocator:
    def __init__(self, page, selector=""):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        return None

    async def count(self):
        if "active" in self.selector or "aria-selected" in self.selector:
            if "上传图文" in self.selector and self.page.current_tab == "upload_image":
                return 1
            if "写长文" in self.selector and self.page.current_tab == "long_text":
                return 1
            if "上传视频" in self.selector and self.page.current_tab == "upload_video":
                return 1
            return 0
        return 1

    async def fill(self, value):
        self.page.filled.append(value)
        self.page.operations.append(("fill", self.selector, value))

    async def set_input_files(self, paths):
        self.page.files = list(paths)
        self.page.operations.append(("set_input_files", self.selector, list(paths)))

    async def click(self):
        self.page.clicked_selectors.append(self.selector)
        if "上传图文" in self.selector:
            self.page.current_tab = "upload_image"
        elif "写长文" in self.selector:
            self.page.current_tab = "long_text"
        elif "发布" in self.selector:
            self.page.clicked = True

    async def evaluate(self, script):
        return "button"

    async def get_attribute(self, name):
        return ""

    async def inner_text(self, timeout=1000):
        return self.selector


class RecordingPage:
    def __init__(self):
        self.clicked = False
        self.filled = []
        self.files = []
        self.url = ""
        self.current_tab = "upload_video"
        self.clicked_selectors = []
        self.goto_urls = []
        self.operations = []

    async def goto(self, url):
        self.url = url
        self.goto_urls.append(url)
        if "target=image" in url:
            self.current_tab = "upload_image"
        elif "target=article" in url:
            self.current_tab = "long_text"
        elif "target=video" in url:
            self.current_tab = "upload_video"

    def locator(self, selector):
        return RecordingLocator(self, selector)

    async def wait_for_timeout(self, milliseconds):
        return None

    async def screenshot(self, path, full_page):
        Path(path).write_bytes(b"fake-png")


class VideoRedirectPage(RecordingPage):
    async def goto(self, url):
        await super().goto(url)
        self.url = "https://creator.xiaohongshu.com/publish/publish?from=menu&target=video"


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [page]

    async def new_page(self):
        return self.page

    async def close(self):
        return None


class FakeBrowser:
    def __init__(self, page):
        self.page = page

    async def new_context(self):
        return FakeContext(self.page)

    async def close(self):
        return None


class FakeChromium:
    def __init__(self, pages):
        self.pages = pages

    async def launch(self, **kwargs):
        return FakeBrowser(self.pages.pop(0))

    async def launch_persistent_context(self, *args, **kwargs):
        return FakeContext(self.pages.pop(0))


class FakePlaywright:
    def __init__(self, pages):
        self.chromium = FakeChromium(pages)

    async def start(self):
        return self

    async def stop(self):
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


def test_resolve_publish_url_uses_target_urls(settings):
    assert "target=image" in resolve_publish_url(settings, "image_upload")
    assert "target=image" in resolve_publish_url(settings, "image_text_to_image")
    assert "target=video" in resolve_publish_url(settings, "video_upload")
    assert "target=article" not in resolve_publish_url(settings, "image_text_to_image")


def test_detect_publish_target_from_url():
    assert detect_publish_target_from_url("https://creator.xiaohongshu.com/publish/publish?from=menu&target=video") == "video"
    assert detect_publish_target_from_url("https://creator.xiaohongshu.com/publish/publish?from=menu&target=image") == "image"
    assert detect_publish_target_from_url("https://creator.xiaohongshu.com/publish/publish?from=menu&target=article") == "unknown"
    assert detect_publish_target_from_url("https://creator.xiaohongshu.com/publish/publish") == "unknown"


def test_publish_kind_defaults_and_switches_with_materials(db, tmp_path):
    note = approved_note(db)
    assert note.publish_kind == "image_text_to_image"
    image = tmp_path / "cover.png"
    image.write_bytes(b"png")
    MaterialService(db).set_note_assets(note.id, [str(image)])
    assert note.publish_kind == "image_upload"


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
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: FakePlaywright(pages))

    PublishService(db, settings, NullNotifier()).fill(note.id, mode="fill_only")
    assert not fill_page.clicked
    assert any("target=image" in url for url in fill_page.goto_urls)
    assert not any("target=video" in url for url in fill_page.goto_urls)
    upload_index = next(index for index, op in enumerate(fill_page.operations) if op[0] == "set_input_files")
    fill_index = next(index for index, op in enumerate(fill_page.operations) if op[0] == "fill")
    assert upload_index < fill_index
    assert note.publish_screenshot_path
    assert note.status == NoteStatus.WAITING_FINAL_CONFIRM

    PublishService(db, settings, NullNotifier()).final_confirm(note.id)
    assert final_page.clicked
    assert note.status == NoteStatus.PUBLISH_UNCERTAIN


def test_dry_run_does_not_start_playwright_or_visit_xhs(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)

    def fail_if_called():
        raise AssertionError("dry_run must not start Playwright")

    monkeypatch.setattr(xhs_module, "async_playwright", fail_if_called)
    PublishService(db, settings, NullNotifier()).fill(note.id, mode="dry_run")
    assert note.status == NoteStatus.WAITING_FINAL_CONFIRM
    assert note.publish_mode == "dry_run"
    assert "dry_run_preview" in note.publish_error_message
    assert Path(note.publish_screenshot_path).exists()
    assert note.publish_preview_html_path
    assert Path(note.publish_preview_html_path).exists()
    with Image.open(note.publish_screenshot_path) as image:
        assert image.size == (1080, 1440)


def test_fill_only_without_assets_uses_text_to_image_not_article(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    settings.browser["screenshots_dir"] = str(tmp_path)
    page = RecordingPage()
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: FakePlaywright([page]))
    PublishService(db, settings, NullNotifier()).fill(note.id, mode="fill_only")
    assert any("target=image" in url for url in page.goto_urls)
    assert not any("target=article" in url for url in page.goto_urls)
    assert not any("target=video" in url for url in page.goto_urls)
    assert not any(op[0] == "set_input_files" for op in page.operations)
    assert any("写文字生成图片" in selector or "文字生成图片" in selector for selector in page.clicked_selectors)


def test_video_upload_uses_video_target_and_does_not_click_publish(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    note.publish_kind = "video_upload"
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    db.add(MediaAsset(note_id=note.id, path=str(video), file_path=str(video), media_type="video", asset_type="video", upload_order=1, status="ready"))
    db.commit()
    settings.browser["screenshots_dir"] = str(tmp_path)
    page = RecordingPage()
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: FakePlaywright([page]))
    PublishService(db, settings, NullNotifier()).fill(note.id, mode="fill_only")
    assert any("target=video" in url for url in page.goto_urls)
    assert any(op[0] == "set_input_files" and op[2] == [str(video)] for op in page.operations)
    assert not page.clicked


def test_video_upload_without_video_is_blocked(db, settings):
    note = approved_note(db)
    note.publish_kind = "video_upload"
    db.commit()
    with pytest.raises(ValueError, match="视频笔记需要先添加一个 mp4/mov 视频文件"):
        PublishService(db, settings, NullNotifier()).fill(note.id, mode="fill_only")


def test_target_video_redirect_records_requested_and_actual_target(db, settings, tmp_path, monkeypatch):
    note = approved_note(db)
    asset = tmp_path / "cover.png"
    asset.write_bytes(b"png")
    MaterialService(db).set_note_assets(note.id, [str(asset)])
    settings.browser["screenshots_dir"] = str(tmp_path)
    monkeypatch.setattr(xhs_module, "async_playwright", lambda: FakePlaywright([VideoRedirectPage()]))
    with pytest.raises(RuntimeError, match="target=video"):
        PublishService(db, settings, NullNotifier()).fill(note.id, mode="fill_only")
    error = db.scalar(select(BrowserError).where(BrowserError.note_id == note.id))
    assert '"requested_target": "image"' in error.metadata_json
    assert '"actual_target": "video"' in error.metadata_json


def test_invalid_and_unsupported_assets_block_publish(db, tmp_path):
    note = approved_note(db)
    with pytest.raises(ValueError, match="不存在"):
        MaterialService(db).set_note_assets(note.id, [str(tmp_path / "missing.png")])
    bad = tmp_path / "bad.gif"
    bad.write_bytes(b"gif")
    with pytest.raises(ValueError, match="不支持"):
        MaterialService(db).set_note_assets(note.id, [str(bad)])


def test_hashtags_are_extracted_or_generated():
    assert ensure_hashtags("title", "正文 #AI #效率工具", [])[:2] == ["AI", "效率工具"]
    generated = ensure_hashtags("编程工具", "正文没有话题", [])
    assert len(generated) >= 3
    assert all(not tag.startswith("#") for tag in generated)


def test_media_upload_reorder_delete_and_generated_cover(db, tmp_path):
    note = approved_note(db)
    first = tmp_path / "a.png"
    second = tmp_path / "b.jpg"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    class Upload:
        def __init__(self, path):
            self.filename = path.name
            self.file = path.open("rb")

    uploads = [Upload(first), Upload(second)]
    try:
        MaterialService(db).upload_files(note.id, uploads)
    finally:
        for upload in uploads:
            upload.file.close()
    assets = list(db.scalars(select(MediaAsset).where(MediaAsset.note_id == note.id).order_by(MediaAsset.upload_order)))
    assert [asset.upload_order for asset in assets] == [1, 2]
    assert all(Path(asset.file_path).exists() for asset in assets)

    MaterialService(db).reorder(note.id, [assets[1].id, assets[0].id])
    reordered = list(db.scalars(select(MediaAsset).where(MediaAsset.note_id == note.id).order_by(MediaAsset.upload_order)))
    assert [asset.id for asset in reordered] == [assets[1].id, assets[0].id]

    MaterialService(db).delete(note.id, reordered[0].id)
    assert db.get(MediaAsset, reordered[0].id) is None
    cover = MaterialService(db).generate_cover(note.id)
    assert cover.source_type == "generated_cover"
    assert Path(cover.file_path).exists()
    with Image.open(cover.file_path) as image:
        assert image.size == (1080, 1440)


def test_note_detail_media_ui_auto_uploads_and_has_no_upload_button(db):
    note = approved_note(db)
    note.publish_kind = "image_upload"
    db.commit()
    with client_for(db) as client:
        response = client.get(f"/notes/{note.id}")
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "添加图片" in response.text
    assert "uploadForm.submit()" in response.text
    assert '<button class="primary">上传图片</button>' not in response.text
    assert "upload-dropzone" in response.text


def test_note_detail_shows_video_and_text_to_image_modes(db):
    note = approved_note(db)
    note.publish_kind = "video_upload"
    db.commit()
    with client_for(db) as client:
        response = client.get(f"/notes/{note.id}")
    assert response.status_code == 200
    assert "添加视频" in response.text
    note.publish_kind = "image_text_to_image"
    db.commit()
    with client_for(db) as client:
        response = client.get(f"/notes/{note.id}")
    main.app.dependency_overrides.clear()
    assert "文字生图" in response.text
    assert "必须添加图片" not in response.text


def test_uploaded_thumbnail_url_is_accessible(db, tmp_path):
    note = approved_note(db)
    image_path = tmp_path / "thumb.png"
    Image.new("RGB", (20, 20), "#ffffff").save(image_path)

    with client_for(db) as client:
        with image_path.open("rb") as stream:
            response = client.post(f"/notes/{note.id}/media/upload", files=[("files", ("thumb.png", stream, "image/png"))])
        assert response.status_code == 200
        page = client.get(f"/notes/{note.id}")
        assert "/media/note-" in page.text
        asset = db.scalar(select(MediaAsset).where(MediaAsset.note_id == note.id))
        media_response = client.get(f"/media/note-{note.id}/{Path(asset.file_path).name}")
        assert media_response.status_code == 200
    main.app.dependency_overrides.clear()


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


def test_plan_detail_shows_batch_generation_buttons(db):
    plan = ContentPlanService(db).create_plan(
        name="Plan",
        audience="builders",
        style="tutorial",
        goal="growth",
        topics_text="topic one",
    )
    with client_for(db) as client:
        response = client.get(f"/plans/{plan.id}")
    main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "批量生成草稿" in response.text
    assert "只生成未生成主题" in response.text
    assert "重新生成失败主题" in response.text


def test_no_real_interaction_logic_added():
    source = Path("app/browser/xhs.py").read_text(encoding="utf-8")
    assert "add_cookies" not in source
    assert "storage_state" not in source
    assert "comment" not in source.casefold()
    assert "private_message" not in source.casefold()
