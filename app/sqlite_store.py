#!/usr/bin/env python3
"""SQLite 存储层：把散落在多个 `*_原始数据.json` 里的主数据建成单库索引。

为什么需要这一层（背景见 docs/架构与网页功能优化建议.md）：
`repository` 原先每次缓存失效都要把 data/ 下**全部**原始数据（主库 56MB）整体
解析一遍，再跨文件按 ID 合并去重。而 `master_summary()` 被 /api/status 调用、
SSE 每秒都可能触发一次，文件 mtime 一变缓存就失效，于是每秒都在重复这轮解析。

导入一次之后，查询侧只剩「带索引的 SELECT」：
  - `load_items` 取合并去重后的记录（payload 全文反序列化）；
  - `stats` 直接 COUNT/MIN/MAX，连 payload 都不用解——这是 master_summary 的主路径。

设计取舍：

1. **保留 payload 全文**：字段是学校网站给的，随时会加列。把原始 JSON 整条存下来，
   新增字段不需要改表结构，也不需要重新导入。关键字段另存成列，只为统计与筛选服务。
2. **去重规则固化在 SQL 里**：跨文件按学校 ID 去重、新文件优先（mtime 新的胜出）。
   为此记录里同时存 `mtime_ns`，查询时按它排序取第一条，而不是导入时算死排名——
   这样单个文件变化时只重导那一个文件，其余行不用动。
3. **只依赖标准库**：本模块不 import repository / crawler / settings，单向被依赖。
   数组键名（招聘信息/双选会/宣讲会）由调用方传入，避免两处各写一份 KINDS。
4. **一律「查不到返回 None」，不抛异常**：库缺失、损坏、表不存在、payload 解析失败，
   统统返回一个明确的「不可用」信号，由 repository 决定回落 JSON 路径。
   界面宁可慢一点，也不能白屏。
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

DB_NAME = "whut_data.db"          # 默认库文件名，落在数据目录下
STORE_ENV = "WHUT_STORE"          # 灰度开关：sqlite（默认）/ json
STORE_PATH_ENV = "WHUT_SQLITE_PATH"   # 运维用：显式指定库路径，覆盖默认位置
SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 表结构
# records：一行 = 一条原始记录（未去重，同一 ID 可能在多个文件里各有一行）
#   rid        学校网站 ID（可能为空 → 不去重，全部保留）
#   mtime_ns   来源文件的修改时间，用于「新文件优先」与增量同步
#   day        导入时按 repository 口径算好的日期（YYYY-MM-DD），供 MIN/MAX 统计
#   detail_len 正文长度（remarks/content 取其一），0 表示详情缺失
#   payload    原始 JSON 全文，兼容任意未知字段
# sources：每个「数据类型 × 文件」的导入快照，用于判断库是否与磁盘文件同步
_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    rid         TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL,
    mtime_ns    INTEGER NOT NULL DEFAULT 0,
    seq         INTEGER NOT NULL DEFAULT 0,
    title       TEXT,
    com_id_name TEXT,
    addtime     INTEGER,
    hold_date   TEXT,
    day         TEXT    NOT NULL DEFAULT '',
    detail_len  INTEGER NOT NULL DEFAULT 0,
    payload     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_kind_rid ON records(kind, rid, mtime_ns DESC);
CREATE INDEX IF NOT EXISTS idx_records_kind_source ON records(kind, source);
CREATE TABLE IF NOT EXISTS sources (
    kind     TEXT    NOT NULL,
    path     TEXT    NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL,
    PRIMARY KEY (kind, path)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 「有效行」筛选条件：同一 (kind, rid) 只留 mtime 最新的那条；rid 为空表示不去重。
# 为什么不用导入时算死的排名：排名在文件增删后会整体失效，那时得重导全部文件。
_DEDUP = (
    "r.kind = :kind AND (r.rid = '' OR r.rowid = ("
    "SELECT x.rowid FROM records x WHERE x.kind = r.kind AND x.rid = r.rid "
    "ORDER BY x.mtime_ns DESC, x.source, x.seq, x.rowid LIMIT 1))"
)
_ITEMS_SQL = f"SELECT r.payload FROM records r WHERE {_DEDUP} ORDER BY r.mtime_ns DESC, r.source, r.seq"
_STATS_SQL = (
    "SELECT COUNT(*), "
    "COALESCE(SUM(CASE WHEN r.detail_len = 0 THEN 1 ELSE 0 END), 0), "
    "COALESCE(MIN(NULLIF(r.day, '')), ''), "
    "COALESCE(MAX(NULLIF(r.day, '')), '') "
    f"FROM records r WHERE {_DEDUP}"
)


# ---------------------------------------------------------------- 开关与路径

def store_mode() -> str:
    """当前存储后端：`WHUT_STORE=json` 可整站灰度切回 JSON 路径，默认 sqlite。"""
    return (os.environ.get(STORE_ENV) or "sqlite").strip().lower()


def db_path_for(data_dir: Path) -> Path:
    """库文件路径：默认 <数据目录>/whut_data.db，可用 WHUT_SQLITE_PATH 覆盖。

    为什么 DAO 每次调用都现算路径：repository.DATA 会被测试与 CLI 改指向别的目录
    （conftest 的 data_dir / analyze 的 --source），写死成导入时的值会读到错库。
    """
    override = (os.environ.get(STORE_PATH_ENV) or "").strip()
    return Path(override) if override else Path(data_dir) / DB_NAME


def iter_source_files(pattern: str, data_dir: Path) -> list[Path]:
    """匹配原始数据文件，按修改时间倒序（越新的文件在合并去重时优先级越高）。

    放在这里而不是调用方，是为了让「文件顺序 = 新文件优先」这条规则和
    SQL 里的 mtime 排序只有一处定义；repository.iter_files 直接转调本函数。
    """
    paths = glob.glob(str(Path(data_dir) / pattern))
    return [Path(p) for p in sorted(paths, key=os.path.getmtime, reverse=True)]


# ---------------------------------------------------------------- 字段计算

def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def day_of(kind: str, item: dict) -> str:
    """记录归入哪一天（YYYY-MM-DD），算不出返回 ''。

    **必须与 repository._dates_of 口径一致**：那边用于 JSON 路径的统计，
    这边在导入时算好存进 day 列供 SQL 的 MIN/MAX 用。两处口径一旦漂移，
    切库前后的覆盖日期就会对不上——所以由 repository 反向调用本函数。
    """
    if kind == "preach":
        d = str(item.get("hold_date", "") or "").strip()
        return d[:10] if len(d) >= 10 else ""
    addtime = item.get("addtime")
    if addtime in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(addtime)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def detail_len_of(item: dict) -> int:
    """正文长度（remarks 优先，其次 content），0 视为「详情缺失」。口径同 repository._missing_detail。"""
    return len(str(item.get("remarks") or item.get("content") or "").strip())


def _row_of(kind: str, item: dict, source: str, mtime_ns: int, seq: int) -> tuple:
    return (
        kind,
        str(item.get("id", "") or ""),
        source,
        mtime_ns,
        seq,
        _text(item.get("title")),
        _text(item.get("com_id_name")),
        _int_or_none(item.get("addtime")),
        _text(item.get("hold_date")),
        day_of(kind, item),
        detail_len_of(item),
        json.dumps(item, ensure_ascii=False),
    )


# ---------------------------------------------------------------- 容错读取

def read_source_json(path: Path) -> dict | None:
    """读原始数据文件；坏文件返回 None（由调用方跳过并报告，不让整批导入崩掉）。

    历史数据里踩过的坑：带 BOM、GBK 编码、空文件、写到一半被硬杀留下的半截 JSON。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    text = ""
    for enc in ("utf-8-sig", "gbk", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return None
    try:
        data = json.loads(text)
    except ValueError:      # JSONDecodeError 是 ValueError 子类
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- 连接

@contextmanager
def _connect(path: Path, *, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))


