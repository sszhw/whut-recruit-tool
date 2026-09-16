#!/usr/bin/env python3
"""统一数据访问层（Repository）。

职责：作为页面展示、企业分析、投递推荐**唯一**的数据入口，保证三处口径一致。

背景（见 docs/架构与网页功能优化建议.md §2.1）：招聘数据原先散落在多个
`*_原始数据.json` 中，页面展示跨文件合并去重，而企业分析/推荐只读"最新修改的那个文件"，
一旦最新文件只是一次当日小快照，分析与推荐的样本就会远小于页面展示范围。

本模块解决的问题：
  1. 所有原始数据文件跨文件合并，按学校网站 ID 去重（新文件优先）；
  2. 带 mtime+size 签名的进程内缓存，避免每次请求重复解析几十 MB 的 JSON；
  3. 提供聚合统计（记录数 / 覆盖日期 / 最近更新时间 / 详情缺失）供数据健康展示；
  4. 提供企业清单（企业分析、投递推荐共用）。

约定：返回的原始记录是**只读**的（可能来自缓存），调用方需要改写时请自行构造新 dict。
"""

from __future__ import annotations

import glob
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import crawler

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DATA = ROOT / "data"

RECRUIT_GLOB = "武汉理工大学招聘信息_*_原始数据.json"
PREACH_GLOB = "宣讲会_*_原始数据.json"

# kind -> (glob, JSON 内的数组键名)
KINDS: dict[str, tuple[str, str]] = {
    "recruit": (RECRUIT_GLOB, "招聘信息"),
    "fair": (RECRUIT_GLOB, "双选会"),
    "preach": (PREACH_GLOB, "宣讲会"),
}

_lock = threading.Lock()
_cache: dict[str, tuple[tuple, Any]] = {}     # cache_key -> (文件签名(+extra), 结果)


# ---------------------------------------------------------------- 文件与读取

def iter_files(pattern: str, data_dir: Path | None = None) -> list[Path]:
    """匹配文件的列表，按修改时间倒序（越新的文件在合并去重时优先级越高）。"""
    base = data_dir or DATA
    paths = glob.glob(str(base / pattern))
    return [Path(p) for p in sorted(paths, key=os.path.getmtime, reverse=True)]


def _signature(files: list[Path]) -> tuple:
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)


