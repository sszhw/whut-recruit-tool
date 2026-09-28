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

数据源有两套，**对上层完全透明**：
  - SQLite（默认）：由 `sqlite_store` 维护，导入一次之后查询走索引，
    `master_summary` 这类每秒都可能被调用的统计不再重复解析 56MB 原始数据；
  - JSON（回退）：逐个文件解析后合并，即迁移前的老路径。
切换开关是环境变量 `WHUT_STORE=json|sqlite`；库损坏 / 同步失败时**自动**回落到 JSON 路径
（宁可慢一点，也不能让界面白屏）。库**不存在**时不回落，而是冷启动全量导入一次
（同一进程内只做一次，见 `_bootstrap`）——否则默认配置在新环境永远停在 JSON 慢路径上，
加速等于没生效。

约定：返回的原始记录是**只读**的（可能来自缓存），调用方需要改写时请自行构造新 dict。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import crawler
import sqlite_store

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
# 同步失败的「库 × 类型 × 文件签名」：记下来避免每个请求都重试一次注定失败的导入。
# 签名一变（例如新抓完一个文件）就自然失效，会再试。
_sqlite_bad: set[tuple] = set()
# 冷启动（库不存在 → 全量导入）的进程内一次性标记：库路径 -> 导入是否成功。
# 为什么必须记成败：Flask 是多线程的，4 个请求会同时发现「库不存在」；
# 没有这层标记就会并发跑 4 次 56MB 全量导入。跨进程并发由 SQLite 事务兜底，不上文件锁。
_bootstrap_lock = threading.Lock()
_bootstrap_done: dict[str, bool] = {}


# ---------------------------------------------------------------- 数据源选择

def _db_path(data_dir: Path) -> Path:
    """库文件路径（每次现算：DATA 会被测试与 CLI 改指向别的目录）。"""
    return sqlite_store.db_path_for(data_dir or DATA)


def _is_default_dir(data_dir: Path | None) -> bool:
    """是否是应用自有的默认数据目录——只有这里才允许冷启动建库。

    为什么收紧：`raw_items_in(kind, data_dir)` 的 data_dir 可能是**任意用户目录**
    （resume.py 在 `workdir != DATA` 时就传用户工作目录）。在别人随手指定的目录里
    落一个 56MB 的 whut_data.db 违反最小惊讶原则——调用方要的是「读」，没预期它写盘；
    目录若是只读或没权限，还会留下建了一半的垃圾文件。默认数据目录才是应用自己的地盘。
    """
    try:
        return Path(data_dir or DATA).resolve() == DATA.resolve()
    except OSError:
        return False


def _bootstrap(path: Path, data_dir: Path) -> bool:
    """库还不存在时全量导入一次；成功与否都记下来，同一进程内不重复尝试。

    为什么不是「缺库就一直走 JSON」：开关默认 sqlite，那样新环境（clone 后没跑过
    导入脚本）会一直停在 0.5s 的 JSON 慢路径上，加速等于没生效。

    只在默认数据目录里调用（判断见 `_is_default_dir`）：用户目录保持只读。
    """
    key = str(path)
    with _bootstrap_lock:
        if key in _bootstrap_done:
            return _bootstrap_done[key]
        try:
            report = sqlite_store.import_all(path, KINDS, data_dir)
        except Exception:                      # 导入是磁盘 + 解析，任何意外都不能打断请求
            report = None
        _bootstrap_done[key] = report is not None
        return _bootstrap_done[key]


def _usable_db(kind: str, files: list[Path], data_dir: Path | None = None) -> Path | None:
    """可用的 SQLite 库路径；不可用返回 None（调用方回落 JSON）。

    不可用的几种情况：开关关掉；目录不是默认 DATA 且库不存在（只读、不建库）；
    冷启动导入失败；库损坏；库与磁盘文件不同步且增量同步失败。
    """
    if sqlite_store.store_mode() != "sqlite":
        return None
    path = _db_path(data_dir)
    try:
        empty = not path.exists() or path.stat().st_size == 0
    except OSError:
        return None
    if empty:
        if not _is_default_dir(data_dir):
            return None                    # 别人的目录：只读，不建库、不写盘
        if not _bootstrap(path, data_dir or DATA):
            with _lock:                    # 记黑名单：本轮之内别再试一次注定失败的导入
                _sqlite_bad.add((str(path), kind, _signature(files)))
            return None
    key = (str(path), kind, _signature(files))
    if key in _sqlite_bad:
        return None
    if sqlite_store.is_synced(path, kind, files):
        return path
    # 库已建过但文件变了 → 就地增量同步（只重导变化的文件）；失败则记黑名单回落 JSON
    if sqlite_store.sync(path, {kind: (KINDS[kind][1], files)}) is None:
        with _lock:
            _sqlite_bad.add(key)
            if len(_sqlite_bad) > 64:
                _sqlite_bad.clear()
        return None
    return path


