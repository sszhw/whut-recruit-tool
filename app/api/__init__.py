"""HTTP API 蓝图集合。

所有蓝图在这里集中注册，避免「新增了 Blueprint 却忘记挂载」——
那种情况下路由会静默消失，靠人工 review 很难发现，
但 `tests/test_api_smoke.py` 的全路由遍历会把 missing 的 URL 暴露出来。
"""

from __future__ import annotations

from flask import Blueprint, Flask

from api.analysis import bp as analysis_bp
from api.browse import bp as browse_bp
from api.crawl import bp as crawl_bp
from api.events import bp as events_bp
from api.exports_rt import bp as exports_bp
from api.preaches import bp as preaches_bp
from api.recommend import bp as recommend_bp
from api.settings_cfg import bp as settings_bp
from api.status import bp as status_bp

ALL: list[Blueprint] = [
    status_bp,      # /           首页 + /api/status /api/health /api/tasks
    events_bp,      # /api/events SSE 推送（status / tasks）
    settings_bp,    # /api/llm/*  /api/config*
    crawl_bp,       # /api/crawl  /api/recruit/update /api/preach/check
    analysis_bp,    # /api/analyze /api/flow /api/task/*
    browse_bp,      # /api/recruitments /api/fairs /api/companies* /api/stats /api/actions
    preaches_bp,    # /api/preachs* /api/preach/fav*
    recommend_bp,   # /api/resume/*
    exports_bp,     # /api/export/* /api/import/md* /api/preach/favs/export*
]


def register_blueprints(app: Flask) -> None:
    """把全部蓝图挂到应用上。"""
    for bp in ALL:
        app.register_blueprint(bp)
