import pytest
from sqlalchemy import select

from app.ai.mock import MockProvider
from app.models import NoteStatus, ReviewQueue
from app.repositories import NoteRepository
from app.schemas import GenerateNoteRequest, NoteUpdate
from app.services.notes import NoteService
from app.services.notifications import NullNotifier
from app.services.review import ReviewService
from app.services.state_machine import ALLOWED_TRANSITIONS, transition_note


def make_note(db):
    request = GenerateNoteRequest(topic="AI")
    return NoteRepository(db).create(request, MockProvider().generate_note(request.topic, request.style, request.audience))


def test_state_machine_defines_every_status():
    assert set(ALLOWED_TRANSITIONS) == set(NoteStatus)


def test_invalid_transition_is_rejected(db):
    note = make_note(db)
    with pytest.raises(ValueError, match="draft -> published"):
        transition_note(note, NoteStatus.PUBLISHED)


def test_model_rejects_unknown_status(db):
    note = make_note(db)
    with pytest.raises(ValueError):
        note.status = "unknown"


def test_editing_approved_note_invalidates_approval(db):
    note = make_note(db)
    review = ReviewService(db, NullNotifier())
    review.submit(note.id)
    review.approve(note.id)
    NoteService(db, MockProvider()).update(note.id, NoteUpdate(title="修改后", body="新正文", hashtags=[]))
    db.refresh(note)
    queue = db.scalar(select(ReviewQueue).where(ReviewQueue.note_id == note.id))
    assert note.status == NoteStatus.DRAFT
    assert note.approved_at is None
    assert queue.status == "invalidated"


def test_regenerate_pending_note_invalidates_review(db):
    note = make_note(db)
    ReviewService(db, NullNotifier()).submit(note.id)
    NoteService(db, MockProvider()).regenerate(note.id)
    db.refresh(note)
    queue = db.scalar(select(ReviewQueue).where(ReviewQueue.note_id == note.id))
    assert note.status == NoteStatus.DRAFT
    assert queue.status == "invalidated"
