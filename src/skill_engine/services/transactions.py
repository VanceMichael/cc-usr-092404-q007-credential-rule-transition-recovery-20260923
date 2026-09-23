"""SQLite 立即写事务。

SQLAlchemy 2.0 的 sqlite 方言不接受 ``isolation_level='IMMEDIATE'``，
因此在 AUTOCOMMIT 连接上手工发出 ``BEGIN IMMEDIATE``：
第一个语句即获取 RESERVED 锁，把“撤回 / 紧急延期 / 发布”串行成唯一时间线。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine

from sqlalchemy.engine import Connection


@contextmanager
def immediate(engine: Engine) -> Iterator[Connection]:
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        conn.execute(text("BEGIN IMMEDIATE"))
        try:
            yield conn
        except Exception:
            conn.execute(text("ROLLBACK"))
            raise
        else:
            conn.execute(text("COMMIT"))
    finally:
        conn.close()
