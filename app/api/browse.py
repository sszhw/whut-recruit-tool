"""招聘信息、双选会、企业分析结果与统计的总览接口。

企业类数据来自「分析结果缓存」（data/企业分析_缓存.json），与招聘列表的原始 JSON
是两份不同来源：前者是 AI 分析产物，后者是学校网站原始数据。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import repository as repo
from dataloaders import (
    load_cache,
    load_fairs,
    load_preach_favs,
    load_preachs,
    load_recruitments,
)
from flask import Blueprint, jsonify, request

bp = Blueprint("browse", __name__)


@bp.route("/api/recruitments")
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


@bp.route("/api/actions")
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


@bp.route("/api/fairs")
def api_fairs():
    return jsonify({"rows": load_fairs()})


@bp.route("/api/companies/filters")
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
        "locations": [{"value": loc, "count": n} for loc, n in loc_counter.most_common()],
    })


@bp.route("/api/companies")
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


@bp.route("/api/stats")
def api_stats():
    cache = load_cache()
    type_counts = Counter(r.get("company_type", "") for r in cache.values())
    so_count = sum(1 for r in cache.values() if r.get("is_state_owned") is True)
    loc_counts = Counter(str(loc).strip() for r in cache.values()
                         for loc in (r.get("locations") or []) if str(loc).strip())
    return jsonify({
        "total": len(cache),
        "state_owned": so_count,
        "types": type_counts.most_common(),
        "locations": loc_counts.most_common(30),
    })
