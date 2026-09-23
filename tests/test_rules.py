"""规则生命周期：职责分离、原子切换、替代关系、撤回/延期竞争。"""

import tempfile
import threading
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from skill_engine import schema
from skill_engine.services.clock import FixedClock
from skill_engine.services import rules
from skill_engine.services.errors import ConflictError, PermissionDeniedError, ValidationError
from tests.conftest import NEW_VERSION, OLD_VERSION, insert_world, make_draft, new_requirements, old_requirements


@pytest.fixture()
def file_world(clock):
    """并发测试必须用文件库：:memory: 会按线程给出独立数据库。"""
    path = Path(tempfile.mktemp(suffix=".db"))
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    schema.metadata.create_all(eng)
    insert_world(eng)
    rules.bootstrap_timeline(
        eng, clock=clock, region="华东", project_type="光伏", version=OLD_VERSION,
        start_on=date(2024, 1, 1), personnel_requirements=old_requirements(),
    )
    yield eng
    eng.dispose()
    path.unlink(missing_ok=True)


def _submit(eng, clock, draft_id, *, maintainer="u-maintainer"):
    return rules.submit_for_approval(
        eng, clock=clock, maintainer=maintainer, draft_id=draft_id
    )


def test_maintainer_cannot_self_approve(world, clock):
    draft = make_draft(world, clock)
    _submit(world, clock, draft["draft_id"])
    with pytest.raises(PermissionDeniedError):
        rules.approve_draft(world, clock=clock, approver="u-maintainer", draft_id=draft["draft_id"])


def test_non_maintainer_cannot_create_or_submit(world, clock):
    with pytest.raises(PermissionDeniedError):
        make_draft(world, clock, maintainer="u-other")
    draft = make_draft(world, clock)
    with pytest.raises(PermissionDeniedError):
        _submit(world, clock, draft["draft_id"], maintainer="u-other")


def test_only_approved_draft_can_publish(world, clock):
    draft = make_draft(world, clock)
    with pytest.raises(ConflictError):
        rules.publish_draft(world, clock=clock, actor="u-approver", draft_id=draft["draft_id"])
    _submit(world, clock, draft["draft_id"])
    with pytest.raises(ConflictError):
        rules.publish_draft(world, clock=clock, actor="u-approver", draft_id=draft["draft_id"])


def test_publish_is_atomic_cutover(world, clock):
    draft = make_draft(world, clock)
    _submit(world, clock, draft["draft_id"])
    rules.approve_draft(world, clock=clock, approver="u-approver", draft_id=draft["draft_id"])
    timeline = rules.publish_draft(
        world, clock=clock, actor="u-approver", draft_id=draft["draft_id"]
    )

    v1, v2 = timeline["segments"]
    assert v1["version"] == OLD_VERSION and v1["status"] == "superseded"
    assert v1["end_on"] == "2026-10-14"  # 生效日前一天
    assert v2["version"] == NEW_VERSION and v2["status"] == "current"
    assert v2["start_on"] == "2026-10-15" and v2["end_on"] is None
    assert v2["supersedes_version"] == OLD_VERSION
    assert timeline["current_published_id"] == v2["published_id"]

    # 事件账本记录唯一、有序
    assert [e["event_type"] for e in timeline["events"]] == ["bootstrapped", "activated"]
    assert [e["seq"] for e in timeline["events"]] == [1, 2]

    # 时间点查询：切换日前 v1、切换日起 v2
    with world.connect() as conn:
        assert rules.get_current_rule(conn, "华东", "光伏", date(2026, 10, 14))["version"] == "v1"
        assert rules.get_current_rule(conn, "华东", "光伏", date(2026, 10, 15))["version"] == "v2"


def test_publish_rejects_wrong_supersedes_version(world, clock):
    draft = make_draft(world, clock, supersedes="v0")
    _submit(world, clock, draft["draft_id"])
    rules.approve_draft(world, clock=clock, approver="u-approver", draft_id=draft["draft_id"])
    with pytest.raises(ConflictError, match="替代"):
        rules.publish_draft(world, clock=clock, actor="u-approver", draft_id=draft["draft_id"])


def test_publish_rejects_duplicate_version(world, clock):
    draft = make_draft(world, clock, new_version=OLD_VERSION)
    _submit(world, clock, draft["draft_id"])
    rules.approve_draft(world, clock=clock, approver="u-approver", draft_id=draft["draft_id"])
    with pytest.raises(ConflictError, match="版本号已存在"):
        rules.publish_draft(world, clock=clock, actor="u-approver", draft_id=draft["draft_id"])


def test_withdraw_closes_timeline_and_extend_reopens(world, clock):
    timeline = rules.withdraw_rule(
        world, clock=clock, actor="u-approver", region="华东", project_type="光伏",
        withdraw_on=date(2026, 11, 1), reason="暂停",
    )
    assert timeline["current_published_id"] is None
    assert timeline["tail_end_on"] == "2026-11-01"
    seg = timeline["segments"][0]
    assert seg["status"] == "withdrawn" and seg["end_on"] == "2026-11-01"

    timeline = rules.emergency_extend(
        world, clock=clock, actor="u-approver", region="华东", project_type="光伏",
        new_end_on=date(2027, 3, 1), reason="紧急延期",
    )
    assert timeline["current_published_id"] == seg["published_id"]
    assert timeline["tail_end_on"] == "2027-03-01"
    types = [e["event_type"] for e in timeline["events"]]
    assert types == ["bootstrapped", "withdrew", "extended"]


