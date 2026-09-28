"""个人求职偏好 + keyring 密钥管理。

偏好部分回答两个问题：
1. **存得住**：文件缺失 / 损坏 / 结构异常都不能抛异常，否则一个坏文件就能让推荐页打不开；
2. **真的复用**：目标城市 / 企业性质没填时要用偏好兜底，黑名单里的企业必须**不占名额**
   地被剔除（剔除晚一步，用户看到的就是「推荐变少了」而不是「黑名单生效了」）。

keyring 部分只守一条底线：**钥匙串不可用时必须静默回落到 config.json**。
没装、抛异常、开关关闭，都不能让配置读写失败——「跑不起来」比「Key 明文落盘」严重得多。
"""

from __future__ import annotations

import json

import pytest
import resume
import server
import settings
from conftest import recruit_item
from services import prefs as prefs_svc

RESUME = "机械 电气 自动化 控制 电力 专业硕士，目标城市武汉，希望进国企"

PREF_KEYS = {"version", "target_cities", "target_positions", "salary_min",
             "blacklist", "company_type", "updated_at"}


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    """偏好测试只写临时文件，不触碰用户真实的 data/求职偏好.json。"""
    monkeypatch.setattr(prefs_svc, "PREFS_PATH", tmp_path / "求职偏好.json")


@pytest.fixture()
def svc(monkeypatch, data_dir):
    """services.recommend：数据目录 / LLM 配置 / 外部依赖全部换成测试替身。"""
    from services import recommend as recommend_svc
    monkeypatch.setattr(recommend_svc, "DATA", data_dir)
    monkeypatch.setattr(recommend_svc, "get_llm",
                        lambda: {"api_key": "sk", "model": "m", "base_url": "", "label": "测试"})
    monkeypatch.setattr(recommend_svc.repo, "master_summary",
                        lambda: {"recruit_count": 0, "coverage_start": "", "coverage_end": "",
                                 "last_update": ""})
    monkeypatch.setattr(recommend_svc, "load_preachs", lambda *a, **k: [])
    monkeypatch.setattr(recommend_svc, "load_cache", lambda: {})
    # 离线兜底路径是确定性的（不依赖外网、不烧 token），推荐相关用例一律走它
    monkeypatch.setattr(resume, "recommend", lambda *a, **k: {"error": "测试环境不走 AI"})
    return recommend_svc


def _preach_row(name: str, city: str) -> dict:
    return {"单位名称": name, "举办日期": "2026-09-20", "宣讲时间": "2026-09-20 19:00~21:00",
            "宣讲会地点": f"{city}，某校区", "城市": city, "场馆": "某厅", "线下/线上": "线下",
            "公司地点": city, "work_cities": [city], "原网页": "", "标题": f"{name}宣讲",
            "正文": "招聘机械工程师"}


# ---------------------------------------------------------------- 存储：绝不抛异常

def test_load_missing_file_returns_empty_prefs():
    assert not prefs_svc.PREFS_PATH.exists()
    assert prefs_svc.load_prefs() == prefs_svc.empty_prefs()


def test_load_corrupt_or_non_dict_file_returns_empty_prefs():
    """损坏的 JSON 与「顶层不是 dict」都要退化成空偏好——推荐链路不能因坏文件崩掉。"""
    prefs_svc.PREFS_PATH.write_text("{坏掉的 json", encoding="utf-8")
    assert prefs_svc.load_prefs() == prefs_svc.empty_prefs()
    prefs_svc.PREFS_PATH.write_text(json.dumps(["不是 dict"], ensure_ascii=False), encoding="utf-8")
    assert prefs_svc.load_prefs() == prefs_svc.empty_prefs()


def test_save_then_load_roundtrip():
    saved = prefs_svc.save_prefs({"target_cities": ["武汉", "深圳"], "salary_min": 8000})
    assert saved["target_cities"] == ["武汉", "深圳"]
    assert saved["salary_min"] == 8000
    assert saved["updated_at"]
    assert prefs_svc.load_prefs() == saved


def test_normalize_dedupes_and_fixes_bad_salary():
    """重复城市去重、薪资传 "abc" 按 0 处理（脏值不能把整个偏好带崩）。"""
    out = prefs_svc.normalize({"target_cities": ["武汉", "武汉", " 深圳 ", ""],
                               "salary_min": "abc", "blacklist": "某外包"})
    assert out["target_cities"] == ["武汉", "深圳"]
    assert out["salary_min"] == 0
    assert out["blacklist"] == ["某外包"]          # 单个字符串也接受
    assert prefs_svc.normalize("根本不是 dict") == prefs_svc.empty_prefs()