# ---------------------------------------------------------------- 文件与读取

def iter_files(pattern: str, data_dir: Path | None = None) -> list[Path]:
    """匹配文件的列表，按修改时间倒序（越新的文件在合并去重时优先级越高）。"""
    return sqlite_store.iter_source_files(pattern, data_dir or DATA)


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


def _merge_json_files(array_key: str, files: list[Path]) -> list[dict]:
    """JSON 路径：跨全部匹配文件按 id 去重（先出现的文件优先，即新文件优先）。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for path in files:
        data = read_json(path)
        for item in (data.get(array_key) or []):
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id", "") or "")
            if iid:
                if iid in seen:
                    continue
                seen.add(iid)
            merged.append(item)
    return merged


def _build_merged(kind: str, data_dir: Path | None = None):
    """构造合并函数：优先读 SQLite，库不可用时回落 JSON 全量合并。"""
    _pattern, array_key = KINDS[kind]

    def builder(files: list[Path]) -> list[dict]:
        db = _usable_db(kind, files, data_dir)
        if db is not None:
            items = sqlite_store.load_items(db, kind)
            if items is not None:
                return items
        return _merge_json_files(array_key, files)

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


def latest_source_file() -> Path | None:
    """全部原始数据文件中最近修改的一个 —— **仅供界面展示**「当前数据文件」。

    为什么单独标明「仅供展示」：
    数据现在是跨全部原始文件合并去重后得到的（见 raw_items），
    按「最新那个文件」取数正是被淘汰的旧口径——它曾在只抓了一天小快照时，
    让企业分析和投递推荐的样本远小于页面展示范围。
    此函数只服务于 /api/status 的 raw_file / raw_mtime 两个展示字段，
    业务逻辑请勿使用。
    """
    candidates: list[Path] = []
    for pattern, _label in KINDS.values():
        try:
            candidates.extend(iter_files(pattern))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def raw_items_in(kind: str, data_dir: Path) -> list[dict]:
    """指定数据目录下的合并结果（不走缓存，供 CLI / 测试使用）。"""
    if kind not in KINDS:
        raise KeyError(f"未知数据类型：{kind}")
    return _build_merged(kind, data_dir)(iter_files(KINDS[kind][0], data_dir))


def invalidate() -> None:
    """清空缓存（数据文件刚被后台任务重写时，可主动调用）。"""
    with _lock:
        _cache.clear()
        _sqlite_bad.clear()     # 数据刚被重写，之前同步失败的签名可能已经能成功了


# ---------------------------------------------------------------- 聚合统计

def _dates_of(kind: str, items: list[dict]) -> list[str]:
    """覆盖日期列表（升序）。日期口径由 sqlite_store.day_of 单点定义，切库前后一致。"""
    return sorted(d for d in (sqlite_store.day_of(kind, item) for item in items) if d)


def _missing_detail(kind: str, items: list[dict]) -> int:
    """详情缺失：招聘信息无正文（remarks/content 均空）。"""
    if kind != "recruit":
        return 0
    return sum(1 for item in items if sqlite_store.detail_len_of(item) == 0)


def aggregate(kind: str) -> dict:
    """单一数据类型的统计：文件数、记录数（去重后）、覆盖日期、最近更新时间、详情缺失。

    SQLite 可用时走 `stats()` 的 COUNT/MIN/MAX，不反序列化任何 payload——
    这是 master_summary（每秒都可能被 /api/status 触发）不被 56MB 原始数据拖慢的关键。
    """
    files = source_files(kind)
    db = _usable_db(kind, files)
    stat = sqlite_store.stats(db, kind) if db is not None else None
    if stat is None:
        items = raw_items(kind)
        dates = _dates_of(kind, items)
        stat = {"records": len(items), "missing_detail": _missing_detail(kind, items),
                "first_date": dates[0] if dates else "", "last_date": dates[-1] if dates else ""}
    last_update = ""
    if files:
        newest = max(files, key=os.path.getmtime)
        last_update = datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return {
        "kind": kind,
        "files": len(files),
        "records": stat["records"],
        "first_date": stat["first_date"],
        "last_date": stat["last_date"],
        "last_update": last_update,
        "missing_detail": stat["missing_detail"] if kind == "recruit" else 0,
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