def test_extend_must_push_end_forward(world, clock):
    rules.withdraw_rule(
        world, clock=clock, actor="u-approver", region="华东", project_type="光伏",
        withdraw_on=date(2026, 11, 1),
    )
    with pytest.raises(ConflictError):
        rules.emergency_extend(
            world, clock=clock, actor="u-approver", region="华东", project_type="光伏",
            new_end_on=date(2026, 10, 1),
        )


def test_draft_validation_rejects_bad_content(world, clock):
    with pytest.raises(ValidationError):
        rules.create_draft(
            world, clock=clock, maintainer="u-maintainer",
            content={
                "region": "华东", "project_type": "光伏", "new_version": "v2",
                "effective_on": "2026-10-15", "supersedes_version": "v1",
                "personnel_requirements": [],
                "grandfather_policy": {"mode": "none"},
            },
        )
    with pytest.raises(ValidationError):
        rules.create_draft(
            world, clock=clock, maintainer="u-maintainer",
            content={
                "region": "华东", "project_type": "光伏", "new_version": "v2",
                "effective_on": "2026-10-15", "supersedes_version": "v1",
                "personnel_requirements": new_requirements(),
                "grandfather_policy": {"mode": "project_start", "grace_days": -1},
            },
        )


def test_revision_bumps_version_and_locks_after_submission(world, clock):
    draft = make_draft(world, clock)
    revised = rules.update_draft(
        world, clock=clock, maintainer="u-maintainer", draft_id=draft["draft_id"],
        content={
            "region": draft["region"], "project_type": draft["project_type"],
            "new_version": "v2.1", "effective_on": draft["effective_on"], "end_on": None,
            "supersedes_version": draft["supersedes_version"],
            "personnel_requirements": draft["personnel_requirements"],
            "grandfather_policy": draft["grandfather_policy"],
            "change_note": "微调",
        },
    )
    assert revised["draft_version"] == 2 and revised["new_version"] == "v2.1"
    _submit(world, clock, draft["draft_id"])
    with pytest.raises(ConflictError):
        rules.update_draft(
            world, clock=clock, maintainer="u-maintainer", draft_id=draft["draft_id"],
            content={
                "region": draft["region"], "project_type": draft["project_type"],
                "new_version": "v2.2", "effective_on": draft["effective_on"], "end_on": None,
                "supersedes_version": draft["supersedes_version"],
                "personnel_requirements": draft["personnel_requirements"],
                "grandfather_policy": draft["grandfather_policy"],
            },
        )


def test_withdraw_and_extend_race_leaves_single_timeline(file_world):
    """撤回与紧急延期并发：只能按提交顺序留下一条事件链。"""
    world = file_world
    clock = FixedClock(date(2026, 9, 1))
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def withdraw():
        barrier.wait()
        try:
            rules.withdraw_rule(
                world, clock=clock, actor="u-approver", region="华东", project_type="光伏",
                withdraw_on=date(2026, 11, 1),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def extend():
        barrier.wait()
        try:
            rules.emergency_extend(
                world, clock=clock, actor="u-approver", region="华东", project_type="光伏",
                new_end_on=date(2026, 12, 1),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=withdraw)
    t2 = threading.Thread(target=extend)
    t1.start(); t2.start(); t1.join(); t2.join()

    timeline = rules.get_timeline(world, "华东", "光伏")
    seqs = [e["seq"] for e in timeline["events"]]
    assert seqs == list(range(1, len(seqs) + 1))  # 账本无空洞
    # 两个竞争操作必须串行成功（先延期的开放段可被撤回；先撤回则延期到 12-01）
    assert [e["event_type"] for e in timeline["events"][-2:]][0] in ("withdrew", "extended")
    # 最终只有一个当前段或明确收尾
    assert timeline["current_published_id"] is None or timeline["tail_end_on"] is not None
    # 段表中至多一个 current
    current = [s for s in timeline["segments"] if s["status"] == "current"]
    assert len(current) <= 1


def test_two_withdrawals_are_serialized(file_world):
    """对同一开放段并发撤回两次：只有一次生效，另一次冲突报错。"""
    world = file_world
    clock = FixedClock(date(2026, 9, 1))
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def do_withdraw(day):
        barrier.wait()
        try:
            rules.withdraw_rule(
                world, clock=clock, actor="u-approver", region="华东", project_type="光伏",
                withdraw_on=day,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=do_withdraw, args=(date(2026, 11, 1),))
    t2 = threading.Thread(target=do_withdraw, args=(date(2026, 11, 2),))
    t1.start(); t2.start(); t1.join(); t2.join()

    timeline = rules.get_timeline(world, "华东", "光伏")
    withdrew = [e for e in timeline["events"] if e["event_type"] == "withdrew"]
    assert len(withdrew) == 1  # 只有一次撤回落账
    assert len(errors) == 1
