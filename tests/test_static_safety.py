import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_no_cookie_or_persistent_profile_api_usage():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.py"))
    forbidden = ["storage_state", "user_data_dir", ".cookies(", "add_cookies("]
    assert not [token for token in forbidden if token in source]


def test_browser_locator_calls_use_central_selector_map():
    tree = ast.parse((ROOT / "app/browser/xhs.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"locator", "wait_for_selector"}]
    assert calls
    assert all(isinstance(call.args[0], ast.Subscript) for call in calls)


def test_phase_one_has_no_playwright_click_calls():
    tree = ast.parse((ROOT / "app/browser/xhs.py").read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "click"]
