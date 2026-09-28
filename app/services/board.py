"""投递看板业务规则：阶段流转、条目管理与转化统计。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from uuid import uuid4

import board_store

from services import ServiceError

STAGES = ("关注", "投递", "笔试", "面试", "Offer", "放弃")
ACTIVE_STAGES = STAGES[:-1]

# 校招流程可能跳过笔试或面试，但不允许倒退；放弃后可重新关注并开始新一轮跟进。
TRANSITIONS = {
    "关注": frozenset(("投递", "放弃")),
    "投递": frozenset(("笔试", "面试", "Offer", "放弃")),
    "笔试": frozenset(("面试", "Offer", "放弃")),
    "面试": frozenset(("Offer", "放弃")),
    "Offer": frozenset(("放弃",)),
    "放弃": frozenset(("关注",)),
}

_SOURCE_ALIASES = {
    "招聘": "招聘",
    "招聘信息": "招聘",
    "recruitment": "招聘",
    "宣讲会": "宣讲会",
    "preach": "宣讲会",
}
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _first(data: dict, *keys: str) -> str:
    for key in keys:
        value = _text(data.get(key))
        if value:
            return value
    return ""


def _source_type(value) -> str:
    source_type = _SOURCE_ALIASES.get(_text(value).lower())
    if not source_type:
        raise ServiceError("来源类型必须是招聘或宣讲会")
    return source_type


def _snapshot(payload: dict) -> dict:
    raw = payload.get("snapshot") or payload.get("record") or {}
    if not isinstance(raw, dict):
        raise ServiceError("snapshot 必须是对象")
    # 同时兼容主库中文展示字段与抓取层英文原始字段；仅固化看板真正需要展示的内容。
    snapshot = {
        "unit": _first(raw, "unit", "单位", "单位名称", "company", "com_id_name", "name")
        or _first(payload, "unit", "单位", "单位名称"),
        "title": _first(raw, "title", "标题") or _first(payload, "title", "标题"),
        "link": _first(raw, "link", "链接", "原网页", "httpurl", "url")
        or _first(payload, "link", "链接", "原网页"),
        "date": _first(raw, "date", "发布日期", "举办日期", "宣讲时间", "hold_date")
        or _first(payload, "date", "发布日期", "举办日期"),
        "location": _first(raw, "location", "宣讲会地点", "城市", "address")
        or _first(payload, "location", "宣讲会地点"),
    }
    if not snapshot["unit"] and not snapshot["title"]:
        raise ServiceError("单位名称和标题至少填写一项")
    return snapshot


def _find(items: list[dict], item_id: str) -> dict:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise ServiceError("看板条目不存在", status=404)


def list_items(stage: str = "") -> list[dict]:
    """列出看板条目，可按当前阶段筛选，最近更新的排在前面。"""
    stage = _text(stage)
    if stage and stage not in STAGES:
        raise ServiceError("未知的看板阶段")
    rows = board_store.load_board()["items"]
    if stage:
        rows = [item for item in rows if item.get("stage") == stage]
    return sorted(rows, key=lambda item: item.get("updated_at", ""), reverse=True)


def create_item(payload: dict) -> dict:
    """新增一条关联招聘或宣讲会的看板条目，并固化展示快照。"""
    if not isinstance(payload, dict):
        raise ServiceError("请求数据必须是对象")
    source_type = _source_type(payload.get("source_type") or payload.get("type"))
    raw = payload.get("snapshot") or payload.get("record") or {}
    source_id = _text(payload.get("source_id") or payload.get("record_id"))
    if not source_id and isinstance(raw, dict):
        source_id = _first(raw, "ID", "id", "source_id")
    if not source_id:
        raise ServiceError("缺少关联记录 ID")
    stage = _text(payload.get("stage") or "关注")
    if stage not in STAGES:
        raise ServiceError("未知的看板阶段")
    snapshot = _snapshot(payload)
    now = _now()
    item = {
        "id": uuid4().hex,
        "source_type": source_type,
        "source_id": source_id,
        "snapshot": snapshot,
        "stage": stage,
        "note": _text(payload.get("note")),
        "created_at": now,
        "updated_at": now,
        "history": [{"stage": stage, "at": now}],
    }
    with _LOCK:
        board = board_store.load_board()
        if any(row.get("source_type") == source_type and _text(row.get("source_id")) == source_id
               for row in board["items"]):
            raise ServiceError("该记录已在投递看板中", status=409)
        board["items"].append(item)
        board_store.save_board(board)
    return item


def update_status(item_id: str, stage: str) -> dict:
    """按状态机更新阶段；重复设置当前阶段视为幂等成功。"""
    item_id = _text(item_id)
    stage = _text(stage)
    if not item_id:
        raise ServiceError("缺少条目 ID")
    if stage not in STAGES:
        raise ServiceError("未知的看板阶段")
    with _LOCK:
        board = board_store.load_board()
        item = _find(board["items"], item_id)
        current = item.get("stage")
        if current not in STAGES:
            raise ServiceError("条目当前阶段无效")
        if current == stage:
            return item
        if stage not in TRANSITIONS[current]:
            raise ServiceError(f"不能从「{current}」流转到「{stage}」")
        now = _now()
        item["stage"] = stage
        item["updated_at"] = now
        history = item.setdefault("history", [])
        if not isinstance(history, list):
            history = item["history"] = []
        history.append({"stage": stage, "at": now})
        board_store.save_board(board)
        return item


def update_note(item_id: str, note: str) -> dict:
    """更新条目备注。"""
    item_id = _text(item_id)
    if not item_id:
        raise ServiceError("缺少条目 ID")
    with _LOCK:
        board = board_store.load_board()
        item = _find(board["items"], item_id)
        item["note"] = _text(note)
        item["updated_at"] = _now()
        board_store.save_board(board)
        return item


def delete_item(item_id: str) -> dict:
    """删除条目并返回被删除的数据。"""
    item_id = _text(item_id)
    if not item_id:
        raise ServiceError("缺少条目 ID")
    with _LOCK:
        board = board_store.load_board()
        item = _find(board["items"], item_id)
        board["items"].remove(item)
        board_store.save_board(board)
        return item


def statistics() -> dict:
    """统计当前阶段计数与历史到达口径的阶段转化率（百分比）。"""
    items = board_store.load_board()["items"]
    counts = {stage: 0 for stage in STAGES}
    reached = {stage: 0 for stage in ACTIVE_STAGES}
    journeys = []
    for item in items:
        stage = item.get("stage")
        if stage in counts:
            counts[stage] += 1
        visited = {
            event.get("stage") for event in item.get("history", [])
            if isinstance(event, dict) and event.get("stage") in STAGES
        }
        if not visited and stage in STAGES:
            visited.add(stage)
        journeys.append(visited)
        for target in ACTIVE_STAGES:
            if target in visited:
                reached[target] += 1

    pairs = (("关注", "投递"), ("投递", "笔试"), ("投递", "面试"), ("面试", "Offer"))
    conversions = []
    rates = {}
    for source, target in pairs:
        denominator = reached[source]
        numerator = sum(1 for visited in journeys if source in visited and target in visited)
        rate = round(numerator * 100 / denominator, 1) if denominator else 0.0
        label = f"{source}→{target}"
        rates[label] = rate
        conversions.append({
            "from": source,
            "to": target,
            "numerator": numerator,
            "denominator": denominator,
            "rate": rate,
        })
    return {
        "total": len(items),
        "counts": counts,
        "reached": reached,
        "conversion_rates": rates,
        "conversions": conversions,
    }
