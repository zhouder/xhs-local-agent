from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.ai.factory import create_provider_from_profile
from app.ai.anthropic import resolve_auth_scheme
from app.ai.endpoints import build_endpoint_url, normalize_ui_api_format
from app.ai.errors import friendly_connection_error
from app.ai.openai_compatible import AIProviderError
from app.browser.xhs import XHSBrowser
from app.config import ROOT, get_settings
from app.database import SessionLocal, get_db, init_db
from app.models import AuditLog, BrowserError, CommandEvent, Comment, ContentPlan, ContentPlanTopic, Interaction, Message, Note, NoteStatus, Setting
from app.repositories import AuditRepository, NoteRepository
from app.schemas import GenerateNoteRequest, NoteUpdate
from app.services.notes import NoteService
from app.services.notifications import create_notifier
from app.services.policy import PolicyEngine
from app.services.commands import CommandExecutor
from app.services.content_plans import ContentPlanService
from app.services.hashtags import split_hashtags
from app.services.materials import MaterialService, parse_asset_paths
from app.services.publish import PublishService
from app.services.publish_kinds import PUBLISH_KIND_LABELS, normalize_publish_kind, publish_kind_label
from app.services.review import ReviewService
from app.services.scheduler import PublishScheduler
from app.security import generate_api_key_env, write_api_key
from app.services.provider_registry import ProviderInput, ProviderRegistry, provider_requires_api_key, provider_view
from app.routes.media import router as media_router


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()
templates = Jinja2Templates(directory=ROOT / "app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="XHS Local Growth Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
app.include_router(media_router)


@app.get("/screenshots/{filename}")
def screenshot_file(filename: str):
    directory = (ROOT / settings.browser["screenshots_dir"]).resolve()
    path = (directory / filename).resolve()
    if directory not in path.parents or not path.exists() or path.suffix.casefold() != ".png":
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/previews/{filename}")
def preview_file(filename: str):
    directory = (ROOT / settings.browser["screenshots_dir"]).resolve()
    path = (directory / filename).resolve()
    if directory not in path.parents or not path.exists() or path.suffix.casefold() != ".html":
        raise HTTPException(404)
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc.casefold() != request.headers.get("host", "").casefold():
            with SessionLocal() as audit_db:
                AuditRepository(audit_db).record("http.csrf_blocked", "blocked", target_type="request", input_summary=str(request.url.path))
            return HTMLResponse("Cross-origin state change blocked", status_code=403)
    return await call_next(request)


def redirect(path: str, message: str = ""):
    suffix = f"?{urlencode({'message': message})}" if message else ""
    return RedirectResponse(path + suffix, status_code=303)


def redirect_error(path: str, error: str):
    return RedirectResponse(path + f"?{urlencode({'error': error})}", status_code=303)


def settings_redirect(*, message: str = "", error: str = ""):
    query = urlencode({key: value for key, value in {"message": message, "error": error}.items() if value})
    return RedirectResponse("/settings" + (f"?{query}" if query else ""), status_code=303)


def notifier():
    return create_notifier(bool(settings.notifications.get("windows_toast_enabled")))


def ai_provider(db: Session):
    profile = None
    try:
        registry = ProviderRegistry(db, settings)
        registry.initialize()
        profile = registry.get_default()
        if profile is None:
            raise ValueError("No enabled default AI provider is configured")
        return create_provider_from_profile(profile, settings)
    except Exception as exc:
        AuditRepository(db).record(
            "ai.provider_init", "failed", target_type="ai_provider",
            target_id=profile.name if profile else "default", error_message=str(exc),
        )
        raise


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    notes = NoteRepository(db).list()
    counts = {status.value: sum(n.status == status.value for n in notes) for status in NoteStatus}
    paused = PolicyEngine(db, settings).is_paused()
    scheduler_paused = PublishScheduler(db, settings, notifier()).paused()
    today = __import__("datetime").date.today().isoformat()
    today_published = db.scalar(select(func.count()).select_from(Note).where(Note.status == NoteStatus.PUBLISHED.value, func.date(Note.published_at) == today)) or 0
    today_generated = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action_type == "ai.generate_note", AuditLog.status == "success", func.date(AuditLog.created_at) == today)) or 0
    plan_count = db.scalar(select(func.count()).select_from(ContentPlan)) or 0
    pending_plan_topics = db.scalar(select(func.count()).select_from(ContentPlanTopic).where(ContentPlanTopic.status == "pending")) or 0
    generated_plan_topics = db.scalar(select(func.count()).select_from(ContentPlanTopic).where(ContentPlanTopic.status == "generated")) or 0
    recent = list(db.scalars(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(10)))
    return templates.TemplateResponse(request, "dashboard.html", {
        "notes": notes[:5], "counts": counts, "paused": paused, "scheduler_paused": scheduler_paused,
        "audit_logs": recent, "today_published": today_published, "today_generated": today_generated,
        "plan_count": plan_count, "pending_plan_topics": pending_plan_topics, "generated_plan_topics": generated_plan_topics,
    })


