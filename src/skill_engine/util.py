"""通用工具：ID、时间戳、JSON 与稳定哈希。"""

import hashlib
import json
import uuid
from datetime import datetime, timezone


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value) -> str:
    """确定性 JSON：键排序、无空白，保证相同输入得到相同摘要。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
