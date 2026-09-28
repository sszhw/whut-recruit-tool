"""repository 统一数据访问层测试：跨文件合并、ID 去重、缓存、聚合统计、企业清单。

后半段是「主数据迁到 SQLite」的用例：要求 SQLite 路径与 JSON 路径口径完全一致，
且库缺失 / 损坏 / 开关关闭时自动回落 JSON，绝不让界面白屏。
"""

from __future__ import annotations

import threading
from pathlib import Path

import repository as repo
import sqlite_store
from conftest import preach_item, preach_path, recruit_item, recruit_path, write_raw


def _build_db(data_dir) -> dict:
    """把临时目录里的原始文件**全量**灌进 <data_dir>/whut_data.db（不碰真实 data/）。"""
    plan = {kind: (key, repo.iter_files(glob, data_dir)) for kind, (glob, key) in repo.KINDS.items()}
    report = sqlite_store.sync(sqlite_store.db_path_for(data_dir), plan, force=True)
    assert report is not None, "测试库导入失败"
    return report


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


def test_companies_result_is_cached(make_recruit):
    """企业清单按数据签名缓存：同一份数据返回同一对象，避免重复清洗正文。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="企业甲")], mtime=1_700_000_000)
    first = repo.companies()
    assert first is repo.companies()
    make_recruit("2026-09-02_至_2026-09-02", [recruit_item("2", name="企业乙")], mtime=1_700_100_000)
    assert {c["name"] for c in repo.companies()} == {"企业甲", "企业乙"}
    assert len(repo.companies(max_items=1)) == 1          # 截断不影响缓存本体
    assert len(repo.companies()) == 2


def test_cached_derived_reuses_and_invalidates(make_recruit):
    """通用派生缓存：签名相同复用、数据变化失效、extra 参与签名。"""
    calls: list[int] = []

    def build() -> list[str]:
        calls.append(1)
        return [c["name"] for c in repo.companies()]

    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="企业甲")], mtime=1_700_000_000)
    a = repo.cached_derived("names", "recruit", build)
    b = repo.cached_derived("names", "recruit", build)
    assert a is b and len(calls) == 1

    repo.cached_derived("names", "recruit", build, extra="2026-09-16")   # extra 变化 → 重算
    assert len(calls) == 2

    make_recruit("2026-09-02_至_2026-09-02", [recruit_item("2", name="企业乙")], mtime=1_700_100_000)
    assert sorted(repo.cached_derived("names", "recruit", build, extra="2026-09-16")) == ["企业乙", "企业甲"]
    assert len(calls) == 3


def test_cached_derived_rejects_kind_name(make_recruit):
    import pytest
    with pytest.raises(ValueError):
        repo.cached_derived("recruit", "recruit", lambda: [])


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


# ---------------------------------------------------------------- SQLite 路径

def test_sqlite_matches_json_path(data_dir, make_recruit, make_preach):
    """切到 SQLite 后，合并去重与「新文件优先」的结果必须和 JSON 路径逐条一致。"""
    make_recruit("2026-09-01_至_2026-09-01", [
        recruit_item("1", name="东方电气", title="旧标题"),
        recruit_item("2", name="东方电气"),
    ], mtime=1_700_000_000)
    make_recruit("2026-09-10_至_2026-09-10", [
        recruit_item("1", name="东方电气", title="新标题"),
        recruit_item("3", name="中国中车"),
    ], mtime=1_700_100_000)
    make_preach("2026年", [preach_item("p1", hold_date="2026-09-20")])

    _build_db(data_dir)
    repo.invalidate()

    items = repo.raw_items("recruit")
    assert [str(i["id"]) for i in items] == ["1", "3", "2"]      # 按文件新旧、文件内顺序
    assert items[0]["title"] == "新标题"
    assert [str(i["id"]) for i in repo.raw_items("preach")] == ["p1"]

    s = repo.master_summary()
    assert (s["recruit_count"], s["preach_count"], s["files"]) == (3, 1, 3)
    assert s["coverage_start"] and s["coverage_end"]


def test_sqlite_and_json_agree_on_aggregate(data_dir, make_recruit, monkeypatch):
    """aggregate 的两条路径（SQL 统计 / 逐条扫描）必须给出同一份数字。"""
    make_recruit("2026-09-01_至_2026-09-01", [
        recruit_item("1", addtime=1786000000),
        recruit_item("2", addtime=1789000000, content="", remarks=""),
    ], mtime=1_700_000_000)
    _build_db(data_dir)

    monkeypatch.setenv("WHUT_STORE", "sqlite")
    repo.invalidate()
    from_sqlite = repo.aggregate("recruit")
    monkeypatch.setenv("WHUT_STORE", "json")
    repo.invalidate()
    from_json = repo.aggregate("recruit")

    assert {k: from_sqlite[k] for k in ("records", "first_date", "last_date", "missing_detail")} == \
           {k: from_json[k] for k in ("records", "first_date", "last_date", "missing_detail")}
    assert from_sqlite["records"] == 2 and from_sqlite["missing_detail"] == 1


def test_new_file_is_picked_up_by_auto_sync(data_dir, make_recruit):
    """库建好后新抓了一个文件：签名对不上 → 就地增量同步，而不是默默用旧库。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="企业甲")], mtime=1_700_000_000)
    _build_db(data_dir)
    repo.invalidate()
    assert len(repo.raw_items("recruit")) == 1

    make_recruit("2026-09-02_至_2026-09-02", [recruit_item("2", name="企业乙")], mtime=1_700_100_000)
    assert {str(i["id"]) for i in repo.raw_items("recruit")} == {"1", "2"}
    assert {c["name"] for c in repo.companies()} == {"企业甲", "企业乙"}