def test_partial_update_keeps_other_fields_and_empty_array_clears():
    prefs_svc.save_prefs({"target_cities": ["武汉"], "blacklist": ["某外包"], "company_type": "国企央企"})
    after = prefs_svc.save_prefs({"blacklist": []})
    assert after["blacklist"] == []                 # 空数组 = 清空该项
    assert after["target_cities"] == ["武汉"]        # 其余字段原样保留
    assert after["company_type"] == "国企央企"


# ---------------------------------------------------------------- 黑名单匹配

def test_blacklist_matches_substring_ignoring_case():
    """「外包」要能挡住「某某外包科技（武汉）」，且大小写不敏感。"""
    assert prefs_svc.blacklist_hit("某外包科技（武汉）", ["外包"])
    assert prefs_svc.blacklist_hit("ABC Outsourcing Ltd", ["outsourcing"])
    assert not prefs_svc.blacklist_hit("中船重工", ["外包"])
    assert not prefs_svc.blacklist_hit("", ["外包"])      # 空名字不算命中


def test_split_blacklisted_keeps_order_and_reports_blocked():
    rows = [{"单位名称": "甲公司"}, {"单位名称": "某外包"}, {"单位名称": "乙公司"}]
    kept, blocked = prefs_svc.split_blacklisted(rows, ["外包"], "单位名称")
    assert [r["单位名称"] for r in kept] == ["甲公司", "乙公司"]
    assert blocked == ["某外包"]


# ---------------------------------------------------------------- 兜底：显式入参优先于偏好

def test_build_recommendation_falls_back_to_prefs(svc, make_recruit):
    """没填目标工作地 / 企业性质时用偏好兜底，并在 prefs_applied 里说明。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="东方电气")])
    prefs_svc.save_prefs({"target_cities": ["深圳"], "company_type": "国企央企"})

    payload = svc.build_recommendation(resume_text=RESUME)
    applied = payload["prefs_applied"]
    assert payload["target_work_place"] == "深圳"
    assert payload["target_company_type"] == "国企央企"
    assert applied["work_place_from_prefs"] is True
    assert applied["company_type_from_prefs"] is True


def test_build_recommendation_explicit_args_win_over_prefs(svc, make_recruit):
    """用户填了就以填的为准——偏好只是缺省值，不该反过来锁死单次查询。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="东方电气")])
    prefs_svc.save_prefs({"target_cities": ["深圳"], "company_type": "国企央企"})

    payload = svc.build_recommendation(resume_text=RESUME, work_place="武汉", company_type="民营")
    assert payload["target_work_place"] == "武汉"
    assert payload["target_company_type"] == "民营"
    assert payload["prefs_applied"]["work_place_from_prefs"] is False


# ---------------------------------------------------------------- 黑名单：真的生效且不占名额

def _seed_many(make_recruit, first: str, total: int = 12) -> None:
    """造一批候选企业：第一家分数最高（关键词最多），其余分数相同。"""
    items = [recruit_item("0", name=first,
                          content="招聘 机械 电气 自动化 控制 电力 工程师")]
    items += [recruit_item(str(i), name=f"企业{i:02d}", content="招聘机械工程师")
              for i in range(1, total)]
    make_recruit("2026-09-01_至_2026-09-01", items)


def test_blacklist_removes_company_without_consuming_a_slot(svc, make_recruit):
    """黑名单企业被剔除，且被剔掉的那一格由后面的企业补上（结果条数不变）。

    条数不变才是关键：若剔除发生在 Top-N 截断之后，用户看到的是「本次只推荐了 9 家」，
    会以为数据少了，而不是想起自己拉黑过谁。
    """
    _seed_many(make_recruit, "外包炮灰")
    base = svc.build_recommendation(resume_text=RESUME)
    names = [r["company"] for r in base["result"]["recommendations"]]
    assert len(names) == 10                       # Top-10 截断生效
    assert names[0] == "外包炮灰"                  # 分数最高，本该占掉第一格
    assert base["prefs_applied"]["blocked_companies"] == []

    prefs_svc.save_prefs({"blacklist": ["外包"]})
    filtered = svc.build_recommendation(resume_text=RESUME)
    names2 = [r["company"] for r in filtered["result"]["recommendations"]]
    assert "外包炮灰" not in names2
    assert len(names2) == 10                      # 名额没被吃掉：仍然是 10 条
    assert filtered["prefs_applied"]["blocked_companies"] == ["外包炮灰"]


