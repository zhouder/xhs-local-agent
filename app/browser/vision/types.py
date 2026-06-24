from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class BBox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    width: float
    height: float


class VisionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    screenshot_path: str
    url: str = ""
    title: str = ""
    viewport_width: int = 0
    viewport_height: int = 0
    page_text_summary: str = ""
    step: str


class VisionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    bbox: BBox | None = None
    center: Point | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = ""
    visible_text: str = ""


class VisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["click", "type_text", "press", "wait", "scroll"]
    target_label: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = ""
    visible_text: str = ""


class VisionPlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    action: VisionAction | None = None
    targets: list[VisionTarget] = Field(default_factory=list)
    refusal_reason: str | None = None


class VisionActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: VisionAction
    before_screenshot_path: str
    after_screenshot_path: str
    before_url: str
    after_url: str

