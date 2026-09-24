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
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

SCRIPT_DIR = Path(__file__).resolve().parent          # 代码所在目录（运行工具/）
ROOT = SCRIPT_DIR.parent                              # 项目根目录（数据/配置所在）
WORKDIR = SCRIPT_DIR                                  # 兼容旧引用：指代码目录
DATA = ROOT / "data"                                  # 数据产物统一存放（抓取/分析/收藏/缓存）
CONFIG_PATH = ROOT / "config.json"
CACHE_PATH = DATA / "企业分析_缓存.json"
FAV_PATH = DATA / "收藏_宣讲会.json"

import analyze  # noqa: E402  复用 analyze.py 的工具函数
import crawler  # noqa: E402  复用 time_text / plain_text / write_json
import exports  # noqa: E402  导出（Excel / CSV / ICS）
import providers  # noqa: E402  LLM 厂商预设与配置解析
import repository as repo  # noqa: E402  统一数据访问层（跨文件合并 + ID 去重 + 缓存）
import resume   # noqa: E402  简历解析 + 投递推荐
import taskcenter  # noqa: E402  后台任务中心（状态机 / 输出采集 / 历史落盘）

MAX_UPLOAD_BYTES = 20 * 1024 * 1024           # 单请求体上限 20MB（简历 / 报告导入）

TASK_HISTORY_PATH = DATA / "任务历史.json"     # 后台任务历史（服务重启后仍可查看）
TASK_LOG_DIR = DATA / "任务日志"               # 每个任务一份独立日志文件
TASK_HISTORY_MAX = 200                         # 历史记录上限
TASK_LOG_MAX = 200                             # 任务日志文件保留上限（超出按修改时间清理）
# 任务 ID 前缀（ASCII，避免中文出现在 URL / 文件名 / HTML id 中）
KIND_SLUGS = taskcenter.KIND_SLUGS   # 任务 ID 前缀表（实现在 taskcenter）

app = Flask(__name__)
app.json.ensure_ascii = False
# 上传体积上限：简历 / 报告导入走内存缓存，超限直接 413，避免大文件撑爆内存
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


# ---------------------------------------------------------------- 结构化日志
# 每个请求一行 JSON（JSON Lines）落盘到 data/服务日志.jsonl，带请求号 rid / 方法 / 路径 / 状态码 / 耗时。
# 控制台只打印 ASCII 摘要——Windows 控制台是 GBK，直接输出中文会抛 UnicodeEncodeError。
LOG_PATH = DATA / "服务日志.jsonl"
LOG_MAX_BYTES = 2 * 1024 * 1024          # 单文件上限，超出滚动保留一份旧日志
_LOG_LOCK = threading.Lock()
logger = logging.getLogger("whut")


def log_event(event: str, **fields) -> None:
    """写一条结构化日志。fields 会被 JSON 序列化，非可序列化对象自动转 str。"""
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}",
           "event": event}
    rec.update(fields)
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return
    try:
        with _LOG_LOCK:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                try:
                    LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.stem + ".old.jsonl"))
                except OSError:
                    LOG_PATH.unlink(missing_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
    try:  # 控制台摘要：只输出 ASCII 字段，避免 GBK 终端乱码
        summary = " ".join(f"{k}={v}" for k, v in fields.items()
                           if isinstance(v, (int, float)) or (isinstance(v, str) and v.isascii()))
        print(f"[{event}] {summary}", flush=True)
    except (OSError, UnicodeEncodeError):
        pass


@app.before_request
def _request_begin() -> None:
    g.rid = uuid.uuid4().hex[:8]
    g.t0 = time.perf_counter()


@app.after_request
def _request_end(resp):
    rid = getattr(g, "rid", "-")
    t0 = getattr(g, "t0", None)
    ms = round((time.perf_counter() - t0) * 1000, 1) if t0 is not None else -1
    resp.headers["X-Request-Id"] = rid
    if not request.path.endswith((".ico", ".css", ".js", ".png", ".svg")):
        log_event("request", rid=rid, method=request.method, path=request.path,
                  status=resp.status_code, ms=ms)
    return resp


# ---------------------------------------------------------------- 统一错误响应

@app.errorhandler(HTTPException)
def _handle_http_error(err: HTTPException):
    """统一 JSON 错误响应：/api/* 的 4xx/5xx 一律返回 {ok:false, error}，前端可直接提示。"""
    detail = err.description or err.name
    code = err.code or 500
    if code >= 500:
        log_event("error", rid=getattr(g, "rid", "-"), method=request.method,
                  path=request.path, status=code, err=str(detail))
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": f"{code} {err.name}：{detail}"}), code
    return err


@app.errorhandler(Exception)
def _handle_unexpected(err: Exception):
    """未捕获异常统一转成 JSON + 记日志。

    注意：不向客户端回显异常细节（可能包含本地路径、配置片段等），只返回请求号 rid，
    用户可用 rid 在 data/服务日志.jsonl 中定位完整堆栈。
    """
    if isinstance(err, HTTPException):
        return _handle_http_error(err)
    rid = getattr(g, "rid", "-")
    app.logger.exception("未处理的异常 rid=%s path=%s", rid, request.path)
    log_event("error", rid=rid, method=request.method, path=request.path,
              status=500, err=repr(err))
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": f"服务内部错误，详情见服务端日志（请求号 {rid}）",
                        "rid": rid}), 500
    return "服务内部错误，请查看服务端日志", 500

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
    crawler.write_json(CONFIG_PATH, cfg)


