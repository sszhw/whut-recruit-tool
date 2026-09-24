#!/usr/bin/env python3
"""每日增量更新统一入口：抓取网站最新列表，对比本地已有数据，把「新增」记录合并写回主库。

按 --kind 区分两种对象（原先拆在 check_recruit_update.py / check_preach_update.py 两个脚本里，
两者结构一致：读已有 → 抓列表 → 按 ID 对比 → 补详情 → 合并写回 → 刷新派生 CSV，故合并为一个入口）：

    --kind recruit   招聘信息 + 双选会（可按 --start/--end 限定日期窗口）
    --kind preach    宣讲会（含新增企业的工作地流动刷新，--all-types 同时含线上）

用法：
    python check_update.py --kind recruit [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    python check_update.py --kind preach  [--all-types] [--input X.json]

退出码：0 = 成功（无论有无新增）；2 = 出错。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import crawler
from crawler import WhutClient, fair_row, list_preach_year, preach_row, recruitment_row

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
YEAR = datetime.now().year


# ============================================================ 招聘 + 双选会

def _load_recruit_existing(path: Path):
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
    """返回某文件里「招聘信息」条数；读取失败返回 -1。"""
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


def _choose_recruit_master(start: str, end: str) -> Path:
    """选取合并写回的目标文件：优先「招聘信息」记录数最多的文件（增量去重的主库）。"""
    candidates = list(DATA.glob("武汉理工大学招聘信息_*_原始数据.json"))
    if not candidates:
        return DATA / f"武汉理工大学招聘信息_{start}_至_{end}_原始数据.json"
    return max(candidates, key=lambda p: (_count_recruit(p), _start_date(p)))


def _update_recruit(args) -> int:
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())

    raw_path = _choose_recruit_master(args.start, args.end)
    existing, rec_ids, fair_ids = _load_recruit_existing(raw_path)
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

    if not (rec_new or fair_new):
        print(f"[5] 数据无变化，未写入；当前 {raw_path.name} 招聘 {len(existing['招聘信息'])} 条，"
              f"双选会 {len(existing['双选会'])} 条")
        return 0

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
    crawler.write_json(raw_path, data)

    r_rows = [recruitment_row(i) for i in merged_rec_list]
    f_rows = [fair_row(i) for i in merged_fair_list]
    if r_rows:
        crawler.write_csv(DATA / f"武汉理工大学招聘信息_{YEAR}年_招聘信息.csv", r_rows)
    if f_rows:
        crawler.write_csv(DATA / f"武汉理工大学招聘信息_{YEAR}年_双选会.csv", f_rows)

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
    return 0


# ============================================================ 宣讲会

def _load_preach_existing(path: Path) -> tuple[list[dict], set[str]]:
    if not path.exists():
        return [], set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], set()
    items = data.get("宣讲会", [])
    ids = {str(i.get("id")) for i in items if i.get("id")}
    return items, ids


def _company_text(item: dict) -> str:
    """构造与 analyze_preach 一致的企业文本用于离线工作地推断。"""
    parts = [str(item.get("title") or "")]
    parts.append(str(item.get("purpose") or ""))
    if item.get("province_id_name") or item.get("city_id_name") or item.get("address"):
        parts.append("宣讲地点：" + "，".join(
            x for x in [item.get("province_id_name", ""), item.get("city_id_name", ""),
                        item.get("address", "")] if x))
    job = []
    for j in (item.get("JobList") or []):
        if isinstance(j, dict):
            t = j.get("jobname") or j.get("job_name") or j.get("stationname") or j.get("name") or ""
            loc = j.get("city_id_name") or j.get("city_name") or j.get("province_id_name") or ""
            if t:
                job.append(f"{t}@{loc}" if loc else str(t))
    if job:
        parts.append("招聘岗位：" + "；".join(job))
    parts.append(crawler.plain_text(item.get("remarks") or ""))
    return "\n".join(p for p in parts if p)


def _refresh_work_flow(merged_items: list[dict]) -> int:
    """补充《宣讲会_工作地流动.csv》中缺失企业的工作地推断，保持 Web 端「公司地点」新鲜。返回新增企业数。"""
    import analyze_preach
    flow_path = DATA / "宣讲会_工作地流动.csv"
    seen: dict[str, str] = {}
    if flow_path.exists():
        try:
            with flow_path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    name = (row.get("单位名称") or "").strip()
                    if name:
                        seen[name] = row.get("工作地城市", "未确定")
        except (OSError, csv.Error):
            seen = {}
    new_rows = []
    used = set(seen)
    for item in merged_items:
        name = (item.get("com_id_name") or "").strip()
        if not name or name in used:
            continue
        text = _company_text(item)
        cities = analyze_preach.infer_work_cities(name, str(item.get("title") or ""), text)
        new_rows.append({"单位名称": name, "工作地城市": "、".join(cities) if cities else "未确定",
                         "判断依据": "离线：依据企业名称/总部映射推断"})
        used.add(name)
    if not new_rows:
        return 0
    all_rows = ([{"单位名称": n, "工作地城市": c, "判断依据": "离线：依据企业名称/总部映射推断"}
                 for n, c in seen.items()] + new_rows)
    with flow_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["单位名称", "工作地城市", "判断依据"])
        w.writeheader()
        w.writerows(all_rows)
    return len(new_rows)


def _update_preach(args) -> int:
    if args.input:
        raw_path = Path(args.input)
    else:
        candidates = sorted(DATA.glob("宣讲会_*_原始数据.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        raw_path = candidates[0] if candidates else DATA / f"宣讲会_{YEAR}年_原始数据.json"

    existing_items, existing_ids = _load_preach_existing(raw_path)
    print(f"[1] 已有数据：{raw_path.name} 共 {len(existing_items)} 场")

    client = WhutClient()
    print(f"[2] 抓取 {YEAR} 年宣讲会列表（{'仅线下' if not args.all_types else '线下+线上'}）……")
    fetched = list_preach_year(client, YEAR, offline_only=not args.all_types)
    print(f"[3] 网站共 {len(fetched)} 场")

    new_items = [i for i in fetched if str(i.get("id")) not in existing_ids]
    print(f"[4] 新增 {len(new_items)} 场" + ("！" if new_items else "（无变化）"))

    merged_items = existing_items
    if new_items:
        crawler.enrich(client, new_items, "/preach/detail")
        merged = {str(i.get("id")): i for i in existing_items}
        for i in fetched:
            merged[str(i.get("id"))] = i
        merged_items = list(merged.values())
        merged_items.sort(key=lambda x: (str(x.get("hold_date", "")), str(x.get("hold_starttime", ""))))
        crawler.write_json(raw_path, {"宣讲会": merged_items})
        rows = [preach_row(i) for i in merged_items]
        csv_path = DATA / f"宣讲会_{YEAR}年线下.csv"
        if rows:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        print("[5] 新增场次明细：")
        for i in new_items:
            r = preach_row(i)
            print("  · " + " | ".join(x for x in [
                r.get("宣讲时间", ""), r.get("单位名称", ""), r.get("宣讲会地点", ""),
                r.get("线下/线上", ""), r.get("原网页", ""),
            ] if x))
        added = _refresh_work_flow(merged_items)
        print(f"[6] 已更新 {raw_path.name}（{len(merged_items)} 场）、{csv_path.name}（{len(rows)} 条）；"
              f"工作地映射新增 {added} 家企业")
    else:
        print(f"[5] 数据无变化，未写入；当前 {raw_path.name} 仍为 {len(existing_items)} 场")
    return 0


# ============================================================ 入口

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=("recruit", "preach"), required=True,
                        help="更新对象：recruit = 招聘信息 + 双选会；preach = 宣讲会")
    parser.add_argument("--start", default=f"{YEAR}-01-01", help="招聘：开始日期 YYYY-MM-DD（默认今年年初）")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"),
                        help="招聘：结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--page-size", type=int, default=500, help="招聘：列表分页大小")
    parser.add_argument("--all-types", action="store_true", help="宣讲会：同时抓线上宣讲会")
    parser.add_argument("--input", default="", help="宣讲会：指定已有原始数据 JSON（默认自动找最新）")
    args = parser.parse_args()

    if args.kind == "recruit":
        return _update_recruit(args)
    return _update_preach(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户已中止。", file=sys.stderr)
        raise SystemExit(130)