@app.get("/notes", response_class=HTMLResponse)
def notes_page(request: Request, plan_id: int | None = None, db: Session = Depends(get_db)):
    notes = NoteRepository(db).list()
    if plan_id:
        notes = [note for note in notes if note.content_plan_id == plan_id]
    plans = list(db.scalars(select(ContentPlan).order_by(desc(ContentPlan.created_at))))
    return templates.TemplateResponse(request, "notes.html", {"notes": notes, "plans": plans, "selected_plan_id": plan_id, "message": request.query_params.get("message", ""), "error": request.query_params.get("error", "")})


@app.post("/notes/generate")
def generate_note(
    topic: str = Form(...), style: str = Form("实用、自然"), audience: str = Form("科技爱好者"),
    min_length: int = Form(200), max_length: int = Form(600),
    controversial_title: bool = Form(False), educational: bool = Form(False),
    growth_oriented: bool = Form(False), publish_kind: str = Form("image_text_to_image"), db: Session = Depends(get_db),
):
    try:
        request_data = GenerateNoteRequest(
            topic=topic, style=style, audience=audience,
            min_length=min_length, max_length=max_length,
            controversial_title=controversial_title, educational=educational,
            growth_oriented=growth_oriented,
            publish_kind=normalize_publish_kind(publish_kind),
        )
        note = NoteService(db, ai_provider(db)).generate(request_data)
        return redirect(f"/notes/{note.id}")
    except AIProviderError as exc:
        return redirect_error("/notes", "AI 生成失败，请检查默认 Provider 和审计日志。")
    except ValueError as exc:
        return redirect_error("/notes", str(exc))
    except Exception as exc:
        AuditRepository(db).record("ai.generate_note", "failed", target_type="note", error_message=str(exc))
        return redirect_error("/notes", "草稿生成失败，请查看审计日志。")


@app.get("/notes/{note_id}", response_class=HTMLResponse)
def note_page(note_id: int, request: Request, db: Session = Depends(get_db)):
    repo = NoteRepository(db)
    note = repo.get(note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    media_assets = repo.media_assets(note_id)
    image_assets = [asset for asset in media_assets if asset.asset_type in {"image", "generated_cover"}]
    video_assets = [asset for asset in media_assets if asset.asset_type == "video"]
    media_views = [
        {
            "id": asset.id,
            "order": asset.upload_order,
            "filename": os.path.basename(asset.file_path or asset.path),
            "url": f"/media/note-{note_id}/{os.path.basename(asset.file_path or asset.path)}",
            "source_type": asset.source_type,
            "license_note": asset.license_note,
        }
        for asset in image_assets
    ]
    video_view = None
    if video_assets:
        asset = video_assets[0]
        path = asset.file_path or asset.path
        video_view = {
            "id": asset.id,
            "filename": os.path.basename(path),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "source_type": asset.source_type,
            "license_note": asset.license_note,
        }
    return templates.TemplateResponse(request, "note_detail.html", {
        "note": note,
        "hashtags": ", ".join(json.loads(note.hashtags_json)),
        "media_paths": repo.media_paths(note_id),
        "media_assets": media_assets,
        "media_views": media_views,
        "video_view": video_view,
        "publish_kind_label": publish_kind_label(note.publish_kind),
        "publish_kind_options": PUBLISH_KIND_LABELS,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    })


@app.post("/notes/{note_id}/edit")
def edit_note(
    note_id: int,
    title: str = Form(...),
    body: str = Form(...),
    hashtags: str = Form(""),
    cover_prompt: str = Form(""),
    publish_kind: str = Form("image_text_to_image"),
    text_to_image_prompt: str = Form(""),
    text_to_image_style: str = Form(""),
    media_path: str = Form(""),
    media_paths: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        paths = parse_asset_paths(media_paths) or ([media_path] if media_path.strip() else [])
        update = NoteUpdate(title=title, body=body, hashtags=split_hashtags(hashtags), cover_prompt=cover_prompt, media_path="")
        note = NoteService(db, None).update(note_id, update)
        note.publish_kind = normalize_publish_kind(publish_kind)
        note.text_to_image_prompt = text_to_image_prompt.strip()
        note.text_to_image_style = text_to_image_style.strip()
        if paths:
            MaterialService(db).set_note_assets(note_id, paths)
        db.commit()
        return redirect(f"/notes/{note_id}", "已保存；如原草稿已提交或批准，审核现已失效并重置为 draft")
    except LookupError as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))
    except ValueError as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@app.post("/notes/{note_id}/submit")
