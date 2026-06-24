from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app.config import ROOT, Settings
from app.browser.vision.types import VisionObservation


def _safe_step(step: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", step).strip("-")[:80] or "vision"


async def capture_vision_observation(page, settings: Settings, step: str, *, suffix: str = "before") -> VisionObservation:
    directory = ROOT / settings.browser.get("screenshots_dir", "data/screenshots")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"vision-{datetime.now():%Y%m%d-%H%M%S-%f}-{_safe_step(step)}-{suffix}.png"
    await page.screenshot(path=str(path), full_page=False)
    viewport = page.viewport_size or {}
    title = ""
    with _suppress():
        title = await page.title()
    page_text_summary = ""
    with _suppress():
        body = page.locator("body")
        page_text_summary = await body.inner_text(timeout=1500)
    page_text_summary = re.sub(r"\s+", " ", page_text_summary or "").strip()[:800]
    return VisionObservation(
        screenshot_path=str(path),
        url=getattr(page, "url", "") or "",
        title=title or "",
        viewport_width=int(viewport.get("width") or 0),
        viewport_height=int(viewport.get("height") or 0),
        page_text_summary=page_text_summary,
        step=step,
    )


class _suppress:
    def __enter__(self):
        return None

    def __exit__(self, *_):
        return True

