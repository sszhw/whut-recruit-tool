"""企业详情 HTTP 接口。

企业名放在 URL path 里（而不是 query string），是为了让详情页链接可复制分享；
代价是要自己处理中文编码 —— Flask/Werkzeug 已经解码过一次，
再 `unquote` 一次是为了兜住被二次编码（%%E4%）的请求。
"""

from __future__ import annotations

from urllib.parse import unquote

from flask import Blueprint, jsonify
from services import ServiceError
from services import company as svc

bp = Blueprint("company", __name__)


def _error(exc: ServiceError):
    return jsonify({"ok": False, "error": str(exc)}), exc.status


@bp.route("/api/company/<path:name>")
def api_company(name: str):
    """GET /api/company/<企业名> —— 聚合招聘公告 / 宣讲会 / 分析画像 / 投递状态。"""
    try:
        profile = svc.build_profile(unquote(name))
    except ServiceError as exc:
        return _error(exc)
    return jsonify({"ok": True, **profile})