# ---------------------------------------------------------------- 导入与同步

def _stored_sources(conn: sqlite3.Connection, kind: str) -> dict[str, tuple[int, int]]:
    rows = conn.execute("SELECT path, mtime_ns, size FROM sources WHERE kind = ?", (kind,)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _file_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _import_file(conn: sqlite3.Connection, kind: str, array_key: str, path: Path) -> int | None:
    """导入单个文件；返回记录数，坏文件返回 None。"""
    sig = _file_sig(path) or (0, 0)
    source = str(path)
    data = read_source_json(path)
    if data is None:
        # 坏文件也要登记签名：否则它永远不在 sources 里，每次请求都会判定「不同步」
        # 并把注定解析失败的文件再读一遍（抓取写到一半时正好是这种状态）。
        conn.execute("INSERT OR REPLACE INTO sources(kind, path, mtime_ns, size) VALUES(?,?,?,?)",
                     (kind, source, sig[0], sig[1]))
        return None
    rows = []
    for seq, item in enumerate(data.get(array_key) or []):
        if not isinstance(item, dict):
            continue
        rows.append(_row_of(kind, item, source, sig[0], seq))
    # 先删后插：同一个文件重导时旧行必须清掉，否则同 ID 会留两条
    conn.execute("DELETE FROM records WHERE kind = ? AND source = ?", (kind, source))
    if rows:
        conn.executemany(
            "INSERT INTO records(kind, rid, source, mtime_ns, seq, title, com_id_name, "
            "addtime, hold_date, day, detail_len, payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    conn.execute("INSERT OR REPLACE INTO sources(kind, path, mtime_ns, size) VALUES(?,?,?,?)",
                 (kind, source, sig[0], sig[1]))
    return len(rows)


def sync(db_path: Path, plan: dict[str, tuple[str, list[Path]]], *, force: bool = False) -> dict | None:
    """把「有变化的文件」灌进库；返回导入报告，失败返回 None。

    plan: {kind: (数组键名, 文件列表)}——文件列表顺序即「新文件优先」的顺序。
    只重导签名（mtime+size）变化的文件；已消失的文件整份删除，避免残留旧记录。
    """
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(db_path, timeout=30.0) as conn:
            init_db(conn)
            report: dict[str, dict] = {}
            for kind, (array_key, files) in plan.items():
                stored = _stored_sources(conn, kind)
                current: dict[str, tuple[int, int]] = {}
                for p in files:
                    sig = _file_sig(p)
                    if sig is not None:
                        current[str(p)] = sig
                todo = list(files) if force else [Path(p) for p, sig in current.items() if stored.get(p) != sig]
                gone = [p for p in stored if p not in current]
                for p in gone:      # 文件已删：整份移除，否则旧记录会一直留在库里
                    conn.execute("DELETE FROM records WHERE kind = ? AND source = ?", (kind, p))
                    conn.execute("DELETE FROM sources WHERE kind = ? AND path = ?", (kind, p))
                records = 0
                bad: list[str] = []
                for p in todo:
                    n = _import_file(conn, kind, array_key, p)
                    if n is None:
                        bad.append(str(p))
                    else:
                        records += n
                report[kind] = {"files": len(files), "imported": len(todo), "removed": len(gone),
                                "records": records, "bad_files": bad}
            conn.commit()
    except (sqlite3.Error, OSError, ValueError):
        return None
    return report


def import_all(db_path: Path, kinds: dict[str, tuple[str, str]], data_dir: Path) -> dict | None:
    """冷启动全量导入：忽略已有快照，把目录下全部匹配文件重新灌一遍（建库用）。

    kinds 形如 `{"recruit": (glob, "招聘信息")}`（即 repository.KINDS）。
    日常增量同步走 `sync()`；只有「库还不存在」时才用这个函数——
    脚本 scripts/migrate_to_sqlite.py 与 repository 的冷启动共用它，避免两份导入逻辑。
    """
    plan = {kind: (key, iter_source_files(pat, data_dir)) for kind, (pat, key) in kinds.items()}
    return sync(db_path, plan, force=True)


def is_synced(db_path: Path, kind: str, files: list[Path]) -> bool:
    """库里该类型的文件快照是否与磁盘一致；库不可用也按「不同步」处理（交给 sync 兜底）。"""
    try:
        with _connect(db_path) as conn:
            stored = _stored_sources(conn, kind)
    except (sqlite3.Error, OSError, ValueError):
        return False
    current: dict[str, tuple[int, int]] = {}
    for p in files:
        sig = _file_sig(p)
        if sig is not None:
            current[str(p)] = sig
    return stored == current


# ---------------------------------------------------------------- 查询

def load_items(db_path: Path, kind: str) -> list[dict] | None:
    """合并去重后的原始记录；库不可用返回 None（调用方回落 JSON）。"""
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(_ITEMS_SQL, {"kind": kind}).fetchall()
        return [json.loads(r[0]) for r in rows]
    except (sqlite3.Error, OSError, ValueError):
        return None


def stats(db_path: Path, kind: str) -> dict | None:
    """单一类型的统计（记录数 / 覆盖起止日期 / 详情缺失数）；库不可用返回 None。

    master_summary 每秒都可能被调用，这里刻意不反序列化 payload，
    只走索引上的 COUNT / MIN / MAX。
    """
    try:
        with _connect(db_path) as conn:
            row = conn.execute(_STATS_SQL, {"kind": kind}).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None
    if not row:
        return None
    return {"records": int(row[0] or 0), "missing_detail": int(row[1] or 0),
            "first_date": row[2] or "", "last_date": row[3] or ""}
