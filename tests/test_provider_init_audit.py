from sqlalchemy import select

from app import main
from app.models import AuditLog


def test_missing_provider_key_is_audited(db, monkeypatch):
    previous = main.settings.ai["default_provider"]
    main.settings.ai["default_provider"] = "deepseek"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        try:
            main.ai_provider(db)
        except ValueError:
            pass
        row = db.scalar(select(AuditLog).where(AuditLog.action_type == "ai.provider_init"))
        assert row.status == "failed"
        assert row.target_id == "deepseek"
    finally:
        main.settings.ai["default_provider"] = previous
