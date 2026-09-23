"""测试夹具：临时文件库 + 已建表 + HTTP 客户端。"""

import pytest
from litestar.testing import TestClient

from skill_engine import create_app
from skill_engine.models import metadata


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.sqlite3"))
    from skill_engine.database import create_database_engine

    eng = create_database_engine()
    metadata.create_all(eng)
    return eng


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine)) as c:
        yield c
