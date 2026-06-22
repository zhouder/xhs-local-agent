from fastapi.testclient import TestClient

from app.main import app


def test_cross_origin_post_is_blocked():
    with TestClient(app) as client:
        response = client.post("/agent/pause", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
