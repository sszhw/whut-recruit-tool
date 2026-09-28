"""个人求职偏好 HTTP 接口。

偏好一存，投递推荐与宣讲会筛选就不必每次重填城市 / 企业性质了——
这是这个接口存在的唯一理由，所以它返回的结构必须能让前端直接回填表单：

- `GET  /api/prefs` → `{ok, prefs, defaults}`（`defaults` 给空模板与下拉候选）
- `POST /api/prefs` → 部分更新（空数组 = 清空该项），返回保存后的**完整**偏好，
  前端省掉一次回查。

读写与归一化全在 `services/prefs.py`，这里只做参数解析与 jsonify。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from services import prefs as svc

bp = Blueprint("prefs", __name__)


@bp.route("/api/prefs")
def api_prefs_get():
    """读取当前求职偏好；文件缺失/损坏时返回空偏好（不会 500）。"""
    return jsonify({"ok": True, "prefs": svc.load_prefs(), "defaults": svc.defaults()})


@bp.route("/api/prefs", methods=["POST"])
def api_prefs_save():
    """保存求职偏好（部分更新）。"""
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    return jsonify({"ok": True, "prefs": svc.save_prefs(payload)})
