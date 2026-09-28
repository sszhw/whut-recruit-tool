"""宣讲会的业务规则：筛选、筛选项聚合、收藏集合运算。

这里不碰 request / jsonify，全部是「进数据、出数据」的纯逻辑，
因此 `tests/test_preach_filters.py` 可以直接对行列表断言，不必起 Flask。

收藏集合的读写仍走 `dataloaders`（它负责落盘口径），本模块只做集合运算。
"""

from __future__ import annotations

from collections import Counter

from dataloaders import load_preach_favs, load_preachs, save_preach_favs

# 场馆名里常带括号说明（如「该场地仅用于宣讲，不用于笔试」），下拉里太长
_VENUE_NOTE_MARKERS = ("（该场地仅用于", "(该场地仅用于")

TRUE_VALUES = ("1", "true", "yes", "on")


def as_bool(value, default: bool = False) -> bool:
    """把界面传来的开关值统一成 bool（兼容 "1"/"true"/"yes"/"on"）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE_VALUES


def venue_label(addr: str) -> str:
    """场馆下拉展示用的短标签，去掉括号内的长说明。"""
    for marker in _VENUE_NOTE_MARKERS:
        idx = addr.find(marker)
        if idx != -1:
            return addr[:idx].strip()
    return addr


def apply_filters(rows: list[dict], q: str, ptype: str,
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


def parse_filter_args(payload: dict) -> dict:
    """从 payload（GET query 或 POST json）抽出并规整筛选条件。

    抽出来是因为「筛选条件」有两组入口：列表查询用 query string，
    批量收藏用 JSON body，两边的空值/大小写口径必须一致，否则
    「筛出来的列表」和「一键收藏的范围」会对不上。
    """
    return {
        "q": str(payload.get("q", "")).strip().lower(),
        "type": str(payload.get("type", "")).strip(),
        "venue": str(payload.get("venue", "")).strip(),
        "work": str(payload.get("work", "")).strip(),
        "start": str(payload.get("start", "")).strip(),
        "end": str(payload.get("end", "")).strip(),
        "show_past": as_bool(payload.get("show_past")),
    }


def collect_ids(payload: dict) -> tuple[list[str], int]:
    """按筛选条件收集宣讲会 ID 列表。返回 (ID列表, 命中数)。"""
    f = parse_filter_args(payload)
    rows = load_preachs(past=f["show_past"])
    rows = apply_filters(rows, f["q"], f["type"], f["venue"], f["work"], f["start"], f["end"])
    ids = [str(r.get("ID", "")) for r in rows if r.get("ID")]
    return ids, len(rows)


def filter_options() -> dict:
    """宣讲会筛选项：公司地点（工作地城市）+ 宣讲会地点（具体场馆），带计数。"""
    rows = load_preachs()
    work_counts: Counter[str] = Counter()
    for r in rows:
        for w in r.get("work_cities") or []:
            work_counts[w] += 1
    venues: dict[str, int] = {}
    for r in rows:
        addr = str(r.get("场馆", "")).strip()
        if addr:
            venues[addr] = venues.get(addr, 0) + 1
    return {
        "work_cities": [{"value": c, "count": n} for c, n in work_counts.most_common()],
        "venues": [{"value": a, "label": venue_label(a), "count": c}
                   for a, c in sorted(venues.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


def matched_favs() -> list[dict]:
    """收藏的宣讲会记录（含过去的，按举办日期排序）。导出与列表共用。"""
    favs = load_preach_favs()
    rows = load_preachs(past=True)
    matched = [r for r in rows if str(r.get("ID", "")) in favs]
    matched.sort(key=lambda r: r.get("举办日期") or "")
    return matched


def bulk_fav(ids: list[str], add: bool) -> dict:
    """批量收藏 / 取消收藏。幂等：只计真正发生变化的数量。

    返回 {"changed": n, "total": 收藏总数}；无变化时不落盘，
    避免「点了一下没选中任何东西」也产生一次文件写入。
    """
    favs = load_preach_favs()
    changed = 0
    for rid in ids:
        if add:
            if rid and rid not in favs:
                favs.add(rid)
                changed += 1
        elif rid in favs:
            favs.discard(rid)
            changed += 1
    if changed:
        save_preach_favs(favs)
    return {"changed": changed, "total": len(favs)}


def toggle_fav(rid: str, add: bool) -> dict:
    """收藏 / 取消收藏单场。幂等。"""
    if not rid:
        raise ValueError("缺少 id")
    return bulk_fav([rid], add=add)
