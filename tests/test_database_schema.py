from sqlalchemy import inspect


EXPECTED_TABLES = {
    "ai_providers", "notes", "media_assets", "comments", "messages",
    "interactions", "review_queue", "audit_logs", "settings",
    "browser_errors", "command_events", "scheduled_jobs", "provider_models",
    "content_plans", "content_plan_topics", "scheduled_publish_slots",
}


def test_all_required_tables_initialize(db):
    assert set(inspect(db.get_bind()).get_table_names()) == EXPECTED_TABLES
    for table in EXPECTED_TABLES:
        columns = {column["name"] for column in inspect(db.get_bind()).get_columns(table)}
        assert {"id", "created_at", "updated_at"} <= columns


def test_publish_kind_columns_initialize(db):
    note_columns = {column["name"] for column in inspect(db.get_bind()).get_columns("notes")}
    assert {"publish_kind", "text_to_image_prompt", "text_to_image_style", "video_file_path"} <= note_columns
    media_columns = {column["name"] for column in inspect(db.get_bind()).get_columns("media_assets")}
    assert {"asset_type", "file_path", "mime_type", "upload_order", "status"} <= media_columns
