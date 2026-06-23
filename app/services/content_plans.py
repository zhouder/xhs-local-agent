from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.base import AIProviderAdapter
from app.models import ContentPlan, ContentPlanTopic, Note, ScheduledPublishSlot
from app.repositories import AuditRepository, NoteRepository
from app.schemas import GenerateNoteRequest


class ContentPlanService:
    def __init__(self, db: Session, provider: AIProviderAdapter | None = None):
        self.db = db
        self.provider = provider
        self.audit = AuditRepository(db)
        self.notes = NoteRepository(db)

    def create_plan(
        self,
        *,
        name: str,
        audience: str,
        style: str,
        goal: str,
        topics_text: str,
        daily_count: int = 1,
        publish_times_text: str = "10:30\n20:30",
    ) -> ContentPlan:
        topics = [line.strip() for line in topics_text.splitlines() if line.strip()]
        if not name.strip():
            raise ValueError("Plan name is required.")
        if not topics:
            raise ValueError("At least one topic is required.")
        plan = ContentPlan(
            name=name.strip(),
            audience=audience.strip(),
            style=style.strip(),
            goal=goal.strip(),
            notes_json=json.dumps({"daily_count": daily_count, "publish_times": [x.strip() for x in publish_times_text.splitlines() if x.strip()]}, ensure_ascii=False),
        )
        self.db.add(plan)
        self.db.flush()
        for topic in topics:
            self.db.add(ContentPlanTopic(plan_id=plan.id, topic=topic))
        self.db.commit()
        self.audit.record("content_plan.created", "success", target_type="content_plan", target_id=plan.id, metadata={"topics": len(topics)})
        return plan

    def generate_drafts(self, plan_id: int, *, statuses: set[str] | None = None) -> list[Note]:
        if self.provider is None:
            raise RuntimeError("AI provider is required for batch generation.")
        plan = self.db.get(ContentPlan, plan_id)
        if not plan:
            raise LookupError("Content plan not found")
        statuses = statuses or {"pending"}
        topics = list(self.db.scalars(select(ContentPlanTopic).where(ContentPlanTopic.plan_id == plan_id, ContentPlanTopic.status.in_(statuses)).order_by(ContentPlanTopic.id)))
        created: list[Note] = []
        schedule_cursor = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
        for index, topic in enumerate(topics):
            try:
                request = GenerateNoteRequest(topic=topic.topic, style=plan.style or "practical", audience=plan.audience or "general readers", growth_oriented=plan.goal == "growth")
                content = self.provider.generate_note(request)
                note = self.notes.create(request, content)
                note.content_plan_id = plan.id
                topic.status = "generated"
                topic.note_id = note.id
                self.db.flush()
                self.db.add(ScheduledPublishSlot(note_id=note.id, planned_time=schedule_cursor + timedelta(hours=index)))
                created.append(note)
                self.audit.record("content_plan.topic_generated", "success", target_type="note", target_id=note.id, input_summary=topic.topic)
            except Exception as exc:
                topic.status = "failed"
                topic.error_message = str(exc)
                self.audit.record("content_plan.topic_generated", "failed", target_type="content_plan_topic", target_id=topic.id, error_message=str(exc))
        self.db.commit()
        return created

    def progress(self, plan_id: int) -> dict[str, int]:
        rows = self.db.execute(
            select(ContentPlanTopic.status, func.count()).where(ContentPlanTopic.plan_id == plan_id).group_by(ContentPlanTopic.status)
        )
        return {status: count for status, count in rows}
