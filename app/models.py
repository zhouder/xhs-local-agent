from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base, utcnow


class NoteStatus(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    WAITING_FINAL_CONFIRM = "waiting_final_confirm"
    PUBLISHED = "published"
    PUBLISH_UNCERTAIN = "publish_uncertain"
    FAILED = "failed"
    REJECTED = "rejected"
    RETURNED_TO_EDIT = "returned_to_edit"
    CANCELLED = "cancelled"


class TimestampMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class AIProvider(TimestampMixin, Base):
    __tablename__ = "ai_providers"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    provider_type: Mapped[str] = mapped_column(String(50), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    # Kept for migration compatibility with the phase-one table.
    model: Mapped[str] = mapped_column(String(120))
    model_id: Mapped[str] = mapped_column(String(200), default="")
    default_model_id: Mapped[str] = mapped_column(String(200), default="")
    api_key_env: Mapped[str] = mapped_column(String(120), default="")
    api_key_configured_status: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_json_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_streaming: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=False)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature_default: Mapped[str] = mapped_column(String(20), default="0.6")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    auth_scheme: Mapped[str] = mapped_column(String(20), default="auto")
    extra_headers_json: Mapped[str] = mapped_column(Text, default="{}")
    extra_body_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str] = mapped_column(Text, default="")


class ProviderModel(TimestampMixin, Base):
    __tablename__ = "provider_models"
    provider_id: Mapped[int] = mapped_column(ForeignKey("ai_providers.id"), index=True)
    model_id: Mapped[str] = mapped_column(String(300))
    display_name: Mapped[str] = mapped_column(String(300), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Note(TimestampMixin, Base):
    __tablename__ = "notes"
    topic: Mapped[str] = mapped_column(String(300))
    style: Mapped[str] = mapped_column(String(100), default="实用、自然")
    audience: Mapped[str] = mapped_column(String(100), default="科技爱好者")
    min_length: Mapped[int] = mapped_column(Integer, default=200)
    max_length: Mapped[int] = mapped_column(Integer, default=600)
    controversial_title: Mapped[bool] = mapped_column(Boolean, default=False)
    educational: Mapped[bool] = mapped_column(Boolean, default=False)
    growth_oriented: Mapped[bool] = mapped_column(Boolean, default=True)
    title: Mapped[str] = mapped_column(String(100))
    body: Mapped[str] = mapped_column(Text)
    hashtags_json: Mapped[str] = mapped_column(Text, default="[]")
    cover_prompt: Mapped[str] = mapped_column(Text, default="")
    media_requirements_json: Mapped[str] = mapped_column(Text, default="{}")
    safety_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(30), default=NoteStatus.DRAFT)
    publish_mode: Mapped[str] = mapped_column(String(40), default="dry_run")
    publish_screenshot_path: Mapped[str] = mapped_column(String(1000), default="")
    publish_error_message: Mapped[str] = mapped_column(Text, default="")
    content_plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    approved_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)

    @validates("status")
    def validate_status(self, _key: str, value: str) -> str:
        return NoteStatus(value).value


class MediaAsset(TimestampMixin, Base):
    __tablename__ = "media_assets"
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    path: Mapped[str] = mapped_column(String(1000))
    media_type: Mapped[str] = mapped_column(String(30), default="image")
    asset_type: Mapped[str] = mapped_column(String(30), default="image")
    file_path: Mapped[str] = mapped_column(String(1000), default="")
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    upload_order: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    error_message: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(40), default="upload")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    license_note: Mapped[str] = mapped_column(Text, default="")


class Comment(TimestampMixin, Base):
    __tablename__ = "comments"
    external_id: Mapped[str] = mapped_column(String(200), unique=True)
    author: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text)
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="new")
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)


class Message(TimestampMixin, Base):
    __tablename__ = "messages"
    external_id: Mapped[str] = mapped_column(String(200), unique=True)
    sender: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text)
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="new")
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)


class Interaction(TimestampMixin, Base):
    __tablename__ = "interactions"
    action_type: Mapped[str] = mapped_column(String(40), index=True)
    external_target_id: Mapped[str] = mapped_column(String(200), index=True)
    target_text: Mapped[str] = mapped_column(Text, default="")
    response_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30))


class ReviewQueue(TimestampMixin, Base):
    __tablename__ = "review_queue"
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    reviewer: Mapped[str] = mapped_column(String(100), default="local_user")
    decision_reason: Mapped[str] = mapped_column(Text, default="")


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"
    action_type: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(80), default="")
    target_id: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), index=True)
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    screenshot_path: Mapped[str] = mapped_column(String(1000), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class Setting(TimestampMixin, Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(200), unique=True)
    value_json: Mapped[str] = mapped_column(Text)


class BrowserError(TimestampMixin, Base):
    __tablename__ = "browser_errors"
    note_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(40), default="")
    step: Mapped[str] = mapped_column(String(80), default="")
    selector_name: Mapped[str] = mapped_column(String(100), default="")
    action_type: Mapped[str] = mapped_column(String(100))
    error_message: Mapped[str] = mapped_column(Text)
    screenshot_path: Mapped[str] = mapped_column(String(1000), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class CommandEvent(TimestampMixin, Base):
    __tablename__ = "command_events"
    channel: Mapped[str] = mapped_column(String(50))
    command: Mapped[str] = mapped_column(String(100))
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(30))
    response: Mapped[str] = mapped_column(Text, default="")


class ScheduledJob(TimestampMixin, Base):
    __tablename__ = "scheduled_jobs"
    name: Mapped[str] = mapped_column(String(200), unique=True)
    job_type: Mapped[str] = mapped_column(String(80))
    schedule_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_status: Mapped[str] = mapped_column(String(30), default="never_run")


class ContentPlan(TimestampMixin, Base):
    __tablename__ = "content_plans"
    name: Mapped[str] = mapped_column(String(200))
    audience: Mapped[str] = mapped_column(String(200), default="")
    style: Mapped[str] = mapped_column(String(100), default="")
    goal: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), default="active")
    notes_json: Mapped[str] = mapped_column(Text, default="{}")


class ContentPlanTopic(TimestampMixin, Base):
    __tablename__ = "content_plan_topics"
    plan_id: Mapped[int] = mapped_column(ForeignKey("content_plans.id"), index=True)
    topic: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    note_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    error_message: Mapped[str] = mapped_column(Text, default="")


class ScheduledPublishSlot(TimestampMixin, Base):
    __tablename__ = "scheduled_publish_slots"
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    planned_time: Mapped[object] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="planned")
