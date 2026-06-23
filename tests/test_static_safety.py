import ast
import inspect
from pathlib import Path

from app import main


ROOT = Path(__file__).resolve().parents[1]


def test_no_cookie_or_persistent_profile_api_usage():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.py"))
    forbidden = ["storage_state", ".cookies(", "add_cookies("]
    assert not [token for token in forbidden if token in source]


def test_browser_locator_calls_use_central_selector_map():
    source = (ROOT / "app/browser/xhs.py").read_text(encoding="utf-8")
    assert "async_find_first_visible(page, self.selectors[" in source
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
    assert 'default="image"' in source
    assert "resolve_publish_url(settings, args.target)" in source


def test_playwright_click_is_limited_to_tabs_and_final_confirm_submit_button():
    source = (ROOT / "app/browser/xhs.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    clicks = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "click"]
    assert len(clicks) == 4
    assert 'self.selectors["submit_button"]' in source
    assert 'selectors.get("tab_upload_image"' in source
    assert 'selectors.get("tab_long_text"' in source
    assert "click_publish" in source
