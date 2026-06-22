import json

from app.ai.mock import MockProvider
from app.repositories import AuditRepository, NoteRepository
from app.schemas import GenerateNoteRequest


def test_note_repository_round_trip(db):
    request = GenerateNoteRequest(topic="编程", style="简洁", audience="初学者")
    content = MockProvider().generate_note(request.topic, request.style, request.audience)
    repo = NoteRepository(db)
    note = repo.create(request, content)
    loaded = repo.get(note.id)
    assert loaded.title == content.title
    assert json.loads(loaded.hashtags_json) == content.hashtags


def test_audit_repository_serializes_metadata(db):
    row = AuditRepository(db).record("test.action", "success", metadata={"value": "中文"})
    assert json.loads(row.metadata_json)["value"] == "中文"
