from app.ai.mock import MockProvider
from app.schemas import GenerateNoteRequest


def test_mock_generation_is_deterministic_and_valid():
    provider = MockProvider(["政治"])
    request = GenerateNoteRequest(topic="编程效率", style="自然", audience="开发者")
    outputs = [provider.generate_note(request) for _ in range(50)]
    assert len({item.model_dump_json() for item in outputs}) == 1
    assert all(item.safety.is_safe for item in outputs)
