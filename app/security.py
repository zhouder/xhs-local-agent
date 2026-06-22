from __future__ import annotations

import os
import re
import shutil
import tempfile
from hashlib import sha1
from pathlib import Path
from typing import Any


def known_secrets() -> list[str]:
    markers = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    return [value for name, value in os.environ.items() if any(marker in name.upper() for marker in markers) and len(value) >= 8]


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value) if value is not None else ""
    for secret in known_secrets():
        text = text.replace(secret, "[REDACTED]")
    return text


KNOWN_PROVIDER_ENV_NAMES = {
    "deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY",
    "通义": "QWEN_API_KEY", "qwen": "QWEN_API_KEY", "dashscope": "QWEN_API_KEY",
    "kimi": "KIMI_API_KEY", "moonshot": "KIMI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY", "硅基": "SILICONFLOW_API_KEY", "siliconflow": "SILICONFLOW_API_KEY",
    "glm": "GLM_API_KEY", "智谱": "GLM_API_KEY", "claude": "ANTHROPIC_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "豆包": "ARK_API_KEY", "doubao": "ARK_API_KEY",
}


def generate_api_key_env(display_name: str) -> str:
    folded = display_name.casefold()
    for marker, env_name in KNOWN_PROVIDER_ENV_NAMES.items():
        if marker in folded:
            return env_name
    ascii_name = re.sub(r"[^A-Z0-9]+", "_", display_name.upper()).strip("_")
    if not ascii_name:
        ascii_name = f"PROVIDER_{sha1(display_name.encode('utf-8')).hexdigest()[:8].upper()}"
    return f"{ascii_name}_API_KEY"


def validate_api_key(api_key: str) -> None:
    if len(api_key) < 8 or any(character.isspace() for character in api_key):
        raise ValueError("API Key 格式疑似错误，请检查长度并确保不包含空格或换行")


def write_api_key(env_name: str, api_key: str, env_path: Path) -> None:
    validate_api_key(api_key)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = env_path.with_name(env_path.name + ".bak")
    if env_path.exists():
        shutil.copy2(env_path, backup_path)
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    replacement = f"{env_name}={api_key}"
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{env_name}=") and not line.lstrip().startswith("#"):
            updated.append(replacement)
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env-", dir=env_path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
        temporary.replace(env_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    os.environ[env_name] = api_key
