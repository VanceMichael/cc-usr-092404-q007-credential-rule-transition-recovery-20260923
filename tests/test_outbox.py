"""持久待发箱：重启不丢、崩溃回收、至少一次 + 幂等去重。"""

import tempfile
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from skill_engine import schema
from skill_engine.services import outbox, rules
from skill_engine.services.clock import Clock, FixedClock
from tests.conftest import REGION, PTYPE, OLD_VERSION, insert_world, make_draft, old_requirements, run_drill


@pytest.fixture()
def file_engine():
    path = Path(tempfile.mktemp(suffix=".db"))
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    schema.metadata.create_all(eng)
    yield eng
    eng.dispose()
    path.unlink(missing_ok=True)


def test_enqueue_is_idempotent(file_engine):
    clock = Clock()
    with file_engine.begin() as conn:
        assert outbox.enqueue(
            conn, clock=clock, idempotency_key="k:1", recipient="a@x", topic="t",
            subject="s", body="b", payload={},
        ) is True
        assert outbox.enqueue(
            conn, clock=clock, idempotency_key="k:1", recipient="a@x", topic="t",
            subject="s2", body="b2", payload={"x": 1},
        ) is False
    with file_engine.connect() as conn:
        rows = conn.execute(select(schema.notifications)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["subject"] == "s"  # 第二次被丢弃


def test_sender_failure_returns_row_to_pending(file_engine):
    clock = Clock()
    with file_engine.begin() as conn:
        outbox.enqueue(conn, clock=clock, idempotency_key="k:1", recipient="a@x", topic="t",
                       subject="s", body="b", payload={})

    calls = []

    def flaky(item):
        calls.append(item["idempotency_key"])
        raise RuntimeError("gateway down")

    result = outbox.send_pending(file_engine, clock=clock, sender=flaky)
    assert result == {"sent": 0, "failed": 1}
    with file_engine.connect() as conn:
        row = conn.execute(select(schema.notifications)).mappings().one()
    assert row["status"] == "pending"
    assert "gateway down" in row["last_error"]
    assert row["attempts"] == 1
    assert len(calls) == 1

    # 恢复后重投成功：同一收件人只收到一次
    delivered = []
    result = outbox.send_pending(file_engine, clock=clock, sender=delivered.append)
    assert result["sent"] == 1
    assert len(delivered) == 1


def test_stuck_sending_row_is_reclaimed(file_engine):
    """模拟发送进程在置 sending 后崩溃：重启后必须能回收。"""
    clock = Clock()
    with file_engine.begin() as conn:
        outbox.enqueue(conn, clock=clock, idempotency_key="k:1", recipient="a@x", topic="t",
                       subject="s", body="b", payload={})
        conn.execute(
            schema.notifications.update()
            .where(schema.notifications.c.idempotency_key == "k:1")
            .values(status="sending", attempts=1)
        )
    delivered = []
    result = outbox.send_pending(file_engine, clock=clock, sender=delivered.append)
    assert result["sent"] == 1
    assert len(delivered) == 1


def test_drill_decisions_persist_across_engine_restart():
    """模拟进程重启：新引擎指向同一文件，待发决定仍可投递且不重复。"""
    path = Path(tempfile.mktemp(suffix=".db"))
    url = f"sqlite:///{path}"
    clock = FixedClock(date(2026, 9, 1))

    eng = create_engine(url, connect_args={"check_same_thread": False})
    schema.metadata.create_all(eng)
    insert_world(eng)
    rules.bootstrap_timeline(
        eng, clock=clock, region=REGION, project_type=PTYPE, version=OLD_VERSION,
        start_on=date(2024, 1, 1), personnel_requirements=old_requirements(),
    )
    draft = make_draft(eng, clock)
    run = run_drill(eng, clock, draft["draft_id"])
    eng.dispose()

    # “重启”
    eng2 = create_engine(url, connect_args={"check_same_thread": False})
    stats = outbox.stats(eng2)
    assert stats["pending"] == run["total_projects"]
    delivered: list[dict] = []
    outbox.send_pending(eng2, clock=clock, sender=delivered.append)
    outbox.send_pending(eng2, clock=clock, sender=delivered.append)  # 重复 drain
    # 每个项目管理员恰好收到一封
    recipients = [d["recipient"] for d in delivered]
    assert len(recipients) == run["total_projects"]
    assert len(set(recipients)) == len(recipients)
    eng2.dispose()
    path.unlink(missing_ok=True)


def test_timeline_change_fans_out_to_matching_subscribers_once(world, clock):
    from tests.conftest import approve_and_publish

    draft = make_draft(world, clock)
    approve_and_publish(world, clock, draft["draft_id"])

    with world.connect() as conn:
        rows = conn.execute(
            select(schema.notifications.c.recipient)
            .where(schema.notifications.c.topic == "rule_timeline")
        ).all()
    recipients = sorted(r[0] for r in rows)
    # 全域订阅 + 华东订阅收到；华北订阅不收
    assert recipients == ["compliance@example.com", "east@example.com"]


def test_repeated_extend_creates_distinct_events_but_no_duplicate_recipient(world, clock):
    rules.emergency_extend(
        world, clock=clock, actor="u-approver", region=REGION, project_type=PTYPE,
        new_end_on=date(2027, 1, 1), reason="第一次",
    )
    rules.emergency_extend(
        world, clock=clock, actor="u-approver", region=REGION, project_type=PTYPE,
        new_end_on=date(2027, 6, 1), reason="第二次",
    )
    with world.connect() as conn:
        rows = conn.execute(
            select(schema.notifications)
            .where(schema.notifications.c.topic == "rule_timeline")
        ).mappings().all()
    # 两次延期是两个独立决定：每个订阅者各收两封，但同一事件不重复
    east = [r for r in rows if r["recipient"] == "east@example.com"]
    assert len(east) == 2
    keys = [r["idempotency_key"] for r in rows]
    assert len(keys) == len(set(keys))
