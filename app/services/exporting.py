"""导出目标的文件定位。

只回答「该发哪个文件、用什么 MIME」，不碰 Flask 的 send_file——
这样这部分可以脱离 Web 单测，路由只剩「取到路径 → 发文件」。

命名为 exporting 而非 exports，是为了跟项目里已有的 `exports.py`
（构造 xlsx / csv / ics 字节流的模块）区分开。
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import analyze
import repository as repo
from settings import DATA

from services import ServiceError

# 导出类型 → (文件名, MIME)；analyze 里定义的是产出文件名，这里只做映射
_EXPORT_TARGETS = {
    "csv": (analyze.CSV_NAME, "text/csv"),
    "md": (analyze.MD_NAME, "text/markdown"),
}


def resolve_export_file(kind: str) -> tuple[Path, str]:
    """按 kind 定位导出文件。返回 (路径, MIME)；不存在则抛 404 的 ServiceError。"""
    if kind not in _EXPORT_TARGETS:
        raise ServiceError("未知导出类型", status=400)
    name, mime = _EXPORT_TARGETS[kind]
    path = DATA / name
    if not path.exists():
        raise ServiceError("文件不存在，请先运行分析", status=404)
    return path, mime


def latest_recruit_csv() -> Path:
    """最新一份招聘信息 CSV。

    先用主库记录数判断有没有数据，再去找文件——
    原先只看「有没有最新原始数据文件」，会出现「没有当天快照、但主库仍有历史数据」
    时误报 404 的情况。
    """
    if not repo.master_summary()["recruit_count"]:
        raise ServiceError("无数据", status=404)
    candidates = sorted(glob.glob(str(DATA / "*_招聘信息.csv")), key=os.path.getmtime, reverse=True)
    if not candidates:
        raise ServiceError("CSV 不存在", status=404)
    return Path(candidates[0])
