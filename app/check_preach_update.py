#!/usr/bin/env python3
"""每日宣讲会更新检查：抓取今年宣讲会列表，对比已有原始数据，输出「新增场次」并更新写盘。

用法：
    python check_preach_update.py             # 检查并更新今年的线下宣讲会
    python check_preach_update.py --all-types # 同时含线上宣讲会
    python check_preach_update.py --input X   # 指定已有原始数据 JSON

行为：
    1. 读取已有《宣讲会_<年份>年_原始数据.json》（默认取最新），记下已有场次 ID；
    2. 抓取网站今年宣讲会列表；
    3. 新增 = 网站有、已有无 的场次（按 ID）；补拉详情并合并写盘；
    4. 同时刷新《宣讲会_<年份>年线下.csv》与《宣讲会_工作地流动.csv》（补充新企业的工作地推断）；
    5. 把新增场次明细打印到 stdout，供定时任务/界面读取。

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
from crawler import WhutClient, list_preach_year, preach_row

ROOT = Path(__file__).resolve().parent.parent
YEAR = datetime.now().year


def load_existing(path: Path) -> tuple[list[dict], set[str]]:
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


def refresh_work_flow(merged_items: list[dict]) -> int:
    """补充《宣讲会_工作地流动.csv》中缺失企业的工作地推断，保持 Web 端「公司地点」新鲜。返回新增企业数。"""
    import analyze_preach
    flow_path = ROOT / "宣讲会_工作地流动.csv"
    seen: dict[str, str] = {}
    if flow_path.exists():
        try:
            for row in csv.DictReader(open(flow_path, encoding="utf-8-sig")):
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all-types", action="store_true", help="同时抓线上宣讲会")
    parser.add_argument("--input", default="", help="已有宣讲会原始数据 JSON（默认自动找最新）")
    args = parser.parse_args()

    if args.input:
        raw_path = Path(args.input)
    else:
        candidates = sorted(ROOT.glob("宣讲会_*_原始数据.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        raw_path = candidates[0] if candidates else ROOT / f"宣讲会_{YEAR}年_原始数据.json"

    existing_items, existing_ids = load_existing(raw_path)
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
        raw_path.write_text(json.dumps({"宣讲会": merged_items}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        rows = [preach_row(i) for i in merged_items]
        csv_path = ROOT / f"宣讲会_{YEAR}年线下.csv"
        if rows:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        # 3) 新增场次明细
        print("[5] 新增场次明细：")
        for i in new_items:
            r = preach_row(i)
            print("  · " + " | ".join(x for x in [
                r.get("宣讲时间", ""), r.get("单位名称", ""), r.get("宣讲会地点", ""),
                r.get("线下/线上", ""), r.get("原网页", ""),
            ] if x))
        added = refresh_work_flow(merged_items)
        print(f"[6] 已更新 {raw_path.name}（{len(merged_items)} 场）、{csv_path.name}（{len(rows)} 条）；"
              f"工作地映射新增 {added} 家企业")
    else:
        print(f"[5] 数据无变化，未写入；当前 {raw_path.name} 仍为 {len(existing_items)} 场")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户已中止。", file=sys.stderr)
        raise SystemExit(130)
