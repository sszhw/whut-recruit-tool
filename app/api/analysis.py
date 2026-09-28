"""分析类与任务控制接口。

- `/api/analyze`     起企业性质 + 工作地点分析子进程（`--merge` 走主库全量口径）；
- `/api/preach/flow` 起宣讲会工作地流动分析，支持离线 / AI 两种方式；
- `/api/task/*`      查日志、停止任务。

分析范围统一取 repository 合并后的全部记录，而非「最新的那个原始数据文件」。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import datetime

import analyze
import repository as repo
from dataloaders import load_cache
from extensions import tasks
from flask import Blueprint, jsonify, request
from settings import DATA, WORKDIR, get_llm

bp = Blueprint("analysis", __name__)

FLOW_CSV = DATA / "宣讲会_工作地流动.csv"
FLOW_MD = DATA / "宣讲会_工作地流动报告.md"


def _llm_env(base: Mapping[str, str]) -> dict[str, str]:
    """把当前厂商的 Key / BaseURL / model 注入子进程环境变量。

    入参是 Mapping 而非 dict：调用方传的是 os.environ（_Environ[str]），
    它是 MutableMapping 但不是 dict 子类。这里只读 + dict(base) 拷贝，Mapping 足够。
    """
    llm = get_llm()
    env = dict(base)
    env["LLM_BASE_URL"] = llm["base_url"]
    env["LLM_MODEL"] = llm["model"]
    env["LLM_API_KEY"] = llm["api_key"]
    # 兼容仍读 SILICONFLOW_* 的旧脚本
    env["SILICONFLOW_API_KEY"] = llm["api_key"]
    env["SILICONFLOW_BASE_URL"] = llm["base_url"]
    return env


@bp.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(force=True, silent=True) or {}
    llm = get_llm()
    if not llm["api_key"]:
        return jsonify({"ok": False, "error": f"请先在设置中配置 {llm['label']} API Key"}), 400
    # 统一口径：分析范围取 repository 合并后的全部招聘信息（而非最新那个文件），
    # 避免「最新文件只是当日小快照」导致分析样本小于页面展示范围。
    if not repo.raw_items("recruit"):
        return jsonify({"ok": False, "error": "没有原始数据，请先运行抓取"}), 400
    limit = int(payload.get("limit") or 0)
    cmd = [sys.executable, str(WORKDIR / "analyze.py"), "--model", llm["model"], "--merge"]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    # 只重算过期条目（prompt 升级 / 公告变更 / 此前失败）：默认模式会跳过全部已缓存企业，
    # 提示词改了规则后旧结论永远不会更新，用户却以为看到的是新口径下的判断。
    if payload.get("only_stale"):
        cmd += ["--only-stale"]
        title = f"重算过期企业分析（{llm['model']}）"
    else:
        title = f"AI 分析企业性质与工作地点（{llm['model']}）"
    result = tasks.start("分析", cmd, _llm_env(os.environ), title=title)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@bp.route("/api/analyze/stale", methods=["GET"])
def api_analyze_stale():
    """列出需要重算的企业：先看看规模再决定要不要跑，别一上来就烧 token。"""
    companies = analyze.extract_companies(repo.raw_items("recruit"))
    info = analyze.stale_entries(load_cache(), companies)
    return jsonify({"ok": True, "stale": len(info["stale"]), "reasons": info["reasons"],
                    "prompt_version": info["prompt_version"], "total": info["total"]})


@bp.route("/api/flow", methods=["GET"])
def api_flow_status():
    """工作地流动分析：返回报告是否存在及生成时间。"""
    return jsonify({
        "ok": True,
        "csv_exists": FLOW_CSV.exists(),
        "md_exists": FLOW_MD.exists(),
        "csv_mtime": datetime.fromtimestamp(FLOW_CSV.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                     if FLOW_CSV.exists() else None,
    })


@bp.route("/api/preach/flow", methods=["POST"])
def api_preach_flow():
    """触发宣讲会企业「工作地流动」分析（offline 离线 / ai 走 LLM），后台任务。"""
    payload = request.get_json(force=True, silent=True) or {}
    method = payload.get("method", "offline")
    if method not in ("offline", "ai"):
        return jsonify({"ok": False, "error": "method 只能是 offline / ai"}), 400
    if method == "ai" and not get_llm()["api_key"]:
        return jsonify({"ok": False, "error": f"AI 方式需要先配置 {get_llm()['label']} API Key"}), 400
    cmd = [sys.executable, str(WORKDIR / "analyze_preach.py"), "--method", method]
    limit = int(payload.get("limit") or 0)
    if limit > 0:
        cmd += ["--limit", str(limit)]
    env = _llm_env(os.environ) if method == "ai" else dict(os.environ)
    title = f"宣讲会工作地流动分析（{'AI' if method == 'ai' else '离线'}）"
    result = tasks.start("工作地流动", cmd, env, title=title)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@bp.route("/api/task/<task_id>/log")
def api_task_log(task_id):
    """任务日志：运行中的任务取内存缓冲；历史任务（含服务重启前）回落到日志文件。"""
    tail = request.args.get("tail", 0, type=int) or 200
    with tasks.lock:
        live = tasks.tasks.get(task_id)
    if live:
        return jsonify({"ok": True, "task": tasks.public(live, tail=tail)})
    record = tasks.get(task_id)
    if not record:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    record = dict(record)
    record["lines"] = tasks.read_log(task_id, tail=tail)
    return jsonify({"ok": True, "task": record})


@bp.route("/api/task/stop", methods=["POST"])
def api_task_stop():
    payload = request.get_json(force=True, silent=True) or {}
    task_id = str(payload.get("task_id", "")).strip()
    if not task_id:
        return jsonify({"ok": False, "error": "缺少 task_id"}), 400
    with tasks.lock:
        task = tasks.tasks.get(task_id)
        if not task or not task["running"]:
            return jsonify({"ok": False, "error": "任务不存在或已结束"}), 400
        task["status"] = "cancelled"
        task["proc"].terminate()
    return jsonify({"ok": True, "task_id": task_id, "status": "cancelled"})
