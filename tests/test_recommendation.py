"""投递推荐：候选企业汇总（统一主库口径）、城市/企业性质过滤、离线关键词兜底。"""

from __future__ import annotations

import json
from pathlib import Path

import resume
from conftest import recruit_item, recruit_path, write_raw

RESUME = "机械专业硕士，熟悉SolidWorks与有限元仿真，目标城市武汉，希望进国企或央企从事结构设计"


def _companies():
    return [
        {"name": "武汉某央企设计院", "type": "央企", "so": "是", "locations": ["武汉"],
         "evidence": "央企子公司", "text": "招聘机械设计工程师，结构设计岗位", "title": "校招",
         "count": 3},
        {"name": "武汉某民营科技", "type": "民企", "so": "否", "locations": ["武汉"],
         "evidence": "民营企业", "text": "招聘机械工程师，结构仿真", "title": "校招", "count": 2},
        {"name": "北京某外企", "type": "外企", "so": "否", "locations": ["北京"],
         "evidence": "外企", "text": "招聘算法工程师", "title": "校招", "count": 1},
        {"name": "无地点企业", "type": "", "so": "", "locations": [], "evidence": "",
         "text": "机械结构设计", "title": "", "count": 0},
    ]


def test_parse_target_cities_variants():
    assert resume.parse_target_cities("武汉、深圳") == ["武汉", "深圳"]
    assert resume.parse_target_cities("武汉, 深圳 / 北京") == ["武汉", "深圳", "北京"]
    assert resume.parse_target_cities("") == []


EXPLAIN_FIELDS = {"company", "match", "location", "position", "reason", "score", "breakdown", "matched"}


def test_keyword_recommend_prefers_city_and_state_owned():
    """指定目标工作地+国企时：命中城市与性质的国企排在最前，异地企业被排除。"""
    result = resume.keyword_recommend(RESUME, _companies(), work_place="武汉", company_type="国企")
    names = [r["company"] for r in result["recommendations"]]
    assert names[0] == "武汉某央企设计院"
    assert "北京某外企" not in names                     # 无任何命中（分数 0）→ 过滤
    rec = result["recommendations"][0]
    assert rec["match"] in ("高", "中")
    assert rec["location"] == "武汉"
    assert "机械" in rec["reason"] or "武汉" in rec["reason"]
    assert result["target_positions"]                            # 岗位方向由简历关键词得出
    # 可解释性：分数、三维拆解、命中项都要带出来，前端才能画对比图
    assert set(rec) == EXPLAIN_FIELDS
    assert rec["score"] == 60                                    # 关键词 10 + 地点 30 + 国企 20
    assert rec["breakdown"] == {"keyword": 10, "location": 30, "nature": 20}
    assert rec["matched"] == {"keywords": ["机械"], "cities": ["武汉"]}


def test_keyword_recommend_private_preference():
    """指定民企倾向时，民营企业获得性质加分。"""
    result = resume.keyword_recommend(RESUME, _companies(), work_place="武汉", company_type="民营")
    scores = {r["company"]: r["match"] for r in result["recommendations"]}
    assert "武汉某民营科技" in scores
    assert scores["武汉某民营科技"] in ("高", "中")


def test_keyword_recommend_score_equals_breakdown_sum_and_dims_are_terms():
    """分数恒等于三维之和；matched.keywords 是关键词原文（term），不是岗位方向。"""
    result = resume.keyword_recommend(RESUME, _companies(), work_place="武汉", company_type="国企")
    terms = {term for term, _ in resume.JOB_KEYWORDS}
    positions = {pos for _, pos in resume.JOB_KEYWORDS}
    for rec in result["recommendations"]:
        assert rec["score"] == sum(rec["breakdown"].values())
        assert set(rec["breakdown"]) == {"keyword", "location", "nature"}
        assert set(rec["matched"]["keywords"]) <= terms              # 关键词原文，如「机械」
        assert set(rec["matched"]["keywords"]).isdisjoint(positions - terms)
        assert set(rec["matched"]["cities"]) <= {"武汉", "北京"}


