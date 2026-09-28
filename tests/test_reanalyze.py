"""重分析机制测试：缓存溯源标记 + 过期判定 + 上限保护 + CLI/接口接线。

要锁住的真问题：默认模式「只要企业名在缓存里就跳过」，于是

1. `PROMPT` 改了判定规则（如央企口径调整），旧结论永远不会重算，用户还以为是新口径；
2. 企业更新了招聘公告，工作地 / 岗位变了，缓存里还是旧公告得出的结论；
3. 一次网络抖动留下的「分析失败」会永久卡住这家企业。

测试全程 monkeypatch LLM，不发真实请求、不读写项目真实 data/。
"""

from __future__ import annotations

import json
import sys

import analyze
import llm_client
import pytest
import server


def _entry(**over) -> dict:
    """一条成功的企业分析结果（字段形状与线上一致）。"""
    entry = {"company_type": "央企", "is_state_owned": True, "confidence": "高",
             "locations": ["武汉"], "evidence": "国务院国资委监管", "_raw": ""}
    entry.update(over)
    return entry


def _fresh(name: str, text: str) -> dict:
    """造一条「刚用当前 prompt + 当前文本分析过」的缓存条目。"""
    return {name: analyze.stamp_result(_entry(), text)}


# ---------- 溯源标记 ----------

def test_prompt_version_tracks_prompt_content(monkeypatch):
    """改一次 PROMPT，版本标记必须跟着变——否则改了规则也不会触发重算。"""
    original = analyze.PROMPT
    assert analyze.PROMPT_VERSION == analyze.source_hash(original)   # 常量与哈希口径一致
    monkeypatch.setattr(analyze, "PROMPT", original + "\n8. 新增一条判定规则")
    assert analyze.source_hash(analyze.PROMPT) != analyze.PROMPT_VERSION


def test_call_api_stamps_provenance(monkeypatch):
    """分析结果必须带上「用什么 prompt、依据哪段文本」，否则无从判断是否过期。"""
    monkeypatch.setattr(llm_client, "chat", lambda *a, **kw: json.dumps(
        {"company_type": "民企", "is_state_owned": False, "confidence": "高",
         "locations": ["武汉"], "evidence": "民营"}, ensure_ascii=False))
    result = analyze.call_api("k", "m", "甲企业", "公告正文")

    assert result["_prompt_version"] == analyze.PROMPT_VERSION
    assert result["_source_hash"] == analyze.source_hash("公告正文")


def test_failed_result_keeps_legacy_shape(monkeypatch):
    """失败结果不打标，字段形状保持原样（既有测试锁死了这套形状）。

    失败条目靠 company_type 判为「此前分析失败」，不需要指纹。
    """
    def boom(*a, **kw):
        raise llm_client.LLMError("API Key 无效或已失效（HTTP 401）", kind="http", status_code=401)

    monkeypatch.setattr(llm_client, "chat", boom)
    result = analyze.call_api("k", "m", "甲企业", "公告正文")

    assert set(result) == {"company_type", "is_state_owned", "confidence",
                           "locations", "evidence", "_raw"}


# ---------- 过期判定 ----------

def test_legacy_cache_without_marks_is_stale():
    """老缓存没有版本字段 → 版本未知 → 需重算（否则历史脏数据永远清不掉）。"""
    cache = {"甲企业": _entry()}
    info = analyze.stale_entries(cache, [{"name": "甲企业", "text": "公告正文"}])

    assert info["stale"] == ["甲企业"]
    assert info["reasons"]["甲企业"] == analyze.REASON_UNKNOWN


def test_prompt_upgrade_makes_entry_stale():
    cache = _fresh("甲企业", "公告正文")
    cache["甲企业"]["_prompt_version"] = "deadbeef"       # 旧 prompt 留下的结论
    info = analyze.stale_entries(cache, [{"name": "甲企业", "text": "公告正文"}])

    assert info["stale"] == ["甲企业"]
    assert info["reasons"]["甲企业"] == analyze.REASON_PROMPT


def test_source_text_changed_makes_entry_stale():
    """企业更新了公告 → 旧结论的依据已经变了。"""
    cache = _fresh("甲企业", "旧公告：只在武汉")
    info = analyze.stale_entries(cache, [{"name": "甲企业", "text": "新公告：武汉、深圳"}])

    assert info["reasons"]["甲企业"] == analyze.REASON_SOURCE


