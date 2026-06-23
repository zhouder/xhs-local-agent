from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.base import AIProviderAdapter
from app.models import NoteStatus, ReviewQueue
from app.repositories import AuditRepository, NoteRepository
from app.schemas import GenerateNoteRequest
from app.services.audit import audited
from app.services.state_machine import transition_note


class NoteService:
    def __init__(self, db: Session, provider: AIProviderAdapter | None):
        self.notes = NoteRepository(db)
        self.audit = AuditRepository(db)
        self.provider = provider

    def generate(self, request: GenerateNoteRequest):
        if self.provider is None:
            raise RuntimeError("AI provider is required for generation")
        with audited(self.audit, "ai.generate_note", target_type="note", input_summary=request.model_dump_json()):
            content = self.provider.generate_note(request)
            self.audit.record("ai.generate_note", "success", target_type="note", input_summary=request.model_dump_json())
            note = self.notes.create(request, content)
            self.audit.record("note.created", "success", target_type="note", target_id=note.id, output_summary=note.title)
            return note

    def regenerate(self, note_id: int):
        if self.provider is None:
            raise RuntimeError("AI provider is required for regeneration")
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        if note.status in {NoteStatus.PUBLISHING, NoteStatus.WAITING_FINAL_CONFIRM, NoteStatus.PUBLISHED, NoteStatus.CANCELLED}:
            raise ValueError("Published notes cannot be regenerated")
        with audited(self.audit, "ai.regenerate_note", target_type="note", target_id=str(note_id)):
            request = GenerateNoteRequest(
                topic=note.topic, style=note.style, audience=note.audience,
                min_length=note.min_length, max_length=note.max_length,
                controversial_title=note.controversial_title,
                educational=note.educational, growth_oriented=note.growth_oriented,
            )
            content = self.provider.generate_note(request)
        self.audit.record("ai.regenerate_note", "success", target_type="note", target_id=note.id)
        note.title, note.body = content.title, content.body
        note.hashtags_json = json.dumps(content.hashtags, ensure_ascii=False)
        note.cover_prompt = content.cover_prompt
        note.media_requirements_json = content.media_requirements.model_dump_json()
        note.safety_json = content.safety.model_dump_json()
        self._reset_review(note)
        self.notes.db.commit()
        self.audit.record("note.regenerated", "success", target_type="note", target_id=note.id)
        return note

    def update(self, note_id: int, update):
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        if note.status in {NoteStatus.PUBLISHING, NoteStatus.WAITING_FINAL_CONFIRM, NoteStatus.PUBLISHED, NoteStatus.CANCELLED}:
            raise ValueError("Publishing or published notes cannot be edited")
        self.notes.update_content(note, update)
        if note.status in {NoteStatus.PENDING_REVIEW, NoteStatus.APPROVED, NoteStatus.FAILED, NoteStatus.REJECTED, NoteStatus.RETURNED_TO_EDIT, NoteStatus.PUBLISH_UNCERTAIN}:
            self._reset_review(note)
        self.notes.db.commit()
        self.audit.record("note.updated", "success", target_type="note", target_id=note.id, metadata={"approval_invalidated": note.approved_at is None})
        return note

    def _reset_review(self, note) -> None:
        if note.status != NoteStatus.DRAFT:
            transition_note(note, NoteStatus.DRAFT)
        note.approved_at = None
        queues = self.notes.db.scalars(select(ReviewQueue).where(ReviewQueue.note_id == note.id, ReviewQueue.status.in_(["pending", "approved"])))
        for queue in queues:
            queue.status = "invalidated"
