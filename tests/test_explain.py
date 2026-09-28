"""推荐可解释性：AI 结果归一化兜底、两条路径对外形状一致、编排层 explain 口径。

离线打分口径本身的用例在 test_recommendation.py，这里只管「补分 / 纠正 / 幂等 / 一致性」。
"""

from __future__ import annotations

import json

import pytest
import resume

RESUME = "机械专业硕士，熟悉SolidWorks与有限元仿真，目标城市武汉，希望进国企或央企从事结构设计"

EXPLAIN_FIELDS = {"company", "match", "location", "position", "reason", "score", "breakdown", "matched"}


def _company(name="武汉某央企设计院", text="招聘机械设计工程师，结构设计岗位", city="武汉",
             so="是", ctype="央企"):
    return {"name": name, "type": ctype, "so": so, "locations": [city], "text": text, "title": "校招"}


def _companies():
    return [
        _company(),
        _company("武汉某民营机械公司", "招聘机械工程师，结构仿真", so="否", ctype="民企"),
        {"name": "北京某外企", "type": "外企", "so": "否", "locations": ["北京"],
         "text": "招聘算法工程师", "title": ""},
    ]


def _clone(payload: dict) -> dict:
    """深拷贝一份 AI 返回，避免归一化就地修改后影响下一个断言。"""
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _ai_payload(recs: list[dict]) -> dict:
    return {"resume_summary": "机械硕士", "target_positions": ["机械工程师"], "recommendations": recs}


def _norm(recs: list[dict], **kw) -> dict:
    kw.setdefault("resume_text", RESUME)
    return resume.normalize_explanations(_ai_payload(recs), _companies(), **kw)


def _offline(**kw):
    return resume.keyword_recommend(RESUME, _companies(), **kw)


