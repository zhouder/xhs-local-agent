from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CommandEvent, Note, NoteStatus
from app.repositories import AuditRepository
from app.services.notifications import Notifier
from app.services.policy import PolicyEngine
from app.services.publish import PublishService
from app.services.review import ReviewService


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    note_id: int | None = None


ALLOWED = {"status", "drafts", "pending", "approve", "reject", "waiting", "final_confirm", "cancel", "pause", "resume", "publish_now"}


def parse_command(text: str) -> ParsedCommand:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        raise ValueError("Command must start with /")
    name = parts[0][1:].lower()
    if name not in ALLOWED:
        raise ValueError("Unsupported command")
    needs_id = name in {"approve", "reject", "final_confirm", "cancel", "publish_now"}
    if needs_id and len(parts) != 2:
        raise ValueError("This command requires exactly one note_id")
    if not needs_id and len(parts) != 1:
        raise ValueError("This command takes no arguments")
    return ParsedCommand(name, int(parts[1]) if needs_id else None)


class CommandExecutor:
    def __init__(self, db: Session, settings, notifier: Notifier):
        self.db = db
        self.settings = settings
        self.notifier = notifier
        self.audit = AuditRepository(db)

    def execute(self, text: str, *, channel: str = "mock") -> str:
        try:
            parsed = parse_command(text)
            response = self._execute(parsed)
            status = "success"
            return response
        except Exception as exc:
            parsed = ParsedCommand("invalid", None)
            with suppress_parse(text) as name:
                parsed = ParsedCommand(name, None)
            response = str(exc)
            status = "failed"
            self.audit.record("command.execute", "failed", target_type="command", input_summary=text, error_message=response)
            raise
        finally:
            self.db.add(CommandEvent(
                channel=channel,
                command=parsed.name,
                arguments_json=json.dumps({"note_id": parsed.note_id}, ensure_ascii=False),
                status=status,
                response=response,
            ))
            self.db.commit()
            if status == "success":
                self.audit.record("command.execute", "success", target_type="command", input_summary=text, output_summary=response[:500])

    def _execute(self, parsed: ParsedCommand) -> str:
        if parsed.name == "status":
            counts = {status.value: self.db.scalar(select(func.count()).select_from(Note).where(Note.status == status.value)) or 0 for status in NoteStatus}
            paused = PolicyEngine(self.db, self.settings).is_paused()
            return f"paused={paused}; " + ", ".join(f"{key}={value}" for key, value in counts.items())
        if parsed.name in {"drafts", "pending", "waiting"}:
            target = {
                "drafts": NoteStatus.DRAFT,
                "pending": NoteStatus.PENDING_REVIEW,
                "waiting": NoteStatus.WAITING_FINAL_CONFIRM,
            }[parsed.name]
            notes = list(self.db.scalars(select(Note).where(Note.status == target.value).order_by(Note.id).limit(10)))
            return "\n".join(f"#{note.id} {note.title} [{note.status}]" for note in notes) or "empty"
        if parsed.name == "approve":
            ReviewService(self.db, self.notifier).approve(parsed.note_id or 0)
            return f"approved note_id={parsed.note_id}; not published"
        if parsed.name == "reject":
            ReviewService(self.db, self.notifier).reject(parsed.note_id or 0, "rejected by command")
            return f"rejected note_id={parsed.note_id}"
        if parsed.name == "final_confirm":
            note = self.db.get(Note, parsed.note_id)
            if not note or note.status != NoteStatus.WAITING_FINAL_CONFIRM or not note.publish_screenshot_path:
                raise ValueError("final_confirm requires waiting_final_confirm status and a fill screenshot.")
            screenshot = PublishService(self.db, self.settings, self.notifier).final_confirm(parsed.note_id or 0)
            return f"final confirm executed; screenshot={screenshot}; status=publish_uncertain"
        if parsed.name == "cancel":
            PublishService(self.db, self.settings, self.notifier).cancel(parsed.note_id or 0)
            return f"cancelled note_id={parsed.note_id}"
        if parsed.name == "pause":
            PolicyEngine(self.db, self.settings).set_paused(True)
            return "paused"
        if parsed.name == "resume":
            PolicyEngine(self.db, self.settings).set_paused(False)
            return "resumed"
        if parsed.name == "publish_now":
            raise PermissionError("publish_now is intentionally disabled; use fill_only and final_confirm.")
        raise ValueError("Unsupported command")


class suppress_parse:
    def __init__(self, text: str):
        self.text = text

    def __enter__(self):
        parts = self.text.strip().split()
        return parts[0].lstrip("/").lower() if parts else "invalid"

    def __exit__(self, exc_type, exc, tb):
        return True
