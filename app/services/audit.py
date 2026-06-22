from __future__ import annotations

from contextlib import contextmanager

from app.repositories import AuditRepository


@contextmanager
def audited(repo: AuditRepository, action: str, *, target_type: str = "", target_id: str = "", input_summary: str = ""):
    try:
        yield
    except Exception as exc:
        repo.record(action, "failed", target_type=target_type, target_id=target_id, input_summary=input_summary, error_message=str(exc))
        raise
