def test_env_example_contains_all_provider_and_feishu_keys():
    text = open(".env.example", encoding="utf-8").read()
    expected = {"DEEPSEEK_API_KEY", "GLM_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN"}
    assert expected <= {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}


def test_config_has_safety_defaults(settings):
    assert settings.app["host"] == "127.0.0.1"
    assert settings.publish["require_review"] is True
    assert settings.browser["dry_run"] is True
    assert settings.browser["screenshots_dir"]
    assert settings.ai["default_provider"] == "mock"
    assert "openai_compatible" in settings.ai["providers"]
    assert settings.interaction["daily_like_limit"] > 0
    assert settings.interaction["sensitive_keywords"]
