"""页面与全局状态：首页、/api/status、/api/health、/api/tasks。

这一组是前端每次加载都要打的「头部」接口：
- `/api/status`  决定配置是否已就绪、主库有多少数据、有没有任务在跑；
- `/api/health`  数据质量体检（缺失 / 未分析 / 工作地未确定）；
- `/api/tasks`   统一任务中心抽屉的数据源。
"""

from __future__ import annotations

import repository as repo
from dataloaders import load_cache, work_undetermined_count
from extensions import tasks
from flask import Blueprint, jsonify, request, send_file
from services.status import build_status_payload, build_tasks_payload
from settings import INDEX_HTML

bp = Blueprint("status", __name__)


@bp.route("/")
def index():
    """首页：单页应用（ui.html 由服务端直出）。"""
    if not INDEX_HTML.exists():
        return "缺少 ui.html", 500
    return send_file(INDEX_HTML)


@bp.route("/api/status")
def api_status():
    # 构造逻辑在 services.status，与 SSE 的 status 事件同源，
    # 保证「轮询拿到的」和「推送拿到的」永远是同一份口径
    return jsonify(build_status_payload())


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
    return jsonify(build_tasks_payload(limit=limit))
