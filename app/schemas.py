from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SafetyResult(StrictModel):
    is_safe: bool
    reason: str = ""
    matched_keywords: list[str] = Field(default_factory=list)


class MediaRequirements(StrictModel):
    type: Literal["image", "video"] = "image"
    count: int = Field(default=1, ge=1, le=20)
    description: str


class NoteContent(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1)
    hashtags: list[str] = Field(default_factory=list)
    cover_prompt: str
    media_requirements: MediaRequirements
    safety: SafetyResult


class GenerateNoteRequest(StrictModel):
    topic: str = Field(min_length=1, max_length=300)
    style: str = Field(default="实用、自然", max_length=100)
    audience: str = Field(default="科技爱好者", max_length=100)
    min_length: int = Field(default=200, ge=80, le=2000)
    max_length: int = Field(default=600, ge=100, le=3000)
    controversial_title: bool = False
    educational: bool = False
    growth_oriented: bool = True

    @model_validator(mode="after")
    def validate_length_range(self):
        if self.min_length > self.max_length:
            raise ValueError("min_length must not exceed max_length")
        return self


class NoteUpdate(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1)
    hashtags: list[str] = Field(default_factory=list)
    cover_prompt: str = ""
    media_path: str = ""
