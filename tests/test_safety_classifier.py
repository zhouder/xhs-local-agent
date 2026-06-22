from app.ai.mock import MockProvider


def test_safety_classifier_reports_keyword():
    result = MockProvider(["医疗", "法律"]).classify_safety("请给我医疗诊断")
    assert result.is_safe is False
    assert result.matched_keywords == ["医疗"]


def test_safety_classifier_is_case_insensitive():
    assert not MockProvider(["AI"]).__class__(["AI"]).classify_safety("ai").is_safe