def test_fresh_entry_is_not_stale():
    """版本一致且依据文本没变 → 不该重复烧 token。"""
    cache = _fresh("甲企业", "公告正文")
    info = analyze.stale_entries(cache, [{"name": "甲企业", "text": "公告正文"}])

    assert info["stale"] == []
    assert info["reasons"] == {}
    assert info["prompt_version"] == analyze.PROMPT_VERSION
    assert info["total"] == 1


def test_failed_entry_is_stale():
    """一次网络抖动留下的失败结果不能永久卡住这家企业。"""
    cache = {"甲企业": analyze.stamp_result(_entry(company_type="分析失败",
                                                 is_state_owned=None,
                                                 evidence="AI 调用失败：请求超时"), "公告正文")}
    info = analyze.stale_entries(cache, [{"name": "甲企业", "text": "公告正文"}])

    assert info["reasons"]["甲企业"] == analyze.REASON_FAILED


def test_parse_failure_entry_is_stale():
    cache = {"甲企业": analyze.stamp_result(_entry(company_type="解析失败",
                                                 is_state_owned=None), "公告正文")}
    info = analyze.stale_entries(cache, [{"name": "甲企业", "text": "公告正文"}])

    assert info["reasons"]["甲企业"] == analyze.REASON_FAILED


def test_uncached_company_is_not_stale():
    """压根没分析过的企业走首次分析路径，不计入「重算」。"""
    info = analyze.stale_entries({}, [{"name": "甲企业", "text": "公告正文"}])

    assert info["stale"] == []
    assert info["total"] == 1


# ---------- 上限保护 ----------

def test_stale_run_capped_at_max_per_run():
    """PROMPT 大改会命中全部历史企业；单次必须封顶，剩下的留到下次运行。"""
    names = [f"企业{i}" for i in range(250)]
    cache = {n: _entry() for n in names}                 # 全都是老缓存 → 全部过期
    companies = [{"name": n, "text": "公告"} for n in names]

    plan = analyze.plan_todo(cache, companies, only_stale=True)

    assert len(plan["todo"]) == analyze.MAX_STALE_PER_RUN == 200
    assert plan["remaining"] == 50
    assert set(plan["counts"]) == {analyze.REASON_UNKNOWN}


def test_limit_narrows_stale_run_but_never_widens_it():
    """--limit 是试跑上限，只能比兜底上限更小，不能绕过它。"""
    companies = [{"name": f"企业{i}", "text": "公告"} for i in range(250)]
    cache = {c["name"]: _entry() for c in companies}

    narrow = analyze.plan_todo(cache, companies, only_stale=True, limit=5)
    assert len(narrow["todo"]) == 5 and narrow["remaining"] == 245

    wide = analyze.plan_todo(cache, companies, only_stale=True, limit=5000)
    assert len(wide["todo"]) == analyze.MAX_STALE_PER_RUN


# ---------- 三种运行模式 ----------

def test_default_mode_still_skips_cached():
    """回归保护：默认行为不变——只分析缓存里没有的企业。"""
    cache = _fresh("甲企业", "公告正文")
    companies = [{"name": "甲企业", "text": "公告正文"}, {"name": "乙企业", "text": "新公告"}]

    plan = analyze.plan_todo(cache, companies)

    assert [c["name"] for c in plan["todo"]] == ["乙企业"]
    assert plan["remaining"] == 0


def test_force_mode_reanalyzes_everything():
    cache = _fresh("甲企业", "公告正文")
    companies = [{"name": "甲企业", "text": "公告正文"}, {"name": "乙企业", "text": "新公告"}]

    plan = analyze.plan_todo(cache, companies, force=True)

    assert [c["name"] for c in plan["todo"]] == ["甲企业", "乙企业"]


