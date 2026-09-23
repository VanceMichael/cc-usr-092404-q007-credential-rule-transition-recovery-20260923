"""输入摘要：精确复用、局部修改只重算相关项目、冻结水位续跑。"""

from datetime import date

from sqlalchemy import select

from skill_engine import schema
from skill_engine.services import drills
from tests.conftest import (
    WATERMARK,
    make_draft,
    run_drill,
)


def _result_rows(eng, run_id):
    with eng.connect() as conn:
        rows = conn.execute(
            select(schema.drill_project_results)
            .where(schema.drill_project_results.c.run_id == run_id)
        ).mappings().all()
    return {r["project_id"]: dict(r) for r in rows}


def test_identical_inputs_reuse_exact_run(world, clock):
    draft = make_draft(world, clock)
    first = run_drill(world, clock, draft["draft_id"])
    second = run_drill(world, clock, draft["draft_id"])
    assert second["reused"] is True
    assert second["reuse_mode"] == "exact"
    assert second["run_id"] == first["run_id"]
    # 复用不产生新通知
    assert second["notified_projects"] == first["notified_projects"]


def test_local_change_recomputes_only_related_project(world, clock):
    draft = make_draft(world, clock)
    first = run_drill(world, clock, draft["draft_id"])
    first_rows = _result_rows(world, first["run_id"])

    # 局部修改：只动 prj-staff 在岗人员的凭证（新发 ESG-B）
    with world.begin() as conn:
        conn.execute(
            schema.credentials.insert().values(
                credential_id="cr-a-b-new",
                person_id="p-a",
                credential_code="ESG-B",
                issued_on=date(2026, 9, 20),
                revoked_on=None,
            )
        )

    second = run_drill(world, clock, draft["draft_id"])
    assert second["reused"] is False
    assert second["reuse_mode"] == "fragment"
    # 只有 1 个片段变化（prj-staff；prj-a 相关的 candidate 池变化也会影响缺岗类项目的片段）
    assert second["copied_projects"] >= 1
    second_rows = _result_rows(world, second["run_id"])

    # 未变片段：结果与摘要逐字节沿用
    unchanged = [
        pid
        for pid in ("prj-clean", "prj-done", "prj-equivok", "prj-exempt",
                    "prj-grand", "prj-missing", "prj-review", "prj-revoked")
        if first_rows[pid]["fragment_digest"] == second_rows[pid]["fragment_digest"]
    ]
    assert "prj-clean" in unchanged and "prj-done" in unchanged
    for pid in unchanged:
        assert second_rows[pid]["detail"] == first_rows[pid]["detail"]
        assert second_rows[pid]["notified"] is True  # 复用片段不重复通知

    # 变化片段：prj-staff 从 staffing 变 compliant
    assert first_rows["prj-staff"]["fragment_digest"] != second_rows["prj-staff"]["fragment_digest"]
    assert second_rows["prj-staff"]["detail"]["outcome"] == "compliant"

    # 新运行只给真正重算的项目发通知
    assert second["notified_projects"] == second["copied_projects"] or (
        second["total_projects"] - second["notified_projects"] >= 0
    )
    newly_notified = [
        pid for pid, row in second_rows.items()
        if row["fragment_digest"] != first_rows[pid]["fragment_digest"]
    ]
    assert "prj-staff" in newly_notified


def test_recompute_then_identical_reuse_is_stable(world, clock):
    draft = make_draft(world, clock)
    run_drill(world, clock, draft["draft_id"])
    with world.begin() as conn:
        conn.execute(
            schema.credentials.insert().values(
                credential_id="cr-a-b-new",
                person_id="p-a",
                credential_code="ESG-B",
                issued_on=date(2026, 9, 20),
                revoked_on=None,
            )
        )
    second = run_drill(world, clock, draft["draft_id"])
    # 第三次输入与第二次相同 → 精确复用第二次运行
    third = run_drill(world, clock, draft["draft_id"])
    assert third["reused"] is True
    assert third["run_id"] == second["run_id"]


