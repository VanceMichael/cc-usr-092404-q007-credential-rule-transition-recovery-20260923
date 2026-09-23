"""SQLite 引擎创建。"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event


def create_database_engine():
    path = Path(os.getenv("DATABASE_PATH", "data/skills.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    # 所有写事务以 BEGIN IMMEDIATE 开始：发布、撤回、延期竞争时直接在库级串行，
    # 配合时间线 (scope_key, seq) 唯一约束，保证只留下一条有效时间线。
    @event.listens_for(engine, "connect")
    def _disable_driver_txn(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
