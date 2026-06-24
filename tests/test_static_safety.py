import ast
import inspect
from pathlib import Path

from app import main
from scripts import check_xhs_selectors as selector_script


ROOT = Path(__file__).resolve().parents[1]


def test_no_cookie_or_persistent_profile_api_usage():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.py"))
    forbidden = ["storage_state", ".cookies(", "add_cookies("]
    assert not [token for token in forbidden if token in source]


def test_browser_locator_calls_use_central_selector_map():
    source = (ROOT / "app/browser/xhs.py").read_text(encoding="utf-8")
    assert "selector_group(self.selectors" in source
    assert "async_find_first_visible(page," in source
    assert "xhs.yaml" in source
    assert "playwright.sync_api" not in source
    assert "from playwright.sync_api" not in source
    tree = ast.parse(source)
    calls = [node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert "sync_playwright" not in calls


def test_fastapi_publish_routes_are_async():
    assert inspect.iscoroutinefunction(main.fill_note)
    assert inspect.iscoroutinefunction(main.final_confirm_note)
    assert inspect.iscoroutinefunction(main.retry_fill)


def test_selector_script_targets_publish_urls():
    source = (ROOT / "scripts/check_xhs_selectors.py").read_text(encoding="utf-8")
    assert "--target" in source
    assert 'default="image-upload"' in source
    assert "resolve_publish_url(settings, publish_kind)" in source
    assert "image-text-to-image" in source
    assert "--click-entry" in source
    assert "click_text_to_image_entry(page, candidates)" in source
    assert "text_input: SKIPPED" in source
    assert "already_on_text_editor_page" in source
    assert "video" in source


class FakeSelectorLocator:
    def __init__(self, page, selector: str):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.selector in self.page.found else 0

    def evaluate(self, script):
        return "div"

    def get_attribute(self, name):
        return ""

    def inner_text(self, timeout=1000):
        return self.selector


class FakeSelectorPage:
    def __init__(self, found=()):
        self.found = set(found)

    def locator(self, selector):
        return FakeSelectorLocator(self, selector)


def test_find_selector_match_optional_missing_is_not_found():
    match = selector_script.find_selector_match(FakeSelectorPage(), ["text=写文字"])
    assert not match.found


def test_text_to_image_state_missing_editor_and_next_is_entry_page():
    group = {"text_editor_page_ready": ["text=写文字"], "next_button": ["button:has-text(\"下一步\")"]}
    state, editor_match, next_match = selector_script.detect_text_to_image_state(FakeSelectorPage(), group)
    assert state == "entry_page"
    assert not editor_match.found
    assert not next_match.found


def test_text_to_image_state_editor_found():
    group = {"text_editor_page_ready": ["text=写文字"], "next_button": ["button:has-text(\"下一步\")"]}
    state, editor_match, next_match = selector_script.detect_text_to_image_state(FakeSelectorPage(["text=写文字"]), group)
    assert state == "already_on_text_editor_page"
    assert editor_match.found


def test_text_to_image_state_next_found():
    group = {"text_editor_page_ready": ["text=写文字"], "next_button": ["button:has-text(\"下一步\")"]}
    state, editor_match, next_match = selector_script.detect_text_to_image_state(FakeSelectorPage(["button:has-text(\"下一步\")"]), group)
    assert state == "generated_page"
    assert next_match.found


def test_static_click_entry_does_not_force_editor_state():
    source = (ROOT / "scripts/check_xhs_selectors.py").read_text(encoding="utf-8")
    assert 'state = "already_on_text_editor_page"' in source
    assert "if text_match.found or generate_match.found" in source
    assert "entry click failed / no state change" in source


def test_playwright_click_is_limited_to_tabs_and_final_confirm_submit_button():
    source = (ROOT / "app/browser/xhs.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    clicks = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "click"]
    assert len(clicks) <= 10
    assert 'selectors["submit_button"]' in source
    assert "async_fill_video_upload_note" in source
    assert "async_fill_image_upload_note" in source
    assert "async_fill_image_text_to_image_note" in source
    assert "click_publish" in source