def get_api_key() -> str:
    return (load_config().get("api_key") or "").strip()


# LLM 厂商预设与解析见 providers.py；此处仅重导出以兼容既有引用
LLM_PROVIDERS = providers.LLM_PROVIDERS
_mask = providers.mask


def get_llm() -> dict:
    """按当前配置的厂商返回 base_url / api_key / model / label。"""
    return providers.resolve(load_config())




# ---------------------------------------------------------------- 后台任务（实现见 taskcenter.py）
# 任务状态机 / 输出采集 / 历史落盘与 HTTP 层解耦，放在 taskcenter.py；
# 这里只做装配：把本项目的数据目录与上限注入进去（路径取自本模块变量，测试可替换）。
_parse_progress = taskcenter.parse_progress   # 兼容旧引用（测试直接调用 server._parse_progress）
_error_summary = taskcenter.error_summary


class TaskManager(taskcenter.TaskManager):
    """绑定本项目数据目录的任务管理器。"""

    def __init__(self) -> None:
        super().__init__(history_path=TASK_HISTORY_PATH, log_dir=TASK_LOG_DIR, workdir=WORKDIR,
                         history_max=TASK_HISTORY_MAX, log_max=TASK_LOG_MAX)


tasks = TaskManager()

# ---------------------------------------------------------------- 数据读取

def _newest_glob(pattern: str) -> Path | None:
    """取项目根下匹配 pattern 的文件中最新的一个。"""
    candidates = sorted(glob.glob(str(DATA / pattern)), key=os.path.getmtime, reverse=True)
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


WORK_FLOW_CSV = DATA / "宣讲会_工作地流动.csv"
_work_map_cache: tuple[str, dict[str, list[str]]] | None = None   # (CSV 签名, 映射)


def _work_flow_signature() -> str:
    """《宣讲会_工作地流动.csv》的签名（mtime+size），用于缓存失效判断。"""
    try:
        st = WORK_FLOW_CSV.stat()
    except OSError:
        return "-"
    return f"{st.st_mtime_ns}:{st.st_size}"


def _company_work_map() -> dict[str, list[str]]:
    """单位名称 → 工作地城市列表。

    优先读取本项目已生成的《宣讲会_工作地流动.csv》（457 家单位均已映射）。
    文件缺失时返回空 dict，由调用方回退到 analyze_preach.infer_work_cities 逐条推断。

    结果按 CSV 签名缓存：数据健康 / 宣讲会列表 / 筛选器等多个接口都会读它，
    避免每次请求重复解析同一份 CSV。
    """
    global _work_map_cache
    sig = _work_flow_signature()
    if _work_map_cache and _work_map_cache[0] == sig:
        return _work_map_cache[1]
    mapping: dict[str, list[str]] = {}
    if WORK_FLOW_CSV.exists():
        try:
            with WORK_FLOW_CSV.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    name = (row.get("单位名称") or "").strip()
                    if not name:
                        continue
                    cities = [c.strip() for c in (row.get("工作地城市") or "").split("、") if c.strip()]
                    mapping[name] = cities
        except (OSError, csv.Error):
            mapping = {}
    _work_map_cache = (sig, mapping)
    return mapping