def test_only_stale_reports_reason_breakdown():
    """打印里要能说清「这次为什么烧 token」。"""
    cache = _fresh("甲企业", "公告正文")
    cache["甲企业"]["_prompt_version"] = "deadbeef"                  # prompt 升级
    cache["乙企业"] = analyze.stamp_result(_entry(), "旧公告")        # 公告变更
    cache["丙企业"] = _entry(company_type="分析失败")                  # 此前失败
    cache["丁企业"] = _entry()                                        # 老缓存，版本未知
    companies = [{"name": n, "text": "新公告"} for n in ("甲企业", "乙企业", "丙企业", "丁企业")]

    plan = analyze.plan_todo(cache, companies, only_stale=True)

    assert plan["counts"] == {analyze.REASON_PROMPT: 1, analyze.REASON_SOURCE: 1,
                              analyze.REASON_FAILED: 1, analyze.REASON_UNKNOWN: 1}


# ---------- 缓存往返 ----------

def test_cache_roundtrip_keeps_marks(tmp_path):
    """落盘再读回，溯源标记不能丢——丢了就又变成「版本未知」无限重算。"""
    path = tmp_path / "cache.json"
    cache = _fresh("甲企业", "公告正文")
    analyze.save_cache(path, cache)

    loaded = analyze.load_cache(path)

    assert loaded["甲企业"]["_prompt_version"] == analyze.PROMPT_VERSION
    assert loaded["甲企业"]["_source_hash"] == analyze.source_hash("公告正文")
    assert analyze.stale_entries(loaded, [{"name": "甲企业", "text": "公告正文"}])["stale"] == []


# ---------- CLI ----------

