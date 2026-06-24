from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.browser.xhs import detect_publish_target_from_url, resolve_publish_url, selector_group
from app.browser.vision.planner import plan_vision_action
from app.browser.vision.providers import selected_visual_model, selected_visual_provider, visual_mode_enabled, visual_provider_source
from app.browser.vision.safety import validate_vision_action
from app.browser.vision.types import VisionObservation
from app.config import ROOT, get_settings
from app.database import SessionLocal


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
    'text=文字配图',
    'span:has-text("文字配图")',
    'div:has-text("文字配图")',
    'button:has-text("文字配图")',
    '[role="button"]:has-text("文字配图")',
    '[class*="btn"]:has-text("文字配图")',
    '[class*="button"]:has-text("文字配图")',
    '[class*="card"]:has-text("文字配图")',
    '[class*="option"]:has-text("文字配图")',
    '[class*="item"]:has-text("文字配图")',
    'text=写文字生成图片',
    'button:has-text("写文字生成图片")',
    '[role="button"]:has-text("写文字生成图片")',
    'div:has-text("写文字生成图片")',
]


@dataclass
class MatchResult:
    found: bool
    candidate_index: int | None = None
    selector: str = ""
    locator: object | None = None
    details: str = ""


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
    match = find_selector_match(page, candidates)
    return match.candidate_index, match.selector, match.locator


def find_selector_match(page, selectors) -> MatchResult:
    candidates = selector_list(selectors)
    for index, candidate in enumerate(candidates, start=1):
        try:
            locator = page.locator(candidate)
            count = locator.count()
            if count:
                first = locator.first
                return MatchResult(True, index, candidate, first, describe_locator(first))
        except Exception as exc:
            last_error = str(exc).splitlines()[0][:120]
            continue
    return MatchResult(False, details=f"candidates={candidates}")


def is_upload_like_candidate(selector: str, text: str) -> bool:
    normalized_selector = (selector or "").casefold().replace(" ", "")
    normalized_text = " ".join((text or "").split())
    if normalized_text in {"文字配图", "写文字生成图片"} or selector in {"text=文字配图", "text=写文字生成图片"}:
        return False
    if any(token.casefold().replace(" ", "") in normalized_selector for token in UPLOAD_ENTRY_SELECTOR):
        return True
    return any(token in normalized_text for token in UPLOAD_ENTRY_TEXT)


def skip_reason(selector: str, text: str, box) -> str:
    clean = " ".join((text or "").split())
    if is_upload_like_candidate(selector, clean):
        return "upload_like"
    if clean not in {"文字配图", "写文字生成图片"} and len(clean) > 80:
        return "container_text_too_long"
    if box:
        width = float(box.get("width") or 0)
        height = float(box.get("height") or 0)
        if width > 900 or height > 500:
            return "container_box_too_large"
    return ""


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
    return {"selector": selector, "tag": tag, "text": text, "box": box, "visible": visible, "upload_like": is_upload_like_candidate(selector, text), "reason_skipped": skip_reason(selector, text, box)}


def text_to_image_candidates(page, selectors: list[str]) -> list[dict]:
    all_selectors = []
    for selector in selectors + TEXT_TO_IMAGE_FALLBACK_ENTRY_SELECTORS:
        if selector not in all_selectors:
            all_selectors.append(selector)
    results = []
    try:
        locator = page.get_by_text("文字配图", exact=True)
        if locator.count():
            results.append({"locator": locator.first, **locator_details(locator.first, "get_by_text:文字配图")})
    except Exception:
        pass
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
        if candidate["reason_skipped"]:
            continue
        try:
            clicked = False
            before_url = page.url
            before_text = page_text_summary(page)
            try:
                with page.expect_file_chooser(timeout=800):
                    candidate["locator"].click()
                    clicked = True
                triggered.append(candidate)
                print(f"  错误：该候选触发了文件选择器，不应作为文字配图入口。selector={candidate['selector']}; text={candidate['text'][:80]}")
                continue
            except PlaywrightTimeoutError as exc:
                if clicked:
                    if wait_for_text_editor(page):
                        print(f"  entry click: OK selector={candidate['selector']}")
                        return True
                    print(
                        f"  entry click no state change selector={candidate['selector']}; "
                        f"before_url={before_url}; after_url={page.url}; "
                        f"before_text={before_text}; after_text={page_text_summary(page)}"
                    )
                    continue
                raise exc
        except Exception as exc:
            print(f"  entry click failed selector={candidate['selector']}; error={str(exc).splitlines()[0][:120]}")
    if triggered:
        print("  文字配图入口识别失败：当前点击会打开本地文件选择器，已停止以避免误上传。")
    return False


