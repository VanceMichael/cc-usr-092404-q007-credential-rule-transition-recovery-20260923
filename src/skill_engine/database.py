"""SQLite 引擎创建。"""

import os
from pathlib import Path

from sqlalchemy import create_engine


def create_database_engine():
    path = Path(os.getenv("DATABASE_PATH", "data/skills.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})
