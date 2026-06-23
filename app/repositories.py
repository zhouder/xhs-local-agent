from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from app.models import AuditLog, MediaAsset, Note
from app.schemas import NoteContent
from app.security import redact_secrets


class AuditRepository:
    def __init__(self, db: Session):
        self.db = db

    def record(self, action_type: str, status: str, *, target_type: str = "", target_id: str = "", input_summary: str = "", output_summary: str = "", error_message: str = "", screenshot_path: str = "", metadata: dict[str, Any] | None = None) -> AuditLog:
        row = AuditLog(
            action_type=action_type, target_type=target_type, target_id=str(target_id), status=status,
            input_summary=redact_secrets(input_summary)[:2000], output_summary=redact_secrets(output_summary)[:2000],
            error_message=redact_secrets(error_message)[:4000], screenshot_path=screenshot_path,
            metadata_json=json.dumps(redact_secrets(metadata or {}), ensure_ascii=False),
        )
        self.db.add(row)
        self.db.commit()
        return row


class NoteRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, request, content: NoteContent) -> Note:
        note = Note(
            topic=request.topic, style=request.style, audience=request.audience,
            min_length=request.min_length, max_length=request.max_length,
            controversial_title=request.controversial_title, educational=request.educational,
            growth_oriented=request.growth_oriented,
            title=content.title, body=content.body,
            hashtags_json=json.dumps(content.hashtags, ensure_ascii=False),
            cover_prompt=content.cover_prompt,
            media_requirements_json=content.media_requirements.model_dump_json(),
            safety_json=content.safety.model_dump_json(),
        )
        self.db.add(note)
        self.db.commit()
        return note

    def get(self, note_id: int) -> Note | None:
        return self.db.get(Note, note_id)

    def list(self) -> list[Note]:
        return list(self.db.scalars(select(Note).order_by(desc(Note.created_at))))

    def update_content(self, note: Note, update) -> Note:
        note.title = update.title
        note.body = update.body
        note.hashtags_json = json.dumps(update.hashtags, ensure_ascii=False)
        note.cover_prompt = update.cover_prompt
        if update.media_path:
            self.replace_media_paths(note.id, [update.media_path])
        self.db.flush()
        return note

    def media_paths(self, note_id: int) -> list[str]:
        rows = self.db.scalars(select(MediaAsset).where(MediaAsset.note_id == note_id).order_by(MediaAsset.upload_order, MediaAsset.id))
        return [row.file_path or row.path for row in rows]

    def media_assets(self, note_id: int) -> list[MediaAsset]:
        return list(self.db.scalars(select(MediaAsset).where(MediaAsset.note_id == note_id).order_by(MediaAsset.upload_order, MediaAsset.id)))

    def replace_media_paths(self, note_id: int, paths: list[str]) -> None:
        self.db.execute(delete(MediaAsset).where(MediaAsset.note_id == note_id))
        for index, path in enumerate(paths, start=1):
            self.db.add(MediaAsset(
                note_id=note_id,
                path=path,
                file_path=path,
                media_type="image",
                asset_type="image",
                upload_order=index,
                status="ready",
            ))
