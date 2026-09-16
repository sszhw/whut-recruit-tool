"""测试公共夹具。

- 把 `app/` 加入 sys.path，使测试可以直接 import server / repository / resume / analyze；
- `data_dir` 夹具把 repository 指向临时目录（隔离真实 data/），并在用例前后清空缓存；
- 提供构造原始数据 JSON 的小工具（文件名与线上 glob 一致）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import repository as repo  # noqa: E402


def recruit_path(data_dir: Path, tag: str) -> Path:
    """招聘信息原始数据文件路径（匹配 repository.RECRUIT_GLOB）。"""
    return Path(data_dir) / f"武汉理工大学招聘信息_{tag}_原始数据.json"


def preach_path(data_dir: Path, tag: str) -> Path:
    """宣讲会原始数据文件路径（匹配 repository.PREACH_GLOB）。"""
    return Path(data_dir) / f"宣讲会_{tag}_原始数据.json"


def write_raw(path: Path, **sections) -> Path:
    """写出一个原始数据 JSON（无 BOM、UTF-8），键名即 招聘信息/双选会/宣讲会。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sections, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """repository.DATA 指向临时目录，避免读写项目真实数据。"""
    repo.invalidate()
    monkeypatch.setattr(repo, "DATA", tmp_path)
    yield tmp_path
    repo.invalidate()


@pytest.fixture()
def make_recruit(data_dir):
    """构造招聘信息数据文件；mtime 可显式指定，用于验证「新文件优先」。"""
    def _make(tag: str, items: list[dict], mtime: float | None = None) -> Path:
        path = write_raw(recruit_path(data_dir, tag), 招聘信息=items)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path
    return _make


@pytest.fixture()
def make_preach(data_dir):
    """构造宣讲会数据文件。"""
    def _make(tag: str, items: list[dict], mtime: float | None = None) -> Path:
        path = write_raw(preach_path(data_dir, tag), 宣讲会=items)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path
    return _make


def recruit_item(iid: str, name: str = "测试企业", title: str = "2027届校园招聘",
                 addtime: int = 1789000000, content: str = "招聘机械工程师，工作地点武汉",
                 **extra) -> dict:
    """构造一条招聘信息原始记录。"""
    item = {"id": iid, "com_id_name": name, "title": title, "addtime": addtime,
            "content": content, "httpurl": f"https://example.com/{iid}"}
    item.update(extra)
    return item


def preach_item(iid: str, name: str = "测试企业", hold_date: str = "2026-09-20",
                start: str = "19:00", end: str = "21:00", address: str = "武汉市，马房山校区，东风厅",
                addtime: int = 1789000000, **extra) -> dict:
    """构造一条宣讲会原始记录。"""
    item = {"id": iid, "com_id_name": name, "title": name, "hold_date": hold_date,
            "hold_starttime": start, "hold_endtime": end, "address": address,
            "city_id_name": "武汉市", "air_type": 0, "addtime": addtime,
            "httpurl": f"https://example.com/preach/{iid}"}
    item.update(extra)
    return item
