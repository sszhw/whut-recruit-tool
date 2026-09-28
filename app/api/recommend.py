"""简历解析与投递推荐 —— HTTP 壳。

编排（候选构建、文字提取、LLM 调用与离线降级、宣讲会匹配）
全在 `services/recommend.py`，这里只从 request 取参数并包成 JSON。

失败统一走 `ServiceError`：业务层抛，路由转成 400 + {ok:false, error}。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from services import ServiceError
from services import recommend as svc

bp = Blueprint("recommend", __name__)


@bp.route("/api/resume/extract", methods=["POST"])
def api_resume_extract():
    try:
        res = svc.extract_resume_text(request.files.get("file"),
                                      vision_model=(request.form.get("vision_model") or "").strip() or None)
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return jsonify({"ok": True, **res})


@bp.route("/api/resume/companies")
def api_resume_companies():
    """候选企业统计（统一口径：主库全部招聘公告去重后的企业数）。"""
    return jsonify(svc.company_stats())


@bp.route("/api/resume/recommend", methods=["POST"])
def api_resume_recommend():
    try:
        payload = svc.build_recommendation(
            resume_text=request.form.get("resume_text") or "",
            work_place=(request.form.get("work_place") or "").strip(),
            company_type=(request.form.get("company_type") or "").strip(),
            model=(request.form.get("model") or "").strip(),
            file_storage=request.files.get("file"),
            vision_model=(request.form.get("vision_model") or "").strip() or None,
        )
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return jsonify({"ok": True, **payload})
