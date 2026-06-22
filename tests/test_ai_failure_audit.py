from sqlalchemy import select

from app.ai.base import AIProviderAdapter
from app.models import AuditLog
from app.schemas import GenerateNoteRequest
from app.services.notes import NoteService


class FailingProvider(AIProviderAdapter):
    def generate_note(self, topic, style="", audience="", **options):
        raise RuntimeError("provider unavailable")

    def generate_reply(self, message, context=""):
        raise RuntimeError

    def classify_safety(self, text):
        raise RuntimeError

    def generate_cover_prompt(self, note):
        raise RuntimeError


def test_ai_failure_is_audited_without_secret(db, monkeypatch):
    secret = "sk-super-secret-value"
    monkeypatch.setenv("TEST_API_KEY", secret)
    monkeypatch.setattr(FailingProvider, "generate_note", lambda self, *args, **kwargs: (_ for _ in ()).throw(RuntimeError(f"provider unavailable {secret}")))
    service = NoteService(db, FailingProvider())
    try:
        service.generate(GenerateNoteRequest(topic="AI"))
    except RuntimeError:
        pass
    row = db.scalar(select(AuditLog).where(AuditLog.action_type == "ai.generate_note"))
    assert row.status == "failed"
    assert "provider unavailable" in row.error_message
    assert secret not in row.error_message
    assert "[REDACTED]" in row.error_message