def test_keyword_recommend_sorted_by_score_desc():
    """分数高的排前面（界面按这个顺序展示，排序口径必须就是展示的分数）。"""
    result = resume.keyword_recommend(RESUME, _companies(), work_place="武汉", company_type="国企")
    scores = [r["score"] for r in result["recommendations"]]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_keyword_recommend_match_level_at_thresholds():
    """分数恰好落在门槛上：70 判「高」、40 判「中」、30 判「低」。"""
    text = "熟悉机械、电气、自动化、控制与电力系统"
    comps = [
        {"name": "高分企业", "type": "", "so": "是", "locations": ["北京"], "title": "",
         "text": "机械 电气 自动化 控制 电力 岗位"},    # 5 关键词 50 + 国企 20 = 70
        {"name": "中分企业", "type": "", "so": "", "locations": ["武汉"], "title": "",
         "text": "机械 岗位"},                          # 关键词 10 + 地点 30 = 40
        {"name": "低分企业", "type": "", "so": "是", "locations": ["上海"], "title": "",
         "text": "机械 岗位"},                          # 关键词 10 + 国企 20 = 30（异地无地点分）
    ]
    result = resume.keyword_recommend(text, comps, work_place="武汉", company_type="国企")
    levels = {r["company"]: (r["score"], r["match"]) for r in result["recommendations"]}
    assert levels["高分企业"] == (70, "高")
    assert levels["中分企业"] == (40, "中")
    assert levels["低分企业"] == (30, "低")


def test_keyword_recommend_without_location_still_filters_zero_score():
    """未指定目标地时不按地点过滤，但完全无重合的企业仍不推荐。"""
    result = resume.keyword_recommend("结构设计 机械", _companies())
    names = [r["company"] for r in result["recommendations"]]
    assert "武汉某央企设计院" in names
    assert "北京某外企" not in names


def test_build_companies_merges_all_files_and_attaches_cache(data_dir):
    """候选企业来自主库全部公告（跨文件合并），并附着企业分析缓存画像。"""
    write_raw(recruit_path(data_dir, "2026-08-08_至_2026-09-07"), 招聘信息=[
        recruit_item("1", name="东方电气", content="招聘机械设计工程师，工作地点武汉"),
        recruit_item("2", name="东方电气", content="招聘工艺工程师，工作地点德阳"),
    ])
    write_raw(recruit_path(data_dir, "2026-09-09_至_2026-09-09"), 招聘信息=[
        recruit_item("3", name="中国船舶七一九所", content="招聘结构设计岗，工作地点武汉"),
    ])
    (Path(data_dir) / resume.CACHE_NAME).write_text(json.dumps({
        "东方电气": {"company_type": "央企", "is_state_owned": True, "confidence": "高",
                     "locations": ["武汉", "德阳"], "evidence": "央企集团"},
    }, ensure_ascii=False), encoding="utf-8")

    rows = resume.build_companies(data_dir, max_items=0)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"东方电气", "中国船舶七一九所"}      # 两个文件都合并进来了
    assert by_name["东方电气"]["count"] == 2
    assert by_name["东方电气"]["type"] == "央企"
    assert by_name["东方电气"]["so"] == "是"
    assert by_name["东方电气"]["locations"] == ["武汉", "德阳"]
    assert by_name["中国船舶七一九所"]["type"] == ""             # 未分析 → 空画像
    # 有画像的排前面；max_items 生效
    assert rows[0]["name"] == "东方电气"
    assert len(resume.build_companies(data_dir, max_items=1)) == 1
    assert len(resume.build_companies(data_dir)) == 2            # 默认 max_items=60 不截断本例


def test_build_companies_uses_repository_cache_after_data_change(data_dir):
    """数据文件变化后候选企业同步变化（repository 缓存按 mtime 失效）。"""
    write_raw(recruit_path(data_dir, "2026-09-01_至_2026-09-01"), 招聘信息=[
        recruit_item("1", name="企业甲"),
    ])
    assert [c["name"] for c in resume.build_companies(data_dir, max_items=0)] == ["企业甲"]

    write_raw(recruit_path(data_dir, "2026-09-02_至_2026-09-02"), 招聘信息=[
        recruit_item("2", name="企业乙"),
    ])
    assert set(c["name"] for c in resume.build_companies(data_dir, max_items=0)) == {"企业甲", "企业乙"}
