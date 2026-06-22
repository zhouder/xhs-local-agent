from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict


ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")
    app: dict[str, Any]
    browser: dict[str, Any]
    ai: dict[str, Any]
    publish: dict[str, Any]
    interaction: dict[str, Any]
    notifications: dict[str, Any]
    feishu: dict[str, Any]

    @property
    def database_url(self) -> str:
        url = str(self.app.get("database_url", "sqlite:///data/xhs_agent.db"))
        if url.startswith("sqlite:///./"):
            return f"sqlite:///{(ROOT / url[12:]).as_posix()}"
        if url.startswith("sqlite:///data/"):
            return f"sqlite:///{(ROOT / url[10:]).as_posix()}"
        return url

    def provider_api_key(self, provider: str) -> str:
        env_name = self.ai["providers"][provider].get("api_key_env", "")
        return os.getenv(env_name, "") if env_name else ""


@lru_cache
def get_settings(config_path: str | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = Path(config_path) if config_path else ROOT / "config.yaml"
    with path.open("r", encoding="utf-8") as stream:
        return Settings.model_validate(yaml.safe_load(stream))
