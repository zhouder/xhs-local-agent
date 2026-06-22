from app.services.policy import PolicyEngine
from app.services.scheduler import SchedulerGuard


def test_scheduler_guard_honors_global_pause(db, settings):
    policy = PolicyEngine(db, settings)
    guard = SchedulerGuard(policy)
    assert guard.may_run("generate_content")[0]
    policy.set_paused(True)
    allowed, reason = guard.may_run("generate_content")
    assert not allowed
    assert reason == "agent_paused"
