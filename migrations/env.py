"""Alembic 数据库环境。"""

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
path = Path(os.getenv("DATABASE_PATH", "data/skills.sqlite3"))
path.parent.mkdir(parents=True, exist_ok=True)
config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")


def online() -> None:
    engine = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


def offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


offline() if context.is_offline_mode() else online()
