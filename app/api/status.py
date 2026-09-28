"""页面与全局状态：首页、/api/status、/api/health、/api/tasks。

这一组是前端每次加载都要打的「头部」接口：
- `/api/status`  决定配置是否已就绪、主库有多少数据、有没有任务在跑；
- `/api/health`  数据质量体检（缺失 / 未分析 / 工作地未确定）；
- `/api/tasks`   统一任务中心抽屉的数据源。
"""

from __future__ import annotations

from datetime import datetime

import analyze
import repository as repo
from dataloaders import load_cache, work_undetermined_count
from extensions import tasks
from flask import Blueprint, jsonify, request, send_file
from settings import (
    DATA,
    INDEX_HTML,
    LLM_PROVIDERS,
    _mask,
    get_llm,
    load_config,
)

bp = Blueprint("status", __name__)


@bp.route("/")
def index():
    """首页：单页应用（ui.html 由服务端直出）。"""
    if not INDEX_HTML.exists():
        return "缺少 ui.html", 500
    return send_file(INDEX_HTML)


@bp.route("/api/status")
def api_status():
    cfg = load_config()
    cache = load_cache()
    llm = get_llm()
    summary = repo.master_summary()   # 主数据统一口径：跨全部原始文件合并 + 按 ID 去重
    raw = repo.latest_source_file()   # 仅用于界面「数据文件」展示，取数请用 raw_items
    task_crawler = tasks.latest("抓取")
    task_update = tasks.latest("招聘更新")
    task_analyze = tasks.latest("分析")
    task_check = tasks.latest("宣讲会检查")
    return jsonify({
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
        "crawler_task": tasks.public(task_crawler) if task_crawler else None,
        "update_task": tasks.public(task_update) if task_update else None,
        "analyze_task": tasks.public(task_analyze) if task_analyze else None,
        "check_task": tasks.public(task_check) if task_check else None,
        "outputs": {
            "csv": (DATA / analyze.CSV_NAME).exists(),
            "md": (DATA / analyze.MD_NAME).exists(),
        },
    })


@bp.route("/api/health")
def api_health():
    """数据健康：最近抓取/更新时间、主库记录数、覆盖日期、详情缺失、未分析企业、工作地未确定。"""
    cache = load_cache()
    payload = repo.health(cache=cache, work_undetermined=work_undetermined_count())
    hist = tasks.history_list(limit=1)
    payload["last_task"] = hist[0] if hist else None
    payload["analyzed_count"] = len(cache)
    return jsonify({"ok": True, "health": payload})


@bp.route("/api/tasks")
def api_tasks():
    """统一任务中心：运行中的任务 + 历史任务（服务重启后仍可查看）。"""
    limit = min(200, max(1, request.args.get("limit", 60, type=int)))
    rows = tasks.history_list(limit=limit)
    return jsonify({"ok": True, "running": tasks.running(), "tasks": rows, "count": len(rows)})