def _iter_recruit_files() -> list[Path]:
    """所有招聘信息原始数据文件（按修改时间倒序）。"""
    return repo.iter_files(repo.RECRUIT_GLOB)


def _build_recruit_rows(today_str: str) -> list[dict]:
    rows = []
    for item in repo.raw_items("recruit"):
        add_date = crawler.time_text(item.get("addtime"), with_time=False)
        rows.append({
            "发布日期": crawler.time_text(item.get("addtime")),
            "发布日期日": add_date,
            "今日更新": bool(add_date) and add_date == today_str,
            "标题": item.get("title", ""),
            "单位": item.get("com_id_name", ""),
            "原网页": item.get("httpurl") or f"https://scc.whut.edu.cn/#/recruitmentInformation/notice?type=enrollment&id={item.get('id','')}",
            "ID": item.get("id", ""),
            "正文": crawler.plain_text(item.get("remarks") or item.get("content") or "")[:600],
        })
    rows.sort(key=lambda r: r["发布日期"], reverse=True)
    return rows


def load_recruitments() -> list[dict]:
    """招聘信息列表（统一口径：repository 跨全部原始文件合并、按 ID 去重）。

    派生结果按「数据文件签名 + 当天日期」缓存，避免每次请求重复清洗 2000+ 条正文。
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    return repo.cached_derived("recruit_rows", "recruit",
                               lambda: _build_recruit_rows(today_str), extra=today_str)


def load_fairs() -> list[dict]:
    """双选会列表（统一口径：repository 合并去重）。"""
    def build() -> list[dict]:
        rows = []
        for f in repo.raw_items("fair"):
            rows.append({
                "标题": f.get("title", ""),
                "地点": f.get("field_id_name", ""),
                "举办时间": f"{crawler.time_text(f.get('start_time'))} 至 {crawler.time_text(f.get('end_time'))}",
                "参会单位数": f.get("verify_count", ""),
                "原网页": f"https://scc.whut.edu.cn/#/doubleElection/{f.get('id','')}",
            })
        return rows
    return repo.cached_derived("fair_rows", "fair", build)


def _build_preach_rows(today_str: str) -> list[dict]:
    """构造全部宣讲会行（不过滤过去的场次）。开销较大，由 load_preachs 走缓存调用。"""
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    soon_end = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    work_map = _company_work_map()
    fallback = None  # 惰性导入 analyze_preach
    rows = []
    for item in repo.raw_items("preach"):
        hold_date = item.get("hold_date", "")
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


def load_preachs(past: bool = False) -> list[dict]:
    """宣讲会列表：数据来自 repository（跨全部宣讲会文件合并、按 ID 去重）。

    行构建（含工作地 CSV 映射 / 离线推断 / 正文清洗）按
    「数据文件签名 + 当天日期 + 《宣讲会_工作地流动.csv》签名」缓存，
    避免列表、筛选器、导出、推荐、行动中心等每个接口都全量重算一次。
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    extra = f"{today_str}|{_work_flow_signature()}"
    all_rows = repo.cached_derived("preach_rows", "preach",
                                   lambda: _build_preach_rows(today_str), extra=extra)
    if past:
        return list(all_rows)
    # 默认只看当天及以后（过去的宣讲会隐藏）
    return [r for r in all_rows if not (r["举办日期"] and r["举办日期"].strip() < today_str)]


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
    """持久化收藏的宣讲会 ID 集合（原子写，防止并发请求写坏收藏文件）。"""
    crawler.write_json(FAV_PATH, {"ids": sorted(ids)})


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

def _work_undetermined_count() -> int:
    """工作地未确定的宣讲会企业数（《宣讲会_工作地流动.csv》映射缺失的企业）。"""
    work_map = _company_work_map()
    names = {str(item.get("com_id_name") or "").strip() for item in repo.raw_items("preach")}
    names.discard("")
    return sum(1 for name in names if not work_map.get(name))


@app.route("/api/status")
def api_status():
    cfg = load_config()
    cache = load_cache()
    llm = get_llm()
    summary = repo.master_summary()   # 主数据统一口径：跨全部原始文件合并 + 按 ID 去重
    raw = latest_raw_json()           # 仅用于界面「数据文件」展示
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


