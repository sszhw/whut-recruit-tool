"""宣讲会浏览、筛选与收藏。

收藏是「宣讲会 → 日历」这条链路的起点：收藏后可导出 Excel 或 ICS
（见 exports 蓝图），因此这里的 ID 集合必须与 dataloaders 的收藏读写保持一致。
"""

from __future__ import annotations

from collections import Counter

from dataloaders import (
    load_preach_favs,
    load_preachs,
    save_preach_favs,
)
from flask import Blueprint, jsonify, request

bp = Blueprint("preaches", __name__)


def _venue_label(addr: str) -> str:
    """场馆下拉展示用的短标签，去掉括号内的长说明（如「该场地仅用于…」）。"""
    for marker in ("（该场地仅用于", "(该场地仅用于"):
        idx = addr.find(marker)
        if idx != -1:
            return addr[:idx].strip()
    return addr


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


@bp.route("/api/preachs/filters")
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


@bp.route("/api/preachs")
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


@bp.route("/api/preach/fav", methods=["POST"])
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


@bp.route("/api/preach/unfav", methods=["POST"])
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


@bp.route("/api/preach/favs")
def api_preach_favs_list():
    """返回收藏的宣讲会完整记录（含过去的，按举办日期排序）。"""
    favs = load_preach_favs()
    rows = load_preachs(past=True)
    matched = [r for r in rows if str(r.get("ID", "")) in favs]
    matched.sort(key=lambda r: r.get("举办日期") or "")
    return jsonify({"count": len(matched), "rows": matched, "ids": sorted(favs)})


@bp.route("/api/preach/fav-all", methods=["POST"])
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


@bp.route("/api/preach/unfav-all", methods=["POST"])
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
