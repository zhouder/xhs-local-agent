from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.models import NoteStatus, ReviewQueue
from app.repositories import AuditRepository, NoteRepository
from app.services.notifications import Notifier
from app.services.state_machine import transition_note


class ReviewService:
    def __init__(self, db: Session, notifier: Notifier):
        self.db = db
        self.notes = NoteRepository(db)
        self.audit = AuditRepository(db)
        self.notifier = notifier

    def submit(self, note_id: int):
        note = self._note(note_id)
        transition_note(note, NoteStatus.PENDING_REVIEW)
        self.db.add(ReviewQueue(note_id=note.id))
        self.db.commit()
        self.audit.record("review.submitted", "success", target_type="note", target_id=note.id)
        try:
            self.notifier.send("小红书草稿待审核", f"note_id={note.id}《{note.title}》请打开本地控制台审核")
            self.audit.record("notification.review_required", "success", target_type="note", target_id=note.id)
        except Exception as exc:
            self.audit.record("notification.review_required", "failed", target_type="note", target_id=note.id, error_message=str(exc))
        return note

    def approve(self, note_id: int):
        note = self._note(note_id)
        transition_note(note, NoteStatus.APPROVED)
        note.approved_at = utcnow()
        queue = self.db.scalar(select(ReviewQueue).where(ReviewQueue.note_id == note.id, ReviewQueue.status == "pending"))
        if queue:
            queue.status = "approved"
        self.db.commit()
        self.audit.record("review.approved", "success", target_type="note", target_id=note.id)
        return note

    def reject(self, note_id: int, reason: str = ""):
        note = self._note(note_id)
        transition_note(note, NoteStatus.REJECTED)
        queue = self.db.scalar(select(ReviewQueue).where(ReviewQueue.note_id == note.id, ReviewQueue.status == "pending"))
        if queue:
            queue.status, queue.decision_reason = "rejected", reason
        self.db.commit()
        self.audit.record("review.rejected", "success", target_type="note", target_id=note.id, input_summary=reason)
        return note

    def _note(self, note_id: int):
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        return note
