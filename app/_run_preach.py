#!/usr/bin/env python3
"""聚焦抓取：今年的线下宣讲会（单位名称/链接/宣讲会地点/时间），并补详情写盘。

输出到项目根目录(D:\\111)：
    宣讲会_2026年线下.csv
    宣讲会_2026年_原始数据.json
"""
import json
import time
from collections import Counter
from pathlib import Path

import crawler
from crawler import WhutClient, preach_row, list_preach_year

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
YEAR = 2026

client = WhutClient()
print(f"抓取 {YEAR} 年线下宣讲会列表……")
items = list_preach_year(client, YEAR, offline_only=True)
print(f"共 {len(items)} 条，开始补详情……")
crawler.enrich(client, items, "/preach/detail")

# 排序：先按日期，再按开始时间
items.sort(key=lambda x: (str(x.get("hold_date", "")), str(x.get("hold_starttime", ""))))

rows = [preach_row(i) for i in items]
print(f"单位去重前 {len(rows)} 条；去重后单位数 {len({r['单位名称'] for r in rows if r['单位名称']})}")

# 写 CSV（utf-8-sig，Excel 打开不乱码）
csv_path = DATA / "宣讲会_2026年线下.csv"
fields = list(rows[0])
import csv as _csv
with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
    w = _csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)

# 写原始数据（分析用）
raw_path = DATA / "宣讲会_2026年_原始数据.json"
raw_path.write_text(json.dumps({"宣讲会": items}, ensure_ascii=False, indent=2), encoding="utf-8")

# 统计
cities = Counter(r["城市"] for r in rows if r["城市"])
venues = Counter(r["宣讲会地点"] for r in rows if r["宣讲会地点"])
print(f"\n输出：{csv_path.name}（{len(rows)} 条）、{raw_path.name}")
print("\n=== 宣讲城市分布 ===")
for city, n in cities.most_common():
    print(f"  {city}: {n}")
print("\n=== 场馆分布（前 15）===")
for v, n in venues.most_common(15):
    print(f"  {v}: {n}")
print(f"\n数据缺失校验：有单位 {sum(1 for r in rows if r['单位名称'])}，有地点 {sum(1 for r in rows if r['宣讲会地点'])}，有链接 {sum(1 for r in rows if r['原网页'])}")
