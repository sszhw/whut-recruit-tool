"""企业详情：把散落在四处的信息聚合成一家企业的完整档案。

背景（`docs/重构规划.md` 缺口清单里它被称作「打通各模块的关键枢纽」）：
同一家企业的信息原本分散在四个地方——招聘公告列表、宣讲会列表、企业分析缓存、
投递看板。用户想知道「这家企业到底什么情况」，得自己去四处各搜一遍再脑内拼接。
本模块负责在服务端拼好，前端只管渲染。

聚合逻辑放在 services 而不是 api，是因为它纯粹是「进名字、出档案」的编排，
不认识 Flask：将来 CLI、定时任务或导出报告要同一份口径时可以直接复用。

唯一需要业务判断的是**企业名匹配**：同一个名字在不同来源里写法不一致
（空格、全半角括号、大小写），直接按字符串相等会漏掉同一家企业，详见 `normalize_name`。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

from dataloaders import load_cache, load_preachs, load_recruitments

from services import ServiceError
from services import board as board_svc

# 归一化后保留的字符：ASCII 字母数字 + CJK 统一表意文字。
# 其余（空格、标点、括号）一律丢弃——它们只反映书写习惯，不代表企业身份。
_NON_ALNUM = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def normalize_name(name: Any) -> str:
    """企业名称的匹配键：NFKC + 小写 + 只保留字母数字与汉字。

    三步各自解决一类差异：
      - NFKC 把全角字母数字与全角括号（）折叠成半角；
      - lower() 抹平英文名大小写（如 "BYD" / "byd"）；
      - 去掉空格与标点，使「某某集团（武汉）」「某某集团 (武汉)」「某某集团 武汉」
        落到同一个键上。
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    return _NON_ALNUM.sub("", text.lower())


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _tag_list(values: Any) -> list[str]:
    """把任意来源的「列表」安全地转成字符串列表（非列表一律当空）。"""
    if not isinstance(values, (list, tuple)):
        return []
    return [_text(v) for v in values if _text(v)]


def _analysis(cache: dict, target: str) -> dict:
    """企业画像：优先按归一化键在分析缓存里找。

    缓存 era 不同可能缺字段（早期版本没有 confidence / locations），
    因此统一 .get 兜底，绝不因缺失字段抛 KeyError。
    """
    info = None
    for name, value in (cache or {}).items():
        if normalize_name(name) == target and isinstance(value, dict):
            info = value
            break
    if info is None:
        return {"has": False, "company_type": "", "state_owned": "未知", "confidence": "",
                "locations": [], "evidence": ""}
    return {
        "has": True,
        "company_type": _text(info.get("company_type")),
        "state_owned": {True: "是", False: "否"}.get(info.get("is_state_owned"), "未知"),
        "confidence": _text(info.get("confidence")),
        "locations": _tag_list(info.get("locations")),
        "evidence": _text(info.get("evidence")),
    }


def _board_item(target: str) -> dict | None:
    """该企业在投递看板里的记录；没有返回 None（绝不抛错打扰详情页）。

    只读不写：投递阶段流转的规则归 `services/board.py` 管，
    详情页只是把状态读出来展示与做快捷入口。
    """
    for item in board_svc.list_items():
        unit = (item.get("snapshot") or {}).get("unit")
        if normalize_name(unit) == target:
            return item
    return None


def _recruit_rows(rows: list[dict]) -> list[dict]:
    """招聘公告 → 详情行。

    为什么要重新挑一遍字段而不是整行透传：列表行的字典里带着 600 字正文与内部标记，
    而详情页的卡片只需要这几个字段，收窄后 API 载荷更小，也不会把内部字段暴露到契约里。
    """
    return [{
        "ID": _text(r.get("ID")),
        "标题": _text(r.get("标题")),
        "发布日期": _text(r.get("发布日期")),
        "今日更新": bool(r.get("今日更新")),
        "原网页": _text(r.get("原网页")),
        "正文": _text(r.get("正文")),
    } for r in rows]


def _preach_rows(rows: list[dict]) -> list[dict]:
    """宣讲会 → 详情行，按举办日期倒序（与招聘公告一致：最新的在最上面）。"""
    selected = [{
        "ID": _text(r.get("ID")),
        "单位名称": _text(r.get("单位名称")),
        "标题": _text(r.get("标题")),
        "宣讲时间": _text(r.get("宣讲时间")),
        "举办日期": _text(r.get("举办日期")),
        "开始时间": _text(r.get("开始时间")),
        "结束时间": _text(r.get("结束时间")),
        "宣讲会地点": _text(r.get("宣讲会地点")),
        "城市": _text(r.get("城市")),
        "公司地点": _text(r.get("公司地点")),
        "work_cities": _tag_list(r.get("work_cities")),
        "线下/线上": _text(r.get("线下/线上")),
        "原网页": _text(r.get("原网页")),
        "正文": _text(r.get("正文")),
    } for r in rows]
    selected.sort(key=lambda r: r["举办日期"], reverse=True)
    return selected


def _pick_canonical(raw: str, spellings: list[str], target: str) -> tuple[str, list[str]]:
    """从同一企业的多种写法里选出展示名，其余作为别名返回。

    排序规则：出现次数多的优先（= 主流写法），同次数下取更完整的（更长的）。
    调用方传入的名字本身若就在候选里，直接用它——用户点的是哪个名字就显示哪个。
    """
    hits = [n for n in spellings if normalize_name(n) == target]
    if not hits:
        return "", []
    if raw in hits:
        canonical = raw
    else:
        counts = Counter(hits)
        canonical = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    alias = sorted(set(hits) - {canonical})
    return canonical, alias


def build_profile(name: str) -> dict:
    """聚合单个企业的全部信息；找不到该企业抛 ServiceError(404)。"""
    raw = _text(name)
    target = normalize_name(raw)
    if not target:
        raise ServiceError("企业名称不能为空")

    # 招聘 / 宣讲会行统一复用 dataloaders 的派生结果：
    # 与列表页同一份缓存、同一套正文清洗口径，详情页不会比列表页多出或少出一条。
    recruits = [r for r in load_recruitments() if normalize_name(r.get("单位")) == target]
    preaches = [p for p in load_preachs(past=True) if normalize_name(p.get("单位名称")) == target]
    cache = load_cache()

    canonical, alias = _pick_canonical(
        raw,
        [_text(k) for k in cache]
        + [_text(r.get("单位")) for r in recruits]
        + [_text(p.get("单位名称")) for p in preaches],
        target,
    )
    if not canonical:
        raise ServiceError(f"没有找到企业「{raw}」的相关数据", status=404)

    recruit_rows = _recruit_rows(recruits)
    preach_rows = _preach_rows(preaches)
    analysis = _analysis(cache, target)
    board_item = _board_item(target)

    return {
        "name": canonical,
        "alias": alias,
        "analysis": analysis,
        "recruitments": recruit_rows,
        "preaches": preach_rows,
        "board": board_item,
        "stats": {
            "recruitments": len(recruit_rows),
            "preaches": len(preach_rows),
            "board": 1 if board_item else 0,
            "analyzed": analysis["has"],
        },
    }
