from app.services.policy import PolicyEngine


class SchedulerGuard:
    def __init__(self, policy: PolicyEngine):
        self.policy = policy

    def may_run(self, action: str) -> tuple[bool, str]:
        decision = self.policy.check(action)
        return decision.allowed, decision.reason
