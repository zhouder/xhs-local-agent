import pytest

from app.ai.mock import MockProvider
from app.models import NoteStatus
from app.repositories import NoteRepository
from app.schemas import GenerateNoteRequest
from app.services.notifications import Notifier
from app.services.policy import PolicyEngine
from app.services.review import ReviewService


class CapturingNotifier(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, title: str, message: str) -> None:
        self.messages.append((title, message))


def make_note(db):
    req = GenerateNoteRequest(topic="AI", style="自然", audience="开发者")
    return NoteRepository(db).create(req, MockProvider().generate_note(req.topic, req.style, req.audience))


def test_review_flow_requires_pending_state(db, settings):
    note = make_note(db)
    notifier = CapturingNotifier()
    service = ReviewService(db, notifier)
    with pytest.raises(ValueError):
        service.approve(note.id)
    service.submit(note.id)
    assert note.status == NoteStatus.PENDING_REVIEW
    assert str(note.id) in notifier.messages[0][1]
    service.approve(note.id)
    assert note.status == NoteStatus.APPROVED
    assert PolicyEngine(db, settings).check("publish", note_status=note.status).allowed


def test_rejection_does_not_approve(db):
    note = make_note(db)
    service = ReviewService(db, CapturingNotifier())
    service.submit(note.id)
    service.reject(note.id, "需要修改")
    assert note.status == NoteStatus.REJECTED