def submit_note(note_id: int, db: Session = Depends(get_db)):
    try:
        ReviewService(db, notifier()).submit(note_id)
        return redirect(f"/notes/{note_id}", "已提交审核")
    except (LookupError, ValueError) as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@app.post("/notes/{note_id}/approve")
def approve_note(note_id: int, confirm: str = Form(""), db: Session = Depends(get_db)):
    if confirm != "APPROVE":
        return redirect_error(f"/notes/{note_id}", "缺少审核确认。")
    try:
        ReviewService(db, notifier()).approve(note_id)
        return redirect(f"/notes/{note_id}", "已批准；仍需单独点击 dry-run，第一阶段不会真实发布")
    except (LookupError, ValueError) as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@app.post("/notes/{note_id}/reject")
def reject_note(note_id: int, reason: str = Form(""), db: Session = Depends(get_db)):
    try:
        ReviewService(db, notifier()).reject(note_id, reason)
        return redirect(f"/notes/{note_id}", "已拒绝")
    except (LookupError, ValueError) as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@app.post("/notes/{note_id}/regenerate")
def regenerate_note(note_id: int, db: Session = Depends(get_db)):
    try:
        NoteService(db, ai_provider(db)).regenerate(note_id)
        return redirect(f"/notes/{note_id}", "已重新生成")
    except (LookupError, ValueError) as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", "重新生成失败，请查看审计日志。")


