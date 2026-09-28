"""宣讲会浏览、筛选与收藏 —— HTTP 壳。

业务规则全在 `services/preaches.py`（筛选、筛选项聚合、收藏集合运算），
这里只做三件事：读参数、调服务、拼响应。

收藏是「宣讲会 → 日历」这条链路的起点：收藏后可导出 Excel 或 ICS
（见 exports 蓝图），因此这里的 ID 集合必须与 dataloaders 的收藏读写保持一致。
"""

from __future__ import annotations

from dataloaders import load_preach_favs, load_preachs
from flask import Blueprint, jsonify, request
from services import preaches as svc

bp = Blueprint("preaches", __name__)


@bp.route("/api/preachs/filters")
def api_preachs_filters():
    """返回宣讲会筛选项：公司地点（工作地城市）+ 宣讲会地点（具体场馆）。"""
    return jsonify(svc.filter_options())


@bp.route("/api/preachs")
def api_preachs():
    rows = load_preachs(past=svc.as_bool(request.args.get("show_past")))
    if svc.as_bool(request.args.get("fav_only")):
        favs = load_preach_favs()
        rows = [r for r in rows if str(r.get("ID", "")) in favs]
    filters = svc.parse_filter_args(request.args)
    rows = svc.apply_filters(rows, filters["q"], filters["type"], filters["venue"],
                             filters["work"], filters["start"], filters["end"])
    # 分页是 HTTP 层的事（page / size 只存在于请求里），不放进服务
    page = max(1, request.args.get("page", 1, type=int))
    size = min(200, max(10, request.args.get("size", 50, type=int)))
    start = (page - 1) * size
    return jsonify({"total": len(rows), "page": page, "size": size,
                    "rows": rows[start:start + size]})


@bp.route("/api/preach/fav", methods=["POST"])
def api_preach_fav():
    """收藏一场宣讲会（幂等）。"""
    data = request.get_json(silent=True) or {}
    try:
        res = svc.toggle_fav(str(data.get("id", "")).strip(), add=True)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "fav": True, "count": res["total"]})


@bp.route("/api/preach/unfav", methods=["POST"])
def api_preach_unfav():
    """取消收藏一场宣讲会（幂等）。"""
    data = request.get_json(silent=True) or {}
    try:
        res = svc.toggle_fav(str(data.get("id", "")).strip(), add=False)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "fav": False, "count": res["total"]})


@bp.route("/api/preach/favs")
def api_preach_favs_list():
    """返回收藏的宣讲会完整记录（含过去的，按举办日期排序）。"""
    favs = load_preach_favs()
    matched = svc.matched_favs()
    return jsonify({"count": len(matched), "rows": matched, "ids": sorted(favs)})


@bp.route("/api/preach/fav-all", methods=["POST"])
def api_preach_fav_all():
    """一键收藏当前筛选条件下的全部宣讲会（幂等，忽略已收藏项）。"""
    return _bulk(add=True)


@bp.route("/api/preach/unfav-all", methods=["POST"])
def api_preach_unfav_all():
    """一键取消收藏当前筛选条件下的全部宣讲会（幂等，忽略已取消项）。"""
    return _bulk(add=False)


def _bulk(add: bool):
    data = request.get_json(force=True, silent=True) or {}
    ids, matched = svc.collect_ids(data)
    res = svc.bulk_fav(ids, add=add)
    key = "added" if add else "removed"
    return jsonify({"ok": True, key: res["changed"], "matched": matched, "total": res["total"]})
