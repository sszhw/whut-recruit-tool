#!/usr/bin/env python3
"""武汉理工招聘信息采集与企业分析工具 —— Web 界面服务。

启动：
    python server.py            # 默认 http://127.0.0.1:8765
    python server.py --port 9000

功能：
    - 一键抓取招聘信息（crawler.py，后台任务 + 实时日志）
    - 一键 AI 分析企业性质与工作地点（analyze.py + 硅基流动 API）
    - 浏览/搜索/筛选招聘信息与企业分析结果
    - 查看统计报告、导出 CSV/Markdown
    - 在界面中配置硅基流动 API Key（保存到 config.json，本地使用）
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

SCRIPT_DIR = Path(__file__).resolve().parent          # 代码所在目录（运行工具/）
ROOT = SCRIPT_DIR.parent                              # 项目根目录（数据/配置所在）
WORKDIR = SCRIPT_DIR                                  # 兼容旧引用：指代码目录
CONFIG_PATH = ROOT / "config.json"
CACHE_PATH = ROOT / "企业分析_缓存.json"
FAV_PATH = ROOT / "收藏_宣讲会.json"

import analyze  # noqa: E402  复用 analyze.py 的工具函数
import crawler  # noqa: E402  复用 time_text / plain_text
import resume   # noqa: E402  简历解析 + 投递推荐

app = Flask(__name__)
app.json.ensure_ascii = False

# ---------------------------------------------------------------- 配置管理

def load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    cfg.setdefault("api_key", os.environ.get("SILICONFLOW_API_KEY", ""))
    cfg.setdefault("model", analyze.DEFAULT_MODEL)
    cfg.setdefault("provider", os.environ.get("LLM_PROVIDER", "siliconflow"))
    cfg.setdefault("deepseek_api_key", "")
    cfg.setdefault("deepseek_model", "")
    cfg.setdefault("deepseek_base_url", "https://api.deepseek.com")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_api_key() -> str:
    return (load_config().get("api_key") or "").strip()


LLM_PROVIDERS = {
    "siliconflow": {"label": "硅基流动", "base_url": "https://api.siliconflow.cn/v1",
                    "api_key_field": "api_key", "model_field": "model", "default_model": "Qwen/Qwen2.5-72B-Instruct"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com",
                 "api_key_field": "deepseek_api_key", "model_field": "deepseek_model", "default_model": "deepseek-chat"},
}


def get_llm() -> dict:
    """按配置返回当前 LLM 提供商的 base_url / api_key / model / label。"""
    cfg = load_config()
    provider = cfg.get("provider", "siliconflow")
    if provider not in LLM_PROVIDERS:
        provider = "siliconflow"
    conf = LLM_PROVIDERS[provider]
    api_key = (cfg.get(conf["api_key_field"], "") or "").strip()
    model = (cfg.get(conf["model_field"], "") or "").strip() or conf["default_model"]
    if provider == "deepseek":
        base_url = (cfg.get("deepseek_base_url", "").strip() or conf["base_url"]).rstrip("/")
    else:
        base_url = conf["base_url"]
    return {"provider": provider, "label": conf["label"], "base_url": base_url,
            "api_key": api_key, "model": model}


# ---------------------------------------------------------------- 后台任务

class TaskManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tasks: dict[str, dict] = {}

    def start(self, kind: str, cmd: list[str], env: dict) -> dict:
        with self.lock:
            for task in self.tasks.values():
                if task["kind"] == kind and task["running"]:
                    return {"ok": False, "error": f"已有{kind}任务在运行", "task": task}
            task_id = f"{kind}_{int(time.time())}"
            env = dict(env)
            env["PYTHONUNBUFFERED"] = "1"  # 让子进程 stdout 实时刷新，界面日志即时可见
            proc = subprocess.Popen(
                cmd, cwd=str(WORKDIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            task = {"id": task_id, "kind": kind, "running": True, "exit_code": None,
                    "started": datetime.now().strftime("%H:%M:%S"), "proc": proc, "lines": []}
            self.tasks[task_id] = task
        threading.Thread(target=self._pump, args=(task,), daemon=True).start()
        return {"ok": True, "task": self.public(task)}

    def _pump(self, task: dict) -> None:
        proc = task["proc"]
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line:
                with self.lock:
                    task["lines"].append(line)
                    if len(task["lines"]) > 5000:
                        del task["lines"][:2500]
        proc.wait()
        with self.lock:
            task["running"] = False
            task["exit_code"] = proc.returncode
            task["finished"] = datetime.now().strftime("%H:%M:%S")

    def public(self, task: dict, tail: int = 0) -> dict:
        with self.lock:
            lines = task["lines"][-tail:] if tail else list(task["lines"])
            return {"id": task["id"], "kind": task["kind"], "running": task["running"],
                    "exit_code": task["exit_code"], "started": task.get("started"),
                    "finished": task.get("finished"), "lines": lines}

    def latest(self, kind: str) -> dict | None:
        with self.lock:
            candidates = [t for t in self.tasks.values() if t["kind"] == kind]
        return max(candidates, key=lambda t: t["id"]) if candidates else None


tasks = TaskManager()

# ---------------------------------------------------------------- 数据读取

def _newest_glob(pattern: str) -> Path | None:
    """取项目根下匹配 pattern 的文件中最新的一个。"""
    candidates = sorted(glob.glob(str(ROOT / pattern)), key=os.path.getmtime, reverse=True)
    return Path(candidates[0]) if candidates else None


def latest_raw_json() -> Path | None:
    """通用：任一新旧兼容（仅用于状态展示/向前兼容，精确取数请用下面的专用函数）。"""
    return _newest_glob("*_原始数据.json")


def latest_recruit_json() -> Path | None:
    """招聘信息 + 双选会所在的原始数据文件。"""
    return _newest_glob("武汉理工大学招聘信息_*_原始数据.json")


def latest_preach_json() -> Path | None:
    """宣讲会所在的原始数据文件。"""
    return _newest_glob("宣讲会_*_原始数据.json")


def _company_work_map() -> dict[str, list[str]]:
    """单位名称 → 工作地城市列表。

    优先读取本项目已生成的《宣讲会_工作地流动.csv》（457 家单位均已映射）。
    文件缺失时返回空 dict，由调用方回退到 analyze_preach.infer_work_cities 逐条推断。
    """
    csv_path = ROOT / "宣讲会_工作地流动.csv"
    mapping: dict[str, list[str]] = {}
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    name = (row.get("单位名称") or "").strip()
                    if not name:
                        continue
                    cities = [c.strip() for c in (row.get("工作地城市") or "").split("、") if c.strip()]
                    mapping[name] = cities
        except (OSError, csv.Error):
            mapping = {}
    return mapping


def _iter_recruit_files() -> list[Path]:
    """所有招聘信息原始数据文件（按修改时间倒序）。"""
    return [Path(p) for p in sorted(glob.glob(str(ROOT / "武汉理工大学招聘信息_*_原始数据.json")),
                                    key=os.path.getmtime, reverse=True)]


def load_recruitments() -> list[dict]:
    seen: dict[str, dict] = {}
    today_str = datetime.now().strftime("%Y-%m-%d")
    for path in _iter_recruit_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get("招聘信息", []):
            iid = str(item.get("id", ""))
            if iid and iid in seen:
                continue
            add_date = crawler.time_text(item.get("addtime"), with_time=False)
            seen[iid] = {
                "发布日期": crawler.time_text(item.get("addtime")),
                "今日更新": bool(add_date) and add_date == today_str,
                "标题": item.get("title", ""),
                "单位": item.get("com_id_name", ""),
                "原网页": item.get("httpurl") or f"https://scc.whut.edu.cn/#/recruitmentInformation/notice?type=enrollment&id={item.get('id','')}",
                "ID": item.get("id", ""),
                "正文": crawler.plain_text(item.get("remarks") or item.get("content") or "")[:600],
            }
    rows = list(seen.values())
    rows.sort(key=lambda r: r["发布日期"], reverse=True)
    return rows


def load_fairs() -> list[dict]:
    seen: dict[str, dict] = {}
    for path in _iter_recruit_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for f in data.get("双选会", []):
            iid = str(f.get("id", ""))
            if iid and iid in seen:
                continue
            seen[iid] = {
                "标题": f.get("title", ""),
                "地点": f.get("field_id_name", ""),
                "举办时间": f"{crawler.time_text(f.get('start_time'))} 至 {crawler.time_text(f.get('end_time'))}",
                "参会单位数": f.get("verify_count", ""),
                "原网页": f"https://scc.whut.edu.cn/#/doubleElection/{f.get('id','')}",
            }
    return list(seen.values())


def load_preachs(past: bool = False) -> list[dict]:
    path = latest_preach_json()
    if not path:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    soon_end = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    work_map = _company_work_map()
    fallback = None  # 惰性导入 analyze_preach
    rows = []
    for item in data.get("宣讲会", []):
        hold_date = item.get("hold_date", "")
        if not past and hold_date and hold_date.strip() < today_str:
            continue  # 默认只看当天及以后（过去的宣讲会隐藏）
        start = item.get("hold_starttime", "")
        end = item.get("hold_endtime", "")
        period = f"{hold_date} {start}~{end}".strip(" ~")
        city = item.get("city_id_name", "") or ""
        venue = (item.get("address", "") or "").strip()
        venue_full = "，".join(part for part in (city, venue) if part)
        name = item.get("com_id_name", "") or ""
        # 工作地城市：优先用工作地流动 CSV 映射，缺失则离线推断
        if name in work_map:
            work_cities = work_map[name]
        else:
            if fallback is None:
                import analyze_preach  # noqa: PLC0415  本函数内存活于请求上下文
                fallback = analyze_preach
            title = item.get("title", "")
            text = "\n".join(str(x) for x in [
                title, item.get("purpose", ""), item.get("address", ""),
                "、".join(str(j.get("city_id_name", "")) for j in (item.get("JobList") or []) if isinstance(j, dict)),
            ] if x)
            work_cities = fallback.infer_work_cities(name, title, text)
        work_cities = [c for c in work_cities if c and c != "未确定"]
        # 提醒标注：今日新出(addtime==今天) / 3天内开始(举办日期在未来3天内)
        try:
            add_date = datetime.fromtimestamp(int(item.get("addtime"))).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            add_date = ""
        new_today = bool(add_date) and add_date == today_str
        soon3 = bool(hold_date) and today_str <= hold_date <= soon_end
        reminds = []
        if new_today:
            reminds.append("今日新出")
        if soon3:
            reminds.append("3天内开始")
        rows.append({
            "宣讲时间": period,
            "举办日期": hold_date,
            "开始时间": start,
            "结束时间": end,
            "单位名称": name,
            "宣讲会地点": venue_full,
            "城市": city,
            "场馆": venue,
            "线下/线上": "线下" if crawler._int_or(item.get("air_type"), 0) == 0 else "线上",
            "标题": item.get("title", ""),
            "公司地点": "、".join(work_cities) if work_cities else "未确定",
            "work_cities": work_cities,
            "原网页": item.get("httpurl") or f"https://scc.whut.edu.cn/#/preachMeeting/{item.get('id','')}/1",
            "ID": item.get("id", ""),
            "正文": crawler.plain_text(item.get("remarks") or item.get("purpose") or "")[:600],
            "今日新出": new_today,
            "3天内开始": soon3,
            "提醒": reminds,
        })
    return rows


def load_preach_favs() -> set[str]:
    """读取收藏的宣讲会 ID 集合。"""
    if not FAV_PATH.exists():
        return set()
    try:
        data = json.loads(FAV_PATH.read_text(encoding="utf-8"))
        return set(data.get("ids") or [])
    except (json.JSONDecodeError, OSError):
        return set()


def save_preach_favs(ids: set[str]) -> None:
    """持久化收藏的宣讲会 ID 集合。"""
    FAV_PATH.write_text(json.dumps({"ids": sorted(ids)}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

# ---------------------------------------------------------------- 页面与静态

INDEX_HTML = WORKDIR / "ui.html"


@app.route("/")
def index():
    if not INDEX_HTML.exists():
        return "缺少 ui.html", 500
    return send_file(INDEX_HTML)

# ---------------------------------------------------------------- API：状态

def _count_ids(pattern: str, key: str) -> int:
    """跨所有匹配文件，按 id 去重后统计某类记录数量（与增量爬取去重保持一致）。"""
    ids: set[str] = set()
    for path in glob.glob(str(ROOT / pattern)):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get(key, []) or []:
            iid = item.get("id")
            if iid:
                ids.add(str(iid))
    return len(ids)


@app.route("/api/status")
def api_status():
    cfg = load_config()
    cache = load_cache()
    # 跨文件按 id 去重计数，避免增量爬取后最新文件只含新增记录导致计数虚低
    recruit_count = _count_ids("武汉理工大学招聘信息_*_原始数据.json", "招聘信息")
    preach_count = _count_ids("宣讲会_*_原始数据.json", "宣讲会")
    raw = latest_raw_json()  # 用于界面"数据文件"展示，取最新即可
    task_crawler = tasks.latest("抓取")
    task_analyze = tasks.latest("分析")
    return jsonify({
        "has_api_key": bool(cfg.get("api_key", "").strip()),
        "api_key_masked": (cfg.get("api_key", "")[:6] + "****") if cfg.get("api_key") else "",
        "model": cfg.get("model", ""),
        "provider": get_llm()["provider"],
        "llm_model": get_llm()["model"],
        "raw_file": raw.name if raw else "",
        "raw_mtime": datetime.fromtimestamp(raw.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if raw else "",
        "recruit_count": recruit_count,
        "preach_count": preach_count,
        "analyzed_count": len(cache),
        "crawler_task": tasks.public(task_crawler) if task_crawler else None,
        "analyze_task": tasks.public(task_analyze) if task_analyze else None,
        "check_task": tasks.public(tasks.latest("宣讲会检查")) if tasks.latest("宣讲会检查") else None,
        "outputs": {
            "csv": (ROOT / analyze.CSV_NAME).exists(),
            "md": (ROOT / analyze.MD_NAME).exists(),
        },
    })

# ---------------------------------------------------------------- API：配置

@app.route("/api/config", methods=["POST"])
def api_config():
    payload = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    if "api_key" in payload:
        cfg["api_key"] = str(payload["api_key"]).strip()
    if "model" in payload and str(payload["model"]).strip():
        cfg["model"] = str(payload["model"]).strip()
    for f in ("provider", "deepseek_api_key", "deepseek_model", "deepseek_base_url"):
        if f in payload:
            cfg[f] = str(payload[f]).strip()
    save_config(cfg)
    return jsonify({"ok": True})

# ---------------------------------------------------------------- API：抓取

@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    payload = request.get_json(force=True, silent=True) or {}
    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    if start and not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        return jsonify({"ok": False, "error": "开始日期格式应为 YYYY-MM-DD"}), 400
    if end and not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
        return jsonify({"ok": False, "error": "结束日期格式应为 YYYY-MM-DD"}), 400
    cmd = [sys.executable, str(WORKDIR / "crawler.py")]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    cmd += ["--output", str(ROOT)]
    env = dict(os.environ)
    result = tasks.start("抓取", cmd, env)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)

@app.route("/api/crawl/today", methods=["POST"])
def api_crawl_today():
    """一键抓取今日（当天）的招聘信息。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cmd = [sys.executable, str(WORKDIR / "crawler.py"),
           "--start", today, "--end", today, "--output", str(ROOT)]
    env = dict(os.environ)
    result = tasks.start("抓取", cmd, env)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)

