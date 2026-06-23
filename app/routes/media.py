from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import ROOT
from app.database import get_db
from app.services.materials import MaterialService


router = APIRouter()


def redirect(path: str, message: str = ""):
    suffix = f"?{urlencode({'message': message})}" if message else ""
    return RedirectResponse(path + suffix, status_code=303)


def redirect_error(path: str, error: str):
    return RedirectResponse(path + f"?{urlencode({'error': error})}", status_code=303)


@router.get("/media/{note_dir}/{filename}")
def media_file(note_dir: str, filename: str):
    directory = (ROOT / "data" / "media").resolve()
    path = (directory / note_dir / filename).resolve()
    if directory not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@router.post("/notes/{note_id}/media/upload")
def upload_media(note_id: int, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    try:
        MaterialService(db).upload_files(note_id, files)
        return redirect(f"/notes/{note_id}", "图片已添加。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@router.post("/notes/{note_id}/media/video/upload")
def upload_video(note_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        MaterialService(db).upload_video(note_id, file)
        return redirect(f"/notes/{note_id}", "视频已添加。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@router.post("/notes/{note_id}/media/reorder")
def reorder_media(note_id: int, ordered_ids: str = Form(""), db: Session = Depends(get_db)):
    try:
        ids = [int(item) for item in ordered_ids.replace(",", " ").split() if item.strip()]
        MaterialService(db).reorder(note_id, ids)
        return redirect(f"/notes/{note_id}", "图片顺序已更新。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@router.post("/notes/{note_id}/media/{asset_id}/delete")
def delete_media(note_id: int, asset_id: int, db: Session = Depends(get_db)):
    try:
        MaterialService(db).delete(note_id, asset_id)
        return redirect(f"/notes/{note_id}", "图片已删除。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@router.post("/notes/{note_id}/media/generate-cover")
def generate_cover(note_id: int, db: Session = Depends(get_db)):
    try:
        MaterialService(db).generate_cover(note_id)
        return redirect(f"/notes/{note_id}", "已生成 1080x1440 本地 AI 封面。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))