def page_text_summary(page) -> str:
    try:
        return (page.locator("body").inner_text(timeout=1000) or "").strip().replace("\n", " ")[:180]
    except Exception:
        return ""


def wait_for_text_editor(page) -> bool:
    for selector in ['text=写文字', 'button:has-text("生成图片")', '[role="button"]:has-text("生成图片")', '[contenteditable="true"]', 'textarea']:
        try:
            locator = page.locator(selector)
            if locator.count():
                return True
        except Exception:
            continue
    return False


def detect_text_to_image_state(page, group: dict) -> tuple[str, MatchResult, MatchResult]:
    editor_match = find_selector_match(page, group.get("text_editor_page_ready", []))
    next_match = find_selector_match(page, group.get("next_button", []))
    if editor_match.found:
        return "already_on_text_editor_page", editor_match, next_match
    if next_match.found:
        return "generated_page", editor_match, next_match
    return "entry_page", editor_match, next_match


def print_selector_result(page, name: str, selectors: dict, *, required: bool = True) -> bool:
    candidates = selectors.get(name, [])
    match = find_selector_match(page, candidates)
    if match.found:
        print(f"  {name}: FOUND candidate={match.candidate_index}; selector={match.selector}; {match.details}")
        return True
    print(f"  {name}: {'NOT_FOUND' if required else 'optional missing'}; candidates={selector_list(candidates)}")
    return not required


