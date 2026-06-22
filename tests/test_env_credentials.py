import os
import pytest

from app.security import generate_api_key_env, validate_api_key, write_api_key


def test_generated_environment_names_are_user_friendly():
    assert generate_api_key_env("DeepSeek") == "DEEPSEEK_API_KEY"
    assert generate_api_key_env("通义千问 Qwen") == "QWEN_API_KEY"
    assert generate_api_key_env("Kimi") == "KIMI_API_KEY"
    assert generate_api_key_env("My Provider") == "MY_PROVIDER_API_KEY"


def test_api_key_write_preserves_other_values_and_creates_backup(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OTHER_VALUE=keep-me\nOPENAI_API_KEY=old-secret\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    write_api_key("OPENAI_API_KEY", "sk-new-secret-value", path)
    assert "OTHER_VALUE=keep-me" in path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-new-secret-value" in path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=old-secret" in (tmp_path / ".env.bak").read_text(encoding="utf-8")
    assert os.getenv("OPENAI_API_KEY") == "sk-new-secret-value"


def test_env_file_is_created_when_missing(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.delenv("NEW_API_KEY", raising=False)
    write_api_key("NEW_API_KEY", "valid-new-secret", path)
    assert path.exists()
    assert "NEW_API_KEY=valid-new-secret" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["short", "contains space", "line\nbreak"])
def test_suspicious_api_key_is_rejected(value):
    with pytest.raises(ValueError, match="格式疑似错误"):
        validate_api_key(value)