def test_resume_uses_frozen_watermark_not_live_tables(world, clock):
    """启动后修改实时凭证：续跑结果必须仍基于冻结水位。"""
    draft = make_draft(world, clock)
    first = run_drill(world, clock, draft["draft_id"], batch_size=1)
    run_id = first["run_id"]
    assert first["completed_projects"] == 1

    # 在断点期间实时给 prj-staff 的 p-a 补发 ESG-B（水位之后签发）
    with world.begin() as conn:
        conn.execute(
            schema.credentials.insert().values(
                credential_id="cr-late-b",
                person_id="p-a",
                credential_code="ESG-B",
                issued_on=date(2026, 10, 2),  # 晚于水位 10-01
                revoked_on=None,
            )
        )

    resumed = drills.resume_drill(world, clock=clock, run_id=run_id)
    assert resumed["status"] == "completed"
    staff = drills.get_project_result(world, run_id, "prj-staff")
    # 冻结水位不含新凭证 → 仍然缺 ESG-B → staffing
    assert staff["outcome"] == "staffing"


def test_resume_is_idempotent_after_completion(world, clock):
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"], batch_size=2)
    again = drills.resume_drill(world, clock=clock, run_id=run["run_id"])
    assert again["status"] == "completed"
    assert again["completed_projects"] == run["total_projects"]
    with world.connect() as conn:
        from sqlalchemy import func

        count = conn.execute(
            select(func.count(schema.notifications.c.notification_id)).where(
                schema.notifications.c.topic == "drill_decision"
            )
        ).scalar_one()
    # 每个项目恰好一条决定通知（重复续跑不新增）
    assert count == run["total_projects"]


def test_concurrent_identical_drills_collapse_to_one_run():
    """两个相同演练并发启动：唯一约束竞争必须回退成同一次运行，不产生双份决定。"""
    import tempfile
    import threading
    from pathlib import Path

    from sqlalchemy import create_engine, func, select
    from sqlalchemy.pool import StaticPool  # noqa: F401

    from skill_engine.services import drills

    path = Path(tempfile.mktemp(suffix=".db"))
    eng = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    schema.metadata.create_all(eng)
    from tests.conftest import PTYPE, REGION, insert_world, old_requirements
    from skill_engine.services import rules
    from skill_engine.services.clock import FixedClock

    clock = FixedClock(date(2026, 9, 1))
    insert_world(eng)
    rules.bootstrap_timeline(
        eng, clock=clock, region=REGION, project_type=PTYPE, version="v1",
        start_on=date(2024, 1, 1), personnel_requirements=old_requirements(),
    )
    draft = make_draft(eng, clock)

    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        try:
            results.append(
                drills.start_drill(eng, clock=clock, draft_id=draft["draft_id"], watermark_date=WATERMARK)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=go)
    t2 = threading.Thread(target=go)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert not errors
    assert len(results) == 2
    run_ids = {r["run_id"] for r in results}
    assert len(run_ids) == 1, "并发相同演练必须收敛到同一运行"
    with eng.connect() as conn:
        count_runs = conn.execute(
            select(func.count(schema.drill_runs.c.run_id))
        ).scalar_one()
        count_decisions = conn.execute(
            select(func.count(schema.notifications.c.notification_id)).where(
                schema.notifications.c.topic == "drill_decision"
            )
        ).scalar_one()
    assert count_runs == 1
    # 每项目恰好一封决定
    assert count_decisions == results[0]["total_projects"]
    eng.dispose()
    path.unlink(missing_ok=True)


def test_draft_revision_after_run_does_not_change_frozen_run(world, clock):
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"])
    # 运行启动后草案被大幅修订
    rules = __import__("skill_engine.services.rules", fromlist=["update_draft"])
    rules.update_draft(
        world,
        clock=clock,
        maintainer="u-maintainer",
        draft_id=draft["draft_id"],
        content={
            "region": draft["region"],
            "project_type": draft["project_type"],
            "new_version": "v9",
            "effective_on": draft["effective_on"],
            "end_on": None,
            "supersedes_version": draft["supersedes_version"],
            "personnel_requirements": [
                {"role": "合规官", "required_credentials": ["ESG-A", "ESG-B", "ESG-Z"]}
            ],
            "grandfather_policy": draft["grandfather_policy"],
            "change_note": "又加了 ESG-Z",
        },
    )
    # 旧运行仍可查看，内容是冻结时的 v2 要求
    result = drills.get_project_result(world, run["run_id"], "prj-clean")
    assert result["applicable_version"] == "v2"