def fill_text_card_for_test(locator, text: str) -> bool:
    try:
        locator.click()
    except Exception:
        pass
    try:
        locator.fill(text)
    except Exception:
        try:
            locator.evaluate(
                """(node, value) => {
                    if ('value' in node) node.value = value;
                    else node.innerText = value;
                    node.textContent = value;
                    node.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
                    node.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                text,
            )
        except Exception:
            try:
                locator.press("Control+A")
                locator.press("Backspace")
                locator.type(text)
            except Exception:
                return False
    prefix = text[: min(8, len(text))]
    for getter in ("input_value", "inner_text", "text_content"):
        try:
            value = getattr(locator, getter)()
            if prefix in (value or ""):
                return True
        except Exception:
            pass
    try:
        value = locator.evaluate("(node) => node.value || node.innerText || node.textContent || ''")
        return prefix in (value or "")
    except Exception:
        return False


def click_selector_match(match: MatchResult, label: str) -> bool:
    if not match.found or match.locator is None:
        print(f"  {label}: NOT_FOUND")
        return False
    try:
        match.locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        match.locator.click()
    except Exception:
        try:
            match.locator.evaluate("node => node.click()")
        except Exception as exc:
            print(f"  {label}: CLICK_FAILED selector={match.selector}; error={str(exc).splitlines()[0][:160]}")
            return False
    print(f"  {label}: CLICKED selector={match.selector}")
    return True


def run_vision_plan(page, settings, screenshot_dir: Path, step: str, goal: str):
    screenshot_path = screenshot_dir / f"vision-check-{step}-{datetime.now():%Y%m%d-%H%M%S}.png"
    page.screenshot(path=str(screenshot_path), full_page=False)
    viewport = page.viewport_size or {}
    observation = VisionObservation(
        screenshot_path=str(screenshot_path),
        url=page.url,
        title=page.title(),
        viewport_width=int(viewport.get("width") or 0),
        viewport_height=int(viewport.get("height") or 0),
        page_text_summary=page_text_summary(page),
        step=step,
    )
    with SessionLocal() as db:
        provider = selected_visual_provider(db, settings)
        provider_source = visual_provider_source(db, settings)
        model = selected_visual_model(db, settings, provider) if provider else "-"
        print(f"  visual mode: {'enabled' if visual_mode_enabled(db, settings) else 'disabled'}")
        print(f"  provider source: {provider_source}")
        print(f"  provider: {provider.display_name if provider else '未配置默认 AI Provider'}")
        print(f"  model: {model}")
        plan = plan_vision_action(db, settings, observation, goal)
    print(f"  vision step: {step}")
    print(f"  vision screenshot: {screenshot_path}")
    print(f"  vision ok: {plan.ok}")
    if plan.action:
        action = plan.action
        print(f"  vision action: type={action.type}; target={action.target_label}; x={action.x}; y={action.y}; confidence={action.confidence}; reason={action.reason}")
        validate_vision_action(observation, action, settings, mode="fill_only")
    else:
        print(f"  vision refusal: {plan.refusal_reason}")
    for index, target in enumerate(plan.targets, start=1):
        center = target.center
        print(f"  vision target {index}: label={target.label}; confidence={target.confidence}; center={center}; text={target.visible_text}; reason={target.reason}")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-page", action="store_true", help="Open XHS publish page and check selectors without filling.")
    parser.add_argument("--target", choices=sorted(TARGET_ALIASES), default="image-upload", help="Publish target URL to open when --open-page is used.")
    parser.add_argument("--click-entry", action="store_true", help="Only for image-text-to-image: safely click the text-to-image entry and then check card text input.")
    parser.add_argument("--test-flow", action="store_true", help="Only for image-text-to-image: click entry, fill test text, and check generate button without generating.")
    parser.add_argument("--click-generate", action="store_true", help="Only with --test-flow: click Generate Image after filling test text.")
    parser.add_argument("--click-next", action="store_true", help="Only with --test-flow --click-generate: click Next after generation is detected.")
    parser.add_argument("--vision-test", action="store_true", help="Only for image-text-to-image: ask the default AI provider to find the text-to-image entry without clicking.")
    parser.add_argument("--vision-click-entry", action="store_true", help="Only with --vision-test: click the vision-detected text-to-image entry.")
    parser.add_argument("--vision-test-flow", action="store_true", help="Use vision to click entry, fill test text, and find Generate Image without generating.")
    parser.add_argument("--vision-click-generate", action="store_true", help="Only with --vision-test-flow: click Generate Image by vision.")
    parser.add_argument("--vision-click-next", action="store_true", help="Only with --vision-test-flow --vision-click-generate: click Next by vision.")
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
                if args.vision_test or args.vision_test_flow:
                    try:
                        plan = run_vision_plan(page, settings, screenshot_dir, "click_text_to_image_entry", "点击页面中的【文字配图】按钮，不要点击上传图片，不要点击发布")
                        if args.vision_click_entry or args.vision_test_flow:
                            if plan.ok and plan.action:
                                page.mouse.click(plan.action.x, plan.action.y)
                                print("  vision click entry: CLICKED")
                                page.wait_for_timeout(1000)
                            else:
                                ok = False
                        if args.vision_test_flow:
                            text_plan = run_vision_plan(page, settings, screenshot_dir, "fill_text_card", "点击中间白色文字卡片的可编辑区域，不要点击上传图片，不要点击发布")
                            if text_plan.ok and text_plan.action:
                                page.mouse.click(text_plan.action.x, text_plan.action.y)
                                page.keyboard.press("Control+A")
                                page.keyboard.press("Backspace")
                                page.keyboard.insert_text("AI工具助我高效学习")
                                print("  vision fill text: OK")
                                page.wait_for_timeout(500)
                            else:
                                ok = False
                            generate_plan = run_vision_plan(page, settings, screenshot_dir, "click_generate_image", "点击【生成图片】按钮，不要点击上传图片，不要点击发布")
                            if args.vision_click_generate:
                                if generate_plan.ok and generate_plan.action:
                                    page.mouse.click(generate_plan.action.x, generate_plan.action.y)
                                    print("  vision click generate: CLICKED")
                                    page.wait_for_timeout(3000)
                                else:
                                    ok = False
                            if args.vision_click_next:
                                next_plan = run_vision_plan(page, settings, screenshot_dir, "click_next", "点击【下一步】按钮，不要点击发布")
                                if next_plan.ok and next_plan.action:
                                    page.mouse.click(next_plan.action.x, next_plan.action.y)
                                    print("  vision click next: CLICKED")
                                else:
                                    ok = False
                    except Exception as exc:
                        ok = False
                        print(f"  vision test failed: {str(exc).splitlines()[0][:300]}")
                state, editor_match, next_match = detect_text_to_image_state(page, group)
                print_selector_result(page, "text_editor_page_ready", group, required=False)
                print_selector_result(page, "next_button", group, required=False)
                print(f"  state: {state}")
                if state == "entry_page":
                    candidates = text_to_image_candidates(page, selector_list(group.get("entry", [])))
                    if candidates:
                        print("  entry candidates:")
                        for index, candidate in enumerate(candidates, start=1):
                            print(
                                f"    {index}. selector={candidate['selector']}; tag={candidate['tag']}; "
                                f"visible={candidate['visible']}; upload_like={candidate['upload_like']}; "
                                f"reason_skipped={candidate['reason_skipped']}; box={candidate['box']}; text={candidate['text'][:120]}"
                            )
                    else:
                        print(f"  entry: NOT_FOUND; candidates={selector_list(group.get('entry', []))}")
                        ok = False
                should_click_entry = args.click_entry or args.test_flow
                if should_click_entry and state == "entry_page":
                    clicked = click_text_to_image_entry(page, candidates)
                    ok = clicked and ok
                    page.wait_for_timeout(1000)
                    state, editor_match, next_match = detect_text_to_image_state(page, group)
                    if state == "entry_page":
                        text_match = find_selector_match(page, group.get("text_input", []))
                        generate_match = find_selector_match(page, group.get("generate_button", []))
                        if text_match.found or generate_match.found:
                            state = "already_on_text_editor_page"
                        else:
                            print("  entry click failed / no state change")
                    print(f"  state: {state}")
                if should_click_entry or state != "entry_page":
                    text_match = find_selector_match(page, group.get("text_input", []))
                    if text_match.found:
                        print(f"  text_input: FOUND candidate={text_match.candidate_index}; selector={text_match.selector}; {text_match.details}")
                    else:
                        print(f"  text_input: NOT_FOUND; candidates={selector_list(group.get('text_input', []))}")
                        ok = False
                    if args.test_flow and text_match.found:
                        flow_text = "AI工具助我高效学习"
                        filled = fill_text_card_for_test(text_match.locator, flow_text)
                        print(f"  test_flow fill_text: {'OK' if filled else 'FAILED'}; text={flow_text}")
                        ok = filled and ok
                        page.wait_for_timeout(500)
                    generate_match = find_selector_match(page, group.get("generate_button", []))
                    if generate_match.found:
                        print(f"  generate_button: FOUND candidate={generate_match.candidate_index}; selector={generate_match.selector}; {generate_match.details}")
                    else:
                        print(f"  generate_button: optional missing; candidates={selector_list(group.get('generate_button', []))}")
                    if args.click_generate:
                        clicked_generate = click_selector_match(generate_match, "generate_button")
                        ok = clicked_generate and ok
                        if clicked_generate:
                            page.wait_for_timeout(3000)
                            generated_match = find_selector_match(page, group.get("next_button", []) + group.get("generated_ready", []))
                            print(
                                f"  generated_result: {'FOUND' if generated_match.found else 'NOT_FOUND'}"
                                f"{'; selector=' + generated_match.selector if generated_match.found else ''}"
                            )
                            ok = generated_match.found and ok
                            if args.click_next:
                                next_match = find_selector_match(page, group.get("next_button", []))
                                ok = click_selector_match(next_match, "next_button") and ok
                        elif args.click_next:
                            print("  next_button: SKIPPED because generate click failed")
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
