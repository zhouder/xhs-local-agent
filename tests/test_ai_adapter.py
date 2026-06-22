import pytest

from app.ai.factory import create_provider
from app.ai.mock import MockProvider
from app.ai.deepseek import DeepSeekProvider
from app.ai.glm import GLMProvider


def test_factory_returns_mock(settings):
    settings.ai["default_provider"] = "mock"
    assert isinstance(create_provider(settings), MockProvider)


def test_real_provider_requires_local_api_key(settings, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        create_provider(settings, "deepseek")


def test_factory_returns_named_real_providers(settings, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-secret")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-secret")
    assert isinstance(create_provider(settings, "deepseek"), DeepSeekProvider)
    assert isinstance(create_provider(settings, "glm"), GLMProvider)
