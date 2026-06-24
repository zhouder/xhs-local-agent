from __future__ import annotations

from app.browser.vision.providers import create_vision_provider
from app.browser.vision.safety import vision_forbidden_click_texts
from app.browser.vision.types import VisionObservation, VisionPlanResult
from app.config import Settings


def plan_vision_action(db, settings: Settings, observation: VisionObservation, goal: str) -> VisionPlanResult:
    provider = create_vision_provider(db, settings)
    return provider.plan(observation, goal, vision_forbidden_click_texts(settings))

