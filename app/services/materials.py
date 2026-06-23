from __future__ import annotations

import mimetypes
import re
import shutil
import textwrap
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.models import MediaAsset
from app.repositories import AuditRepository, NoteRepository


SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
COVER_SIZE = (1080, 1440)


def parse_asset_paths(text: str) -> list[str]:
    return [line.strip().strip('"') for line in text.replace(";", "\n").splitlines() if line.strip()]


def validate_image_assets(paths: list[str]) -> tuple[bool, str]:
    if len(paths) > 9:
        return False, "最多只能添加 9 张图片。"
    for item in paths:
        path = Path(item)
        if not path.exists():
            return False, f"图片文件不存在：{item}"
        if path.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
            return False, f"不支持的图片格式：{item}"
    return True, ""


class MaterialService:
    def __init__(self, db: Session):
        self.db = db
        self.notes = NoteRepository(db)
        self.audit = AuditRepository(db)

    def set_note_assets(self, note_id: int, paths: list[str]) -> None:
        note = self.notes.get(note_id)
        if not note:
            raise LookupError("草稿不存在。")
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
            raise LookupError("草稿不存在。")
        files = [item for item in files if getattr(item, "filename", "")]
        if not files:
            raise ValueError("请选择要上传的图片。")
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
                message = f"不支持的图片格式：{original}"
                self.audit.record("materials.upload", "blocked", target_type="note", target_id=note_id, error_message=message)
                raise ValueError(message)
            target = directory / f"{uuid.uuid4().hex}{suffix}"
            with target.open("wb") as output:
                shutil.copyfileobj(upload.file, output)
            row = MediaAsset(
                note_id=note_id,
                path=str(target),
                file_path=str(target),
                media_type="image",
                asset_type="image",
                mime_type=mimetypes.guess_type(str(target))[0] or "image/*",
                upload_order=len(existing) + len(created) + 1,
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
            raise LookupError("草稿不存在。")
        if len(self.notes.media_assets(note_id)) >= 9:
            raise ValueError("最多只能添加 9 张图片。")
        directory = ROOT / "data" / "media" / f"note-{note_id}"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"cover-{datetime.now():%Y%m%d-%H%M%S}.png"
        _render_cover_png(target, note.title, note.cover_prompt)
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
            license_note=f"本地生成封面，无外部版权图片：{_clean_text(note.title)}",
        )
        self.db.add(row)
        self.db.commit()
        self.audit.record("materials.generated_cover", "success", target_type="note", target_id=note_id, metadata={"path": str(target), "size": list(COVER_SIZE)})
        return row


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:80]


def _font(size: int, bold: bool = False):
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _wrapped_lines(text: str, width: int) -> list[str]:
    clean = _clean_text(text) or "AI 内容封面"
    lines: list[str] = []
    for paragraph in clean.splitlines() or [clean]:
        lines.extend(textwrap.wrap(paragraph, width=width) or [paragraph])
    return lines[:5]


def _keyword_badges(title: str, prompt: str) -> list[str]:
    words = re.findall(r"[\w\u4e00-\u9fff]{2,}", f"{title} {prompt}")
    picked: list[str] = []
    for word in words:
        if word not in picked:
            picked.append(word)
        if len(picked) >= 4:
            break
    return (picked + ["AI", "科技", "效率"])[:4]


def _render_cover_png(path: Path, title: str, prompt: str = "") -> None:
    width, height = COVER_SIZE
    image = Image.new("RGB", COVER_SIZE, "#f7f2ff")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / height
        draw.line([(0, y), (width, y)], fill=(int(247 - 38 * ratio), int(250 - 58 * ratio), int(255 - 8 * ratio)))
    draw.rounded_rectangle((76, 92, width - 76, height - 92), radius=56, fill="#ffffff", outline="#e4d7ff", width=4)
    draw.ellipse((width - 320, 60, width + 120, 500), fill="#ebe3ff")
    draw.ellipse((-160, height - 420, 320, height + 80), fill="#d8f7ef")
    draw.text((120, 150), "XHS LOCAL AGENT", font=_font(34, True), fill="#6f52ed")
    y = 265
    for line in _wrapped_lines(title, 13):
        draw.text((120, y), line, font=_font(78, True), fill="#182230")
        y += 104
    y += 40
    for keyword in _keyword_badges(title, prompt):
        badge_width = max(180, 70 + len(keyword) * 42)
        draw.rounded_rectangle((120, y, 120 + badge_width, y + 70), radius=35, fill="#f3f0ff", outline="#d6bbfb", width=2)
        draw.text((150, y + 14), keyword, font=_font(34, True), fill="#53389e")
        y += 92
    draw.text((120, height - 190), "AI / 科技 / 效率", font=_font(42, True), fill="#344054")
    draw.text((120, height - 125), "本地生成封面 | 未使用外部版权图片", font=_font(28), fill="#667085")
    image.save(path, format="PNG")