def test_cold_start_builds_db_automatically(data_dir, make_recruit, make_preach, monkeypatch):
    """库不存在时首次访问自动全量建库——新环境 clone 后没跑脚本也能拿到加速。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="企业甲"), recruit_item("2", name="企业甲")])
    make_preach("2026年", [preach_item("p1")])
    db = sqlite_store.db_path_for(data_dir)
    assert not db.exists()

    s = repo.master_summary()
    assert db.exists(), "冷启动应自动建库"
    assert (s["recruit_count"], s["preach_count"]) == (2, 1)

    from_sqlite = [str(i["id"]) for i in repo.raw_items("recruit")]
    monkeypatch.setenv("WHUT_STORE", "json")
    repo.invalidate()
    assert [str(i["id"]) for i in repo.raw_items("recruit")] == from_sqlite


def test_default_dir_bootstraps_even_via_raw_items_in(data_dir, make_recruit):
    """默认数据目录（DATA 被夹具指向的目录）下库缺失 → 仍然自动建库。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="企业甲")])
    db = sqlite_store.db_path_for(data_dir)
    assert not db.exists()

    assert [str(i["id"]) for i in repo.raw_items_in("recruit", data_dir)] == ["1"]
    assert db.exists()                      # data_dir == DATA，可以在自己的地盘建库


def test_foreign_dir_is_readonly(tmp_path):
    """非默认目录（用户工作目录）保持只读：不建库、不写盘，走 JSON 合并。"""
    write_raw(preach_path(tmp_path, "2026年"), 宣讲会=[preach_item("p1")])
    assert Path(tmp_path).resolve() != repo.DATA.resolve()

    items = repo.raw_items_in("preach", tmp_path)
    assert [str(i["id"]) for i in items] == ["p1"]
    assert not sqlite_store.db_path_for(tmp_path).exists()

    repo.raw_items_in("recruit", tmp_path)
    assert not sqlite_store.db_path_for(tmp_path).exists()


def test_bootstrap_failure_falls_back_to_json_and_is_tried_once(data_dir, make_recruit, monkeypatch):
    """导入失败（磁盘不可写等）→ 回落 JSON，且同一进程内不反复重试。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1")])
    calls: list[int] = []

    def boom(db_path, kinds, d):
        calls.append(1)
        raise OSError("模拟磁盘不可写")

    monkeypatch.setattr(sqlite_store, "import_all", boom)

    assert len(repo.raw_items("recruit")) == 1
    assert repo.master_summary()["recruit_count"] == 1
    repo.invalidate()                       # 数据被重写后也不会再撞一次注定失败的导入
    assert len(repo.raw_items("recruit")) == 1
    assert len(calls) == 1


def test_json_mode_never_creates_db(data_dir, make_recruit, monkeypatch):
    """WHUT_STORE=json 时不建库、不写盘，纯 JSON 路径。"""
    monkeypatch.setenv("WHUT_STORE", "json")
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1")])

    assert len(repo.raw_items("recruit")) == 1
    assert repo.master_summary()["recruit_count"] == 1
    assert not sqlite_store.db_path_for(data_dir).exists()


def test_concurrent_first_call_imports_once(data_dir, make_recruit, monkeypatch):
    """Flask 是多线程的：4 个请求同时冷启动，56MB 全量导入只能跑一次。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="企业甲")])
    calls: list[int] = []
    real = sqlite_store.import_all

    def spy(db_path, kinds, d):
        calls.append(1)
        return real(db_path, kinds, d)

    monkeypatch.setattr(sqlite_store, "import_all", spy)

    barrier = threading.Barrier(4)
    seen: list[int] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        got = len(repo.raw_items("recruit"))
        with guard:
            seen.append(got)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen == [1, 1, 1, 1]
    assert len(calls) == 1