@app.route("/api/crawl/dates")
def api_crawl_dates():
    """返回默认抓取起止日期（默认近 30 天 / 今日）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    return jsonify({"start": start, "end": today, "today": today})


@app.route("/api/preach/check", methods=["POST"])
def api_preach_check():
    """检查宣讲会是否有新增（抓取今年最新列表 vs 已有缓存），后台任务 + 实时日志。"""
    payload = request.get_json(force=True, silent=True) or {}
    cmd = [sys.executable, str(WORKDIR / "check_preach_update.py")]
    if payload.get("all_types"):
        cmd.append("--all-types")
    env = dict(os.environ)
    result = tasks.start("宣讲会检查", cmd, env)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


# ---------------------------------------------------------------- API：分析

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(force=True, silent=True) or {}
    llm = get_llm()
    if not llm["api_key"]:
        return jsonify({"ok": False, "error": f"请先在设置中配置 {llm['label']} API Key"}), 400
    if not latest_raw_json():
        return jsonify({"ok": False, "error": "没有原始数据，请先运行抓取"}), 400
    limit = int(payload.get("limit") or 0)
    cmd = [sys.executable, str(WORKDIR / "analyze.py"), "--model", llm["model"]]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    env = dict(os.environ)
    env["LLM_BASE_URL"] = llm["base_url"]
    env["LLM_MODEL"] = llm["model"]
    env["LLM_API_KEY"] = llm["api_key"]
    env["SILICONFLOW_API_KEY"] = llm["api_key"]
    env["SILICONFLOW_BASE_URL"] = llm["base_url"]
    result = tasks.start("分析", cmd, env)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/flow", methods=["GET"])
def api_flow_status():
    """工作地流动分析：返回报告是否存在及生成时间。"""
    csv = ROOT / "宣讲会_工作地流动.csv"
    md = ROOT / "宣讲会_工作地流动报告.md"
    return jsonify({
        "ok": True,
        "csv_exists": csv.exists(),
        "md_exists": md.exists(),
        "csv_mtime": datetime.fromtimestamp(csv.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if csv.exists() else None,
    })


@app.route("/api/preach/flow", methods=["POST"])
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
    env = dict(os.environ)
    if method == "ai":
        llm = get_llm()
        env["LLM_BASE_URL"] = llm["base_url"]
        env["LLM_MODEL"] = llm["model"]
        env["LLM_API_KEY"] = llm["api_key"]
        env["SILICONFLOW_API_KEY"] = llm["api_key"]
        env["SILICONFLOW_BASE_URL"] = llm["base_url"]
    result = tasks.start("工作地流动", cmd, env)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/task/<task_id>/log")
def api_task_log(task_id):
    tail = request.args.get("tail", 0, type=int)
    with tasks.lock:
        task = tasks.tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "task": tasks.public(task, tail=tail or 200)})


@app.route("/api/task/stop", methods=["POST"])
def api_task_stop():
    payload = request.get_json(force=True, silent=True) or {}
    task_id = payload.get("task_id", "")
    with tasks.lock:
        task = tasks.tasks.get(task_id)
    if not task or not task["running"]:
        return jsonify({"ok": False, "error": "任务不存在或已结束"}), 400
    task["proc"].terminate()
    return jsonify({"ok": True})

# ---------------------------------------------------------------- API：数据浏览

@app.route("/api/recruitments")
def api_recruitments():
    q = request.args.get("q", "").strip().lower()
    rows = load_recruitments()
    if q:
        rows = [r for r in rows if q in r["标题"].lower() or q in r["单位"].lower()]
    total = len(rows)
    page = max(1, request.args.get("page", 1, type=int))
    size = min(200, max(10, request.args.get("size", 50, type=int)))
    start = (page - 1) * size
    return jsonify({"total": total, "page": page, "size": size, "rows": rows[start:start + size]})


@app.route("/api/fairs")
def api_fairs():
    return jsonify({"rows": load_fairs()})


def _venue_label(addr: str) -> str:
    """场馆下拉展示用的短标签，去掉括号内的长说明（如"该场地仅用于…"）。"""
    for marker in ("（该场地仅用于", "(该场地仅用于"):
        idx = addr.find(marker)
        if idx != -1:
            return addr[:idx].strip()
    return addr


@app.route("/api/preachs/filters")
def api_preachs_filters():
    """返回宣讲会筛选项：公司地点（工作地城市）+ 宣讲会地点（具体场馆）。"""
    rows = load_preachs()
    work_counts: Counter[str] = Counter()
    for r in rows:
        for w in r.get("work_cities") or []:
            work_counts[w] += 1
    work_opts = [{"value": c, "count": n} for c, n in work_counts.most_common()]
    venues: dict[str, int] = {}
    for r in rows:
        addr = str(r.get("场馆", "")).strip()
        if addr:
            venues[addr] = venues.get(addr, 0) + 1
    venue_opts = [{"value": a, "label": _venue_label(a), "count": c}
                  for a, c in sorted(venues.items(), key=lambda kv: (-kv[1], kv[0]))]
    return jsonify({"work_cities": work_opts, "venues": venue_opts})


def _apply_preach_filters(rows: list[dict], q: str, ptype: str,
                          venue: str, work: str, start_d: str, end_d: str) -> list[dict]:
    """对宣讲会行应用 搜索 / 类型 / 场馆 / 工作地 / 日期范围 过滤。"""
    if q:
        rows = [r for r in rows
                if q in str(r["单位名称"]).lower() or q in str(r["标题"]).lower()
                or q in str(r["宣讲会地点"]).lower() or q in str(r["城市"]).lower()
                or q in str(r["公司地点"]).lower()]
    if ptype:
        rows = [r for r in rows if r["线下/线上"] == ptype]
    if venue:
        rows = [r for r in rows if str(r.get("场馆", "")).strip() == venue]
    if work:
        rows = [r for r in rows if any(work in str(w) for w in (r.get("work_cities") or []))]
    if start_d or end_d:
        rows = [r for r in rows
                if (not start_d or (r.get("举办日期") or "") >= start_d)
                and (not end_d or (r.get("举办日期") or "") <= end_d)]
    return rows


@app.route("/api/preachs")
def api_preachs():
    q = request.args.get("q", "").strip().lower()
    ptype = request.args.get("type", "").strip()      # 线下 / 线上 / ""
    venue = request.args.get("venue", "").strip()      # 宣讲会地点（具体场馆 address）
    work = request.args.get("work", "").strip()        # 公司地点（工作地城市）
    start_d = request.args.get("start", "").strip()     # 举办起始日期 YYYY-MM-DD
    end_d = request.args.get("end", "").strip()         # 举办结束日期 YYYY-MM-DD
    show_past = request.args.get("show_past", "").strip().lower() in ("1", "true", "yes", "on")
    fav_only = request.args.get("fav_only", "").strip().lower() in ("1", "true", "yes", "on")
    rows = load_preachs(past=show_past)
    if fav_only:
        favs = load_preach_favs()
        rows = [r for r in rows if str(r.get("ID", "")) in favs]
    rows = _apply_preach_filters(rows, q, ptype, venue, work, start_d, end_d)
    total = len(rows)
    page = max(1, request.args.get("page", 1, type=int))
    size = min(200, max(10, request.args.get("size", 50, type=int)))
    start = (page - 1) * size
    return jsonify({"total": total, "page": page, "size": size, "rows": rows[start:start + size]})


@app.route("/api/preach/fav", methods=["POST"])
def api_preach_fav():
    """收藏一场宣讲会（幂等）。"""
    data = request.get_json(silent=True) or {}
    rid = str(data.get("id", "")).strip()
    if not rid:
        return jsonify({"ok": False, "error": "缺少 id"}), 400
    favs = load_preach_favs()
    favs.add(rid)
    save_preach_favs(favs)
    return jsonify({"ok": True, "fav": True, "count": len(favs)})


@app.route("/api/preach/unfav", methods=["POST"])
def api_preach_unfav():
    """取消收藏一场宣讲会（幂等）。"""
    data = request.get_json(silent=True) or {}
    rid = str(data.get("id", "")).strip()
    if not rid:
        return jsonify({"ok": False, "error": "缺少 id"}), 400
    favs = load_preach_favs()
    favs.discard(rid)
    save_preach_favs(favs)
    return jsonify({"ok": True, "fav": False, "count": len(favs)})


@app.route("/api/preach/favs")
def api_preach_favs_list():
    """返回收藏的宣讲会完整记录（含过去的，按举办日期排序）。"""
    favs = load_preach_favs()
    rows = load_preachs(past=True)
    matched = [r for r in rows if str(r.get("ID", "")) in favs]
    matched.sort(key=lambda r: r.get("举办日期") or "")
    return jsonify({"count": len(matched), "rows": matched, "ids": sorted(favs)})


def _collect_preach_ids(payload: dict) -> tuple[list[str], int]:
    """按 payload 中的筛选条件收集宣讲会 ID 列表。返回 (ID列表, 命中数)。"""
    q = str(payload.get("q", "")).strip().lower()
    ptype = str(payload.get("type", "")).strip()
    venue = str(payload.get("venue", "")).strip()
    work = str(payload.get("work", "")).strip()
    start_d = str(payload.get("start", "")).strip()
    end_d = str(payload.get("end", "")).strip()
    show_past = str(payload.get("show_past", "")).strip().lower() in ("1", "true", "yes", "on")
    rows = load_preachs(past=show_past)
    rows = _apply_preach_filters(rows, q, ptype, venue, work, start_d, end_d)
    ids = [str(r.get("ID", "")) for r in rows if r.get("ID")]
    return ids, len(rows)


@app.route("/api/preach/fav-all", methods=["POST"])
def api_preach_fav_all():
    """一键收藏当前筛选条件下的全部宣讲会（幂等，忽略已收藏项）。"""
    data = request.get_json(force=True, silent=True) or {}
    ids, matched = _collect_preach_ids(data)
    favs = load_preach_favs()
    added = 0
    for rid in ids:
        if rid and rid not in favs:
            favs.add(rid)
            added += 1
    if added:
        save_preach_favs(favs)
    return jsonify({"ok": True, "added": added, "matched": matched, "total": len(favs)})


@app.route("/api/preach/unfav-all", methods=["POST"])
def api_preach_unfav_all():
    """一键取消收藏当前筛选条件下的全部宣讲会（幂等，忽略已取消项）。"""
    data = request.get_json(force=True, silent=True) or {}
    ids, matched = _collect_preach_ids(data)
    favs = load_preach_favs()
    removed = 0
    for rid in ids:
        if rid in favs:
            favs.discard(rid)
            removed += 1
    if removed:
        save_preach_favs(favs)
    return jsonify({"ok": True, "removed": removed, "matched": matched, "total": len(favs)})


@app.route("/api/companies/filters")
def api_companies_filters():
    """企业分析结果筛选项：工作地点（城市）+ 企业类型（带数量）。"""
    cache = load_cache()
    loc_counter: Counter[str] = Counter()
    type_counter: Counter[str] = Counter()
    for _name, result in cache.items():
        t = result.get("company_type", "")
        if t:
            type_counter[t] += 1
        for loc in (result.get("locations") or []):
            s = str(loc).strip()
            if s:
                loc_counter[s] += 1
    return jsonify({
        "types": [{"value": t, "count": n} for t, n in type_counter.most_common()],
        "locations": [{"value": l, "count": n} for l, n in loc_counter.most_common()],
    })


@app.route("/api/companies")
def api_companies():
    cache = load_cache()
    q = request.args.get("q", "").strip().lower()
    type_filter = request.args.get("type", "").strip()
    so_filter = request.args.get("so", "").strip()  # yes / no / ""
    loc_filter = request.args.get("loc", "").strip()
    rows = []
    for name, result in cache.items():
        so = result.get("is_state_owned")
        locs = result.get("locations") or []
        row = {
            "name": name,
            "type": result.get("company_type", ""),
            "so": {True: "是", False: "否"}.get(so, "未知"),
            "confidence": result.get("confidence", ""),
            "locations": locs,
            "evidence": result.get("evidence", ""),
        }
        if q and q not in name.lower() and q not in row["evidence"].lower():
            continue
        if type_filter and row["type"] != type_filter:
            continue
        if so_filter == "yes" and row["so"] != "是":
            continue
        if so_filter == "no" and row["so"] != "否":
            continue
        if loc_filter and not any(loc_filter in str(loc) for loc in locs):
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["so"] != "是", r["type"], r["name"]))
    total = len(rows)
    page = max(1, request.args.get("page", 1, type=int))
    size = min(500, max(10, request.args.get("size", 100, type=int)))
    start = (page - 1) * size
    return jsonify({"total": total, "page": page, "size": size, "rows": rows[start:start + size]})


@app.route("/api/stats")
def api_stats():
    cache = load_cache()
    type_counts = Counter(r.get("company_type", "") for r in cache.values())
    so_count = sum(1 for r in cache.values() if r.get("is_state_owned") is True)
    loc_counts = Counter(str(loc).strip() for r in cache.values() for loc in (r.get("locations") or []) if str(loc).strip())
    return jsonify({
        "total": len(cache),
        "state_owned": so_count,
        "types": type_counts.most_common(),
        "locations": loc_counts.most_common(30),
    })

# ---------------------------------------------------------------- API：简历与投递推荐

ALLOWED_EXT = {"pdf", "docx", "png", "jpg", "jpeg", "bmp", "webp"}


def save_upload(file_storage) -> Path:
    ext = Path(file_storage.filename).suffix.lower()
    tmp = WORKDIR / f"_upload_tmp_{int(time.time() * 1000)}{ext}"
    file_storage.save(tmp)
    return tmp


def _extract_upload(api_key: str, file_storage) -> tuple:
    """保存临时文件并提取文字，返回 (text, err)。无论成功与否都会清理临时文件。"""
    ext = Path(file_storage.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        return "", f"不支持的格式 .{ext}，请用 PDF / .docx / 图片"
    tmp = save_upload(file_storage)
    try:
        text = resume.extract_text(api_key, str(tmp), ext, request.form.get("vision_model") or None)
    except ValueError as exc:
        return "", str(exc)
    except Exception as exc:
        return "", f"解析失败：{exc}"
    finally:
        tmp.unlink(missing_ok=True)
    return text, ""


@app.route("/api/resume/extract", methods=["POST"])
def api_resume_extract():
    api_key = get_api_key()
    if not api_key:
        return jsonify({"ok": False, "error": "请先在设置中填写硅基流动 API Key"}), 400
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "请选择要上传的简历文件"}), 400
    text, err = _extract_upload(api_key, file)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if not text.strip():
        return jsonify({"ok": False, "error": "未能从该文件中提取到文字（可能是扫描件或空白），请直接粘贴简历文字"}), 400
    return jsonify({"ok": True, "text": text, "chars": len(text)})


@app.route("/api/resume/companies")
def api_resume_companies():
    return jsonify({"count": len(resume.build_companies(ROOT))})


def _build_so_map() -> dict[str, str]:
    """企业分析缓存 → 企业名 → 国企标签（是/否/空串）。"""
    cache = load_cache()
    return {name: {True: "是", False: "否"}.get(info.get("is_state_owned"), "")
            for name, info in cache.items()}


def _recommend_preachs(text: str, target_cities: list[str], company_type: str = "",
                       limit: int = 40) -> list[dict]:
    """基于「轨迹流动」工作地映射，推荐求职者可参加的宣讲会。

    匹配规则：宣讲会企业的工作地城市与目标工作地命中 → 再按企业性质（若有）过滤 → 结合日期排序。
    未填目标城市时，从简历文本里嗅探城市；仍无则展示全部有工作地映射的场次。
    """
    rows = load_preachs()
    so_map = _build_so_map()
    if not target_cities:
        target_cities = sorted({c for r in rows for c in (r.get("work_cities") or []) if c in text})
    want_state = any(k in (company_type or "") for k in ("国企", "央企", "事业"))
    want_private = any(k in (company_type or "") for k in ("民营", "私企", "外企", "互联网"))
    today = datetime.now().strftime("%Y-%m-%d")

    matched = []
    for r in rows:
        wc = r.get("work_cities") or []
        if not wc:
            continue  # 无工作地映射的场次无法基于轨迹流动推荐
        if target_cities and not any(c in w or w in c for c in target_cities for w in wc):
            continue
        so = so_map.get(r["单位名称"], "")
        if want_state and so == "否":
            continue  # 已分析出是非国企，与期望冲突，排除
        if want_private and so == "是":
            continue
        matched.append({
            "单位名称": r["单位名称"],
            "举办日期": r.get("举办日期", ""),
            "宣讲时间": r["宣讲时间"],
            "宣讲会地点": r["宣讲会地点"],
            "公司地点": r["公司地点"],
            "工作地城市": wc,
            "线下/线上": r["线下/线上"],
            "原网页": r["原网页"],
            "正文": r.get("正文", ""),
            "企业性质": so,
            "reason": (f"工作地 {r['公司地点']} 与目标地命中" if target_cities else "有明确工作地映射")
                      + ("，且企业性质符合期望" if (want_state or want_private) else ""),
        })
    matched.sort(key=lambda x: (x["举办日期"] < today, x["举办日期"]))
    return matched[:limit]


@app.route("/api/resume/recommend", methods=["POST"])
def api_resume_recommend():
    api_key = get_api_key()
    if not api_key:
        return jsonify({"ok": False, "error": "请先在设置中填写硅基流动 API Key"}), 400
    companies = resume.build_companies(ROOT)
    if not companies:
        return jsonify({"ok": False, "error": "没有可推荐的企业数据，请先运行「抓取」与「企业分析」"}), 400

    model = (request.form.get("model") or "").strip() or load_config().get("model", resume.DEFAULT_MODEL)
    work_place = (request.form.get("work_place") or "").strip()
    company_type = (request.form.get("company_type") or "").strip()
    text = (request.form.get("resume_text") or "").strip()
    file = request.files.get("file")
    if file and file.filename:
        extracted, err = _extract_upload(api_key, file)
        if err:
            # 文件解析失败：若用户已粘贴文字，则用粘贴文字继续
            if not text:
                return jsonify({"ok": False, "error": err}), 400
        elif not extracted.strip():
            if not text:
                return jsonify({"ok": False, "error": "未能从文件提取到文字，请粘贴简历文字或换用其他格式"}), 400
        else:
            text = text or extracted
    if not text.strip():
        return jsonify({"ok": False, "error": "请上传简历文件，或直接粘贴简历文字"}), 400

    result = resume.recommend(api_key, model, text, companies, work_place=work_place,
                              company_type=company_type)
    source = "ai"
    note = ""
    if result.get("error"):
        # AI 不可用（余额不足/网络异常/模型被禁用）→ 降级为本地关键词匹配，保证有可用推荐
        source = "offline"
        note = f"AI 模型暂不可用（{result['error']}），已用本地关键词匹配生成推荐（充值后自动恢复 AI）。"
        result = resume.keyword_recommend(text, companies, work_place=work_place,
                                          company_type=company_type)
        result["note"] = note
    target_cities = resume.parse_target_cities(work_place)
    recommended_preachs = _recommend_preachs(text, target_cities, company_type)
    return jsonify({"ok": True, "result": result, "source": source, "companies_count": len(companies),
                    "recommended_preachs": recommended_preachs,
                    "target_work_place": work_place, "target_company_type": company_type,
                    "model": model, "resume_chars": len(text)})


# ---------------------------------------------------------------- API：导出

@app.route("/api/export/<kind>")
def api_export(kind):
    if kind == "csv":
        path = ROOT / analyze.CSV_NAME
        mime = "text/csv"
    elif kind == "md":
        path = ROOT / analyze.MD_NAME
        mime = "text/markdown"
    else:
        return jsonify({"ok": False, "error": "未知导出类型"}), 400
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在，请先运行分析"}), 404
    return send_file(path, mimetype=mime + "; charset=utf-8", as_attachment=True, download_name=path.name)


@app.route("/api/recruitments/export")
def api_export_recruitments():
    raw = latest_raw_json()
    if not raw:
        return jsonify({"ok": False, "error": "无数据"}), 404
    csv_path = sorted(glob.glob(str(ROOT / "*_招聘信息.csv")), key=os.path.getmtime, reverse=True)
    if csv_path:
        return send_file(csv_path[0], mimetype="text/csv; charset=utf-8", as_attachment=True,
                         download_name=Path(csv_path[0]).name)
    return jsonify({"ok": False, "error": "CSV 不存在"}), 404


_PREACH_FAV_HEADERS = ["宣讲时间", "举办日期", "单位名称", "宣讲会地点", "城市",
                       "线下/线上", "标题", "公司地点（工作地）", "链接"]


def _preach_fav_row(r: dict) -> list[str]:
    return [r.get("宣讲时间", ""), r.get("举办日期", ""), r.get("单位名称", ""),
            r.get("宣讲会地点", ""), r.get("城市", ""), r.get("线下/线上", ""),
            r.get("标题", ""), r.get("公司地点", ""), r.get("原网页", "")]


@app.route("/api/preach/favs/export")
def api_export_preach_favs():
    """导出收藏的宣讲会为 Excel（.xlsx）/ 降级 CSV。"""
    favs = load_preach_favs()
    rows = load_preachs(past=True)
    matched = [r for r in rows if str(r.get("ID", "")) in favs]
    matched.sort(key=lambda r: r.get("举办日期") or "")
    if not matched:
        return jsonify({"ok": False, "error": "尚无收藏的宣讲会"}), 404
    fmt = datetime.now().strftime("%Y%m%d")
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = "收藏宣讲会"
        header_fill = PatternFill("solid", fgColor="4F8EF7")
        header_font = Font(bold=True, color="FFFFFF")
        ws.append(_PREACH_FAV_HEADERS)
        for c in ws[1]:
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws.freeze_panes = "A2"
        for r in matched:
            ws.append(_preach_fav_row(r))
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col) + 4
            ws.column_dimensions[col[0].column_letter].width = min(max(width, 10), 60)
        for row in ws.iter_rows(min_row=2):
            cell = row[8]
            if cell.value:
                try:
                    cell.hyperlink = cell.value
                    cell.style = "Hyperlink"
                except Exception:
                    pass
        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return send_file(bio,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"收藏宣讲会_{fmt}.xlsx")
    except Exception:
        msg = io.StringIO()
        msg.write("\ufeff")  # UTF-8 BOM，Excel 识别中文
        w = csv.writer(msg)
        w.writerow(_PREACH_FAV_HEADERS)
        for r in matched:
            w.writerow(_preach_fav_row(r))
        resp = Response(msg.getvalue(), mimetype="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = f"attachment; filename=收藏宣讲会_{fmt}.csv"
        return resp


def _ics_escape(value: str) -> str:
    """转义 ICS 文本值中的反斜杠 / 分号 / 逗号。"""
    return (str(value)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,"))


def _ics_fold(line: str, limit: int = 74) -> str:
    """ICS 行按 75 字节（含换行）上限折行，续行以空格开头。"""
    if len(line.encode("utf-8")) <= limit:
        return line
    out: list[str] = []
    cur = ""
    cur_bytes = 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if cur_bytes + b > limit:
            out.append(cur)
            cur = " " + ch
            cur_bytes = 1 + b
        else:
            cur += ch
            cur_bytes += b
    if cur:
        out.append(cur)
    return "\r\n".join(out)


def _preach_ics_event(r: dict) -> str:
    """把一场宣讲会转换成一个完整的 VEVENT 块。"""
    name = str(r.get("单位名称", "")).strip()
    title = str(r.get("标题", "")).strip()
    summary = name if not title or title == name else f"{name}（{title}）"
    location = str(r.get("宣讲会地点", "")).strip()
    url = str(r.get("原网页", "")).strip()
    online = str(r.get("线下/线上", "")).strip()

    hold_date = str(r.get("举办日期", "")).strip()
    start_time = str(r.get("开始时间", "")).strip()
    end_time = str(r.get("结束时间", "")).strip()

    # 事件时间：默认用当地时间（浮动时间），无具体时刻则以全天事件呈现
    try:
        if hold_date and start_time:
            start_dt = datetime.strptime(f"{hold_date} {start_time}", "%Y-%m-%d %H:%M")
            if end_time:
                end_dt = datetime.strptime(f"{hold_date} {end_time}", "%Y-%m-%d %H:%M")
                if end_dt <= start_dt:
                    end_dt = start_dt + timedelta(hours=1)
            else:
                end_dt = start_dt + timedelta(hours=1)
            dtstart = f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}"
            dtend = f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}"
        else:
            d = hold_date.replace("-", "") or "19700101"
            dtstart = f"DTSTART;VALUE=DATE:{d}"
            dtend = f"DTEND;VALUE=DATE:{d}"
    except ValueError:
        d = hold_date.replace("-", "") or "19700101"
        dtstart = f"DTSTART;VALUE=DATE:{d}"
        dtend = f"DTEND;VALUE=DATE:{d}"

    uid_base = str(r.get("ID", "")) or url or name
    uid = re.sub(r"[^A-Za-z0-9._-]", "-", uid_base) + "-whutrecruit"
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    desc_parts = []
    if title and title != name:
        desc_parts.append(f"宣讲标题：{title}")
    if title is not None and title == name:
        desc_parts.append(f"单位名称：{name}")
    if location:
        desc_parts.append(f"地点：{location}")
    if online:
        desc_parts.append(f"形式：{online}")
    if url:
        desc_parts.append(f"链接：{url}")
    description = "；".join(desc_parts) or summary

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        dtstart,
        dtend,
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
    ]
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    if url:
        lines.append(f"URL:{url}")
    lines.append("TRANSP:OPAQUE")
    lines.append("END:VEVENT")
    return lines


@app.route("/api/preach/favs/export-ics")
def api_export_preach_favs_ics():
    """导出收藏的宣讲会为 iCalendar (.ics)，供日历 App 订阅/导入。"""
    favs = load_preach_favs()
    rows = load_preachs(past=True)
    matched = [r for r in rows if str(r.get("ID", "")) in favs]
    matched.sort(key=lambda r: r.get("举办日期") or "")
    if not matched:
        return jsonify({"ok": False, "error": "尚无收藏的宣讲会"}), 404
    fmt = datetime.now().strftime("%Y%m%d")
    blocks: list[list[str]] = [[
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//WHUT Recruit Tool//收藏宣讲会//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:WHUT 宣讲会收藏",
    ]]
    for r in matched:
        blocks.append(_preach_ics_event(r))
    blocks.append(["END:VCALENDAR"])
    body_lines = [folded for block in blocks for folded in (_ics_fold(line) for line in block)]
    body = "\r\n".join(body_lines) + "\r\n"
    bio = io.BytesIO(body.encode("utf-8"))
    bio.seek(0)
    return send_file(bio, mimetype="text/calendar",
                     as_attachment=True, download_name=f"收藏宣讲会_{fmt}.ics")


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(f"启动界面：http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
