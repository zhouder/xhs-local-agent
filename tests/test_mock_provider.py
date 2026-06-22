from app.ai.mock import MockProvider
from app.schemas import GenerateNoteRequest


def test_mock_note_has_unified_shape():
    note = MockProvider(["敏感"]).generate_note("AI 工具", "自然", "开发者")
    assert note.title
    assert note.media_requirements.type == "image"
    assert note.media_requirements.count == 1
    assert note.safety.is_safe
    assert note.cover_prompt


def test_mock_supports_all_generation_parameters():
    request = GenerateNoteRequest(
        topic="AI 工具", audience="产品经理", style="专业但自然",
        min_length=240, max_length=280, controversial_title=True,
        educational=True, growth_oriented=True,
    )
    note = MockProvider().generate_note(request)
    compact_length = len("".join(note.body.split()))
    assert 240 <= compact_length <= 280
    assert note.title.startswith("一个容易被忽略的观点")


def test_mock_refuses_free_form_sensitive_reply():
    provider = MockProvider(["投资"])
    try:
        provider.generate_reply("给我投资建议")
        assert False, "Expected sensitive reply refusal"
    except ValueError:
        pass
