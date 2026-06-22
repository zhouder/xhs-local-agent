from datetime import datetime

from app.models import Interaction, NoteStatus
from app.services.policy import PolicyEngine


def test_publish_always_requires_approval(db, settings):
    policy = PolicyEngine(db, settings)
    assert not policy.check("publish", note_status=NoteStatus.PENDING_REVIEW).allowed
    assert policy.check("publish", note_status=NoteStatus.APPROVED).allowed


def test_pause_blocks_actions(db, settings):
    policy = PolicyEngine(db, settings)
    policy.set_paused(True)
    assert policy.check("publish", note_status=NoteStatus.APPROVED).reason == "agent_paused"


def test_sensitive_reply_requires_fixed_template(db, settings):
    decision = PolicyEngine(db, settings).check("comment_reply", text="请提供投资建议", now=datetime(2026, 1, 1, 12, 0))
    assert decision.allowed
    assert decision.use_safe_template


def test_limit_and_duplicate_are_enforced(db, settings):
    settings.interaction["daily_like_limit"] = 1
    db.add(Interaction(action_type="like", external_target_id="one", status="success", created_at=datetime(2026, 1, 1, 10, 0)))
    db.commit()
    policy = PolicyEngine(db, settings)
    assert policy.check("like", target_id="one", now=datetime(2026, 1, 1, 12, 0)).reason == "duplicate_interaction"
    assert policy.check("like", target_id="two", now=datetime(2026, 1, 1, 12, 0)).reason == "daily_limit_reached"
