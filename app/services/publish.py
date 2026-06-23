from __future__ import annotations

from sqlalchemy.orm import Session

from app.browser.xhs import XHSBrowser
from app.models import NoteStatus
from app.repositories import AuditRepository, NoteRepository
from app.services.notifications import Notifier
from app.services.state_machine import transition_note


class PublishService:
    def __init__(self, db: Session, settings, notifier: Notifier):
        self.db = db
        self.settings = settings
        self.notifier = notifier
        self.notes = NoteRepository(db)
        self.audit = AuditRepository(db)

    def fill(self, note_id: int, mode: str = "dry_run") -> str:
        return XHSBrowser(self.db, self.settings, self.notifier).fill_approved_note(note_id, dry_run=(mode == "dry_run"), mode=mode)

    def final_confirm(self, note_id: int) -> str:
        return XHSBrowser(self.db, self.settings, self.notifier).final_confirm_publish(note_id)

    def cancel(self, note_id: int):
        note = self._waiting_note(note_id)
        transition_note(note, NoteStatus.CANCELLED)
        self.db.commit()
        self.audit.record("publish.cancelled", "success", target_type="note", target_id=note.id)
        return note

    def return_to_edit(self, note_id: int):
        note = self._waiting_note(note_id)
        transition_note(note, NoteStatus.RETURNED_TO_EDIT)
        transition_note(note, NoteStatus.DRAFT)
        note.approved_at = None
        self.db.commit()
        self.audit.record("publish.returned_to_edit", "success", target_type="note", target_id=note.id)
        return note

    def retry_fill(self, note_id: int, mode: str = "fill_only") -> str:
        note = self._waiting_note(note_id)
        note.status = NoteStatus.APPROVED
        self.db.commit()
        self.audit.record("publish.retry_fill", "success", target_type="note", target_id=note.id, metadata={"mode": mode})
        return self.fill(note_id, mode)

    def _waiting_note(self, note_id: int):
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        if note.status != NoteStatus.WAITING_FINAL_CONFIRM:
            self.audit.record("publish.action", "blocked", target_type="note", target_id=note_id, output_summary=f"status={note.status}")
            raise ValueError("Note is not waiting for final confirmation.")
        return note
