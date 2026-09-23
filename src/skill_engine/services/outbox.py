"""持久待发箱。

所有决定（演练逐项目结论、规则时间线变更）都在业务事务内以幂等键落库，
发送器只搬运已经提交的行：

* 重启不丢：未发送的 pending/sending 行留在库里，下次继续；
* 不重复：幂等键唯一约束 + 发送成功才置 sent，外部投递侧也按该键去重；
* 崩溃回收：发送中宕机留下的 sending 行会被回收重试（至少一次）。
"""

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from ..schema import notifications, subscriptions
from .clock import Clock

# 测试替身只需抛出任意异常即可模拟投递失败
Sender = Callable[[dict[str, Any]], None]


def fanout(
    conn: Connection,
    *,
    clock: Clock,
    region: str,
    project_type: str,
    idempotency_prefix: str,
    topic: str,
    subject: str,
    body: str,
    payload: dict[str, Any],
) -> int:
    """按订阅把一条范围通知展开成逐收件人待发项。

    订阅的 region/project_type 支持 ``*`` 通配。每个收件人的幂等键独立，
    同一事件重放（撤回/延期竞争重试）不会让任何人收到两份。
    """
    rows = conn.execute(select(subscriptions)).mappings().all()
    count = 0
    for row in rows:
        if row["region"] != "*" and row["region"] != region:
            continue
        if row["project_type"] != "*" and row["project_type"] != project_type:
            continue
        created = enqueue(
            conn,
            clock=clock,
            idempotency_key=f"{idempotency_prefix}:{row['recipient']}",
            recipient=row["recipient"],
            topic=topic,
            subject=subject,
            body=body,
            payload=payload,
        )
        count += int(created)
    return count


def enqueue(
    conn: Connection,
    *,
    clock: Clock,
    idempotency_key: str,
    recipient: str,
    topic: str,
    subject: str,
    body: str,
    payload: dict[str, Any],
) -> bool:
    """在当前事务内写入待发通知；幂等键已存在则跳过。

    返回是否新写入。调用方无需先查重。
    """
    existing = conn.execute(
        select(notifications.c.notification_id).where(
            notifications.c.idempotency_key == idempotency_key
        )
    ).first()
    if existing is not None:
        return False
    conn.execute(
        notifications.insert().values(
            notification_id=uuid.uuid4().hex,
            idempotency_key=idempotency_key,
            recipient=recipient,
            topic=topic,
            subject=subject,
            body=body,
            payload=payload,
            status="pending",
            created_at=clock.now(),
        )
    )
    return True


def list_pending(engine: Engine, *, limit: int = 100) -> Sequence[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(notifications)
            .where(notifications.c.status.in_(["pending", "sending"]))
            .order_by(notifications.c.created_at, notifications.c.notification_id)
            .limit(limit)
        ).mappings().all()
        return [dict(r) for r in rows]


def send_pending(
    engine: Engine, *, clock: Clock, sender: Sender, limit: int = 100
) -> dict[str, int]:
    """搬运一批待发通知。

    每条先 claim（pending→sending，sending 行视为崩溃回收继续投递），
    再调用 ``sender``；成功置 sent，失败回到 pending。
    外部系统必须按 ``idempotency_key`` 幂等。
    """
    sent = failed = 0
    for item in list_pending(engine, limit=limit):
        nid = item["notification_id"]
        with engine.begin() as conn:
            claimed = conn.execute(
                update(notifications)
                .where(
                    notifications.c.notification_id == nid,
                    notifications.c.status.in_(["pending", "sending"]),
                )
                .values(status="sending", attempts=notifications.c.attempts + 1)
            ).rowcount
            if claimed == 0:  # 被别的投递者抢先完成
                continue
        try:
            sender(item)
        except Exception as exc:  # 投递失败：回到 pending 等待重试
            with engine.begin() as conn:
                conn.execute(
                    update(notifications)
                    .where(notifications.c.notification_id == nid)
                    .values(status="pending", last_error=str(exc)[:500])
                )
            failed += 1
            continue
        with engine.begin() as conn:
            conn.execute(
                update(notifications)
                .where(notifications.c.notification_id == nid)
                .values(status="sent", sent_at=clock.now(), last_error=None)
            )
        sent += 1
    return {"sent": sent, "failed": failed}


def stats(engine: Engine) -> dict[str, int]:
    from sqlalchemy import func

    with engine.connect() as conn:
        rows = conn.execute(
            select(notifications.c.status, func.count(notifications.c.notification_id))
            .group_by(notifications.c.status)
        ).all()
    counts = {"pending": 0, "sending": 0, "sent": 0}
    for status, count in rows:
        counts[status] = count
    return counts
