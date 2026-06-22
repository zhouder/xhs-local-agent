from sqlalchemy import create_engine, inspect, text

from app.database import _migrate_ai_providers


def test_legacy_deepseek_glm_rows_survive_migration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ai_providers (id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL UNIQUE, base_url VARCHAR(500) NOT NULL DEFAULT '', model VARCHAR(120) NOT NULL, api_key_env VARCHAR(120) NOT NULL DEFAULT '', enabled BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME, updated_at DATETIME)"))
        connection.execute(text("INSERT INTO ai_providers (name, base_url, model, api_key_env, created_at, updated_at) VALUES ('deepseek','https://api.deepseek.com','deepseek-chat','DEEPSEEK_API_KEY',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP), ('glm','https://open.bigmodel.cn/api/paas/v4','glm-4','GLM_API_KEY',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    _migrate_ai_providers(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("ai_providers")}
    assert {"display_name", "provider_type", "model_id", "is_default", "extra_headers_json"} <= columns
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT name, model_id, api_key_env FROM ai_providers ORDER BY id")).all()
    assert rows == [("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"), ("glm", "glm-4", "GLM_API_KEY")]
