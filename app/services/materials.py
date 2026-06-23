from __future__ import annotations

import mimetypes
import re
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.models import MediaAsset
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

    def upload_files(self, note_id: int, files) -> list[MediaAsset]:
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        existing = self.notes.media_assets(note_id)
        if len(existing) + len(files) > 9:
            raise ValueError("最多只能添加 9 张图片。")
        directory = ROOT / "data" / "media" / f"note-{note_id}"
        directory.mkdir(parents=True, exist_ok=True)
        created: list[MediaAsset] = []
        for upload in files:
            original = Path(upload.filename or "image").name
            suffix = Path(original).suffix.casefold()
            if suffix not in SUPPORTED_IMAGE_SUFFIXES:
                self.audit.record("materials.upload", "blocked", target_type="note", target_id=note_id, error_message=f"unsupported_format:{original}")
                raise ValueError(f"不支持的图片格式：{original}")
            target = directory / f"{uuid.uuid4().hex}{suffix}"
            with target.open("wb") as output:
                shutil.copyfileobj(upload.file, output)
            order = len(existing) + len(created) + 1
            row = MediaAsset(
                note_id=note_id,
                path=str(target),
                file_path=str(target),
                media_type="image",
                asset_type="image",
                mime_type=mimetypes.guess_type(str(target))[0] or "image/*",
                upload_order=order,
                status="ready",
                source_type="upload",
                license_note="用户本地上传，版权由用户自行确认。",
            )
            self.db.add(row)
            created.append(row)
        self.db.commit()
        self.audit.record("materials.upload", "success", target_type="note", target_id=note_id, metadata={"count": len(created)})
        return created

    def reorder(self, note_id: int, ordered_ids: list[int]) -> None:
        assets = {asset.id: asset for asset in self.notes.media_assets(note_id)}
        if set(ordered_ids) != set(assets):
            raise ValueError("图片排序参数无效。")
        for index, asset_id in enumerate(ordered_ids, start=1):
            assets[asset_id].upload_order = index
        self.db.commit()
        self.audit.record("materials.reordered", "success", target_type="note", target_id=note_id, metadata={"order": ordered_ids})

    def delete(self, note_id: int, asset_id: int) -> None:
        asset = self.db.scalar(select(MediaAsset).where(MediaAsset.id == asset_id, MediaAsset.note_id == note_id))
        if not asset:
            raise LookupError("图片不存在。")
        path = Path(asset.file_path or asset.path)
        self.db.delete(asset)
        self.db.flush()
        for index, row in enumerate(self.notes.media_assets(note_id), start=1):
            row.upload_order = index
        self.db.commit()
        if path.exists() and ROOT in path.resolve().parents:
            path.unlink(missing_ok=True)
        self.audit.record("materials.deleted", "success", target_type="note", target_id=note_id, metadata={"asset_id": asset_id})

    def generate_cover(self, note_id: int) -> MediaAsset:
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("Note not found")
        if len(self.notes.media_assets(note_id)) >= 9:
            raise ValueError("最多只能添加 9 张图片。")
        directory = ROOT / "data" / "media" / f"note-{note_id}"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "cover.png"
        target.write_bytes(_simple_png())
        row = MediaAsset(
            note_id=note_id,
            path=str(target),
            file_path=str(target),
            media_type="image",
            asset_type="image",
            mime_type="image/png",
            upload_order=len(self.notes.media_assets(note_id)) + 1,
            status="ready",
            source_type="generated_cover",
            license_note=f"本地生成占位封面：{_clean_text(note.title)}",
        )
        self.db.add(row)
        self.db.commit()
        self.audit.record("materials.generated_cover", "success", target_type="note", target_id=note_id, metadata={"path": str(target)})
        return row


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "")[:80]


def _simple_png() -> bytes:
    # 1x1 blue PNG placeholder; generated locally, no external copyright source.
    import base64

    return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
