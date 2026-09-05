#!/usr/bin/env python3
"""每日招聘信息/双选会更新检查：抓取网站招聘信息与双选会列表，对比最新
《武汉理工大学招聘信息_*_原始数据.json》中已有 ID，把「新增」条目补详情后
合并写回同一文件（保持 server 端 latest 可读），并刷新 招聘信息/双选会 两个 CSV。

宣讲会由 check_preach_update.py 单独负责。

用法：
    python check_recruit_update.py                    # 今年年初至今，增量
    python check_recruit_update.py --start YYYY-MM-DD # 自指定日期
    python check_recruit_update.py --end   YYYY-MM-DD

退出码：0 = 成功（无论有无新增）；2 = 出错。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import crawler
from crawler import WhutClient, fair_row, recruitment_row

ROOT = Path(__file__).resolve().parent.parent
YEAR = datetime.now().year


def load_existing(path: Path):
    """返回 (已有数据dict, 招聘id集合, 双选会id集合)。"""
    empty = {"招聘信息": [], "双选会": []}
    if not path.exists():
        return empty, set(), set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty, set(), set()
    rec = data.get("招聘信息", []) or []
    fair = data.get("双选会", []) or []
    return ({"招聘信息": rec, "双选会": fair},
            {str(i.get("id")) for i in rec if i.get("id")},
            {str(i.get("id")) for i in fair if i.get("id")})


def _count_recruit(path: Path) -> int:
    """返回某文件里「招聘信息」的条数；读取失败返回 -1。"""
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("招聘信息") or [])
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return -1


def _start_date(path: Path) -> str:
    """取文件名里「起」日期（如 2026-07-01），用于在记录数相同时选出最早的主库。"""
    name = path.name
    for part in name.split("_"):
        if "-" in part and part[:4].isdigit():
            return part
    return ""


def choose_master(start: str, end: str) -> Path:
    """选取合并写回的目标文件：优先「招聘信息」记录数最多的文件（增量去重的主库），
    避免把刚创建的单日小文件误当作主库而导致每日全量重抓。"""
    candidates = list(ROOT.glob("武汉理工大学招聘信息_*_原始数据.json"))
    if not candidates:
        return ROOT / f"武汉理工大学招聘信息_{start}_至_{end}_原始数据.json"
    best = max(candidates, key=lambda p: (_count_recruit(p), _start_date(p)))
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default=f"{YEAR}-01-01", help="开始日期 YYYY-MM-DD（默认今年年初）")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--page-size", type=int, default=500, help="列表分页大小")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())

    raw_path = choose_master(args.start, args.end)
    existing, rec_ids, fair_ids = load_existing(raw_path)
    print(f"[1] 已有数据：{raw_path.name}（招聘 {len(existing['招聘信息'])} 条，双选会 {len(existing['双选会'])} 条）")

    client = WhutClient()
    print(f"[2] 抓取招聘信息/双选会列表（{args.start} ~ {args.end}）……")
    rec_all = client.list_all("/enrollment/getlist", args.page_size)
    fair_all = client.list_all("/jobfair/getlist", args.page_size)

    rec_in_window = [i for i in rec_all if start_ts <= crawler.timestamp(i.get("addtime")) <= end_ts]
    fair_in_window = [i for i in fair_all if start_ts <= crawler.timestamp(i.get("addtime")) <= end_ts]
    rec_new = [i for i in rec_in_window if str(i.get("id")) not in rec_ids]
    fair_new = [i for i in fair_in_window if str(i.get("id")) not in fair_ids]
    print(f"[3] 日期范围内：招聘 {len(rec_in_window)} 条，双选会 {len(fair_in_window)} 条")
    print(f"[4] 新增：招聘 {len(rec_new)} 条，双选会 {len(fair_new)} 条" +
          ("！" if rec_new or fair_new else "（无变化）"))

    if rec_new or fair_new:
        if rec_new:
            rec_new.sort(key=lambda x: crawler.timestamp(x.get("addtime")), reverse=True)
            crawler.enrich(client, rec_new, "/enrollment/detail")
        if fair_new:
            fair_new.sort(key=lambda x: crawler.timestamp(x.get("addtime")), reverse=True)
            crawler.enrich(client, fair_new, "/jobfair/detail")

        merged_rec = {str(i.get("id")): i for i in existing["招聘信息"]}
        merged_fair = {str(i.get("id")): i for i in existing["双选会"]}
        for i in rec_new:
            merged_rec[str(i.get("id"))] = i
        for i in fair_new:
            merged_fair[str(i.get("id"))] = i
        merged_rec_list = sorted(merged_rec.values(),
                                 key=lambda x: crawler.timestamp(x.get("addtime")), reverse=True)
        merged_fair_list = sorted(merged_fair.values(),
                                  key=lambda x: crawler.timestamp(x.get("addtime")), reverse=True)

        # 保留文件里已有的宣讲会等其它键，只更新招聘信息/双选会
        data = json.loads(raw_path.read_text(encoding="utf-8"))
        data["招聘信息"] = merged_rec_list
        data["双选会"] = merged_fair_list
        raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 刷新派生 CSV（含原有全部，供查看/备份）
        r_rows = [recruitment_row(i) for i in merged_rec_list]
        f_rows = [fair_row(i) for i in merged_fair_list]
        if r_rows:
            crawler.write_csv(ROOT / f"武汉理工大学招聘信息_{YEAR}年_招聘信息.csv", r_rows)
        if f_rows:
            crawler.write_csv(ROOT / f"武汉理工大学招聘信息_{YEAR}年_双选会.csv", f_rows)

        print("[5] 新增明细：")
        for i in rec_new:
            r = recruitment_row(i)
            print("  · 招聘 " + " | ".join(x for x in [
                r.get("发布日期", ""), r.get("单位", ""), r.get("标题", ""), r.get("原网页", "")] if x))
        for i in fair_new:
            r = fair_row(i)
            print("  · 双选会 " + " | ".join(x for x in [
                r.get("发布日期", ""), r.get("标题", ""), r.get("地点", "")] if x))
        print(f"[6] 已更新 {raw_path.name}（招聘 {len(merged_rec_list)} 条，双选会 {len(merged_fair_list)} 条）")
    else:
        print("[5] 数据无变化，未写入；当前 {raw_path.name} 招聘 {len(existing['招聘信息'])} 条，"
              f"双选会 {len(existing['双选会'])} 条")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户已中止。", file=sys.stderr)
        raise SystemExit(130)