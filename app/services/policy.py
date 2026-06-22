from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Interaction, NoteStatus, Setting


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    use_safe_template: bool = False


class PolicyEngine:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def is_paused(self) -> bool:
        row = self.db.scalar(select(Setting).where(Setting.key == "agent_paused"))
        return bool(row and row.value_json.lower() == "true")

    def set_paused(self, paused: bool) -> None:
        row = self.db.scalar(select(Setting).where(Setting.key == "agent_paused"))
        if row:
            row.value_json = str(paused).lower()
        else:
            self.db.add(Setting(key="agent_paused", value_json=str(paused).lower()))
        self.db.commit()

    def _keyword_matches(self, text: str, key: str) -> list[str]:
        folded = text.casefold()
        return [word for word in self.settings.interaction.get(key, []) if word.casefold() in folded]

    def _in_work_hours(self, now: datetime) -> bool:
        window = self.settings.interaction["allowed_work_hours"]
        start = time.fromisoformat(window["start"])
        end = time.fromisoformat(window["end"])
        current = now.time().replace(second=0, microsecond=0)
        return start <= current <= end if start <= end else current >= start or current <= end

    def check(self, action: str, *, text: str = "", target_id: str = "", note_status: str | None = None, now: datetime | None = None) -> PolicyDecision:
        if self.is_paused():
            return PolicyDecision(False, "agent_paused")
        if self._keyword_matches(text, "blacklist_keywords"):
            return PolicyDecision(False, "blacklist_keyword")
        if self._keyword_matches(text, "sensitive_keywords"):
            return PolicyDecision(action in {"comment_reply", "dm_reply"}, "sensitive_content", True)
        if action == "publish":
            if note_status != NoteStatus.APPROVED:
                return PolicyDecision(False, "human_approval_required")
            return PolicyDecision(True, "approved_by_human")
        if action in {"like", "comment", "comment_reply", "dm_reply"}:
            current = now or datetime.now()
            if not self._in_work_hours(current):
                return PolicyDecision(False, "outside_work_hours")
            if target_id and self.db.scalar(select(Interaction.id).where(Interaction.action_type == action, Interaction.external_target_id == target_id)):
                return PolicyDecision(False, "duplicate_interaction")
            limits = {"like": "daily_like_limit", "comment": "daily_comment_limit", "comment_reply": "daily_comment_limit", "dm_reply": "daily_dm_reply_limit"}
            count = self.db.scalar(select(func.count()).select_from(Interaction).where(Interaction.action_type == action, func.date(Interaction.created_at) == current.date().isoformat())) or 0
            if count >= int(self.settings.interaction[limits[action]]):
                return PolicyDecision(False, "daily_limit_reached")
        return PolicyDecision(True, "allowed")
