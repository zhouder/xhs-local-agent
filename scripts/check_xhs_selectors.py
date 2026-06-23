from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.browser.xhs import detect_publish_target_from_url, resolve_publish_url
from app.config import ROOT, get_settings


REQUIRED = {
    "image_page_ready",
    "article_page_ready",
    "video_page_ready",
    "image_upload_area",
    "title",
    "body",
    "file_input",
    "submit_button",
}


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


def first_visible(page, candidates: list[str]):
    for index, candidate in enumerate(candidates, start=1):
        locator = page.locator(candidate)
        count = locator.count()
        if count:
            return index, candidate, locator.first
    return None, "", None


def print_selector_result(page, name: str, selectors: dict, *, required: bool = True) -> bool:
    candidates = selector_list(selectors.get(name, []))
    index, candidate, locator = first_visible(page, candidates)
    if locator:
        print(f"  {name}: FOUND candidate={index}; selector={candidate}; {describe_locator(locator)}")
        return True
    print(f"  {name}: {'NOT_FOUND' if required else 'optional missing'}; candidates={candidates}")
    return not required


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-page", action="store_true", help="Open XHS publish page and check selectors without filling.")
    parser.add_argument("--target", choices=["image", "article", "video"], default="image", help="Publish target URL to open when --open-page is used.")
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
    screenshot_path = screenshot_dir / f"selector-check-{args.target}-{datetime.now():%Y%m%d-%H%M%S}.png"
    requested_url = resolve_publish_url(settings, args.target)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(profile_dir), channel=channel, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(requested_url)
            print("请在打开的浏览器中手动登录小红书。本脚本不会填写内容、不会发布、不会读取 cookie。")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            if "/login" in (page.url or "") or "login?" in (page.url or ""):
                print("当前仍在登录页。请登录后重新运行 --open-page，或登录后让脚本继续检查。")
                page.wait_for_timeout(3000)
                page.goto(requested_url)
            print(f"\nrequested target: {args.target}")
            print(f"requested URL: {requested_url}")
            print(f"actual URL: {page.url}")
            print(f"detected target: {detect_publish_target_from_url(page.url)}")
            with contextlib_suppress():
                print(f"page title: {page.title()}")

            ok = True
            if args.target == "image":
                print("\n[image target]")
                for name in ["image_page_ready", "image_upload_area", "file_input", "title", "body"]:
                    ok = print_selector_result(page, name, selectors, required=name in {"image_page_ready", "image_upload_area", "file_input"}) and ok
                ok = print_selector_result(page, "topic_input", selectors, required=False) and ok
            elif args.target == "article":
                print("\n[article target]")
                for name in ["article_page_ready", "title", "body"]:
                    ok = print_selector_result(page, name, selectors) and ok
                ok = print_selector_result(page, "topic_input", selectors, required=False) and ok
            else:
                print("\n[video target]")
                ok = print_selector_result(page, "video_page_ready", selectors) and ok

            print("\n[tab diagnostics only]")
            for name in ["tab_upload_video", "tab_upload_image", "tab_long_text"]:
                print_selector_result(page, name, selectors, required=False)

            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"\nDiagnostic screenshot: {screenshot_path}")
            if not settings.browser.get("keep_open_on_error", True):
                context.close()
            return 0 if ok else 2
    except Exception as exc:
        print(f"选择器诊断失败：{str(exc).splitlines()[0][:300]}")
        return 1


class contextlib_suppress:
    def __enter__(self):
        return None

    def __exit__(self, *_):
        return True


if __name__ == "__main__":
    raise SystemExit(main())
