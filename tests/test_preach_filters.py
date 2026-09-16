"""宣讲会筛选（搜索 / 线下线上 / 场馆 / 工作地 / 日期范围）与提醒标注测试。"""

from __future__ import annotations

import server
from conftest import preach_item

ROWS = [
    {
        "单位名称": "东风汽车集团", "标题": "东风汽车2027届校园招聘",
        "宣讲会地点": "武汉市，马房山校区，东风厅", "城市": "武汉市",
        "场馆": "武汉市，马房山校区，东风厅", "线下/线上": "线下",
        "公司地点": "武汉、十堰", "work_cities": ["武汉", "十堰"], "举办日期": "2026-09-20",
    },
    {
        "单位名称": "华为技术有限公司", "标题": "华为软件类专场",
        "宣讲会地点": "武汉市，东院就业大楼多功能厅", "城市": "武汉市",
        "场馆": "武汉市，东院就业大楼多功能厅", "线下/线上": "线上",
        "公司地点": "深圳", "work_cities": ["深圳"], "举办日期": "2026-09-25",
    },
    {
        "单位名称": "中国船舶集团", "标题": "中船集团宣讲",
        "宣讲会地点": "线上直播", "城市": "", "场馆": "线上直播", "线下/线上": "线上",
        "公司地点": "上海", "work_cities": ["上海"], "举办日期": "2026-10-08",
    },
]


def flt(**kw):
    args = {"q": "", "ptype": "", "venue": "", "work": "", "start_d": "", "end_d": ""}
    args.update(kw)
    return server._apply_preach_filters(ROWS, args["q"], args["ptype"], args["venue"],
                                        args["work"], args["start_d"], args["end_d"])


def test_no_filter_returns_all():
    assert len(flt()) == 3


def test_search_hits_unit_title_venue_city_workplace():
    assert [r["单位名称"] for r in flt(q="东风")] == ["东风汽车集团"]
    assert [r["单位名称"] for r in flt(q="软件类")] == ["华为技术有限公司"]      # 标题
    assert [r["单位名称"] for r in flt(q="东院就业大楼")] == ["华为技术有限公司"]  # 宣讲会地点
    assert [r["单位名称"] for r in flt(q="武汉市")] == ["东风汽车集团", "华为技术有限公司"]  # 城市
    assert [r["单位名称"] for r in flt(q="上海")] == ["中国船舶集团"]            # 公司地点
    assert flt(q="不存在的关键词") == []


def test_offline_online_filter():
    assert [r["单位名称"] for r in flt(ptype="线下")] == ["东风汽车集团"]
    assert len(flt(ptype="线上")) == 2


def test_venue_filter_matches_exact_venue():
    assert [r["单位名称"] for r in flt(venue="线上直播")] == ["中国船舶集团"]
    assert flt(venue="不存在的场馆") == []


def test_work_city_filter_matches_any_work_city():
    assert [r["单位名称"] for r in flt(work="武汉")] == ["东风汽车集团"]
    assert [r["单位名称"] for r in flt(work="十堰")] == ["东风汽车集团"]
    assert [r["单位名称"] for r in flt(work="深圳")] == ["华为技术有限公司"]


def test_date_range_filter_inclusive():
    assert [r["单位名称"] for r in flt(start_d="2026-09-21")] == ["华为技术有限公司", "中国船舶集团"]
    assert [r["单位名称"] for r in flt(end_d="2026-09-20")] == ["东风汽车集团"]
    assert [r["单位名称"] for r in flt(start_d="2026-09-21", end_d="2026-09-30")] == ["华为技术有限公司"]


def test_combined_filters_intersect():
    assert flt(ptype="线上", work="上海") == [ROWS[2]]
    assert flt(q="校园招聘", ptype="线上") == []


def test_preach_item_dates_and_times_survive_pipeline(make_preach):
    """原始数据 → repository 的关键字段（未走 server 的推断，只验证取数与去重）。"""
    import repository as repo
    make_preach("2026年", [preach_item("p1", name="东风汽车集团", hold_date="2026-09-20",
                                       start="19:00", end="21:00"),
                           preach_item("p1", name="东风汽车集团", hold_date="2026-09-20"),
                           preach_item("p2", name="华为技术有限公司", hold_date="2026-09-25")])
    items = repo.raw_items("preach")
    assert [i["id"] for i in items] == ["p1", "p2"]          # 同 ID 去重
    assert items[0]["hold_date"] == "2026-09-20"
    assert items[0]["hold_starttime"] == "19:00"
