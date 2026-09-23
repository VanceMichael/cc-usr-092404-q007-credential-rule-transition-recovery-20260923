"""测试夹具：内存库 + 固定时钟 + 完整的绿色项目世界。"""

import os
from datetime import date

# 同步处理器跑在线程池是有意为之（SQLite 阻塞调用），关闭 Litestar 提示噪音
os.environ.setdefault("LITESTAR_WARN_IMPLICIT_SYNC_TO_THREAD", "0")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from skill_engine import schema
from skill_engine.httpapi import create_app
from skill_engine.services.clock import FixedClock

WATERMARK = date(2026, 10, 1)
EFFECTIVE = date(2026, 10, 15)
REGION = "华东"
PTYPE = "光伏"
OLD_VERSION = "v1"
NEW_VERSION = "v2"


@pytest.fixture()
def engine():
    # Litestar 同步处理器在线程池中运行：StaticPool 让所有线程共享同一内存连接
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    schema.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def clock():
    return FixedClock(date(2026, 9, 1))


@pytest.fixture()
def app(engine, clock):
    return create_app(engine, clock=clock)


@pytest.fixture()
def client(app):
    from litestar.testing import TestClient

    with TestClient(app) as c:
        yield c


def insert_world(eng) -> None:
    """9 个项目覆盖全部裁定分支。"""
    with eng.begin() as conn:
        conn.execute(schema.users.insert(), [
            {"user_id": "u-maintainer", "display_name": "维护者", "is_rule_maintainer": True},
            {"user_id": "u-approver", "display_name": "批准人", "is_rule_maintainer": False},
            {"user_id": "u-other", "display_name": "路人", "is_rule_maintainer": False},
        ])
        conn.execute(schema.persons.insert(), [
            {"person_id": "p-ab", "name": "甲乙双证", "region": REGION},
            {"person_id": "p-sim", "name": "乙加待裁", "region": REGION},
            {"person_id": "p-old", "name": "老证等效", "region": REGION},
            {"person_id": "p-done", "name": "已签人员", "region": REGION},
            {"person_id": "p-revoked", "name": "被撤人员", "region": REGION},
            {"person_id": "p-a", "name": "仅甲证", "region": REGION},
            {"person_id": "p-spare", "name": "空闲双证", "region": REGION},
            {"person_id": "p-north", "name": "华北双证", "region": "华北"},
        ])
        conn.execute(schema.credentials.insert(), [
            {"credential_id": "cr-ab-a", "person_id": "p-ab", "credential_code": "ESG-A",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-ab-b", "person_id": "p-ab", "credential_code": "ESG-B",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-sim-b", "person_id": "p-sim", "credential_code": "ESG-B",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-sim", "person_id": "p-sim", "credential_code": "ESG-SIM",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-old", "person_id": "p-old", "credential_code": "ESG-OLD",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-old-b", "person_id": "p-old", "credential_code": "ESG-B",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-done", "person_id": "p-done", "credential_code": "ESG-X",
             "issued_on": date(2024, 1, 1), "revoked_on": date(2026, 9, 1)},
            {"credential_id": "cr-revoked", "person_id": "p-revoked", "credential_code": "ESG-A",
             "issued_on": date(2024, 1, 1), "revoked_on": date(2026, 9, 10)},
            {"credential_id": "cr-a", "person_id": "p-a", "credential_code": "ESG-A",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-spare-a", "person_id": "p-spare", "credential_code": "ESG-A",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-spare-b", "person_id": "p-spare", "credential_code": "ESG-B",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-north-a", "person_id": "p-north", "credential_code": "ESG-A",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
            {"credential_id": "cr-north-b", "person_id": "p-north", "credential_code": "ESG-B",
             "issued_on": date(2024, 1, 1), "revoked_on": None},
        ])
        conn.execute(schema.credential_equivalences.insert(), [
            {"equivalence_id": "eq-old", "required_code": "ESG-A",
             "accepted_code": "ESG-OLD", "status": "accepted"},
            {"equivalence_id": "eq-sim", "required_code": "ESG-A",
             "accepted_code": "ESG-SIM", "status": "pending"},
        ])

        def project(pid, name, started, *, recipient=None, exemption=None, exempt_until=None):
            return {
                "project_id": pid, "name": name, "region": REGION, "project_type": PTYPE,
                "manager_recipient": recipient or f"{pid}@example.com",
                "started_on": started, "completed_on": None,
                "exemption_code": exemption, "exemption_expires_on": exempt_until,
            }

        conn.execute(schema.projects.insert(), [
            project("prj-clean", "清洁项目", date(2026, 1, 1)),
            project("prj-done", "签署项目", date(2026, 1, 1)),
            project("prj-equivok", "等效合规项目", date(2026, 1, 1)),
            project("prj-exempt", "豁免项目", date(2026, 1, 1),
                    exemption="EXP-9", exempt_until=date(2026, 12, 31)),
            project("prj-grand", "过渡保留项目", date(2026, 9, 1)),
            project("prj-missing", "缺岗项目", date(2026, 1, 1)),
            project("prj-review", "复核项目", date(2026, 1, 1)),
            project("prj-revoked", "撤销项目", date(2026, 1, 1)),
            project("prj-staff", "补员项目", date(2026, 1, 1)),
        ])

        def pos(pid, project_id, role, person, *, signed=None, version=None, match=None):
            return {
                "position_id": pid, "project_id": project_id, "role": role,
                "person_id": person, "signed_on": signed,
                "signed_rule_version": version, "signed_match": match,
            }

        conn.execute(schema.project_positions.insert(), [
            pos("pos-clean", "prj-clean", "合规官", "p-ab"),
            pos("pos-done", "prj-done", "合规官", "p-done",
                signed=date(2026, 8, 1), version="v1", match=True),
            pos("pos-equivok", "prj-equivok", "合规官", "p-old"),
            pos("pos-grand", "prj-grand", "合规官", "p-a"),
            # 豁免项目即使在岗人员不合格也直接豁免
            pos("pos-exempt", "prj-exempt", "合规官", "p-old"),
            # prj-missing 没有任何岗位
            pos("pos-review", "prj-review", "合规官", "p-sim"),
            pos("pos-revoked", "prj-revoked", "合规官", "p-revoked"),
            pos("pos-staff", "prj-staff", "合规官", "p-a"),
        ])
        conn.execute(schema.subscriptions.insert(), [
            {"subscription_id": "sub-all", "recipient": "compliance@example.com",
             "region": "*", "project_type": "*"},
            {"subscription_id": "sub-east", "recipient": "east@example.com",
             "region": REGION, "project_type": "*"},
            {"subscription_id": "sub-west", "recipient": "west@example.com",
             "region": "华北", "project_type": "*"},
        ])


def old_requirements():
    return [{"role": "合规官", "required_credentials": ["ESG-A"]}]


def new_requirements():
    return [{"role": "合规官", "required_credentials": ["ESG-A", "ESG-B"]}]


def grandfather_policy(grace_days: int = 60):
    return {"mode": "project_start", "grace_days": grace_days}


def make_draft(eng, clock, *, grace_days: int = 60, effective_on=None, new_version=NEW_VERSION,
               supersedes=OLD_VERSION, requirements=None, maintainer="u-maintainer"):
    from skill_engine.services import rules

    return rules.create_draft(
        eng,
        clock=clock,
        maintainer=maintainer,
        content={
            "region": REGION,
            "project_type": PTYPE,
            "new_version": new_version,
            "effective_on": effective_on or EFFECTIVE.isoformat(),
            "end_on": None,
            "supersedes_version": supersedes,
            "personnel_requirements": requirements or new_requirements(),
            "grandfather_policy": grandfather_policy(grace_days),
            "change_note": "新版加严",
        },
    )


def approve_and_publish(eng, clock, draft_id, *, approver="u-approver"):
    from skill_engine.services import rules

    rules.submit_for_approval(eng, clock=clock, maintainer="u-maintainer", draft_id=draft_id)
    rules.approve_draft(eng, clock=clock, approver=approver, draft_id=draft_id)
    return rules.publish_draft(eng, clock=clock, actor=approver, draft_id=draft_id)


def run_drill(eng, clock, draft_id, **kwargs):
    from skill_engine.services import drills

    return drills.start_drill(
        eng,
        clock=clock,
        draft_id=draft_id,
        watermark_date=kwargs.pop("watermark_date", WATERMARK),
        **kwargs,
    )


@pytest.fixture()
def world(engine, clock):
    from skill_engine.services import rules

    insert_world(engine)
    rules.bootstrap_timeline(
        engine,
        clock=clock,
        region=REGION,
        project_type=PTYPE,
        version=OLD_VERSION,
        start_on=date(2024, 1, 1),
        personnel_requirements=old_requirements(),
    )
    return engine
