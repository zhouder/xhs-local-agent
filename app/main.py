from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.ai.factory import create_provider_from_profile
from app.ai.anthropic import resolve_auth_scheme
from app.ai.endpoints import build_endpoint_url, normalize_ui_api_format
from app.ai.errors import friendly_connection_error
from app.ai.openai_compatible import AIProviderError
from app.browser.xhs import XHSBrowser
from app.config import ROOT, get_settings
from app.database import SessionLocal, get_db, init_db
from app.models import AuditLog, BrowserError, Comment, Interaction, Message, NoteStatus
from app.repositories import AuditRepository, NoteRepository
from app.schemas import GenerateNoteRequest, NoteUpdate
from app.services.notes import NoteService
from app.services.notifications import create_notifier
from app.services.policy import PolicyEngine
from app.services.review import ReviewService
from app.security import generate_api_key_env, write_api_key
from app.services.provider_registry import ProviderInput, ProviderRegistry, provider_requires_api_key, provider_view


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()
templates = Jinja2Templates(directory=ROOT / "app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="XHS Local Growth Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")


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
    recent = list(db.scalars(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(10)))
    return templates.TemplateResponse(request, "dashboard.html", {"notes": notes[:5], "counts": counts, "paused": paused, "audit_logs": recent})


@app.get("/notes", response_class=HTMLResponse)
def notes_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "notes.html", {"notes": NoteRepository(db).list(), "message": request.query_params.get("message", "")})


@app.post("/notes/generate")
def generate_note(
    topic: str = Form(...), style: str = Form("实用、自然"), audience: str = Form("科技爱好者"),
    min_length: int = Form(200), max_length: int = Form(600),
    controversial_title: bool = Form(False), educational: bool = Form(False),
    growth_oriented: bool = Form(False), db: Session = Depends(get_db),
):
    try:
        request_data = GenerateNoteRequest(
            topic=topic, style=style, audience=audience,
            min_length=min_length, max_length=max_length,
            controversial_title=controversial_title, educational=educational,
            growth_oriented=growth_oriented,
        )
        note = NoteService(db, ai_provider(db)).generate(request_data)
        return redirect(f"/notes/{note.id}")
    except AIProviderError as exc:
        raise HTTPException(502, "AI provider call failed; see audit log") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, "Draft generation failed; see audit log") from exc


@app.get("/notes/{note_id}", response_class=HTMLResponse)
def note_page(note_id: int, request: Request, db: Session = Depends(get_db)):
    repo = NoteRepository(db)
    note = repo.get(note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    return templates.TemplateResponse(request, "note_detail.html", {"note": note, "hashtags": ", ".join(json.loads(note.hashtags_json)), "media_paths": repo.media_paths(note_id), "message": request.query_params.get("message", "")})


@app.post("/notes/{note_id}/edit")
def edit_note(note_id: int, title: str = Form(...), body: str = Form(...), hashtags: str = Form(""), cover_prompt: str = Form(""), media_path: str = Form(""), db: Session = Depends(get_db)):
    try:
        update = NoteUpdate(title=title, body=body, hashtags=[x.strip().lstrip("#") for x in hashtags.split(",") if x.strip()], cover_prompt=cover_prompt, media_path=media_path)
        NoteService(db, None).update(note_id, update)
        return redirect(f"/notes/{note_id}", "已保存；如原草稿已提交或批准，审核现已失效并重置为 draft")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/notes/{note_id}/submit")
def submit_note(note_id: int, db: Session = Depends(get_db)):
    try:
        ReviewService(db, notifier()).submit(note_id)
        return redirect(f"/notes/{note_id}", "已提交审核")
    except (LookupError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/notes/{note_id}/approve")
def approve_note(note_id: int, confirm: str = Form(""), db: Session = Depends(get_db)):
    if confirm != "APPROVE":
        raise HTTPException(400, "Approval confirmation missing")
    try:
        ReviewService(db, notifier()).approve(note_id)
        return redirect(f"/notes/{note_id}", "已批准；仍需单独点击 dry-run，第一阶段不会真实发布")
    except (LookupError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/notes/{note_id}/reject")
def reject_note(note_id: int, reason: str = Form(""), db: Session = Depends(get_db)):
    try:
        ReviewService(db, notifier()).reject(note_id, reason)
        return redirect(f"/notes/{note_id}", "已拒绝")
    except (LookupError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/notes/{note_id}/regenerate")
def regenerate_note(note_id: int, db: Session = Depends(get_db)):
    try:
        NoteService(db, ai_provider(db)).regenerate(note_id)
        return redirect(f"/notes/{note_id}", "已重新生成")
    except (LookupError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "AI regeneration failed; see audit log") from exc


@app.post("/notes/{note_id}/dry-run")
def dry_run(note_id: int, db: Session = Depends(get_db)):
    try:
        XHSBrowser(db, settings, notifier()).fill_approved_note(note_id, dry_run=True)
        return redirect(f"/notes/{note_id}", "Dry-run 已结束，未点击发布")
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/agent/{action}")
def agent_state(action: str, db: Session = Depends(get_db)):
    if action not in {"pause", "resume"}:
        raise HTTPException(404)
    paused = action == "pause"
    PolicyEngine(db, settings).set_paused(paused)
    AuditRepository(db).record(f"agent.{action}", "success", target_type="agent")
    return redirect("/", "")


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    rows = list(db.scalars(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(500)))
    return templates.TemplateResponse(request, "audit.html", {"rows": rows})


@app.get("/records/{kind}", response_class=HTMLResponse)
def records_page(kind: str, request: Request, db: Session = Depends(get_db)):
    models = {"comments": Comment, "messages": Message, "interactions": Interaction, "errors": BrowserError}
    model = models.get(kind)
    if not model:
        raise HTTPException(404)
    rows = list(db.scalars(select(model).order_by(desc(model.created_at)).limit(500)))
    return templates.TemplateResponse(request, "records.html", {"kind": kind, "rows": rows})


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
    return templates.TemplateResponse(request, "settings.html", {
        "config": settings, "paused": PolicyEngine(db, settings).is_paused(),
        "providers": views, "current_provider": provider_view(current) if current else None,
        "message": request.query_params.get("message", ""), "error": request.query_params.get("error", ""),
    })


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
