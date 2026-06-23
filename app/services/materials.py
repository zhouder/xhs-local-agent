from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.repositories import AuditRepository, NoteRepository


SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def parse_asset_paths(text: str) -> list[str]:
    return [line.strip().strip('"') for line in text.replace(";", "\n").splitlines() if line.strip()]


def validate_image_assets(paths: list[str]) -> tuple[bool, str]:
    if len(paths) > 9:
        return False, "At most 9 images are supported."
    for item in paths:
        path = Path(item)
        if not path.exists():
            return False, f"Asset does not exist: {item}"
        if path.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
            return False, f"Unsupported image format: {item}"
    return True, ""


class MaterialService:
    def __init__(self, db: Session):
        self.db = db
        self.notes = NoteRepository(db)
        self.audit = AuditRepository(db)

    def set_note_assets(self, note_id: int, paths: list[str]) -> None:
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        ok, reason = validate_image_assets(paths)
        if not ok:
            self.audit.record("materials.validate", "blocked", target_type="note", target_id=note_id, error_message=reason)
            raise ValueError(reason)
        self.notes.replace_media_paths(note_id, paths)
        self.db.commit()
        self.audit.record("materials.updated", "success", target_type="note", target_id=note_id, metadata={"count": len(paths)})
