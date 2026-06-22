from sqlalchemy import inspect


EXPECTED_TABLES = {
    "ai_providers", "notes", "media_assets", "comments", "messages",
    "interactions", "review_queue", "audit_logs", "settings",
    "browser_errors", "command_events", "scheduled_jobs", "provider_models",
}


def test_all_required_tables_initialize(db):
    assert set(inspect(db.get_bind()).get_table_names()) == EXPECTED_TABLES
    for table in EXPECTED_TABLES:
        columns = {column["name"] for column in inspect(db.get_bind()).get_columns(table)}
        assert {"id", "created_at", "updated_at"} <= columns
