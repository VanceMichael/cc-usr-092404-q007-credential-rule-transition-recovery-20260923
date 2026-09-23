"""Litestar 应用装配。"""

from litestar import Litestar, get
from sqlalchemy import text

from .database import create_database_engine


def create_app(engine=None) -> Litestar:
    storage = engine or create_database_engine()

    @get("/health")
    def health() -> dict[str, str]:
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "storage": "sqlite"}

    return Litestar(route_handlers=[health])


app = create_app()
