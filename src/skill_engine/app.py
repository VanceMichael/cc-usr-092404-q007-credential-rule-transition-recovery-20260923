"""Litestar 应用装配（委托到 httpapi，保留默认应用入口）。"""

from .httpapi import create_app

__all__ = ["create_app"]

app = create_app()
