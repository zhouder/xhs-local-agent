from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.browser.xhs import detect_publish_target_from_url, resolve_publish_url, selector_group
from app.config import ROOT, get_settings


REQUIRED_GROUPS = {"common", "video_upload", "image_upload", "image_text_to_image"}
TARGET_ALIASES = {
    "video": "video_upload",
    "image": "image_upload",
    "image-upload": "image_upload",
    "text2image": "image_text_to_image",
    "image-text-to-image": "image_text_to_image",
}
UPLOAD_ENTRY_TEXT = ("上传图片", "拖拽图片", "点击上传", "上传图文", "input[type=file]")
UPLOAD_ENTRY_SELECTOR = ("input[type=\"file\"]", "input[type='file']", "[class*=\"upload\"]", "[class*='upload']", "text=上传图片", "text=图片配图")
TEXT_TO_IMAGE_FALLBACK_ENTRY_SELECTORS = [
    'button:has-text("文字配图")',
    '[role="button"]:has-text("文字配图")',
    'button:has-text("写文字生成图片")',
    '[role="button"]:has-text("写文字生成图片")',
    'div:has-text("写文字生成图片") button',
    'div:has-text("写文字生成图片") [role="button"]',
    '[class*="card"]:has-text("写文字生成图片") button',
    '[class*="card"]:has-text("写文字生成图片") [role="button"]',
    'div:has-text("写文字生成图片")',
]


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


def is_upload_like_candidate(selector: str, text: str) -> bool:
    normalized_selector = (selector or "").casefold().replace(" ", "")
    normalized_text = " ".join((text or "").split())
    if any(token.casefold().replace(" ", "") in normalized_selector for token in UPLOAD_ENTRY_SELECTOR):
        return True
    return any(token in normalized_text for token in UPLOAD_ENTRY_TEXT)


def locator_details(locator, selector: str) -> dict:
    tag = ""
    text = ""
    box = None
    visible = None
    try:
        tag = locator.evaluate("node => node.tagName ? node.tagName.toLowerCase() : ''")
    except Exception:
        pass
    try:
        text = (locator.inner_text(timeout=1000) or "").strip().replace("\n", " ")[:200]
    except Exception:
        pass
    try:
        box = locator.bounding_box()
    except Exception:
        pass
    try:
        visible = locator.is_visible()
    except Exception:
        pass
    return {"selector": selector, "tag": tag, "text": text, "box": box, "visible": visible, "upload_like": is_upload_like_candidate(selector, text)}


def text_to_image_candidates(page, selectors: list[str]) -> list[dict]:
    all_selectors = []
    for selector in selectors + TEXT_TO_IMAGE_FALLBACK_ENTRY_SELECTORS:
        if selector not in all_selectors:
            all_selectors.append(selector)
    results = []
    for selector in all_selectors:
        try:
            locator = page.locator(selector)
            if locator.count():
                results.append({"locator": locator.first, **locator_details(locator.first, selector)})
        except Exception:
            continue
    return results


