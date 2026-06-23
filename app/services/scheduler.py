import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Note, NoteStatus, ScheduledJob
from app.repositories import AuditRepository
from app.services.notifications import Notifier
from app.services.policy import PolicyEngine
from app.services.publish import PublishService


class SchedulerGuard:
    def __init__(self, policy: PolicyEngine):
        self.policy = policy

    def may_run(self, action: str) -> tuple[bool, str]:
        decision = self.policy.check(action)
        return decision.allowed, decision.reason


class PublishScheduler:
    def __init__(self, db: Session, settings, notifier: Notifier):
        self.db = db
        self.settings = settings
        self.notifier = notifier
        self.audit = AuditRepository(db)

    def paused(self) -> bool:
        row = self.db.scalar(select(ScheduledJob).where(ScheduledJob.name == "publish_scheduler"))
        return bool(row and not row.enabled)

    def set_paused(self, paused: bool) -> None:
        row = self.db.scalar(select(ScheduledJob).where(ScheduledJob.name == "publish_scheduler"))
        if not row:
            row = ScheduledJob(name="publish_scheduler", job_type="publish", schedule_json=json.dumps({"times": self.settings.publish.get("default_publish_times", [])}), enabled=True)
            self.db.add(row)
        row.enabled = not paused
        row.last_status = "paused" if paused else "enabled"
        self.db.commit()
        self.audit.record("scheduler.paused" if paused else "scheduler.resumed", "success", target_type="scheduler")

    def run_once(self) -> int:
        guard = SchedulerGuard(PolicyEngine(self.db, self.settings))
        allowed, reason = guard.may_run("publish")
        if not allowed and reason == "agent_paused":
            self.audit.record("scheduler.run_once", "blocked", target_type="scheduler", output_summary=reason)
            return 0
        if self.paused():
            self.audit.record("scheduler.run_once", "blocked", target_type="scheduler", output_summary="scheduler_paused")
            return 0
        notes = list(self.db.scalars(select(Note).where(Note.status == NoteStatus.APPROVED.value).order_by(Note.approved_at, Note.id).limit(1)))
        count = 0
        for note in notes:
            try:
                PublishService(self.db, self.settings, self.notifier).fill(note.id, mode="fill_only")
                count += 1
            except Exception as exc:
                self.audit.record("scheduler.fill", "failed", target_type="note", target_id=note.id, error_message=str(exc))
        self.audit.record("scheduler.run_once", "success", target_type="scheduler", output_summary=f"filled={count}", metadata={"at": datetime.now().isoformat(timespec="seconds")})
        return count
