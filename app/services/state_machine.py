from __future__ import annotations

from app.models import Note, NoteStatus


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    NoteStatus.DRAFT: {NoteStatus.PENDING_REVIEW},
    NoteStatus.PENDING_REVIEW: {NoteStatus.APPROVED, NoteStatus.REJECTED, NoteStatus.DRAFT},
    NoteStatus.APPROVED: {NoteStatus.PUBLISHING, NoteStatus.DRAFT},
    NoteStatus.PUBLISHING: {NoteStatus.WAITING_FINAL_CONFIRM, NoteStatus.FAILED},
    NoteStatus.WAITING_FINAL_CONFIRM: {
        NoteStatus.PUBLISHED,
        NoteStatus.PUBLISH_UNCERTAIN,
        NoteStatus.CANCELLED,
        NoteStatus.RETURNED_TO_EDIT,
        NoteStatus.FAILED,
    },
    NoteStatus.PUBLISHED: set(),
    NoteStatus.PUBLISH_UNCERTAIN: {NoteStatus.PUBLISHED, NoteStatus.FAILED, NoteStatus.RETURNED_TO_EDIT},
    NoteStatus.FAILED: {NoteStatus.DRAFT, NoteStatus.PUBLISHING},
    NoteStatus.REJECTED: {NoteStatus.DRAFT},
    NoteStatus.RETURNED_TO_EDIT: {NoteStatus.DRAFT},
    NoteStatus.CANCELLED: set(),
}


def transition_note(note: Note, target: NoteStatus) -> None:
    current = NoteStatus(note.status)
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid note transition: {current.value} -> {target.value}")
    note.status = target