def click_text_to_image_entry(page, candidates: list[dict]):
    triggered = []
    for candidate in candidates:
        if candidate["upload_like"]:
            continue
        try:
            clicked = False
            try:
                with page.expect_file_chooser(timeout=800):
                    candidate["locator"].click()
                    clicked = True
                triggered.append(candidate)
                print(f"  错误：该候选触发了文件选择器，不应作为文字配图入口。selector={candidate['selector']}; text={candidate['text'][:80]}")
                continue
            except PlaywrightTimeoutError as exc:
                if clicked:
                    print(f"  entry click: OK selector={candidate['selector']}")
                    return True
                raise exc
        except Exception as exc:
            print(f"  entry click failed selector={candidate['selector']}; error={str(exc).splitlines()[0][:120]}")
    if triggered:
        print("  文字配图入口识别失败：当前点击会打开本地文件选择器，已停止以避免误上传。")
    return False


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
    parser.add_argument("--target", choices=sorted(TARGET_ALIASES), default="image-upload", help="Publish target URL to open when --open-page is used.")
    parser.add_argument("--click-entry", action="store_true", help="Only for image-text-to-image: safely click the text-to-image entry and then check card text input.")
    args = parser.parse_args()
    publish_kind = TARGET_ALIASES[args.target]
    selector_path = ROOT / "app/browser/selectors/xhs.yaml"
    selectors = yaml.safe_load(selector_path.read_text(encoding="utf-8"))["publish"]
    missing = sorted(REQUIRED_GROUPS - set(selectors))
    if missing:
        print("ERROR: missing selector groups: " + ", ".join(missing))
        return 1
    print("Selector file: OK")
    for group_name in sorted(selectors):
        print(f"[{group_name}]")
        value = selectors[group_name]
        if isinstance(value, dict):
            for key in sorted(value):
                print(f"  {key}: {selector_list(value[key])}")
        else:
            print(f"  {selector_list(value)}")
    if not args.open_page:
        return 0

    settings = get_settings()
    channel = settings.browser.get("channel", "chrome")
    profile_dir = ROOT / settings.browser.get("profile_dir", f"data/browser-profiles/{channel}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir = ROOT / settings.browser.get("screenshots_dir", "data/screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / f"selector-check-{args.target}-{datetime.now():%Y%m%d-%H%M%S}.png"
    requested_url = resolve_publish_url(settings, publish_kind)

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
            print(f"publish kind: {publish_kind}")
            print(f"requested URL: {requested_url}")
            print(f"actual URL: {page.url}")
            print(f"detected target: {detect_publish_target_from_url(page.url)}")
            with contextlib_suppress():
                print(f"page title: {page.title()}")

            ok = True
            group = selector_group(selectors, publish_kind)
            if publish_kind == "video_upload":
                print("\n[video_upload]")
                for name in ["page_ready", "file_input", "upload_area"]:
                    ok = print_selector_result(page, name, group, required=True) and ok
            elif publish_kind == "image_upload":
                print("\n[image_upload]")
                for name in ["page_ready", "file_input", "upload_area"]:
                    ok = print_selector_result(page, name, group, required=True) and ok
            else:
                print("\n[image_text_to_image]")
                on_editor = print_selector_result(page, "text_editor_page_ready", group, required=False)
                on_generated = print_selector_result(page, "next_button", group, required=False)
                if on_editor and first_visible(page, selector_list(group.get("text_editor_page_ready", [])))[2]:
                    state = "already_on_text_editor_page"
                elif on_generated and first_visible(page, selector_list(group.get("next_button", [])))[2]:
                    state = "generated_page"
                else:
                    state = "entry_page"
                print(f"  state: {state}")
                if state == "entry_page":
                    candidates = text_to_image_candidates(page, selector_list(group.get("entry", [])))
                    if candidates:
                        print("  entry candidates:")
                        for index, candidate in enumerate(candidates, start=1):
                            print(
                                f"    {index}. selector={candidate['selector']}; tag={candidate['tag']}; "
                                f"visible={candidate['visible']}; upload_like={candidate['upload_like']}; "
                                f"box={candidate['box']}; text={candidate['text'][:120]}"
                            )
                    else:
                        print(f"  entry: NOT_FOUND; candidates={selector_list(group.get('entry', []))}")
                        ok = False
                if args.click_entry and state == "entry_page":
                    ok = click_text_to_image_entry(page, candidates) and ok
                    page.wait_for_timeout(1000)
                    state = "already_on_text_editor_page"
                    print(f"  state: {state}")
                if args.click_entry or state != "entry_page":
                    ok = print_selector_result(page, "text_input", group, required=True) and ok
                    ok = print_selector_result(page, "generate_button", group, required=False) and ok
                    ok = print_selector_result(page, "next_button", group, required=False) and ok
                else:
                    print("  text_input: SKIPPED；加 --click-entry 后才点击入口并检查。")

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