@app.route("/api/health")
def api_health():
    """数据健康：最近抓取/更新时间、主库记录数、覆盖日期、详情缺失、未分析企业、工作地未确定。"""
    cache = load_cache()
    payload = repo.health(cache=cache, work_undetermined=_work_undetermined_count())
    hist = tasks.history_list(limit=1)
    payload["last_task"] = hist[0] if hist else None
    payload["analyzed_count"] = len(cache)
    return jsonify({"ok": True, "health": payload})


@app.route("/api/tasks")
def api_tasks():
    """统一任务中心：运行中的任务 + 历史任务（服务重启后仍可查看）。"""
    limit = min(200, max(1, request.args.get("limit", 60, type=int)))
    rows = tasks.history_list(limit=limit)
    return jsonify({"ok": True, "running": tasks.running(), "tasks": rows, "count": len(rows)})

# ---------------------------------------------------------------- API：配置

@app.route("/api/llm/catalog")
def api_llm_catalog():
    """返回各厂家预设 + 每家已保存的 Key/模型/BaseURL，供「预设供应商」卡片网格与配置表单使用。"""
    cfg = load_config()
    llm = get_llm()
    providers = []
    for pid, conf in LLM_PROVIDERS.items():
        key = (cfg.get(conf["api_key_field"], "") or "").strip()
        model = (cfg.get(conf["model_field"], "") or "").strip()
        saved_base = (cfg.get(conf.get("base_url_field") or "", "") or "").strip()
        providers.append({
            "id": pid, "label": conf["label"], "logo": conf.get("logo", ""),
            "desc": conf.get("desc", ""), "base_url": conf["base_url"],
            "default_model": conf["default_model"], "models": conf.get("models", []),
            "context_window": conf.get("context_window"),
            "docs_url": conf.get("docs_url", ""), "compute_url": conf.get("compute_url", ""),
            "api_key_set": bool(key), "api_key_masked": _mask(key),
            "model": model, "base_url_saved": saved_base,
            "current": pid == cfg.get("provider", ""),
        })
    return jsonify({
        "providers": providers,
        "current": {"provider": llm["provider"], "model": llm["model"],
                    "label": llm["label"], "base_url": llm["base_url"]},
    })


@app.route("/api/llm/models/<pid>")
def api_llm_models(pid):
    """用已保存的 Key + BaseURL 调用 /models，刷新该厂家的模型清单。"""
    conf = LLM_PROVIDERS.get(pid)
    if not conf:
        return jsonify({"ok": False, "error": "未知厂商"}), 404
    cfg = load_config()
    key = (cfg.get(conf["api_key_field"], "") or "").strip()
    base = (cfg.get(conf.get("base_url_field") or "", "") or conf["base_url"]).rstrip("/")
    if not key:
        return jsonify({"ok": False, "error": "请先填写该厂商的 API Key", "models": conf.get("models", [])})
    if not base:
        return jsonify({"ok": False, "error": "请先填写 Base URL", "models": conf.get("models", [])})
    try:
        import requests
        r = requests.get(base + "/models", headers={"Authorization": "Bearer " + key}, timeout=20)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        return jsonify({"ok": True, "models": ids or conf.get("models", [])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "models": conf.get("models", [])})


