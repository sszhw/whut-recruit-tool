#!/usr/bin/env python3
"""武汉理工招聘信息采集与企业分析工具 —— Web 界面服务。

启动：
    python server.py            # 默认 http://127.0.0.1:8765
    python server.py --port 9000

架构（阶段 2 拆分后）：

    server.py          只做「组装」：create_app() + 请求钩子 + 命令行入口
      └─ api/          Blueprint，只负责参数校验与 HTTP 响应
           ├─ status.py       页面 / 全局状态 / 数据健康 / 任务列表
           ├─ settings_cfg.py LLM 厂商与 config.json
           ├─ crawl.py        抓取 / 增量更新 / 宣讲会检查
           ├─ analysis.py     企业分析 / 工作地流动 / 任务日志与停止
           ├─ browse.py       招聘、双选会、企业、统计
           ├─ preaches.py     宣讲会筛选与收藏
           ├─ recommend.py    简历解析与投递推荐
           └─ exports_rt.py   CSV/MD/Excel/ICS 导出与报告导入
      └─ dataloaders.py 数据读取与派生（统一走 repository）
      └─ extensions.py  运行时共享对象（后台任务管理器）
      └─ settings.py    路径常量、配置读写、日志

下方 `__all__` 里其余名字是为保持向后兼容而重导出的旧入口
（测试与既有脚本仍通过 `server.xxx` 访问），实际实现已迁到上述模块。
"""

from __future__ import annotations

import argparse
import time
import uuid

import exports  # 导出相关的旧兼容引用（ICS / 收藏表头）见下方重导出
from api import register_blueprints
from api.exports_rt import _IMPORT_TEMPLATE, _md_cell, parse_report_md
from api.preaches import _apply_preach_filters, _collect_preach_ids, _venue_label
from api.recommend import (
    ALLOWED_EXT,
    RESUME_PROMPT_LIMIT,
    _build_so_map,
    _recommend_preachs,
    save_upload,
)
from dataloaders import (
    load_cache,
    load_fairs,
    load_preach_favs,
    load_preachs,
    load_recruitments,
    save_preach_favs,
    work_undetermined_count,
)
from extensions import KIND_SLUGS, TaskManager, _error_summary, _parse_progress, tasks
from flask import Flask, g, jsonify, request
from settings import (
    CACHE_PATH,
    DATA,
    FAV_PATH,
    INDEX_HTML,
    LLM_PROVIDERS,
    MAX_UPLOAD_BYTES,
    ROOT,
    TASK_HISTORY_MAX,
    TASK_HISTORY_PATH,
    TASK_LOG_DIR,
    TASK_LOG_MAX,
    WORK_FLOW_CSV,
    WORKDIR,
    _mask,
    get_api_key,
    get_llm,
    load_config,
    log_event,
    save_config,
)
from werkzeug.exceptions import HTTPException

__all__ = [
    # 应用
    "app", "create_app", "main",
    # 运行时对象
    "tasks", "TaskManager", "KIND_SLUGS", "_parse_progress", "_error_summary",
    # 配置与日志
    "load_config", "save_config", "get_api_key", "get_llm", "log_event", "_mask", "LLM_PROVIDERS",
    # 数据读取
    "load_recruitments", "load_fairs", "load_preachs", "load_cache",
    "load_preach_favs", "save_preach_favs", "work_undetermined_count", "_work_undetermined_count",
    # 简历推荐
    "save_upload", "_build_so_map", "_recommend_preachs", "ALLOWED_EXT", "RESUME_PROMPT_LIMIT",
    # 报告导入 / 导出
    "parse_report_md", "_md_cell", "_IMPORT_TEMPLATE",
    "_apply_preach_filters", "_collect_preach_ids", "_venue_label",
    # ICS / 收藏导出（原先就在此重导出）
    "_ics_escape", "_ics_fold", "_preach_ics_event", "_PREACH_FAV_HEADERS", "_preach_fav_row",
    # 路径常量（旧引用）
    "DATA", "ROOT", "WORKDIR", "CACHE_PATH", "FAV_PATH", "INDEX_HTML", "WORK_FLOW_CSV",
    "MAX_UPLOAD_BYTES", "TASK_HISTORY_PATH", "TASK_LOG_DIR", "TASK_HISTORY_MAX", "TASK_LOG_MAX",
]


def create_app() -> Flask:
    """应用工厂： Flask 实例 + 请求钩子 + 蓝图注册。

    用工厂而非模块单例，是为了让测试能为不同用例各自建一个干净的 app，
    且不依赖模块导入顺序。
    """
    app = Flask(__name__)
    app.json.ensure_ascii = False
    # 上传体积上限：简历 / 报告导入走内存缓存，超限直接 413，避免大文件撑爆内存
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

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

    register_blueprints(app)
    return app


app = create_app()

# 以下两项原先就在此重导出，测试与旧脚本仍通过 server.* 访问
_ics_escape = exports.ics_escape
_ics_fold = exports.ics_fold
_preach_ics_event = exports.preach_ics_event
_PREACH_FAV_HEADERS = exports.PREACH_FAV_HEADERS
_preach_fav_row = exports.preach_fav_row

_work_undetermined_count = work_undetermined_count   # 兼容旧名


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