def test_blacklist_is_case_insensitive_in_recommendation(svc, make_recruit):
    """黑名单大小写 / 子串匹配在推荐结果里同样生效。"""
    _seed_many(make_recruit, "ABC Outsourcing 科技")
    prefs_svc.save_prefs({"blacklist": ["outsourcing"]})
    payload = svc.build_recommendation(resume_text=RESUME)
    names = [r["company"] for r in payload["result"]["recommendations"]]
    assert "ABC Outsourcing 科技" not in names
    assert payload["prefs_applied"]["blocked_companies"] == ["ABC Outsourcing 科技"]


def test_recommend_preachs_uses_prefs_cities_and_blacklist(svc, monkeypatch):
    """宣讲会：目标城市用偏好兜底；黑名单同样在截断之前剔除。"""
    prefs_svc.save_prefs({"target_cities": ["北京", "武汉"], "blacklist": ["外包"]})
    monkeypatch.setattr(svc, "load_preachs", lambda *a, **k: [
        _preach_row("外包炮灰", "北京"),      # 命中偏好的城市，但被拉黑
        _preach_row("北京某企业", "北京"),
        _preach_row("武汉某企业", "武汉"),
        _preach_row("武汉另一家", "武汉"),
    ])
    monkeypatch.setattr(svc, "build_so_map", lambda: {})

    # 不传城市 → 用偏好的「北京、武汉」
    out = svc.recommend_preachs(RESUME, [])
    assert [r["单位名称"] for r in out] == ["北京某企业", "武汉某企业", "武汉另一家"]

    # limit=2 时被拉黑的那条不能占名额：两条名额都该给没被拉黑的，而不是被它吃掉一条
    out2 = svc.recommend_preachs(RESUME, [], limit=2)
    assert [r["单位名称"] for r in out2] == ["北京某企业", "武汉某企业"]

    # 显式传参仍然优先于偏好
    out3 = svc.recommend_preachs(RESUME, ["武汉"])
    assert [r["单位名称"] for r in out3] == ["武汉某企业", "武汉另一家"]


