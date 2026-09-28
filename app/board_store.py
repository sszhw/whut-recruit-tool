"""投递看板的独立 JSON 存储。

看板保存招聘来源快照，不依赖招聘主库；这样主库迁移、更新或删除记录时，
用户已经维护的求职进度仍可正常查看。
"""

from __future__ import annotations

import threading
from pathlib import Path

from settings import DATA
from utils.io import load_json_dict, write_json_atomic

BOARD_PATH = DATA / "投递看板.json"
_SCHEMA_VERSION = 1
_LOCK = threading.RLock()


def empty_board() -> dict:
    """返回一份新的空看板结构。"""
    return {"version": _SCHEMA_VERSION, "items": []}


def load_board() -> dict:
    """容错读取看板；文件缺失、损坏或结构异常时退化为空看板。"""
    with _LOCK:
        data = load_json_dict(BOARD_PATH)
    items = data.get("items")
    if not isinstance(items, list):
        return empty_board()
    return {
        "version": _SCHEMA_VERSION,
        "items": [dict(item) for item in items if isinstance(item, dict)],
    }


def save_board(data: dict) -> Path:
    """原子保存看板数据。"""
    items = data.get("items")
    clean = {
        "version": _SCHEMA_VERSION,
        "items": [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else [],
    }
    BOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        return write_json_atomic(BOARD_PATH, clean)