@pytest.fixture()
def stub_llm(monkeypatch):
    """把 llm_client.chat 换成直接吐给定 JSON 的假实现（不发起真实网络请求）。"""
    def _stub(payload: dict):
        monkeypatch.setattr(resume.llm_client, "chat",
                            lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    return _stub


# ---------------------------------------------------------------- 归一化：补齐

def test_normalize_fills_missing_score_with_offline_rule():
    """AI 完全没给 score/breakdown → 用离线规则补上，且与离线路径算出来的一致。"""
    out = _norm([{"company": "武汉某央企设计院", "match": "高", "location": "武汉",
                  "position": "机械工程师", "reason": "很合适"}],
                work_place="武汉", company_type="国企")
    rec = out["recommendations"][0]
    offline = _offline(work_place="武汉", company_type="国企")["recommendations"][0]
    assert rec["score"] == offline["score"] == 60
    assert rec["breakdown"] == offline["breakdown"] == {"keyword": 10, "location": 30, "nature": 20}
    assert rec["matched"] == {"keywords": ["机械"], "cities": ["武汉"]}


def test_normalize_recovers_partial_breakdown():
    """breakdown 缺一半字段（只有 location）→ 视为没给，整体走本地规则。"""
    out = _norm([{"company": "武汉某民营机械公司", "match": "中", "score": 55,
                  "breakdown": {"location": 30}}], work_place="武汉", company_type="国企")
    rec = out["recommendations"][0]
    assert rec["breakdown"] == {"keyword": 10, "location": 30, "nature": 0}
    assert rec["score"] == 40


def test_normalize_folds_ai_breakdown_into_three_dims():
    """模型给的 major/skill 合并成 keyword 维度，并按各维度上限夹紧。"""
    out = _norm([{"company": "武汉某央企设计院", "match": "高", "score": 75,
                  "breakdown": {"major": 30, "skill": 25, "location": 20, "nature": 5},
                  "matched": {"keywords": ["机械"], "cities": ["武汉"]}}],
                work_place="武汉", company_type="国企")
    rec = out["recommendations"][0]
    assert rec["breakdown"] == {"keyword": 50, "location": 20, "nature": 5}   # 30+25 → 上限 50
    assert rec["score"] == 75 == sum(rec["breakdown"].values())


# ---------------------------------------------------------------- 归一化：纠正

@pytest.mark.parametrize("bad_score", [999, -5, "80", None, 12.9])
def test_normalize_replaces_invalid_score(bad_score):
    """越界 / 非数字 / 小数 → 一律换成本地规则算出的合法分数（0-100 整数）。"""
    out = _norm([{"company": "武汉某央企设计院", "match": "高", "score": bad_score,
                  "breakdown": {"major": 30, "skill": 20, "location": 20, "nature": 0}}],
                work_place="武汉", company_type="国企")
    rec = out["recommendations"][0]
    assert isinstance(rec["score"], int) and 0 <= rec["score"] <= resume.MAX_SCORE
    assert rec["score"] == 60                      # 与离线一致，而不是沿用 AI 的非法值


def test_normalize_clamps_ai_score_over_100():
    """AI 给 999 分不会污染排序：分数必须落在 0-100。"""
    out = _norm([{"company": "武汉某央企设计院", "match": "高", "score": 999,
                  "breakdown": {"major": 999, "skill": 999, "location": 999, "nature": 999}}],
                work_place="武汉", company_type="国企")
    assert 0 <= out["recommendations"][0]["score"] <= 100


def test_normalize_fixes_match_contradicting_score():
    """AI 写「高」却只给 10 分 → 档位按分数重算成「低」，理由文案保留。"""
    out = _norm([{"company": "武汉某央企设计院", "match": "高", "location": "武汉",
                  "position": "机械工程师", "reason": "非常匹配", "score": 10,
                  "breakdown": {"keyword": 10, "location": 0, "nature": 0}}],
                work_place="武汉", company_type="国企")
    rec = out["recommendations"][0]
    assert rec["match"] == "低"
    assert rec["reason"] == "非常匹配"                 # 模型写的理由不丢


def test_normalize_reorders_by_score():
    """模型顺序写反了也要按分数降序重排，保证「排前面的分更高」。"""
    out = _norm([
        {"company": "武汉某民营机械公司", "match": "中", "score": 90,
         "breakdown": {"keyword": 40, "location": 30, "nature": 20}},
        {"company": "武汉某央企设计院", "match": "高", "score": 30,
         "breakdown": {"keyword": 10, "location": 20, "nature": 0}},
    ], work_place="武汉", company_type="国企")
    assert [r["company"] for r in out["recommendations"]] == ["武汉某民营机械公司", "武汉某央企设计院"]
    assert [r["score"] for r in out["recommendations"]] == [90, 30]


# ---------------------------------------------------------------- 归一化：幂等

def test_normalize_is_idempotent():
    """跑两次结果完全一致：界面可能对同一份结果重复渲染/缓存后再规范化。"""
    recs = [
        {"company": "武汉某央企设计院", "match": "高", "score": 78,
         "breakdown": {"major": 30, "skill": 25, "location": 20, "nature": 5},
         "matched": {"keywords": ["机械"], "cities": ["武汉"]}},
        {"company": "武汉某民营机械公司", "match": "中", "score": 999},     # 越界 → 本地重算
        {"company": "查无此企业", "match": "高", "location": "武汉", "reason": "缺 score"},
    ]
    first = _norm(recs, work_place="武汉", company_type="国企")
    second = resume.normalize_explanations(_clone(first), _companies(), work_place="武汉",
                                           company_type="国企", resume_text=RESUME)
    assert first == second


# ---------------------------------------------------------------- 两条路径形状一致

def test_ai_and_offline_paths_share_identical_fields(stub_llm):
    """AI 成功与离线降级两条路径的条目字段集合完全一致——前端靠它统一渲染。"""
    stub_llm(_ai_payload([
        {"company": "武汉某央企设计院", "match": "高", "location": "武汉",
         "position": "机械工程师", "reason": "机械对口", "score": 82,
         "breakdown": {"major": 30, "skill": 25, "location": 27, "nature": 5},
         "matched": {"keywords": ["机械"], "cities": ["武汉"]}},
    ]))
    ai = resume.recommend("sk", "m", RESUME, _companies(), work_place="武汉", company_type="国企")
    off = _offline(work_place="武汉", company_type="国企")

    assert set(ai) == set(off)                                   # 顶层字段集合
    assert set(ai["recommendations"][0]) == EXPLAIN_FIELDS
    assert set(off["recommendations"][0]) == EXPLAIN_FIELDS
    assert (set(ai["recommendations"][0]["breakdown"])
            == set(off["recommendations"][0]["breakdown"]) == {"keyword", "location", "nature"})
    assert (set(ai["recommendations"][0]["matched"])
            == set(off["recommendations"][0]["matched"]) == {"keywords", "cities"})
    assert ai["recommendations"][0]["score"] == 82


def test_recommend_keeps_error_shape_untouched(stub_llm):
    """解析失败的 {"error": ...} 不能被归一化动过——上层靠 error 判断要不要降级。"""
    out = resume.parse_recommend_json("不是 JSON")
    assert resume.normalize_explanations({"error": "boom"}, _companies()) == {"error": "boom"}
    assert "error" in out


# ---------------------------------------------------------------- 编排层

@pytest.fixture()
def svc(monkeypatch, data_dir):
    """services.recommend：数据目录/LLM 配置/外部依赖全部替换成测试替身。"""
    from services import recommend as recommend_svc
    monkeypatch.setattr(recommend_svc, "DATA", data_dir)
    monkeypatch.setattr(recommend_svc, "get_llm",
                        lambda: {"api_key": "sk", "model": "m", "base_url": "", "label": "测试"})
    monkeypatch.setattr(recommend_svc.repo, "master_summary",
                        lambda: {"recruit_count": 0, "coverage_start": "", "coverage_end": "",
                                 "last_update": ""})
    monkeypatch.setattr(recommend_svc, "load_preachs", lambda *a, **k: [])
    monkeypatch.setattr(recommend_svc, "load_cache", lambda: {})
    return recommend_svc


def _seed(svc, make_recruit):
    make_recruit("2026-09-01_至_2026-09-01", [
        {"id": "1", "com_id_name": "东方电气", "title": "2027届校园招聘", "addtime": 1789000000,
         "content": "招聘机械工程师，工作地点武汉", "httpurl": "https://example.com/1"},
    ])


def test_build_recommendation_ai_path_carries_explain(svc, monkeypatch, make_recruit):
    """AI 路径：payload 带 explain 口径，推荐条目带 score/breakdown/matched。"""
    _seed(svc, make_recruit)
    monkeypatch.setattr(resume, "recommend", lambda *a, **k: _ai_payload([
        {"company": "东方电气", "match": "高", "location": "武汉", "position": "机械工程师",
         "reason": "机械对口", "score": 82,
         "breakdown": {"major": 30, "skill": 25, "location": 25, "nature": 2},
         "matched": {"keywords": ["机械"], "cities": ["武汉"]}},
    ]))
    payload = svc.build_recommendation(resume_text=RESUME, work_place="武汉")
    assert payload["source"] == "ai"
    assert payload["explain"]["dimensions"] == ["keyword", "location", "nature"]
    assert payload["explain"]["max_score"] == 100
    assert payload["explain"]["note"]
    rec = payload["result"]["recommendations"][0]
    assert set(rec) == EXPLAIN_FIELDS and rec["score"] == 82


def test_build_recommendation_offline_path_carries_explain(svc, monkeypatch, make_recruit):
    """降级路径：explain 仍在（口径换成本地规则的说法），条目字段与 AI 路径一致。"""
    _seed(svc, make_recruit)
    monkeypatch.setattr(resume, "recommend", lambda *a, **k: {"error": "余额不足"})
    payload = svc.build_recommendation(resume_text=RESUME, work_place="武汉", company_type="国企")
    assert payload["source"] == "offline"
    assert payload["explain"]["max_score"] == 100
    assert "本地" in payload["explain"]["note"]
    rec = payload["result"]["recommendations"][0]
    assert set(rec) == EXPLAIN_FIELDS
    assert rec["score"] == sum(rec["breakdown"].values()) > 0


def test_recommend_preachs_scores_with_same_rubric(svc, monkeypatch):
    """宣讲会推荐用同一套口径打分，带 score 与 matched_cities，可与企业推荐横向比较。"""
    monkeypatch.setattr(svc, "load_preachs", lambda *a, **k: [{
        "单位名称": "东风汽车", "举办日期": "2026-09-20", "宣讲时间": "2026-09-20 19:00~21:00",
        "宣讲会地点": "武汉市，马房山校区", "城市": "武汉市", "场馆": "东风厅", "线下/线上": "线下",
        "公司地点": "武汉", "work_cities": ["武汉"], "原网页": "", "标题": "东风汽车宣讲",
        "正文": "招聘机械工程师",
    }])
    monkeypatch.setattr(svc, "build_so_map", lambda: {"东风汽车": "是"})
    out = svc.recommend_preachs(RESUME, ["武汉"], "国企")
    assert out[0]["score"] == 60                       # 关键词 10 + 地点 30 + 国企 20
    assert out[0]["matched_cities"] == ["武汉"]
    assert resume.match_level(out[0]["score"]) == "中"
