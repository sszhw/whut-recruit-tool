"""repository 统一数据访问层测试：跨文件合并、ID 去重、缓存、聚合统计、企业清单。"""

from __future__ import annotations

import repository as repo
from conftest import preach_item, preach_path, recruit_item, recruit_path, write_raw


def test_merge_all_files_and_dedup_newest_wins(make_recruit):
    """跨全部原始文件合并，按 ID 去重，新文件优先（同 ID 取新文件的版本）。"""
    make_recruit("2026-09-01_至_2026-09-01", [
        recruit_item("1", name="东方电气", title="旧标题"),
        recruit_item("2", name="东方电气"),
    ], mtime=1_700_000_000)
    make_recruit("2026-09-10_至_2026-09-10", [
        recruit_item("1", name="东方电气", title="新标题"),
        recruit_item("3", name="中国中车"),
    ], mtime=1_700_100_000)

    items = repo.raw_items("recruit")
    ids = [str(i["id"]) for i in items]
    assert sorted(ids) == ["1", "2", "3"]
    first = next(i for i in items if str(i["id"]) == "1")
    assert first["title"] == "新标题"


def test_kinds_read_correct_array_key(data_dir, make_recruit, make_preach):
    """招聘信息 / 双选会 / 宣讲会 分别读各自的数组键。"""
    write_raw(recruit_path(data_dir, "2026-09-01_至_2026-09-01"),
              招聘信息=[recruit_item("10")],
              双选会=[{"id": "f1", "title": "秋季双选会", "field_id_name": "东院体育馆",
                      "verify_count": 120}])
    make_preach("2026年", [preach_item("p1")])

    assert len(repo.raw_items("recruit")) == 1
    assert len(repo.raw_items("fair")) == 1
    assert repo.raw_items("fair")[0]["title"] == "秋季双选会"
    assert len(repo.raw_items("preach")) == 1


def test_aggregate_counts_coverage_and_missing_detail(make_recruit):
    """聚合统计：记录数、覆盖日期（按 addtime）、最近更新时间、详情缺失条数。"""
    make_recruit("2026-08-08_至_2026-09-07", [
        recruit_item("1", addtime=1786000000),                                  # 2026-08-06 前后
        recruit_item("2", addtime=1789000000, content="", remarks=""),          # 无正文 → 计入缺失
    ], mtime=1_700_000_000)

    agg = repo.aggregate("recruit")
    assert agg["records"] == 2
    assert agg["files"] == 1
    assert agg["missing_detail"] == 1
    assert agg["first_date"] <= agg["last_date"]
    assert agg["last_update"]  # 形如 2026-09-16 19:20


def test_master_summary_merges_recruit_and_preach(make_recruit, make_preach):
    make_recruit("2026-08-08_至_2026-09-07", [recruit_item("1", addtime=1787000000)])
    make_preach("2026年", [preach_item("p1", hold_date="2026-09-20"),
                           preach_item("p2", hold_date="2026-10-01")])

    s = repo.master_summary()
    assert s["recruit_count"] == 1
    assert s["preach_count"] == 2
    assert s["files"] == 2
    assert s["coverage_start"] <= "2026-09-20" <= s["coverage_end"]


def test_cache_reuses_result_and_invalidates_on_change(make_recruit):
    """同一份文件签名下复用缓存；数据文件变化后自动失效重算。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1")], mtime=1_700_000_000)
    first = repo.raw_items("recruit")
    assert first is repo.raw_items("recruit")          # 同一个对象 → 命中缓存
    assert repo.aggregate("recruit")["records"] == 1

    make_recruit("2026-09-02_至_2026-09-02", [recruit_item("2")], mtime=1_700_100_000)
    assert len(repo.raw_items("recruit")) == 2         # 文件变化 → 缓存失效

    repo.invalidate()
    assert len(repo.raw_items("recruit")) == 2


def test_companies_dedupe_count_and_longest_text(make_recruit):
    """企业清单：按单位名去重、统计公告数、取最长正文、按公告数排序。"""
    make_recruit("2026-09-01_至_2026-09-01", [
        recruit_item("1", name="东风汽车", content="短正文"),
        recruit_item("2", name="东风汽车", content="这是一段更长的招聘正文，包含岗位与工作地点信息"),
        recruit_item("3", name="中国船舶", content="第七一九研究所招聘"),
        recruit_item("4", name="", content="无单位名 → 忽略"),
    ])

    rows = repo.companies()
    names = [r["name"] for r in rows]
    assert names[0] == "东风汽车"                       # 公告数多者优先
    assert set(names) == {"东风汽车", "中国船舶"}
    dongfeng = rows[0]
    assert dongfeng["count"] == 2
    assert "更长的招聘正文" in dongfeng["text"]

    assert [c["name"] for c in repo.companies(max_items=1)] == ["东风汽车"]


def test_unanalyzed_and_health(make_recruit):
    make_recruit("2026-09-01_至_2026-09-01", [
        recruit_item("1", name="已分析企业"),
        recruit_item("2", name="待分析企业"),
    ])
    cache = {"已分析企业": {"company_type": "央企", "is_state_owned": True, "locations": ["武汉"]}}

    assert repo.unanalyzed(cache) == ["待分析企业"]
    health = repo.health(cache=cache, work_undetermined=3)
    assert health["analyzed_count"] == 1
    assert health["unanalyzed_count"] == 1
    assert health["work_undetermined"] == 3
    assert health["recruit_count"] == 2


def test_raw_items_in_uses_given_dir(tmp_path):
    """raw_items_in 不依赖全局 DATA，供 CLI（analyze.py --merge）与测试使用。"""
    write_raw(preach_path(tmp_path, "2026年"), 宣讲会=[preach_item("p1")])
    items = repo.raw_items_in("preach", tmp_path)
    assert len(items) == 1 and items[0]["id"] == "p1"
    assert repo.raw_items_in("recruit", tmp_path) == []


def test_newest_file_priority_is_by_mtime_not_name(make_recruit):
    """优先级按文件修改时间，而不是文件名（避免"看起来更新"的名字干扰）。"""
    make_recruit("zzz_旧内容", [recruit_item("1", title="时间旧")], mtime=1_600_000_000)
    make_recruit("aaa_新内容", [recruit_item("1", title="时间新")], mtime=1_700_000_000)
    assert repo.raw_items("recruit")[0]["title"] == "时间新"
