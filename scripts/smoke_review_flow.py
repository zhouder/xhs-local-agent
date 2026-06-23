from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ai.mock import MockProvider
from app.database import SessionLocal, init_db
from app.models import NoteStatus
from app.repositories import NoteRepository
from app.schemas import GenerateNoteRequest
from app.services.notifications import NullNotifier
from app.services.publish import PublishService
from app.services.review import ReviewService
from app.config import get_settings


def main() -> int:
    init_db()
    settings = get_settings()
    with SessionLocal() as db:
        request = GenerateNoteRequest(topic="smoke review flow")
        note = NoteRepository(db).create(request, MockProvider().generate_note(request))
        review = ReviewService(db, NullNotifier())
        review.submit(note.id)
        assert note.status == NoteStatus.PENDING_REVIEW
        review.approve(note.id)
        assert note.status == NoteStatus.APPROVED
        print(f"Created and approved note_id={note.id}. Browser dry_run requires manual page login; skipping real browser launch.")
        print(f"Default publish mode={settings.browser.get('dry_run', True)}; final confirm remains required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