def read_json(path: Path) -> dict:
    """容错读取 JSON（损坏/缺失返回空 dict，不抛异常）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cached(kind: str, builder) -> Any:
    """按 (文件列表, mtime, size) 签名缓存；数据文件变化时自动失效。"""
    pattern, _key = KINDS[kind]
    files = iter_files(pattern)
    sig = _signature(files)
    with _lock:
        hit = _cache.get(kind)
        if hit and hit[0] == sig:
            return hit[1]
    value = builder(files)
    with _lock:
        _cache[kind] = (sig, value)
    return value


def _build_merged(kind: str):
    """构造合并函数：跨全部匹配文件按 id 去重（先出现的文件优先，即新文件优先）。"""
    _pattern, key = KINDS[kind]

    def builder(files: list[Path]) -> list[dict]:
        merged: list[dict] = []
        seen: set[str] = set()
        for path in files:
            data = read_json(path)
            for item in (data.get(key) or []):
                if not isinstance(item, dict):
                    continue
                iid = str(item.get("id", "") or "")
                if iid:
                    if iid in seen:
                        continue
                    seen.add(iid)
                merged.append(item)
        return merged

    return builder


def raw_items(kind: str) -> list[dict]:
    """跨全部文件合并去重后的原始记录（只读）。kind ∈ recruit / fair / preach。"""
    if kind not in KINDS:
        raise KeyError(f"未知数据类型：{kind}")
    return _cached(kind, _build_merged(kind))


def cached_derived(name: str, kind: str, builder, extra: str = "") -> Any:
    """按 kind 的数据文件签名，缓存任意派生结果（builder 无参、只读原始数据）。

    例：把「原始记录 → 页面行」的映射缓存起来，避免每次请求重复做文本清洗 / 字典构造。
    extra 会把结果相关的外部变量（如「今天」的日期）纳入签名，避免跨天复用过期结果。
    """
    if name in KINDS:
        raise ValueError(f"缓存名与原始数据类型冲突：{name}")
    pattern, _key = KINDS[kind]
    sig = _signature(iter_files(pattern)) + ((extra,) if extra else ())
    with _lock:
        hit = _cache.get(name)
        if hit and hit[0] == sig:
            return hit[1]
    value = builder()
    with _lock:
        _cache[name] = (sig, value)
    return value


def source_files(kind: str) -> list[Path]:
    return iter_files(KINDS[kind][0])


def raw_items_in(kind: str, data_dir: Path) -> list[dict]:
    """指定数据目录下的合并结果（不走缓存，供 CLI / 测试使用）。"""
    if kind not in KINDS:
        raise KeyError(f"未知数据类型：{kind}")
    return _build_merged(kind)(iter_files(KINDS[kind][0], data_dir))


def invalidate() -> None:
    """清空缓存（数据文件刚被后台任务重写时，可主动调用）。"""
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------- 聚合统计

def _dates_of(kind: str, items: list[dict]) -> list[str]:
    out: list[str] = []
    if kind == "preach":
        for item in items:
            d = str(item.get("hold_date", "") or "").strip()
            if len(d) >= 10:
                out.append(d[:10])
    else:
        for item in items:
            addtime = item.get("addtime")
            if addtime in (None, ""):
                continue
            try:
                out.append(datetime.fromtimestamp(int(addtime)).strftime("%Y-%m-%d"))
            except (TypeError, ValueError, OSError, OverflowError):
                continue
    return sorted(out)


def _missing_detail(kind: str, items: list[dict]) -> int:
    """详情缺失：招聘信息无正文（remarks/content 均空）。"""
    if kind != "recruit":
        return 0
    return sum(1 for item in items if not str(item.get("remarks") or item.get("content") or "").strip())


def aggregate(kind: str) -> dict:
    """单一数据类型的统计：文件数、记录数（去重后）、覆盖日期、最近更新时间、详情缺失。"""
    items = raw_items(kind)
    files = source_files(kind)
    dates = _dates_of(kind, items)
    last_update = ""
    if files:
        newest = max(files, key=os.path.getmtime)
        last_update = datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return {
        "kind": kind,
        "files": len(files),
        "records": len(items),
        "first_date": dates[0] if dates else "",
        "last_date": dates[-1] if dates else "",
        "last_update": last_update,
        "missing_detail": _missing_detail(kind, items),
    }


def master_summary() -> dict:
    """招聘主数据（招聘信息 + 双选会 + 宣讲会）的统一口径摘要。"""
    recruit = aggregate("recruit")
    fair = aggregate("fair")
    preach = aggregate("preach")
    updates = [x["last_update"] for x in (recruit, preach) if x["last_update"]]
    starts = [x["first_date"] for x in (recruit, preach) if x["first_date"]]
    ends = [x["last_date"] for x in (recruit, preach) if x["last_date"]]
    return {
        "recruit": recruit,
        "fair": fair,
        "preach": preach,
        "recruit_count": recruit["records"],
        "fair_count": fair["records"],
        "preach_count": preach["records"],
        "files": recruit["files"] + preach["files"],
        "coverage_start": min(starts) if starts else "",
        "coverage_end": max(ends) if ends else "",
        "last_update": max(updates) if updates else "",
        "missing_detail": recruit["missing_detail"],
    }


# ---------------------------------------------------------------- 企业清单

def _build_companies() -> list[dict]:
    """从合并后的招聘信息里汇总企业清单（企业分析 / 投递推荐共用同一份）。"""
    bucket: dict[str, dict] = {}
    for item in raw_items("recruit"):
        name = str(item.get("com_id_name") or "").strip()
        if not name:
            continue
        row = bucket.setdefault(name, {"name": name, "title": "", "text": "", "count": 0})
        row["count"] += 1
        if item.get("title") and not row["title"]:
            row["title"] = str(item["title"]).strip()
        body = crawler.plain_text(item.get("content") or item.get("remarks") or "")
        if body and len(body) > len(row["text"]):
            row["text"] = body[:300]
    return sorted(bucket.values(), key=lambda r: (-r["count"], r["name"]))


def companies(max_items: int = 0) -> list[dict]:
    """企业清单（缓存派生结果，避免每次请求重复清洗 2000+ 条公告正文）。

    每项：{name, title, text, count}。text 取该公司所有公告中最长的一篇正文前 300 字。
    返回按「公告数多的优先」排序；max_items=0 表示不截断。
    """
    rows = cached_derived("companies", "recruit", _build_companies)
    return rows[:max_items] if max_items else rows


def company_names() -> list[str]:
    return [c["name"] for c in companies()]


def unanalyzed(cache: dict) -> list[str]:
    """已抓取但尚未有分析结果的企业名。"""
    return [c["name"] for c in companies() if c["name"] not in (cache or {})]


# ---------------------------------------------------------------- 数据健康

def health(cache: dict | None = None, work_undetermined: int | None = None,
           last_task: dict | None = None) -> dict:
    """总览页「数据健康」所需的一站式指标。"""
    summary = master_summary()
    payload = dict(summary)
    payload["analyzed_count"] = len(cache or {})
    payload["unanalyzed_count"] = len(unanalyzed(cache or {}))
    if work_undetermined is not None:
        payload["work_undetermined"] = work_undetermined
    if last_task:
        payload["last_task"] = last_task
    return payload
