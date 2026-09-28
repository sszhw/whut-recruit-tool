"""全局状态的构造与变更检测。

同时供 `/api/status`（一次性查询）与 `/api/events`（SSE 推送）使用，
保证两条路径拿到的是**同一份口径**，不会出现「轮询看到 A、推送看到 B」。

关键设计：**revision 必须便宜**。
`build_status_payload()` 要合并全部原始文件，代价不小；SSE 每秒都要判断
「变没变」，如果每次都构造完整 payload，就比前端轮询还贵。
所以 revision 只由 stat() 与内存任务状态构成，payload 仅在 revision 变化时才构造。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import analyze
import repository as repo
from dataloaders import load_cache
from extensions import tasks
from settings import (
    CACHE_PATH,
    CONFIG_PATH,
    DATA,
    LLM_PROVIDERS,
    _mask,
    get_llm,
    load_config,
)

# 需要侦听变化的产物文件：抓取/分析跑完会改这些
_WATCHED_PRODUCTS = (
    DATA / analyze.CSV_NAME,
    DATA / analyze.MD_NAME,
    DATA / "宣讲会_工作地流动.csv",
    DATA / "宣讲会_工作地流动报告.md",
    CACHE_PATH,
)


def _stat_signature(paths) -> str:
    """一组路径的 (名字, mtime, 大小) 签名；文件不存在记 '-'。"""
    parts = []
    for p in paths:
        try:
            st = os.stat(p)
            parts.append(f"{Path(p).name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            parts.append(f"{Path(p).name}:-")
    return "|".join(parts)


def _raw_files_signature() -> str:
    """原始数据文件的签名。

    用 glob + stat 而不是 `master_summary()`：后者会把所有文件读进内存再合并，
    每秒跑一次代价太高，而这里只需要知道「有没有新增/改动」。
    """
    files = sorted(DATA.glob(repo.RECRUIT_GLOB)) + sorted(DATA.glob(repo.PREACH_GLOB))
    return _stat_signature(files) + f"#{len(files)}"


def _tasks_signature() -> str:
    """任务状态的签名：只看 id / 状态 / 日志行数 / 进度，不拼完整日志。"""
    parts = []
    with tasks.lock:
        for tid in sorted(tasks.tasks):
            t = tasks.tasks[tid]
            prog = t.get("progress") or {}
            parts.append(f"{tid}:{t.get('status')}:{len(t.get('lines') or [])}:"
                         f"{prog.get('done')}/{prog.get('total')}")
    return "|".join(parts)


def status_revision() -> str:
    """当前全局状态的指纹。变化即代表前端需要刷新。"""
    return "#".join((
        _raw_files_signature(),
        _stat_signature(_WATCHED_PRODUCTS),
        _stat_signature([CONFIG_PATH]),
        _tasks_signature(),
    ))


def _pub(kind: str):
    """取某类任务的最新一条对外视图；没有该类任务时为 None。

    `tasks.latest()` 会返回 None，而 `tasks.public()` 只接受 dict，
    直接串起来会在「这类任务从没跑过」时抛 TypeError。
    """
    task = tasks.latest(kind)
    return tasks.public(task) if task else None


def build_status_payload() -> dict:
    """构造 /api/status 的完整响应体（SSE 的 `status` 事件体与之完全一致）。"""
    cfg = load_config()
    cache = load_cache()
    llm = get_llm()
    summary = repo.master_summary()   # 主数据统一口径：跨全部原始文件合并 + 按 ID 去重
    raw = repo.latest_source_file()   # 仅用于界面「数据文件」展示，取数请用 raw_items
    return {
        "has_api_key": bool(llm["api_key"]),
        "api_key_masked": _mask(llm["api_key"]),
        "model": cfg.get("model", ""),
        "provider": llm["provider"],
        "llm_model": llm["model"],
        "models": LLM_PROVIDERS[llm["provider"]].get("models", []),
        "default_model": LLM_PROVIDERS[llm["provider"]].get("default_model", ""),
        "raw_file": raw.name if raw else "",
        "raw_mtime": datetime.fromtimestamp(raw.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if raw else "",
        "recruit_count": summary["recruit_count"],
        "fair_count": summary["fair_count"],
        "preach_count": summary["preach_count"],
        "analyzed_count": len(cache),
        "unanalyzed_count": len(repo.unanalyzed(cache)),
        "master_files": summary["files"],
        "coverage_start": summary["coverage_start"],
        "coverage_end": summary["coverage_end"],
        "data_updated": summary["last_update"],
        "running_tasks": len(tasks.running()),
        "crawler_task": _pub("抓取"),
        "update_task": _pub("招聘更新"),
        "analyze_task": _pub("分析"),
        "check_task": _pub("宣讲会检查"),
        # 工作地流动分析：原先前端单独用 1.5s 轮询拉这一个任务的日志，
        # 一并纳入状态推送后那条轮询就可以去掉
        "flow_task": _pub("工作地流动"),
        "outputs": {
            "csv": (DATA / analyze.CSV_NAME).exists(),
            "md": (DATA / analyze.MD_NAME).exists(),
        },
    }


def build_tasks_payload(limit: int = 60) -> dict:
    """统一任务中心的数据源（SSE 的 `tasks` 事件体与之完全一致）。"""
    rows = tasks.history_list(limit=limit)
    return {"ok": True, "running": tasks.running(), "tasks": rows, "count": len(rows)}
