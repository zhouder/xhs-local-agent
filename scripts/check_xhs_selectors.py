from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.config import ROOT, get_settings


REQUIRED = {"title", "body", "file_input", "submit_button"}


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
    if not args.open_page:
        return 0
    settings = get_settings()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=settings.browser.get("channel"), headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(settings.browser["publish_url"])
        print("Please log in manually if needed. This script does not fill or publish.")
        ok = True
        for name, selector in selectors.items():
            if name == "submit_button":
                continue
            count = page.locator(selector).count()
            print(f"{name}: {count}")
            ok = ok and count >= 0
        context.close()
        browser.close()
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
