"""采集类接口：按日期范围抓取、招聘增量更新、宣讲会检查、默认日期。

这些端点本身**不干活**，只负责拼命令并交给 TaskManager 起子进程；
实时日志由 `/api/tasks` 轮询（后续计划换成 SSE）。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta

from extensions import tasks
from flask import Blueprint, jsonify, request
from settings import DATA, WORKDIR

bp = Blueprint("crawl", __name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@bp.route("/api/crawl", methods=["POST"])
def api_crawl():
    payload = request.get_json(force=True, silent=True) or {}
    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    if start and not _DATE_RE.match(start):
        return jsonify({"ok": False, "error": "开始日期格式应为 YYYY-MM-DD"}), 400
    if end and not _DATE_RE.match(end):
        return jsonify({"ok": False, "error": "结束日期格式应为 YYYY-MM-DD"}), 400
    if start and end and start > end:
        return jsonify({"ok": False, "error": "开始日期不能晚于结束日期"}), 400
    cmd = [sys.executable, str(WORKDIR / "crawler.py")]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    cmd += ["--output", str(DATA)]
    env = dict(os.environ)
    result = tasks.start("抓取", cmd, env, title=f"抓取招聘信息（{start or '默认'} ~ {end or '今日'}）")
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@bp.route("/api/recruit/update", methods=["POST"])
def api_recruit_update():
    """增量更新招聘信息（招聘公告 + 双选会）。

    抓取学校网站最新列表，与本地已有数据**按 ID 比对**，只把新增记录合并进主库
    （`check_update.py --kind recruit`，与每日 09:00 计划任务同一套逻辑）。
    与「按日期范围抓取」不同：不新建单日快照文件，因此页面/分析/推荐的数据口径不会被打散。
    """
    cmd = [sys.executable, str(WORKDIR / "check_update.py"), "--kind", "recruit"]
    env = dict(os.environ)
    result = tasks.start("招聘更新", cmd, env, title="更新招聘信息（增量合并进主库）")
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@bp.route("/api/crawl/dates")
def api_crawl_dates():
    """返回默认抓取起止日期（默认近 30 天 / 今日）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    return jsonify({"start": start, "end": today, "today": today})


@bp.route("/api/preach/check", methods=["POST"])
def api_preach_check():
    """检查宣讲会是否有新增（抓取今年最新列表 vs 已有缓存），后台任务 + 实时日志。"""
    payload = request.get_json(force=True, silent=True) or {}
    cmd = [sys.executable, str(WORKDIR / "check_update.py"), "--kind", "preach"]
    if payload.get("all_types"):
        cmd.append("--all-types")
    env = dict(os.environ)
    result = tasks.start("宣讲会检查", cmd, env, title="检查宣讲会更新")
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)