@app.post("/notes/{note_id}/dry-run")
async def dry_run(note_id: int, db: Session = Depends(get_db)):
    try:
        await PublishService(db, settings, notifier()).fill_async(note_id, mode="dry_run")
        return redirect(f"/notes/{note_id}/final-review", "dry_run 预览已生成：未打开小红书，未上传素材，未点击发布。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@app.post("/notes/{note_id}/fill")
async def fill_note(note_id: int, mode: str = Form("fill_only"), db: Session = Depends(get_db)):
    try:
        await PublishService(db, settings, notifier()).fill_async(note_id, mode=mode)
        return redirect(f"/notes/{note_id}/final-review", "发布页已填好，等待最终确认。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}", str(exc))


@app.get("/notes/{note_id}/final-review", response_class=HTMLResponse)
def final_review_page(note_id: int, request: Request, db: Session = Depends(get_db)):
    repo = NoteRepository(db)
    note = repo.get(note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    errors = list(db.scalars(select(BrowserError).where(BrowserError.note_id == note_id).order_by(desc(BrowserError.created_at)).limit(5)))
    image_paths = repo.media_paths_by_type(note_id, "image")
    video_paths = repo.media_paths_by_type(note_id, "video")
    if note.publish_kind == "video_upload":
        material_summary = f"视频素材：{os.path.basename(video_paths[0])}" if video_paths else "视频素材：未添加"
    elif note.publish_kind == "image_upload":
        material_summary = f"图片素材：{len(image_paths)} 张" if image_paths else "图片素材：未添加"
    else:
        material_summary = "生成方式：小红书文字生图"
    return templates.TemplateResponse(request, "final_review.html", {
        "note": note,
        "hashtags": ", ".join(json.loads(note.hashtags_json)),
        "media_paths": repo.media_paths(note_id),
        "publish_kind_label": publish_kind_label(note.publish_kind),
        "material_summary": material_summary,
        "errors": errors,
        "screenshot_filename": os.path.basename(note.publish_screenshot_path) if note.publish_screenshot_path else "",
        "preview_filename": os.path.basename(note.publish_preview_html_path) if note.publish_preview_html_path else "",
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    })


@app.post("/notes/{note_id}/final-confirm")
async def final_confirm_note(note_id: int, db: Session = Depends(get_db)):
    try:
        await PublishService(db, settings, notifier()).final_confirm_async(note_id)
        return redirect(f"/notes/{note_id}/final-review", "已点击发布按钮；结果标记为 publish_uncertain，请人工核验。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}/final-review", str(exc))


@app.post("/notes/{note_id}/cancel-publish")
def cancel_publish(note_id: int, db: Session = Depends(get_db)):
    try:
        PublishService(db, settings, notifier()).cancel(note_id)
        return redirect(f"/notes/{note_id}", "发布已取消。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}/final-review", str(exc))


@app.post("/notes/{note_id}/return-to-edit")
def return_to_edit(note_id: int, db: Session = Depends(get_db)):
    try:
        PublishService(db, settings, notifier()).return_to_edit(note_id)
        return redirect(f"/notes/{note_id}", "已返回编辑，状态重置为 draft。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}/final-review", str(exc))


@app.post("/notes/{note_id}/retry-fill")
async def retry_fill(note_id: int, mode: str = Form("fill_only"), db: Session = Depends(get_db)):
    try:
        await PublishService(db, settings, notifier()).retry_fill_async(note_id, mode=mode)
        return redirect(f"/notes/{note_id}/final-review", "已重新填表，仍等待最终确认。")
    except Exception as exc:
        return redirect_error(f"/notes/{note_id}/final-review", str(exc))


@app.post("/agent/{action}")
def agent_state(action: str, db: Session = Depends(get_db)):
    if action not in {"pause", "resume"}:
        raise HTTPException(404)
    paused = action == "pause"
    PolicyEngine(db, settings).set_paused(paused)
    AuditRepository(db).record(f"agent.{action}", "success", target_type="agent")
    return redirect("/", "")


@app.post("/scheduler/{action}")
def scheduler_action(action: str, db: Session = Depends(get_db)):
    scheduler = PublishScheduler(db, settings, notifier())
    if action == "pause":
        scheduler.set_paused(True)
        return redirect("/", "Scheduler paused")
    if action == "resume":
        scheduler.set_paused(False)
        return redirect("/", "Scheduler resumed")
    if action == "run-once":
        count = scheduler.run_once()
        return redirect("/", f"Scheduler run finished; filled={count}")
    raise HTTPException(404)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, action_type: str = "", target_id: str = "", status: str = "", db: Session = Depends(get_db)):
    query = select(AuditLog)
    if action_type:
        query = query.where(AuditLog.action_type.contains(action_type))
    if target_id:
        query = query.where(AuditLog.target_id == target_id)
    if status:
        query = query.where(AuditLog.status == status)
    rows = list(db.scalars(query.order_by(desc(AuditLog.created_at)).limit(500)))
    return templates.TemplateResponse(request, "audit.html", {"rows": rows, "filters": {"action_type": action_type, "target_id": target_id, "status": status}})


@app.get("/records/{kind}", response_class=HTMLResponse)
def records_page(kind: str, request: Request, db: Session = Depends(get_db)):
    models = {"comments": Comment, "messages": Message, "interactions": Interaction, "errors": BrowserError, "commands": CommandEvent}
    model = models.get(kind)
    if not model:
        raise HTTPException(404)
    rows = list(db.scalars(select(model).order_by(desc(model.created_at)).limit(500)))
    return templates.TemplateResponse(request, "records.html", {"kind": kind, "rows": rows})


@app.get("/plans", response_class=HTMLResponse)
def plans_page(request: Request, db: Session = Depends(get_db)):
    plans = list(db.scalars(select(ContentPlan).order_by(desc(ContentPlan.created_at))))
    progress = {plan.id: ContentPlanService(db).progress(plan.id) for plan in plans}
    return templates.TemplateResponse(request, "plans.html", {"plans": plans, "progress": progress, "message": request.query_params.get("message", ""), "error": request.query_params.get("error", "")})


@app.post("/plans")
def create_plan(
    name: str = Form(...), audience: str = Form(""), style: str = Form(""), goal: str = Form("growth"),
    topics_text: str = Form(...), daily_count: int = Form(1), publish_times_text: str = Form("10:30\n20:30"),
    db: Session = Depends(get_db),
):
    try:
        plan = ContentPlanService(db).create_plan(name=name, audience=audience, style=style, goal=goal, topics_text=topics_text, daily_count=daily_count, publish_times_text=publish_times_text)
        return redirect(f"/plans/{plan.id}", "Content plan created.")
    except ValueError as exc:
        return redirect_error("/plans", str(exc))


@app.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan_detail(plan_id: int, request: Request, db: Session = Depends(get_db)):
    plan = db.get(ContentPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    topics = list(db.scalars(select(ContentPlanTopic).where(ContentPlanTopic.plan_id == plan_id).order_by(ContentPlanTopic.id)))
    return templates.TemplateResponse(request, "plan_detail.html", {"plan": plan, "topics": topics, "progress": ContentPlanService(db).progress(plan_id), "message": request.query_params.get("message", ""), "error": request.query_params.get("error", "")})


@app.post("/plans/{plan_id}/generate")
def generate_plan_drafts(plan_id: int, mode: str = Form("pending"), db: Session = Depends(get_db)):
    try:
        statuses = {"failed"} if mode == "failed" else {"pending"}
        created = ContentPlanService(db, ai_provider(db)).generate_drafts(plan_id, statuses=statuses)
        failed = db.scalar(select(func.count()).select_from(ContentPlanTopic).where(ContentPlanTopic.plan_id == plan_id, ContentPlanTopic.status == "failed")) or 0
        total = db.scalar(select(func.count()).select_from(ContentPlanTopic).where(ContentPlanTopic.plan_id == plan_id)) or 0
        return redirect(f"/plans/{plan_id}", f"批量生成完成：总主题 {total}，成功新增 {len(created)}，失败 {failed}。")
    except Exception as exc:
        return redirect_error(f"/plans/{plan_id}", str(exc))


@app.post("/commands/mock", response_class=HTMLResponse)
def mock_command(command: str = Form(...), db: Session = Depends(get_db)):
    try:
        response = CommandExecutor(db, settings, notifier()).execute(command, channel="mock")
        return HTMLResponse(response.replace("\n", "<br>"))
    except Exception as exc:
        return HTMLResponse(f"Command failed: {exc}", status_code=400)


@app.post("/webhooks/feishu")
async def feishu_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    command = payload.get("command") or payload.get("text") or ""
    if not command:
        return {"ok": False, "error": "missing command"}
    try:
        response = CommandExecutor(db, settings, notifier()).execute(command, channel="feishu")
        return {"ok": True, "response": response}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    registry = ProviderRegistry(db, settings)
    registry.initialize()
    rows = registry.list_all()
    current = registry.get_default()
    views = []
    for row in rows:
        view = provider_view(row, registry.models_for(row.id))
        normalized = normalize_ui_api_format(row.provider_type)
        view["api_format_label"] = API_FORMAT_LABELS.get(normalized, "旧版 / 高级 Provider")
        view["legacy_format"] = normalized is None
        view["key_status"] = "不需要" if not provider_requires_api_key(row.provider_type, row.base_url) else ("已配置" if row.api_key_configured_status else "未配置")
        views.append(view)
    browser_row = db.scalar(select(Setting).where(Setting.key == "browser_channel"))
    browser_channel = browser_row.value_json if browser_row else settings.browser.get("channel", "chrome")
    return templates.TemplateResponse(request, "settings.html", {
        "config": settings, "paused": PolicyEngine(db, settings).is_paused(),
        "providers": views, "current_provider": provider_view(current) if current else None,
        "browser_channel": browser_channel,
        "message": request.query_params.get("message", ""), "error": request.query_params.get("error", ""),
    })


@app.post("/settings/browser")
def update_browser_channel(browser_channel: str = Form("chrome"), db: Session = Depends(get_db)):
    if browser_channel not in {"chrome", "msedge", "chromium"}:
        return settings_redirect(error="浏览器选择无效。")
    row = db.scalar(select(Setting).where(Setting.key == "browser_channel"))
    if row:
        row.value_json = browser_channel
    else:
        db.add(Setting(key="browser_channel", value_json=browser_channel))
    db.commit()
    AuditRepository(db).record("settings.browser_changed", "success", target_type="settings", output_summary=browser_channel)
    return settings_redirect(message="浏览器选择已更新。")


@app.post("/settings/ai-provider")
def update_ai_provider(provider: str = Form(""), db: Session = Depends(get_db)):
    try:
        registry = ProviderRegistry(db, settings)
        registry.initialize()
        row = registry.get_by_name(provider)
        if not row:
            raise ValueError("Provider not found")
        registry.set_default(row.id)
        AuditRepository(db).record("settings.ai_provider_changed", "success", target_type="ai_provider", target_id=row.id, output_summary=row.name)
        return settings_redirect(message="默认 AI Provider 已更新")
    except ValueError as exc:
        AuditRepository(db).record("settings.ai_provider_changed", "blocked", target_type="settings", input_summary=provider, error_message=str(exc))
        return settings_redirect(error=str(exc))


API_FORMAT_LABELS = {
    "chat_completions": "Chat Completions (/chat/completions)",
    "anthropic_messages": "Anthropic Messages (/v1/messages)",
    "responses": "Responses (/responses)",
}


def _provider_view_for_form(registry: ProviderRegistry, row):
    view = provider_view(row, registry.models_for(row.id))
    normalized = normalize_ui_api_format(row.provider_type)
    view["stored_provider_type"] = row.provider_type
    view["provider_type"] = normalized or row.provider_type
    view["legacy_format"] = normalized is None
    view["key_status"] = "不需要" if not provider_requires_api_key(row.provider_type, row.base_url) else ("已配置" if row.api_key_configured_status else "未配置")
    view["request_url"] = build_endpoint_url(row.base_url, normalized or row.provider_type)
    view["resolved_auth_scheme"] = resolve_auth_scheme(row.auth_scheme, row.base_url) if normalized == "anthropic_messages" else row.auth_scheme
    return view


def _provider_input(
    display_name: str, provider_type: str, base_url: str, models_text: str, default_model_id: str, api_key_env: str,
    supports_json_mode: bool, supports_streaming: bool, supports_vision: bool, supports_tools: bool,
    extra_headers_json: str, extra_body_json: str, notes: str, timeout_seconds,
    max_output_tokens: str, temperature_default: str, auth_scheme: str,
) -> ProviderInput:
    if not provider_type:
        raise ValueError("请选择 API 格式")
    if provider_type != "mock" and not base_url.strip():
        raise ValueError("请填写请求地址 Base URL")
    try:
        parsed_timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("请求超时必须是数字") from None
    try:
        parsed_temperature = float(temperature_default or "0.6")
    except ValueError:
        raise ValueError("默认温度必须是数字") from None
    if not 0 <= parsed_temperature <= 2:
        raise ValueError("默认温度必须在 0 到 2 之间")
    return ProviderInput(
        display_name=display_name, provider_type=provider_type, base_url=base_url,
        api_key_env=api_key_env, models_text=models_text, default_model_id=default_model_id,
        supports_json_mode=supports_json_mode, supports_streaming=supports_streaming,
        supports_vision=supports_vision, supports_tools=supports_tools,
        extra_headers_json=extra_headers_json, extra_body_json=extra_body_json,
        notes=notes, timeout_seconds=parsed_timeout,
        max_output_tokens=int(max_output_tokens) if max_output_tokens.strip() else None,
        temperature_default=str(parsed_temperature),
        auth_scheme=auth_scheme,
    )


def _provider_form_context(request: Request, db: Session, *, provider=None, form_data=None, error: str = "", message: str = "", status_code: int = 200):
    registry = ProviderRegistry(db, settings)
    presets = {
        key: {field: value for field, value in preset.items() if field != "api_key_env"}
        for key, preset in settings.ai.get("presets", {}).items()
        if key in {"openai", "deepseek", "qwen_dashscope", "moonshot_kimi", "glm", "doubao_ark", "openrouter", "siliconflow", "anthropic", "openmodel"}
    }
    context = {
        "provider": provider, "form": form_data or {}, "error": error, "message": message,
        "api_formats": API_FORMAT_LABELS, "presets": presets,
        "models": registry.models_for(provider["id"]) if provider else [],
    }
    return templates.TemplateResponse(request, "provider_form.html", context, status_code=status_code)


@app.get("/providers/new", response_class=HTMLResponse)
def new_provider_page(request: Request, db: Session = Depends(get_db)):
    return _provider_form_context(request, db)


@app.post("/providers")
def add_provider(
    request: Request, display_name: str = Form(""), provider_type: str = Form(""), base_url: str = Form(""),
    api_key: str = Form(""), models_text: str = Form(""), default_model_id: str = Form(""),
    supports_json_mode: bool = Form(False),
    supports_streaming: bool = Form(False), supports_vision: bool = Form(False), supports_tools: bool = Form(False),
    extra_headers_json: str = Form("{}"), extra_body_json: str = Form("{}"), notes: str = Form(""),
    timeout_seconds: str = Form("60"), max_output_tokens: str = Form(""), temperature_default: str = Form("0.6"),
    auth_scheme: str = Form("auto"),
    db: Session = Depends(get_db),
):
    safe_form = {"display_name": display_name, "provider_type": provider_type, "base_url": base_url, "models_text": models_text, "default_model_id": default_model_id, "supports_json_mode": supports_json_mode, "supports_streaming": supports_streaming, "supports_vision": supports_vision, "supports_tools": supports_tools, "extra_headers_json": extra_headers_json, "extra_body_json": extra_body_json, "notes": notes, "timeout_seconds": timeout_seconds, "max_output_tokens": max_output_tokens, "temperature_default": temperature_default, "auth_scheme": auth_scheme}
    try:
        registry = ProviderRegistry(db, settings)
        api_key_env = generate_api_key_env(display_name)
        data = _provider_input(display_name, provider_type, base_url, models_text, default_model_id, api_key_env, supports_json_mode, supports_streaming, supports_vision, supports_tools, extra_headers_json, extra_body_json, notes, timeout_seconds, max_output_tokens, temperature_default, auth_scheme)
        registry.validate_input(data)
        if provider_requires_api_key(provider_type, base_url) and not api_key and not os.getenv(api_key_env):
            raise ValueError("该 API 格式需要填写 API Key")
        if api_key:
            write_api_key(api_key_env, api_key, ROOT / ".env")
        row = registry.create(data)
        AuditRepository(db).record("ai_provider.created", "success", target_type="ai_provider", target_id=row.id, output_summary=row.name)
        return _provider_form_context(request, db, provider=_provider_view_for_form(registry, row), message="保存成功")
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        AuditRepository(db).record("ai_provider.created", "failed", target_type="ai_provider", error_message=str(exc))
        return _provider_form_context(request, db, form_data=safe_form, error=str(exc), status_code=400)


@app.get("/providers/{provider_id}/edit", response_class=HTMLResponse)
def edit_provider_page(provider_id: int, request: Request, db: Session = Depends(get_db)):
    registry = ProviderRegistry(db, settings)
    row = registry.get(provider_id)
    if not row:
        return settings_redirect(error="Provider 不存在")
    return _provider_form_context(request, db, provider=_provider_view_for_form(registry, row))


@app.post("/providers/{provider_id}")
def edit_provider(
    provider_id: int, request: Request, display_name: str = Form(""), provider_type: str = Form(""), base_url: str = Form(""),
    api_key: str = Form(""), models_text: str = Form(""), default_model_id: str = Form(""), action: str = Form("save"), supports_json_mode: bool = Form(False),
    supports_streaming: bool = Form(False), supports_vision: bool = Form(False), supports_tools: bool = Form(False),
    extra_headers_json: str = Form("{}"), extra_body_json: str = Form("{}"), notes: str = Form(""),
    timeout_seconds: str = Form("60"), max_output_tokens: str = Form(""), temperature_default: str = Form("0.6"),
    auth_scheme: str = Form("auto"),
    db: Session = Depends(get_db),
):
    registry = ProviderRegistry(db, settings)
    existing = registry.get(provider_id)
    if not existing:
        return settings_redirect(error="Provider 不存在")
    safe_form = {"display_name": display_name, "provider_type": provider_type, "base_url": base_url, "models_text": models_text, "default_model_id": default_model_id, "supports_json_mode": supports_json_mode, "supports_streaming": supports_streaming, "supports_vision": supports_vision, "supports_tools": supports_tools, "extra_headers_json": extra_headers_json, "extra_body_json": extra_body_json, "notes": notes, "timeout_seconds": timeout_seconds, "max_output_tokens": max_output_tokens, "temperature_default": temperature_default, "auth_scheme": auth_scheme}
    try:
        api_key_env = existing.api_key_env or generate_api_key_env(display_name)
        data = _provider_input(display_name, provider_type, base_url, models_text, default_model_id, api_key_env, supports_json_mode, supports_streaming, supports_vision, supports_tools, extra_headers_json, extra_body_json, notes, timeout_seconds, max_output_tokens, temperature_default, auth_scheme)
        registry.validate_input(data)
        if provider_requires_api_key(provider_type, base_url) and not api_key and not os.getenv(api_key_env):
            raise ValueError("该 API 格式需要 API Key；请输入 Key，或确认本地 .env 已配置")
        if api_key:
            write_api_key(api_key_env, api_key, ROOT / ".env")
        row = registry.update(provider_id, data)
        AuditRepository(db).record("ai_provider.updated", "success", target_type="ai_provider", target_id=row.id)
        view = _provider_view_for_form(registry, row)
        if action == "test":
            try:
                _execute_connection_test(registry, row, row.default_model_id, db)
                return _provider_form_context(request, db, provider=view, message=f"连接成功，模型 {row.default_model_id} 可用。")
            except Exception as test_error:
                AuditRepository(db).record("ai_provider.test_connection", "failed", target_type="ai_provider", target_id=row.id, error_message=str(test_error))
                return _provider_form_context(request, db, provider=view, error=f"连接失败：{friendly_connection_error(test_error)}", status_code=400)
        return _provider_form_context(request, db, provider=view, message="保存成功")
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        AuditRepository(db).record("ai_provider.updated", "failed", target_type="ai_provider", target_id=provider_id, error_message=str(exc))
        safe_provider = _provider_view_for_form(registry, existing)
        safe_provider.update(safe_form)
        return _provider_form_context(request, db, provider=safe_provider, error=str(exc), status_code=400)


@app.post("/providers/{provider_id}/set-default")
def set_default_provider(provider_id: int, db: Session = Depends(get_db)):
    try:
        row = ProviderRegistry(db, settings).set_default(provider_id)
        AuditRepository(db).record("ai_provider.set_default", "success", target_type="ai_provider", target_id=row.id)
        return settings_redirect(message="默认 Provider 已更新")
    except (LookupError, ValueError) as exc:
        return settings_redirect(error=str(exc))


@app.post("/providers/{provider_id}/toggle")
def toggle_provider(provider_id: int, db: Session = Depends(get_db)):
    registry = ProviderRegistry(db, settings)
    row = registry.get(provider_id)
    if not row:
        return settings_redirect(error="Provider 不存在")
    try:
        target_enabled = not row.enabled
        registry.set_enabled(provider_id, target_enabled)
        AuditRepository(db).record("ai_provider.toggled", "success", target_type="ai_provider", target_id=row.id, output_summary=str(target_enabled))
        return settings_redirect(message="Provider 状态已更新")
    except ValueError as exc:
        return settings_redirect(error=str(exc))


@app.post("/providers/{provider_id}/delete")
def delete_provider(provider_id: int, db: Session = Depends(get_db)):
    try:
        ProviderRegistry(db, settings).delete(provider_id)
        AuditRepository(db).record("ai_provider.deleted", "success", target_type="ai_provider", target_id=provider_id)
        return settings_redirect(message="Provider 已删除")
    except LookupError as exc:
        return settings_redirect(error=str(exc))
    except ValueError as exc:
        return settings_redirect(error=str(exc))


@app.post("/providers/{provider_id}/test")
def test_provider_connection(provider_id: int, db: Session = Depends(get_db), model_id: str = Form("")):
    registry = ProviderRegistry(db, settings)
    row = registry.get(provider_id)
    if not row:
        return settings_redirect(error="Provider 不存在")
    try:
        if not isinstance(model_id, str):
            model_id = ""
        selected_model = model_id or row.default_model_id or row.model_id
        _execute_connection_test(registry, row, selected_model, db)
        return settings_redirect(message="连接成功，模型可用。")
    except Exception as exc:
        AuditRepository(db).record("ai_provider.test_connection", "failed", target_type="ai_provider", target_id=row.id, error_message=str(exc))
        return settings_redirect(error=f"连接失败：{friendly_connection_error(exc)}")


def _execute_connection_test(registry: ProviderRegistry, row, selected_model: str, db: Session) -> None:
    models = [model.model_id for model in registry.models_for(row.id)]
    if selected_model not in models:
        raise ValueError("模型不存在，请检查模型 ID。")
    adapter = create_provider_from_profile(row, settings)
    if hasattr(adapter, "model"):
        adapter.model = selected_model
    if not adapter.test_connection():
        raise AIProviderError("接口返回的不是可解析 JSON")
    AuditRepository(db).record("ai_provider.test_connection", "success", target_type="ai_provider", target_id=row.id, metadata={"model_id": selected_model})


@app.get("/settings/providers/{provider_id}/edit")
def legacy_edit_provider(provider_id: int):
    return RedirectResponse(f"/providers/{provider_id}/edit", status_code=303)
