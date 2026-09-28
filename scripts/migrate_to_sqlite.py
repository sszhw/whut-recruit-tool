#!/usr/bin/env python3
"""一次性把 `data/*_原始数据.json` 全量灌进 SQLite。

**只导入，不删除、不改写任何现有 JSON** —— 它们既是回退路径，也是备份。
导入完成后默认再做一次「JSON 口径 vs SQLite 口径」比对，两边记录数与 ID 集合不一致就报错退出。

用法（在项目根目录执行）：
    python scripts/migrate_to_sqlite.py                  # 导入 + 校验
    python scripts/migrate_to_sqlite.py --no-verify      # 只要导入
    python scripts/migrate_to_sqlite.py --data-dir DIR   # 指定数据目录（默认 repository.DATA）
    python scripts/migrate_to_sqlite.py --db data/x.db   # 指定库路径（默认 <数据目录>/whut_data.db）

日常抓取之后无需手工再跑：`repository` 发现库与文件不同步时会就地增量同步。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import repository as repo  # noqa: E402
import sqlite_store  # noqa: E402


def _ids(kind: str) -> list[str]:
    return sorted(str(i.get("id", "") or "") for i in repo.raw_items(kind))


def _snapshot() -> dict[str, tuple[int, list[str]]]:
    repo.invalidate()
    return {kind: (len(repo.raw_items(kind)), _ids(kind)) for kind in repo.KINDS}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把原始 JSON 主数据导入 SQLite")
    ap.add_argument("--data-dir", default=str(repo.DATA), help="数据目录（默认 repository.DATA）")
    ap.add_argument("--db", default="", help="库文件路径（默认 <数据目录>/whut_data.db）")
    ap.add_argument("--no-verify", action="store_true", help="导入后不做 JSON / SQLite 口径比对")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    repo.DATA = data_dir
    db_path = Path(args.db).resolve() if args.db else sqlite_store.db_path_for(data_dir)
    if args.db:
        # 校验阶段 repository 自己会按 db_path_for() 找库，显式路径必须同步给它，
        # 否则会比到一个不存在的默认库、静默回落 JSON，校验就变成了自己跟自己比。
        os.environ["WHUT_SQLITE_PATH"] = str(db_path)

    print(f"数据目录：{data_dir}")
    print(f"库文件  ：{db_path}")
    print(f"开关    ：WHUT_STORE={sqlite_store.store_mode() or 'sqlite'}（默认 sqlite）")

    t0 = time.perf_counter()
    report = sqlite_store.import_all(db_path, repo.KINDS, data_dir)   # 与冷启动共用同一份导入逻辑
    elapsed = time.perf_counter() - t0
    if report is None:
        print("导入失败：无法写入 SQLite（磁盘满 / 权限 / 文件被占用？），现有 JSON 不受影响。")
        return 1

    bad_all: list[str] = []
    for kind, r in report.items():
        print(f"  {kind:<8} 文件 {r['files']} 个，导入 {r['imported']} 个，"
              f"记录 {r['records']} 条，移除失效来源 {r['removed']} 个")
        for p in r["bad_files"]:
            bad_all.append(p)
            print(f"       [跳过坏文件] {p}")
    size_mb = db_path.stat().st_size / 1024 / 1024 if db_path.exists() else 0.0
    print(f"导入耗时 {elapsed:.2f}s，库大小 {size_mb:.1f}MB")

    if bad_all:
        print(f"警告：{len(bad_all)} 个文件无法解析已跳过（半截 JSON / 非 UTF-8 / 空文件），"
              f"其余文件已正常入库。")

    if args.no_verify:
        return 0

    # 校验：先把开关拨到 json 取一份「老口径」快照，再拨回 sqlite，逐类型比对记录数与 ID 集合。
    os.environ["WHUT_STORE"] = "json"
    json_side = _snapshot()
    os.environ["WHUT_STORE"] = "sqlite"
    repo.invalidate()
    sqlite_side = _snapshot()

    ok = True
    for kind in repo.KINDS:
        j_count, j_ids = json_side[kind]
        s_count, s_ids = sqlite_side[kind]
        if j_count != s_count or j_ids != s_ids:
            ok = False
            print(f"  [口径不一致] {kind}: JSON {j_count} 条 / SQLite {s_count} 条")
            diff = set(j_ids) ^ set(s_ids)
            print(f"       差异 ID 示例：{sorted(diff)[:5]}")
        else:
            print(f"  [一致] {kind}: {s_count} 条")
    print("校验通过：SQLite 与 JSON 口径完全一致。" if ok else "校验未通过：请勿切换开关，排查后再导入。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