def test_falls_back_to_json_when_db_corrupt(data_dir, make_recruit):
    """库被写坏（截断 / 非数据库文件）→ 回落 JSON，不能把 500 抛给页面。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="企业甲")])
    db = sqlite_store.db_path_for(data_dir)
    db.write_text("这不是一个 sqlite 库，只是被覆盖过的垃圾内容", encoding="utf-8")

    repo.invalidate()
    assert [c["name"] for c in repo.companies()] == ["企业甲"]
    assert repo.master_summary()["recruit_count"] == 1


def test_store_env_json_disables_sqlite(data_dir, make_recruit, monkeypatch):
    """WHUT_STORE=json 时完全不碰 SQLite（灰度回退开关）。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1")])
    _build_db(data_dir)

    calls: list[str] = []
    real = sqlite_store.load_items

    def spy(db_path, kind):
        calls.append(kind)
        return real(db_path, kind)

    monkeypatch.setattr(sqlite_store, "load_items", spy)

    monkeypatch.setenv("WHUT_STORE", "json")
    repo.invalidate()
    assert len(repo.raw_items("recruit")) == 1
    assert calls == []

    monkeypatch.setenv("WHUT_STORE", "sqlite")
    repo.invalidate()
    repo.raw_items("recruit")
    assert calls == ["recruit"]


def test_import_skips_broken_files(data_dir, make_recruit):
    """导入要跳过坏文件并报告，而不是整批崩掉（历史数据里有半截 JSON / GBK / 空文件）。"""
    make_recruit("2026-09-01_至_2026-09-01", [recruit_item("1", name="正常企业")])

    good = recruit_path(data_dir, "2026-09-02_至_2026-09-02")
    good.write_text('﻿' + '{"招聘信息": [{"id": "2", "com_id_name": "带BOM企业"}]}', encoding="utf-8")
    (data_dir / "武汉理工大学招聘信息_半截_原始数据.json").write_text('{"招聘信息": [{"id": "3"', encoding="utf-8")
    (data_dir / "武汉理工大学招聘信息_空_原始数据.json").write_text("", encoding="utf-8")
    (data_dir / "武汉理工大学招聘信息_GBK_原始数据.json").write_bytes(
        '{"招聘信息": [{"id": "4", "com_id_name": "GBK企业"}]}'.encode("gbk"))

    report = _build_db(data_dir)
    bad = {Path(p).name for p in report["recruit"]["bad_files"]}
    assert bad == {"武汉理工大学招聘信息_半截_原始数据.json", "武汉理工大学招聘信息_空_原始数据.json"}

    repo.invalidate()
    names = {c["name"] for c in repo.companies()}
    assert names == {"正常企业", "带BOM企业", "GBK企业"}


def test_public_signatures_are_unchanged():
    """对外签名零变化：这是本次「换存储不换接口」的硬约束，改一个字都要显式确认。"""
    expected = [
        "def iter_files(pattern: str, data_dir: Path | None = None) -> list[Path]:",
        "def read_json(path: Path) -> dict:",
        "def raw_items(kind: str) -> list[dict]:",
        "def cached_derived(name: str, kind: str, builder, extra: str = \"\") -> Any:",
        "def source_files(kind: str) -> list[Path]:",
        "def latest_source_file() -> Path | None:",
        "def raw_items_in(kind: str, data_dir: Path) -> list[dict]:",
        "def invalidate() -> None:",
        "def aggregate(kind: str) -> dict:",
        "def master_summary() -> dict:",
        "def companies(max_items: int = 0) -> list[dict]:",
        "def company_names() -> list[str]:",
        "def unanalyzed(cache: dict) -> list[str]:",
        "def health(cache: dict | None = None, work_undetermined: int | None = None,",
    ]
    lines = Path(repo.__file__).read_text(encoding="utf-8").splitlines()
    public = [ln for ln in lines if ln.startswith("def ") and not ln.startswith("def _")]
    missing = [e for e in expected if e not in public]
    assert not missing, f"对外签名发生变化：{missing}"


def test_raw_items_in_reads_db_of_given_dir(tmp_path):
    """raw_items_in 指定目录时也认该目录下的库（analyze --source 场景）。"""
    write_raw(preach_path(tmp_path, "2026年"), 宣讲会=[preach_item("p1")])
    _build_db(tmp_path)
    assert [str(i["id"]) for i in repo.raw_items_in("preach", tmp_path)] == ["p1"]
    assert repo.raw_items_in("recruit", tmp_path) == []