@app.route("/api/llm/test", methods=["POST"])
def api_llm_test():
    """测试连接：用当前填写的 Key/BaseURL/模型向该厂商发一条最小请求。"""
    payload = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    provider = str(payload.get("provider") or "").strip() or cfg.get("provider", "siliconflow")
    conf = LLM_PROVIDERS.get(provider) or LLM_PROVIDERS["siliconflow"]
    key = str(payload.get("api_key") or "").strip() or (cfg.get(conf["api_key_field"], "") or "").strip()
    base = (str(payload.get("base_url") or "").strip()
            or (cfg.get(conf.get("base_url_field") or "", "") or "").strip()
            or conf["base_url"]).rstrip("/")
    model = str(payload.get("model") or "").strip() or conf["default_model"]
    if not key:
        return jsonify({"ok": False, "error": "未填写 API Key"})
    if not base:
        return jsonify({"ok": False, "error": "未填写 Base URL"})
    try:
        import requests
        r = requests.post(base + "/chat/completions",
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                          json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
                          timeout=30)
        return jsonify({"ok": r.ok, "status": r.status_code,
                        "error": ("" if r.ok else r.text[:300])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config", methods=["POST"])
def api_config():
    payload = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    if "provider" in payload and str(payload["provider"]).strip() in LLM_PROVIDERS:
        cfg["provider"] = str(payload["provider"]).strip()
    provider = cfg.get("provider", "siliconflow")
    if provider not in LLM_PROVIDERS:
        provider = "siliconflow"
    conf = LLM_PROVIDERS[provider]
    # 所选模型写入对应厂家（由所选模型决定厂家）的 model 字段
    if "model" in payload and str(payload["model"]).strip():
        cfg[conf["model_field"]] = str(payload["model"]).strip()
    # 通用字段：api_key / base_url 按当前厂商写入独立字段（空值不动，保留已保存）
    if "api_key" in payload and str(payload["api_key"]).strip():
        cfg[conf["api_key_field"]] = str(payload["api_key"]).strip()
    if "base_url" in payload and str(payload["base_url"]).strip():
        cfg[conf.get("base_url_field") or ""] = str(payload["base_url"]).strip()
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/key", methods=["POST", "DELETE"])
def api_config_key_delete():
    """删除某厂商已保存的 API Key（仅清空本机 config.json 中的密钥，不影响 Base URL/模型）。"""
    payload = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    provider = str(payload.get("provider") or "").strip() or cfg.get("provider", "siliconflow")
    conf = LLM_PROVIDERS.get(provider)
    if not conf:
        return jsonify({"ok": False, "error": "未知厂商"}), 404
    cfg[conf["api_key_field"]] = ""
    save_config(cfg)
    return jsonify({"ok": True, "provider": provider})

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

@app.route("/api/recruit/update", methods=["POST"])
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
    cmd = [sys.executable, str(WORKDIR / "check_update.py"), "--kind", "preach"]
    if payload.get("all_types"):
        cmd.append("--all-types")
    env = dict(os.environ)
    result = tasks.start("宣讲会检查", cmd, env, title="检查宣讲会更新")
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
    # 统一口径：分析范围取 repository 合并后的全部招聘信息（而非最新那个文件），
    # 避免「最新文件只是当日小快照」导致分析样本小于页面展示范围。
    if not repo.raw_items("recruit"):
        return jsonify({"ok": False, "error": "没有原始数据，请先运行抓取"}), 400
    limit = int(payload.get("limit") or 0)
    cmd = [sys.executable, str(WORKDIR / "analyze.py"), "--model", llm["model"], "--merge"]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    env = dict(os.environ)
    env["LLM_BASE_URL"] = llm["base_url"]
    env["LLM_MODEL"] = llm["model"]
    env["LLM_API_KEY"] = llm["api_key"]
    env["SILICONFLOW_API_KEY"] = llm["api_key"]
    env["SILICONFLOW_BASE_URL"] = llm["base_url"]
    result = tasks.start("分析", cmd, env, title=f"AI 分析企业性质与工作地点（{llm['model']}）")
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/flow", methods=["GET"])
def api_flow_status():
    """工作地流动分析：返回报告是否存在及生成时间。"""
    csv = DATA / "宣讲会_工作地流动.csv"
    md = DATA / "宣讲会_工作地流动报告.md"
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
    title = f"宣讲会工作地流动分析（{'AI' if method == 'ai' else '离线'}）"
    result = tasks.start("工作地流动", cmd, env, title=title)
    if not result["ok"]:
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/task/<task_id>/log")
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


@app.route("/api/task/stop", methods=["POST"])
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

# ---------------------------------------------------------------- API：数据浏览

@app.route("/api/recruitments")
def api_recruitments():
    """招聘信息列表：支持 关键词（标题/单位/正文）、单位精确、只看今日新增、排序。"""
    q = request.args.get("q", "").strip().lower()
    unit = request.args.get("unit", "").strip()
    today_only = request.args.get("today", "").strip().lower() in ("1", "true", "yes", "on")
    sort = request.args.get("sort", "").strip()
    rows = load_recruitments()
    today_count = sum(1 for r in rows if r.get("今日更新"))
    if unit:
        rows = [r for r in rows if r["单位"] == unit]
    if q:
        rows = [r for r in rows
                if q in r["标题"].lower() or q in r["单位"].lower() or q in r["正文"].lower()]
    if today_only:
        rows = [r for r in rows if r.get("今日更新")]
    if sort == "unit":
        rows.sort(key=lambda r: (r["单位"], r["发布日期"]))
    total = len(rows)
    page = max(1, request.args.get("page", 1, type=int))
    size = min(200, max(10, request.args.get("size", 50, type=int)))
    start = (page - 1) * size
    return jsonify({"total": total, "page": page, "size": size, "today_count": today_count,
                    "rows": rows[start:start + size]})


@app.route("/api/actions")
def api_actions():
    """首页「今日行动中心」：今日新增招聘 / 近期宣讲会 / 收藏 / 待分析企业 等可点击指标。"""
    today = datetime.now().strftime("%Y-%m-%d")
    recruit_rows = load_recruitments()
    preach_rows = load_preachs(past=False)
    favs = load_preach_favs()
    cache = load_cache()
    return jsonify({
        "ok": True,
        "today": today,
        "today_recruit": sum(1 for r in recruit_rows if r.get("今日更新")),
        "preach_soon3": sum(1 for r in preach_rows if r.get("3天内开始")),
        "preach_today": sum(1 for r in preach_rows if r.get("举办日期") == today),
        "preach_new_today": sum(1 for r in preach_rows if r.get("今日新出")),
        "preach_upcoming": len(preach_rows),
        "preach_favs": sum(1 for r in preach_rows if str(r.get("ID", "")) in favs),
        "preach_favs_total": len(favs),
        "recruit_total": len(recruit_rows),
        "unanalyzed": len(repo.unanalyzed(cache)),
        "analyzed": len(cache),
        "data_updated": repo.master_summary()["last_update"],
    })


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

# 送入 LLM 的候选企业条数上限（受模型上下文限制；排序取自主库全量候选，避免样本偏小）
RESUME_PROMPT_LIMIT = 80


def save_upload(file_storage) -> Path:
    ext = Path(file_storage.filename).suffix.lower()
    tmp = WORKDIR / f"_upload_tmp_{int(time.time() * 1000)}{ext}"
    file_storage.save(tmp)
    return tmp


def _extract_upload(api_key: str, file_storage, base_url: str = None) -> tuple:
    """保存临时文件并提取文字，返回 (text, err)。无论成功与否都会清理临时文件。"""
    ext = Path(file_storage.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        return "", f"不支持的格式 .{ext}，请用 PDF / .docx / 图片"
    tmp = save_upload(file_storage)
    try:
        text = resume.extract_text(api_key, str(tmp), ext, request.form.get("vision_model") or None,
                                   base_url=base_url)
    except ValueError as exc:
        return "", str(exc)
    except Exception as exc:
        return "", f"解析失败：{exc}"
    finally:
        tmp.unlink(missing_ok=True)
    return text, ""


@app.route("/api/resume/extract", methods=["POST"])
def api_resume_extract():
    llm = get_llm()
    if not llm["api_key"]:
        return jsonify({"ok": False, "error": f"请先在设置中配置 {llm['label']} API Key"}), 400
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "请选择要上传的简历文件"}), 400
    text, err = _extract_upload(llm["api_key"], file, base_url=llm["base_url"])
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if not text.strip():
        return jsonify({"ok": False, "error": "未能从该文件中提取到文字（可能是扫描件或空白），请直接粘贴简历文字"}), 400
    return jsonify({"ok": True, "text": text, "chars": len(text)})


@app.route("/api/resume/companies")
def api_resume_companies():
    """候选企业统计（统一口径：主库全部招聘公告去重后的企业数）。"""
    all_companies = resume.build_companies(DATA, max_items=0)
    analyzed = sum(1 for c in all_companies if c["type"] or c["locations"])
    return jsonify({"count": len(all_companies), "analyzed": analyzed})


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
    llm = get_llm()
    if not llm["api_key"]:
        return jsonify({"ok": False, "error": f"请先在设置中配置 {llm['label']} API Key"}), 400
    # 统一口径：候选企业来自主库合并后的全部招聘公告（不再是"最新那个文件"）；
    # 送入 LLM 的条数受上下文限制，但排序基于全量，并向界面回报真实总量。
    all_companies = resume.build_companies(DATA, max_items=0)
    if not all_companies:
        return jsonify({"ok": False, "error": "没有可推荐的企业数据，请先运行「抓取」与「企业分析」"}), 400
    companies = all_companies[:RESUME_PROMPT_LIMIT]

    model = (request.form.get("model") or "").strip() or llm["model"]
    work_place = (request.form.get("work_place") or "").strip()
    company_type = (request.form.get("company_type") or "").strip()
    text = (request.form.get("resume_text") or "").strip()
    file = request.files.get("file")
    if file and file.filename:
        # 文件文字提取走视觉模型 OCR（同样用当前配置的 LLM 密钥/地址）；推荐文本生成走当前配置的 LLM
        extracted, err = _extract_upload(llm["api_key"], file, base_url=llm["base_url"])
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

    result = resume.recommend(llm["api_key"], model, text, companies, work_place=work_place,
                              company_type=company_type, base_url=llm["base_url"])
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
    summary = repo.master_summary()
    return jsonify({"ok": True, "result": result, "source": source, "companies_count": len(companies),
                    "companies_total": len(all_companies),
                    "data_source": {"recruit_count": summary["recruit_count"],
                                    "coverage_start": summary["coverage_start"],
                                    "coverage_end": summary["coverage_end"],
                                    "updated": summary["last_update"]},
                    "recommended_preachs": recommended_preachs,
                    "target_work_place": work_place, "target_company_type": company_type,
                    "model": model, "resume_chars": len(text)})


# ---------------------------------------------------------------- API：导出

@app.route("/api/export/<kind>")
def api_export(kind):
    if kind == "csv":
        path = DATA / analyze.CSV_NAME
        mime = "text/csv"
    elif kind == "md":
        path = DATA / analyze.MD_NAME
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
    csv_path = sorted(glob.glob(str(DATA / "*_招聘信息.csv")), key=os.path.getmtime, reverse=True)
    if csv_path:
        return send_file(csv_path[0], mimetype="text/csv; charset=utf-8", as_attachment=True,
                         download_name=Path(csv_path[0]).name)
    return jsonify({"ok": False, "error": "CSV 不存在"}), 404


# ---------------------------------------------------------------- API：导入报告

# 导入模板：让用户按「全部企业明细」表格格式手写/复用导出报告
_IMPORT_TEMPLATE = "# 企业性质与工作地点分析报告\n" + \
                   "\n" + \
                   "- 生成时间：2026-09-09 15:00\n" + \
                   "- 数据来源：自定义导入\n" + \
                   "- 分析模型：自定义\n" + \
                   "- 企业总数：2\n" + \
                   "\n" + \
                   "## 全部企业明细\n" + \
                   "\n" + \
                   "| 企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据 |\n" + \
                   "|---|---|---|---|---|---|\n" + \
                   "| 中国建筑第三工程局 | 央企 | 是 | 高 | 武汉、深圳 | 央企子公司，总部武汉 |\n" + \
                   "| 某科技公司 | 民企 | 否 | 中 | 北京 | 民营互联网企业，总部北京 |\n"


def parse_report_md(text: str) -> dict:
    """解析导入的 Markdown 报告，从「全部企业明细」表格重建企业分析缓存。

    返回 {"entries": {企业名: 记录}, "meta": {元信息}, "bad_rows": 忽略行数}。
    列顺序（表头自动识别，兼容导出报告的「企业名称|类型|国企|置信度|工作地点|依据」）：
      企业名称、类型、国企、置信度、工作地点、依据
    """
    entries: dict[str, dict] = {}
    meta: dict[str, str] = {}
    cols = None          # 列名 -> 下标
    in_detail = False
    bad = 0
    for raw in text.splitlines():
        line = raw.strip()
        # 元信息行：- 生成时间：xxx / - 数据来源：xxx 等
        m = re.match(r"^-\s*([^：:]+?)[：:]\s*(.*)$", line)
        if m and not line.startswith("|"):
            meta[m.group(1).strip()] = m.group(2).strip()
            continue
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # 跳过分隔行 |---|
        if cells and all(set(c) <= set("-: ") for c in cells if c):
            continue
        if not in_detail:
            joined = "".join(cells)
            if ("企业名称" in joined and ("国企" in joined or "是否国企" in joined)
                    and "工作地点" in joined):
                cols = {}
                for i, h in enumerate(cells):
                    if h == "企业名称": cols["name"] = i
                    elif h in ("国企", "是否国企"): cols["so"] = i
                    elif "类型" in h: cols["type"] = i
                    elif "置信度" in h: cols["conf"] = i
                    elif "工作地点" in h: cols["loc"] = i
                    elif h in ("依据", "判断依据"): cols["evidence"] = i
                in_detail = True
            continue

        def g(key, default=""):
            i = cols.get(key) if cols else None
            return cells[i].strip() if (i is not None and i < len(cells)) else default

        name = g("name")
        if not name or name in ("企业名称",):
            continue
        so_raw = g("so")
        so = True if so_raw == "是" else (False if so_raw == "否" else None)
        loc_str = g("loc")
        locs = [c.strip() for c in loc_str.replace("、", ",").split(",") if c.strip()] if loc_str and loc_str != "-" else []
        entries[name] = {
            "company_type": g("type"),
            "is_state_owned": so,
            "confidence": g("conf"),
            "locations": locs,
            "evidence": g("evidence"),
            "_raw": "",
        }
    return {"entries": entries, "meta": meta, "bad_rows": bad}


@app.route("/api/import/md/template")
def api_import_md_template():
    """下载一份「导入报告 Markdown」模板供参考。"""
    return send_file(io.BytesIO(_IMPORT_TEMPLATE.encode("utf-8")),
                     mimetype="text/markdown; charset=utf-8", as_attachment=True,
                     download_name="导入报告模板.md")


@app.route("/api/import/md", methods=["POST"])
def api_import_report():
    """导入 Markdown 分析报告，重建「企业分析」缓存并同步刷新 CSV / MD 报告。"""
    text = ""
    if request.files.get("file"):
        text = request.files["file"].read().decode("utf-8", errors="replace")
    elif request.is_json and request.get_json(silent=True):
        text = (request.get_json(silent=True) or {}).get("text", "")
    text = (text or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请选择或粘贴要导入的 Markdown 报告"}), 400

    parsed = parse_report_md(text)
    if not parsed["entries"]:
        return jsonify({"ok": False, "error": "未识别到「全部企业明细」表格。请按模板格式：企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据"}), 400

    cache = load_cache()
    cache.update(parsed["entries"])
    analyze.save_cache(CACHE_PATH, cache)
    # 用缓存重建 CSV / Markdown，让导出与统计保持一致
    model = parsed["meta"].get("分析模型", "") or get_llm()["model"]
    src = parsed["meta"].get("数据来源", "") or "导入的分析报告"
    try:
        analyze.build_outputs(DATA, [{"name": n} for n in cache], cache, model, src)
    except Exception:
        pass  # 缓存已更新，报告文件重建失败不影响导入结果

    return jsonify({"ok": True, "imported": len(parsed["entries"]),
                    "total": len(cache), "bad_rows": parsed["bad_rows"]})


# 导出表头与行转换见 exports.py（此处重导出以兼容既有引用）
_PREACH_FAV_HEADERS = exports.PREACH_FAV_HEADERS
_preach_fav_row = exports.preach_fav_row


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
        bio = exports.build_preach_favs_xlsx(matched)
        return send_file(bio, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"收藏宣讲会_{fmt}.xlsx")
    except Exception:      # openpyxl 缺失或写表失败 → 降级为 CSV
        bio = exports.build_preach_favs_csv(matched)
        return send_file(bio, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name=f"收藏宣讲会_{fmt}.csv")


# ICS 日历生成见 exports.py（此处重导出以兼容既有引用）
_ics_escape = exports.ics_escape
_ics_fold = exports.ics_fold
_preach_ics_event = exports.preach_ics_event




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
    bio = io.BytesIO(exports.build_preach_ics(matched))
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
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # 本服务无鉴权：配置、API Key、抓取/删除操作对任何能访问该地址的人开放
        print("⚠️  安全警告：当前监听非本地地址，服务无鉴权，同网段任何人都可以读取配置、"
              "修改 API Key 并触发抓取/导出任务。")
        print("    确认你处于可信网络，否则请改回 --host 127.0.0.1")
    if args.debug:
        print("⚠️  debug 模式已开启：异常会在浏览器显示堆栈，且代码改动会自动重载，请勿在公共环境使用。")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
