"""基础接口检查。"""

from litestar.testing import TestClient
from sqlalchemy import create_engine

from skill_engine import create_app


def test_health() -> None:
    with TestClient(create_app(create_engine("sqlite:///:memory:"))) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["storage"] == "sqlite"
