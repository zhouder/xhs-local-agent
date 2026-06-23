from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.config import ROOT, get_settings


REQUIRED = {"title", "body", "file_input", "submit_button"}


def selector_list(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value or []]


def describe_locator(locator) -> str:
    try:
        element = locator.first
        tag = element.evaluate("node => node.tagName.toLowerCase()")
        placeholder = element.get_attribute("placeholder") or ""
        text = (element.inner_text(timeout=1000) or "").strip().replace("\n", " ")[:80]
        return f"tag={tag}; placeholder={placeholder[:80]}; text={text}"
    except Exception as exc:
        return f"summary_error={str(exc).splitlines()[0][:120]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-page", action="store_true", help="Open XHS publish page and check selectors without filling.")
    args = parser.parse_args()
    selector_path = ROOT / "app/browser/selectors/xhs.yaml"
    selectors = yaml.safe_load(selector_path.read_text(encoding="utf-8"))["publish"]
    missing = sorted(REQUIRED - set(selectors))
    if missing:
        print("ERROR: missing selector keys: " + ", ".join(missing))
        return 1
    print("Selector file: OK")
    for key in sorted(selectors):
        print(f"{key}: {selector_list(selectors[key])}")
    if not args.open_page:
        return 0

    settings = get_settings()
    channel = settings.browser.get("channel", "chrome")
    profile_dir = ROOT / settings.browser.get("profile_dir", f"data/browser-profiles/{channel}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir = ROOT / settings.browser.get("screenshots_dir", "data/screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / f"selector-check-{datetime.now():%Y%m%d-%H%M%S}.png"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), channel=channel, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(settings.browser["publish_url"])
        print("请在打开的浏览器中手动登录小红书。本脚本不会填写内容、不会发布、不会读取 cookie。")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        if "/login" in (page.url or "") or "login?" in (page.url or ""):
            print("当前仍在登录页。请登录后重新运行 --open-page，或在页面中进入发布页后再检查。")
        else:
            page.goto(settings.browser["publish_url"])
        ok = True
        for name, selector in selectors.items():
            candidates = selector_list(selector)
            found = False
            print(f"\n[{name}]")
            for index, candidate in enumerate(candidates, start=1):
                locator = page.locator(candidate)
                count = locator.count()
                if count:
                    found = True
                    print(f"  {index}. FOUND count={count}; selector={candidate}; {describe_locator(locator)}")
                    break
                print(f"  {index}. miss; selector={candidate}")
            if not found:
                ok = False
                print(f"  result=NOT_FOUND; candidates={candidates}")
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"\nDiagnostic screenshot: {screenshot_path}")
        if not settings.browser.get("keep_open_on_error", True):
            context.close()
        return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
