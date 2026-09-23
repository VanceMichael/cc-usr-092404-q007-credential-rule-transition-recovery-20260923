"""跨引擎重启的断点续跑与真实 Alembic 迁移。"""

import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect

from factories import draft_rule, make_credential, make_person, make_position, make_project


def test_checkpoint_survives_engine_restart(tmp_path):
    """模拟服务重启：换一个全新引擎/进程内连接，演练仍从断点继续，结果不重不漏。"""
    db_path = tmp_path / "restart.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")

    from skill_engine.models import metadata
    from skill_engine import create_app
    from litestar.testing import TestClient

    metadata.create_all(engine)

    with TestClient(create_app(engine)) as client:
        a = make_person(client, name="A", region="华东", person_id="a")
        b = make_person(client, name="B", region="华东", person_id="b")
        make_credential(client, person_id=a, credential_type="C1", issued_at="2025-01-01")
        make_credential(client, person_id=b, credential_type="C2", issued_at="2025-01-01")
        make_project(client, name="P1", region="华东", project_type="光伏",
                     started_at="2026-03-01", project_id="p1")
        make_position(client, project_id="p1", role="安全员", holder_id="a")
        make_project(client, name="P2", region="华东", project_type="光伏",
                     started_at="2026-03-01", project_id="p2")
        make_position(client, project_id="p2", role="安全员", holder_id="b")
        draft = draft_rule(client, body={"requirements": {"安全员": ["C2"]}})
        first = client.post("/dry-runs", json={
            "draft_version_id": draft, "as_of": "2026-09-23", "batch_size": 1,
        }).json()
        assert first["status"] == "running"
        run_id = first["dry_run_id"]

    # “重启”：丢弃旧引擎与连接池，用同一路径新建引擎。
    engine.dispose()
    engine2 = create_engine(f"sqlite:///{db_path}")
    with TestClient(create_app(engine2)) as client:
        resumed = client.post(f"/dry-runs/{run_id}/resume", json={"batch_size": 1})
        assert resumed.status_code == 201
        final = client.get(f"/dry-runs/{run_id}").json()
        assert final["status"] == "completed"
        assert [r["project_id"] for r in final["results"]] == ["p1", "p2"]
        # 待发箱在重启后依旧持久：p1 补员决定仍在。
        pending = client.get("/notifications").json()["pending"]
        assert any(n["payload"]["project_id"] == "p1" for n in pending)


def test_alembic_migrations_build_schema_from_scratch(tmp_path):
    """在空文件库上执行真实迁移链（容器启动路径），所有表必须就位。"""
    db_path = tmp_path / "migrated.sqlite3"
    env = {**os.environ, "DATABASE_PATH": str(db_path)}
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    expected = {
        "service_metadata", "persons", "credentials", "projects", "positions",
        "equivalences", "rule_versions", "rule_scopes", "rule_timeline_events",
        "dry_runs", "dry_run_results", "decision_cache",
        "dry_run_checkpoints", "outbox",
    }
    assert expected <= tables