def _write_input(data_dir, items: list[dict]) -> str:
    path = data_dir / "武汉理工大学招聘信息_t_原始数据.json"
    path.write_text(json.dumps({"招聘信息": items}, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _run_main(monkeypatch, data_dir, argv: list[str], cache: dict, items: list[dict]) -> tuple[list[str], dict]:
    """跑 analyze.main()，把 LLM / 落盘 / 报告全部打桩，返回 (被分析的企业名, 最终缓存)。"""
    analyzed: list[str] = []
    saved: dict = {}

    def fake_call(api_key, model, name, text, max_retries=3):
        analyzed.append(name)
        return analyze.stamp_result(_entry(company_type="民企", is_state_owned=False), text)

    monkeypatch.setattr(analyze, "call_api", fake_call)
    monkeypatch.setattr(analyze, "load_cache", lambda path: dict(cache))
    monkeypatch.setattr(analyze, "save_cache", lambda path, c: saved.update(c))
    monkeypatch.setattr(analyze, "build_outputs",
                        lambda *a, **kw: {"total": 0, "state_owned": 0, "csv": "c", "md": "m"})
    monkeypatch.setattr(analyze.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(analyze.settings, "get_llm",
                        lambda: {"api_key": "k", "model": "m", "base_url": "http://x", "label": "测试"})
    monkeypatch.setattr(sys, "argv", ["analyze.py", "--input", _write_input(data_dir, items), *argv])

    assert analyze.main() == 0
    return analyzed, saved


_ITEMS = [{"id": "1", "com_id_name": "甲企业", "title": "招聘", "content": "武汉"},
          {"id": "2", "com_id_name": "乙企业", "title": "招聘", "content": "深圳"}]


def test_cli_default_skips_cached(monkeypatch, data_dir):
    """CLI 默认语义：缓存里已有的直接跳过（别把这条改坏）。"""
    analyzed, _ = _run_main(monkeypatch, data_dir, [], _fresh("甲企业", "武汉"), _ITEMS)
    assert analyzed == ["乙企业"]


def test_cli_force_reanalyzes_all(monkeypatch, data_dir):
    analyzed, _ = _run_main(monkeypatch, data_dir, ["--force"], _fresh("甲企业", "武汉"), _ITEMS)
    assert analyzed == ["甲企业", "乙企业"]


def test_cli_only_stale_reanalyzes_only_stale(monkeypatch, data_dir):
    cache = _fresh("甲企业", "武汉")      # 甲：当前 prompt + 当前文本 → 有效
    cache["乙企业"] = _entry()            # 乙：老缓存 → 版本未知
    analyzed, saved = _run_main(monkeypatch, data_dir, ["--only-stale"], cache, _ITEMS)

    assert analyzed == ["乙企业"]
    # 重算后必须补上标记，否则下次还会被判为过期
    assert saved["乙企业"]["_prompt_version"] == analyze.PROMPT_VERSION


def test_cli_only_stale_prints_remaining(monkeypatch, data_dir, capsys):
    """超出上限要明确告知还剩多少家，避免用户以为已经跑完。"""
    items = [{"id": str(i), "com_id_name": f"企业{i}", "content": "武汉"} for i in range(250)]
    cache = {f"企业{i}": _entry() for i in range(250)}

    analyzed, _ = _run_main(monkeypatch, data_dir, ["--only-stale"], cache, items)
    out = capsys.readouterr().out

    assert len(analyzed) == 200
    assert "还有 50 家待重算，请再次运行" in out
    assert "版本未知 250 家" in out


# ---------- 接口 ----------

def _stale_route_body(monkeypatch, data_dir):
    """调 GET /api/analyze/stale，返回响应体。"""
    import api.analysis as analysis_api

    _write_input(data_dir, [{"id": "1", "com_id_name": "甲企业", "content": "武汉"}])
    monkeypatch.setattr(analysis_api, "load_cache", lambda: {"甲企业": _entry()})
    resp = server.app.test_client().get("/api/analyze/stale")
    assert resp.status_code == 200
    return resp.get_json()


def test_stale_route_reports_reasons(monkeypatch, data_dir):
    body = _stale_route_body(monkeypatch, data_dir)

    assert body["ok"] is True
    assert body["stale"] == 1 and body["total"] == 1
    assert body["reasons"] == {"甲企业": analyze.REASON_UNKNOWN}
    assert body["prompt_version"] == analyze.PROMPT_VERSION


def test_post_analyze_only_stale_adds_flag(monkeypatch, data_dir):
    """payload["only_stale"] 必须真的变成子进程参数，否则前端开关是假的。"""
    import api.analysis as analysis_api

    _write_input(data_dir, [{"id": "1", "com_id_name": "甲企业", "content": "武汉"}])
    captured: dict = {}
    monkeypatch.setattr(analysis_api, "get_llm",
                        lambda: {"api_key": "k", "model": "m", "base_url": "", "label": "测试"})
    monkeypatch.setattr(analysis_api.tasks, "start",
                        lambda kind, cmd, env, title="": (
                            captured.update(cmd=list(cmd), title=title), {"ok": True})[1])

    resp = server.app.test_client().post("/api/analyze", json={"only_stale": True})
    assert resp.status_code == 200
    assert "--only-stale" in captured["cmd"]
    assert "重算" in captured["title"]


def test_post_analyze_without_only_stale(monkeypatch, data_dir):
    """不传 only_stale 时不能偷偷加上——全量重算会烧掉大量 token。"""
    import api.analysis as analysis_api

    _write_input(data_dir, [{"id": "1", "com_id_name": "甲企业", "content": "武汉"}])
    captured: dict = {}
    monkeypatch.setattr(analysis_api, "get_llm",
                        lambda: {"api_key": "k", "model": "m", "base_url": "", "label": "测试"})
    monkeypatch.setattr(analysis_api.tasks, "start",
                        lambda kind, cmd, env, title="": (
                            captured.update(cmd=list(cmd)), {"ok": True})[1])

    resp = server.app.test_client().post("/api/analyze", json={})
    assert resp.status_code == 200
    assert "--only-stale" not in captured["cmd"]


# ---------- 与报告导入的一致性 ----------

def test_imported_entries_are_unknown_version():
    """反向导入的条目没有源文本 → 版本未知 → 下次 --only-stale 会重算。

    否则手写 / 外部生成的结果会被当成当前 prompt 口径下的结论长期沿用。
    """
    from services import reports

    entries = reports.parse_report_md(reports.IMPORT_TEMPLATE)["entries"]
    assert entries, "模板应能解析出条目"
    for entry in entries.values():
        assert entry["_prompt_version"] == analyze.PROMPT_VERSION
        assert entry["_source_hash"] == ""

    info = analyze.stale_entries(entries, [{"name": n, "text": ""} for n in entries])
    assert info["reasons"] == {n: analyze.REASON_UNKNOWN for n in entries}


@pytest.mark.parametrize("entry", [
    _entry(),                                   # 老缓存
    _entry(company_type="分析失败"),             # 此前失败
])
def test_stale_reason_never_empty_for_bad_entries(entry):
    """mutation 守门：这两种条目一旦被判为「有效」，脏数据就再也清不掉。"""
    assert analyze.stale_reason(entry, "公告正文") != ""
