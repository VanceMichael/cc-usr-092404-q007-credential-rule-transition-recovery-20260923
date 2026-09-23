"""逐项目裁定：补员 / 豁免 / 复核 / 过渡保留 / 签署快照 / 撤销风险。"""

from datetime import date

from tests.conftest import (
    EFFECTIVE,
    NEW_VERSION,
    OLD_VERSION,
    WATERMARK,
    grandfather_policy,
    make_draft,
    new_requirements,
    old_requirements,
    run_drill,
)
from skill_engine.services import drills, rules


def _outcomes(eng, run_id):
    impact = drills.get_impact(eng, run_id)
    by_project = {p["project_id"]: p for p in impact["projects"]}
    return by_project, impact


def test_all_adjudication_branches(world, clock):
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"])

    by_project, impact = _outcomes(world, run["run_id"])
    assert by_project["prj-clean"]["outcome"] == "compliant"
    assert by_project["prj-clean"]["applicable_version"] == NEW_VERSION
    assert by_project["prj-equivok"]["outcome"] == "compliant"
    assert by_project["prj-grand"]["outcome"] == "compliant"
    assert by_project["prj-grand"]["applicable_version"] == OLD_VERSION  # 过渡保留旧资格
    assert by_project["prj-exempt"]["outcome"] == "exempt"
    assert by_project["prj-exempt"]["applicable_version"] == NEW_VERSION
    assert by_project["prj-review"]["outcome"] == "review"
    assert by_project["prj-missing"]["outcome"] == "staffing"
    assert by_project["prj-revoked"]["outcome"] == "staffing"
    assert by_project["prj-staff"]["outcome"] == "staffing"
    assert by_project["prj-done"]["outcome"] == "completed_snapshot"
    assert by_project["prj-done"]["applicable_version"] == OLD_VERSION

    assert run["outcome_counts"] == {
        "compliant": 3,
        "staffing": 3,
        "review": 1,
        "exempt": 1,
        "completed_snapshot": 1,
    }


def test_single_project_view_explains_version_and_reason(world, clock):
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"])

    grand = drills.get_project_result(world, run["run_id"], "prj-grand")
    assert grand["applicable_version"] == OLD_VERSION
    assert grand["grandfather"]["retained"] is True
    assert any("过渡条款" in r and "保留旧资格" in r for r in grand["reasons"])

    staff = drills.get_project_result(world, run["run_id"], "prj-staff")
    assert staff["applicable_version"] == NEW_VERSION
    assert staff["outcome"] == "staffing"
    assert any("ESG-B" in r and "可用替补" in r for r in staff["reasons"])
    # 空闲双证是唯一未在其他在办项目上忙碌的合格替补
    affected = staff["affected_positions"][0]
    assert [c["person_id"] for c in affected["available_replacements"]] == ["p-spare"]

    review = drills.get_project_result(world, run["run_id"], "prj-review")
    assert review["outcome"] == "review"
    assert review["unresolved_equivalences"][0]["accepted_code"] == "ESG-SIM"
    assert any("人工复核" in r for r in review["reasons"])


def test_exemption_takes_priority_over_gaps(world, clock):
    # prj-exempt 在岗人员不满足新版要求，但豁免有效，结论必须是 exempt
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"])
    exempt = drills.get_project_result(world, run["run_id"], "prj-exempt")
    assert exempt["outcome"] == "exempt"
    assert exempt["affected_positions"] == []
    assert any("豁免" in r for r in exempt["reasons"])


def test_expired_exemption_falls_through_to_adjudication(world, clock):
    from skill_engine import schema

    with world.begin() as conn:
        # 豁免在生效日前一天到期；岗位改配只持 ESG-A 的人，确保落入补员
        conn.execute(
            schema.projects.update()
            .where(schema.projects.c.project_id == "prj-exempt")
            .values(exemption_expires_on=date(2026, 10, 14))
        )
        conn.execute(
            schema.project_positions.update()
            .where(schema.project_positions.c.project_id == "prj-exempt")
            .values(person_id="p-a")
        )
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"])
    result = drills.get_project_result(world, run["run_id"], "prj-exempt")
    assert result["outcome"] == "staffing"
    assert "ESG-B" in result["affected_positions"][0]["missing_credentials"]


def test_signed_project_is_frozen_but_revocation_added_as_risk(world, clock):
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"])
    done = drills.get_project_result(world, run["run_id"], "prj-done")
    assert done["outcome"] == "completed_snapshot"
    assert done["applicable_version"] == OLD_VERSION
    assert len(done["revocation_risks"]) == 1
    risk = done["revocation_risks"][0]
    assert risk["credential_code"] == "ESG-X"
    assert risk["signed_frozen"] is True
    assert any("撤销风险" in r for r in done["reasons"])
    # 快照固定意味着受影响岗位为空，不进入补员
    assert done["affected_positions"] == []


def test_revoked_credential_on_active_position_creates_gap_and_risk(world, clock):
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"])
    result = drills.get_project_result(world, run["run_id"], "prj-revoked")
    assert result["outcome"] == "staffing"
    assert result["revocation_risks"][0]["credential_code"] == "ESG-A"
    assert result["revocation_risks"][0]["signed_frozen"] is False
    assert "ESG-A" in result["affected_positions"][0]["missing_credentials"]


def test_grandfather_expires_when_grace_days_run_out(world, clock):
    # prj-grand 启动于 2026-09-01，生效日 10-15；宽限 0 天 → 不保留旧资格
    draft = make_draft(world, clock, grace_days=0)
    run = run_drill(world, clock, draft["draft_id"])
    result = drills.get_project_result(world, run["run_id"], "prj-grand")
    assert result["grandfather"]["retained"] is False
    assert result["applicable_version"] == NEW_VERSION
    assert result["outcome"] == "staffing"  # p-a 只有 ESG-A，缺 ESG-B


def test_no_grandfather_policy_always_uses_new_version(world, clock):
    draft = make_draft(
        world,
        clock,
        requirements=new_requirements(),
    )
    # 直接改草案内容为 mode=none
    draft2 = rules.update_draft(
        world,
        clock=clock,
        maintainer="u-maintainer",
        draft_id=draft["draft_id"],
        content={
            "region": draft["region"],
            "project_type": draft["project_type"],
            "new_version": draft["new_version"],
            "effective_on": draft["effective_on"],
            "end_on": None,
            "supersedes_version": draft["supersedes_version"],
            "personnel_requirements": draft["personnel_requirements"],
            "grandfather_policy": {"mode": "none"},
            "change_note": "无过渡",
        },
    )
    run = run_drill(world, clock, draft2["draft_id"])
    result = drills.get_project_result(world, run["run_id"], "prj-grand")
    assert result["grandfather"]["mode"] == "none"
    assert result["applicable_version"] == NEW_VERSION
    assert result["outcome"] == "staffing"


def test_watermark_must_precede_effective_date(world, clock):
    draft = make_draft(world, clock)
    import pytest

    from skill_engine.services.errors import ValidationError

    with pytest.raises(ValidationError):
        run_drill(world, clock, draft["draft_id"], watermark_date=EFFECTIVE)


def test_pending_project_visible_before_resume(world, clock):
    draft = make_draft(world, clock)
    run = run_drill(world, clock, draft["draft_id"], batch_size=1)
    pending = drills.get_project_result(world, run["run_id"], "prj-missing")
    assert pending["status"] == "pending"
    drills.resume_drill(world, clock=clock, run_id=run["run_id"])
    finished = drills.get_project_result(world, run["run_id"], "prj-missing")
    assert finished["status"] == "completed"
    assert finished["outcome"] == "staffing"
