from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    database_url = url or get_settings().database_url
    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)
    if engine.url.get_backend_name() == "sqlite":
        columns = {column["name"] for column in inspect(engine).get_columns("notes")}
        additions = {
            "min_length": "INTEGER NOT NULL DEFAULT 200",
            "max_length": "INTEGER NOT NULL DEFAULT 600",
            "controversial_title": "BOOLEAN NOT NULL DEFAULT 0",
            "educational": "BOOLEAN NOT NULL DEFAULT 0",
            "growth_oriented": "BOOLEAN NOT NULL DEFAULT 1",
            "publish_mode": "VARCHAR(40) NOT NULL DEFAULT 'dry_run'",
            "publish_screenshot_path": "VARCHAR(1000) NOT NULL DEFAULT ''",
            "publish_error_message": "TEXT NOT NULL DEFAULT ''",
            "content_plan_id": "INTEGER",
        }
        with engine.begin() as connection:
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE notes ADD COLUMN {name} {definition}"))
        _migrate_media_assets(engine)
        _migrate_browser_errors(engine)
        _migrate_ai_providers(engine)
    from app.services.provider_registry import ProviderRegistry

    with SessionLocal() as db:
        ProviderRegistry(db, get_settings()).initialize()


def _migrate_ai_providers(target_engine=None) -> None:
    target_engine = target_engine or engine
    columns = {column["name"] for column in inspect(target_engine).get_columns("ai_providers")}
    additions = {
        "display_name": "VARCHAR(120) NOT NULL DEFAULT ''",
        "provider_type": "VARCHAR(50) NOT NULL DEFAULT 'openai_compatible'",
        "model_id": "VARCHAR(200) NOT NULL DEFAULT ''",
        "default_model_id": "VARCHAR(200) NOT NULL DEFAULT ''",
        "api_key_configured_status": "BOOLEAN NOT NULL DEFAULT 0",
        "is_default": "BOOLEAN NOT NULL DEFAULT 0",
        "supports_json_mode": "BOOLEAN NOT NULL DEFAULT 1",
        "supports_streaming": "BOOLEAN NOT NULL DEFAULT 0",
        "supports_vision": "BOOLEAN NOT NULL DEFAULT 0",
        "supports_tools": "BOOLEAN NOT NULL DEFAULT 0",
        "max_input_tokens": "INTEGER",
        "max_output_tokens": "INTEGER",
        "temperature_default": "VARCHAR(20) NOT NULL DEFAULT '0.6'",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 60",
        "auth_scheme": "VARCHAR(20) NOT NULL DEFAULT 'auto'",
        "extra_headers_json": "TEXT NOT NULL DEFAULT '{}'",
        "extra_body_json": "TEXT NOT NULL DEFAULT '{}'",
        "notes": "TEXT NOT NULL DEFAULT ''",
    }
    with target_engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE ai_providers ADD COLUMN {name} {definition}"))
        connection.execute(text("UPDATE ai_providers SET model_id = model WHERE model_id = '' OR model_id IS NULL"))
        connection.execute(text("UPDATE ai_providers SET default_model_id = model_id WHERE default_model_id = '' OR default_model_id IS NULL"))
        connection.execute(text("UPDATE ai_providers SET display_name = name WHERE display_name = '' OR display_name IS NULL"))
        connection.execute(text("UPDATE ai_providers SET provider_type = 'mock' WHERE name = 'mock'"))


def _migrate_media_assets(target_engine=None) -> None:
    target_engine = target_engine or engine
    columns = {column["name"] for column in inspect(target_engine).get_columns("media_assets")}
    additions = {
        "asset_type": "VARCHAR(30) NOT NULL DEFAULT 'image'",
        "file_path": "VARCHAR(1000) NOT NULL DEFAULT ''",
        "mime_type": "VARCHAR(100) NOT NULL DEFAULT ''",
        "upload_order": "INTEGER NOT NULL DEFAULT 1",
        "status": "VARCHAR(30) NOT NULL DEFAULT 'ready'",
        "error_message": "TEXT NOT NULL DEFAULT ''",
        "source_type": "VARCHAR(40) NOT NULL DEFAULT 'upload'",
        "source_url": "VARCHAR(1000) NOT NULL DEFAULT ''",
        "license_note": "TEXT NOT NULL DEFAULT ''",
    }
    with target_engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE media_assets ADD COLUMN {name} {definition}"))
        connection.execute(text("UPDATE media_assets SET file_path = path WHERE file_path = '' OR file_path IS NULL"))
        connection.execute(text("UPDATE media_assets SET asset_type = media_type WHERE asset_type = '' OR asset_type IS NULL"))


def _migrate_browser_errors(target_engine=None) -> None:
    target_engine = target_engine or engine
    columns = {column["name"] for column in inspect(target_engine).get_columns("browser_errors")}
    additions = {
        "note_id": "INTEGER",
        "mode": "VARCHAR(40) NOT NULL DEFAULT ''",
        "step": "VARCHAR(80) NOT NULL DEFAULT ''",
        "selector_name": "VARCHAR(100) NOT NULL DEFAULT ''",
    }
    with target_engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE browser_errors ADD COLUMN {name} {definition}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