def test_preachs_blacklist_is_reported_in_payload(svc, monkeypatch, make_recruit):
    """宣讲会被黑名单剔除的单位名也要回报出去，否则用户会以为那场宣讲会不存在。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="东方电气")])
    monkeypatch.setattr(svc, "load_preachs",
                        lambda *a, **k: [_preach_row("某外包", "北京"), _preach_row("甲公司", "北京")])
    monkeypatch.setattr(svc, "build_so_map", lambda: {})
    prefs_svc.save_prefs({"blacklist": ["外包"]})

    payload = svc.build_recommendation(resume_text=RESUME)
    assert payload["prefs_applied"]["blocked_preachs"] == ["某外包"]
    assert [r["单位名称"] for r in payload["recommended_preachs"]] == ["甲公司"]


# ---------------------------------------------------------------- HTTP 接口

def test_api_prefs_get_returns_full_structure():
    resp = server.app.test_client().get("/api/prefs")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert set(data["prefs"]) == PREF_KEYS
    assert data["defaults"]["prefs"] == prefs_svc.empty_prefs()
    assert data["defaults"]["company_types"]


def test_api_prefs_post_partial_update():
    client = server.app.test_client()
    first = client.post("/api/prefs", json={"target_cities": ["武汉", "深圳"],
                                            "salary_min": "abc"})
    assert first.status_code == 200
    saved = first.get_json()["prefs"]
    assert saved["target_cities"] == ["武汉", "深圳"]
    assert saved["salary_min"] == 0                       # 非数字按 0

    second = client.post("/api/prefs", json={"blacklist": ["某外包"]})
    saved2 = second.get_json()["prefs"]
    assert saved2["blacklist"] == ["某外包"]
    assert saved2["target_cities"] == ["武汉", "深圳"]      # 未传入的字段保留

    assert client.get("/api/prefs").get_json()["prefs"] == saved2


def test_api_prefs_post_rejects_non_object_body():
    resp = server.app.test_client().post("/api/prefs", json=["不是对象"])
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ---------------------------------------------------------------- keyring（可选依赖）

class _FakeKeyring:
    """记录调用、可按需抛异常的钥匙串替身。"""

    def __init__(self, fail: bool = False):
        self.store: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []
        self.fail = fail

    def _tick(self, op: str) -> None:
        self.calls.append(op)
        if self.fail:
            raise RuntimeError("钥匙串服务未启动")

    def get_password(self, service: str, field: str):
        self._tick("get")
        return self.store.get((service, field))

    def set_password(self, service: str, field: str, value: str) -> None:
        self._tick("set")
        self.store[(service, field)] = value

    def delete_password(self, service: str, field: str) -> None:
        self._tick("delete")
        if (service, field) not in self.store:
            raise RuntimeError("条目不存在（多数后端就是这个行为）")
        del self.store[(service, field)]


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    """config.json 指向临时文件，避免测试改写用户真实配置。"""
    path = tmp_path / "config.json"
    monkeypatch.setattr(settings, "CONFIG_PATH", path)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("WHUT_KEYRING", raising=False)
    return path


def test_without_keyring_module_behaviour_is_unchanged(cfg_path, monkeypatch):
    """没装 keyring：读写完全走 config.json，与接入钥匙串之前一模一样。"""
    monkeypatch.setattr(settings, "keyring", None)
    settings.save_config({"api_key": "sk-file", "provider": "siliconflow"})
    assert settings.load_config()["api_key"] == "sk-file"
    assert settings.get_api_key() == "sk-file"
    status = settings.keyring_status()
    assert status["available"] is False and status["enabled"] is False
    assert status["source"] == "file"


def test_keyring_is_never_touched_when_switch_is_off(cfg_path, monkeypatch):
    """开关关闭（默认）时一次都不碰钥匙串：不装的人不该被拖慢，也不该有意外副作用。"""
    fake = _FakeKeyring()
    monkeypatch.setattr(settings, "keyring", fake)
    cfg_path.write_text(json.dumps({"api_key": "sk-file"}), encoding="utf-8")

    assert settings.load_config()["api_key"] == "sk-file"
    settings.save_config({"api_key": "sk-file", "provider": "siliconflow"})
    assert fake.calls == []


def test_keyring_errors_fall_back_to_file_silently(cfg_path, monkeypatch):
    """钥匙串抛任何异常都必须静默回落到 config.json——这是本功能唯一的硬底线。"""
    monkeypatch.setattr(settings, "keyring", _FakeKeyring(fail=True))
    monkeypatch.setenv("WHUT_KEYRING", "1")

    settings.save_config({"api_key": "sk-file", "provider": "siliconflow"})     # 不抛
    assert settings.load_config()["api_key"] == "sk-file"
    assert settings.get_api_key() == "sk-file"
    assert settings.keyring_status()["source"] == "file"


def test_keyring_enabled_writes_and_is_preferred_on_read(cfg_path, monkeypatch):
    """开关打开且可用：Key 进钥匙串；读取时钥匙串优先于 config.json。"""
    fake = _FakeKeyring()
    monkeypatch.setattr(settings, "keyring", fake)
    monkeypatch.setenv("WHUT_KEYRING", "1")

    settings.save_config({"api_key": "sk-from-file", "provider": "siliconflow"})
    assert fake.store[(settings.KEYRING_SERVICE, "api_key")] == "sk-from-file"
    # 迁移不主动删文件里的旧 Key：清不清由用户决定，避免「两边都没有」的窗口期
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["api_key"] == "sk-from-file"

    fake.store[(settings.KEYRING_SERVICE, "api_key")] = "sk-from-keyring"
    assert settings.load_config()["api_key"] == "sk-from-keyring"     # 钥匙串优先
    status = settings.keyring_status()
    assert status["source"] == "keyring" and status["enabled"] is True


def test_use_keyring_config_switch_also_enables(cfg_path, monkeypatch):
    """除了环境变量，config.json 里的 use_keyring 也能开启。"""
    fake = _FakeKeyring()
    monkeypatch.setattr(settings, "keyring", fake)
    settings.save_config({"api_key": "sk", "provider": "siliconflow", "use_keyring": True})
    assert fake.calls, "配置项 use_keyring 没生效"
    assert settings.keyring_enabled(settings.load_config()) is True


def test_deleting_key_clears_keyring_entry_too(cfg_path, monkeypatch):
    """删除 Key 时钥匙串那份也要清掉，否则下次读取又会冒出来，删除看起来像没生效。"""
    fake = _FakeKeyring()
    monkeypatch.setattr(settings, "keyring", fake)
    monkeypatch.setenv("WHUT_KEYRING", "1")

    settings.save_config({"api_key": "sk", "provider": "siliconflow"})
    settings.save_config({"api_key": "", "provider": "siliconflow"})
    assert (settings.KEYRING_SERVICE, "api_key") not in fake.store
    assert settings.get_api_key() == ""
    assert settings.keyring_status()["source"] == "none"
