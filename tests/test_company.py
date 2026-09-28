"""企业详情页：聚合服务 + HTTP 接口测试。

覆盖四类风险：
  1. 聚合口径 —— 一家企业的公告 / 宣讲会 / 分析 / 看板条数必须与造的数据一致；
  2. 缺数据不崩 —— 分析缓存缺失字段、看板没有记录时给空值而不是 KeyError / 抛错；
  3. 企业名归一化 —— 带空格与全半角括号的同名企业要能命中同一条；
  4. 路由接线 —— 新增的 GET 路由被全路由冒烟遍历到。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import quote

import board_store
import dataloaders
import pytest
from conftest import preach_item, recruit_item
from services import ServiceError
from services import board as board_svc
from services import company as company_svc

COMPANY_ROUTE = "/api/company/<path:name>"


@pytest.fixture(autouse=True)
def isolated_cache_and_board(tmp_path, monkeypatch):
    """分析缓存与投递看板都落到临时目录，不在测试里动用户真实数据。"""
    monkeypatch.setattr(dataloaders, "CACHE_PATH", tmp_path / "企业分析_缓存.json")
    monkeypatch.setattr(board_store, "BOARD_PATH", tmp_path / "投递看板.json")


def write_cache(entries: dict) -> None:
    dataloaders.CACHE_PATH.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def add_to_board(source_id: str, unit: str) -> dict:
    return board_svc.create_item({"source_type": "招聘", "source_id": source_id,
                                  "snapshot": {"单位": unit, "标题": "校园招聘"}})


def test_profile_merges_all_four_sources(data_dir, make_recruit, make_preach):
    make_recruit("a", [recruit_item("r1", name="甲公司", title="2027届校园招聘"),
                       recruit_item("r2", name="甲公司", title="补录公告")])
    make_preach("a", [preach_item("p1", name="甲公司", hold_date="2026-09-20"),
                      preach_item("p2", name="甲公司", hold_date="2026-10-08")])
    write_cache({"甲公司": {"company_type": "央企", "is_state_owned": True, "confidence": "高",
                            "locations": ["武汉", "宜昌"], "evidence": "国务院国资委出资"}})
    add_to_board("r1", "甲公司")

    profile = company_svc.build_profile("甲公司")

    assert profile["name"] == "甲公司"
    assert profile["stats"] == {"recruitments": 2, "preaches": 2, "board": 1, "analyzed": True}
    assert profile["analysis"]["company_type"] == "央企"
    assert profile["analysis"]["state_owned"] == "是"
    assert profile["analysis"]["confidence"] == "高"
    assert profile["analysis"]["locations"] == ["武汉", "宜昌"]
    assert profile["analysis"]["evidence"] == "国务院国资委出资"
    assert {r["标题"] for r in profile["recruitments"]} == {"2027届校园招聘", "补录公告"}
    assert profile["recruitments"][0]["原网页"] == "https://example.com/r1"
    # 宣讲会按举办日期倒序：最新的一场在最上面，与招聘公告的排序方向一致
    assert [p["举办日期"] for p in profile["preaches"]] == ["2026-10-08", "2026-09-20"]
    assert profile["board"]["source_id"] == "r1"


def test_other_companies_are_not_mixed_in(data_dir, make_recruit):
    make_recruit("a", [recruit_item("r1", name="甲公司"), recruit_item("r2", name="乙公司")])
    write_cache({"甲公司": {"company_type": "央企"}, "乙公司": {"company_type": "民企"}})
    add_to_board("r2", "乙公司")

    profile = company_svc.build_profile("甲公司")
    assert profile["stats"]["recruitments"] == 1
    assert profile["analysis"]["company_type"] == "央企"
    assert profile["board"] is None          # 看板记录属于乙公司，不该漏过来


def test_unknown_company_raises_404(data_dir, make_recruit):
    make_recruit("a", [recruit_item("r1", name="甲公司")])
    with pytest.raises(ServiceError) as caught:
        company_svc.build_profile("乙公司")
    assert caught.value.status == 404


def test_empty_name_raises_service_error(data_dir, make_recruit):
    make_recruit("a", [recruit_item("r1", name="甲公司")])
    with pytest.raises(ServiceError):
        company_svc.build_profile("   ")


def test_missing_analysis_and_board_do_not_break(data_dir, make_recruit):
    """缓存文件不存在、看板无记录：给空值 / null，而不是报错。"""
    make_recruit("a", [recruit_item("r1", name="乙公司")])

    profile = company_svc.build_profile("乙公司")

    assert profile["analysis"] == {"has": False, "company_type": "", "state_owned": "未知",
                                   "confidence": "", "locations": [], "evidence": ""}
    assert profile["board"] is None
    assert profile["stats"] == {"recruitments": 1, "preaches": 0, "board": 0, "analyzed": False}


def test_partial_analysis_fields_do_not_raise(data_dir, make_recruit):
    """早期版本的缓存只有 company_type，缺 confidence / locations —— 不能 KeyError。"""
    make_recruit("a", [recruit_item("r1", name="丙公司")])
    write_cache({"丙公司": {"company_type": "民企"}})

    profile = company_svc.build_profile("丙公司")

    assert profile["analysis"]["has"] is True
    assert profile["analysis"]["company_type"] == "民企"
    assert profile["analysis"]["confidence"] == ""
    assert profile["analysis"]["locations"] == []
    assert profile["analysis"]["state_owned"] == "未知"


def test_company_known_only_by_analysis_cache_is_resolvable(data_dir, make_recruit):
    """企业分析页里列出的企业（只有缓存没有公告）也要能点开，而不是 404。"""
    make_recruit("a", [recruit_item("r1", name="丁公司")])
    write_cache({"戊公司": {"company_type": "事业单位", "is_state_owned": False}})

    profile = company_svc.build_profile("戊公司")

    assert profile["name"] == "戊公司"
    assert profile["stats"]["recruitments"] == 0
    assert profile["analysis"]["state_owned"] == "否"


@pytest.mark.parametrize("query", [
    "东风汽车（武汉）有限公司",      # 原样
    "东风汽车 (武汉) 有限公司",      # 半角括号 + 空格
    " 东风汽车（武汉）有限公司 ",     # 首尾空格
    "东风汽车（武汉）有限公司",  # NFKC 前的全角括号
])
def test_name_normalization_merges_spellings(data_dir, make_recruit, make_preach, query):
    full_width = "东风汽车（武汉）有限公司"
    with_space = "东风汽车 (武汉) 有限公司"
    make_recruit("a", [recruit_item("r1", name=full_width)])
    make_preach("a", [preach_item("p1", name=with_space)])
    write_cache({full_width: {"company_type": "央企", "is_state_owned": True}})

    profile = company_svc.build_profile(query)

    assert profile["stats"] == {"recruitments": 1, "preaches": 1, "board": 0, "analyzed": True}
    # 两种写法互为别名，说明它们被认成同一家企业
    assert profile["name"] == query.strip()
    assert full_width in profile["alias"] or with_space in profile["alias"]


def test_api_returns_profile_and_404(data_dir, make_recruit):
    make_recruit("a", [recruit_item("r1", name="甲公司")])
    import server

    client = server.app.test_client()
    ok = client.get("/api/company/" + quote("甲公司"))
    assert ok.status_code == 200
    payload = ok.get_json()
    assert payload["ok"] is True
    assert payload["name"] == "甲公司"
    assert payload["stats"]["recruitments"] == 1

    missing = client.get("/api/company/" + quote("查无此公司"))
    assert missing.status_code == 404
    assert missing.get_json()["ok"] is False


def test_route_is_registered_and_smoke_covered():
    """新路由必须真的挂上了，并且被 test_api_smoke 的全路由遍历覆盖（GET 自动纳入）。"""
    import server

    assert COMPANY_ROUTE in {r.rule for r in server.app.url_map.iter_rules()}

    path = Path(__file__).with_name("test_api_smoke.py")
    spec = importlib.util.spec_from_file_location("test_api_smoke_loaded", path)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    assert COMPANY_ROUTE not in smoke._SKIP_RULES
    assert COMPANY_ROUTE in smoke._smoke_rules()
