#!/usr/bin/env python3
"""用硅基流动（SiliconFlow）API 分析招聘信息中的企业：是否国企 + 工作地点。

用法：
    python analyze.py                     # 分析最新的 原始数据.json
    python analyze.py --model Qwen/Qwen2.5-72B-Instruct
    python analyze.py --limit 10          # 只分析前 10 家（试跑）

也可以直接使用 server.py 提供的网页界面运行本分析。

环境变量：
    SILICONFLOW_API_KEY   硅基流动 API 密钥（也可写入 config.json）
    SILICONFLOW_BASE_URL  可选，默认 https://api.siliconflow.cn/v1

输出：
    企业分析_国企与工作地点.csv   逐企业结果
    企业分析报告.md               汇总报告（按企业类型分组）
    企业分析_缓存.json            分析缓存（重复运行自动跳过已分析企业）
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

import crawler  # 复用 plain_text 等工具函数

BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL") or "Qwen/Qwen2.5-72B-Instruct"
CACHE_NAME = "企业分析_缓存.json"
CSV_NAME = "企业分析_国企与工作地点.csv"
MD_NAME = "企业分析报告.md"
PROMPT = """你是中国企业背景分析专家。给你一家企业的名称和它的校园招聘公告节选，请分析并只输出一个 JSON 对象，不要输出任何其他内容。

JSON 格式：
{"company_type":"央企|地方国企|事业单位|国企子公司或分支机构|民企|外企|合资|混合所有制|高校|科研院所|其他|不确定","is_state_owned":true或false,"confidence":"高|中|低","locations":["工作地点城市1","工作地点城市2"],"evidence":"一句话判断依据"}

规则：
1. 央企指国务院国资委/财政部等中央机构监管的企业及其各级子公司（如中国建筑、国家电网、中核集团、中国中铁的下属单位）。
2. 地方国企指地方政府国资委监管的企业及其子公司。
3. 事业单位、高校、科研院所属于体制内但不是企业，is_state_owned 填 false，类型照实填。
4. 银行分行、信用卡中心、分公司、子公司（如"平安银行股份有限公司上海分行"）按其集团性质判断；平安集团是混合所有制民企，其分支机构不是国企。
5. 工作地点从公告正文中提取实际招聘的工作城市（不是公司注册地、不是学校地点）；若正文提到多个城市全部列出；若正文无信息，可根据企业常识推断并在 confidence 中体现；完全无法判断则 locations 为空数组。
6. 城市名写到市级，如"武汉"、"深圳"、"北京"，不要"武汉市"。
7. 不确定时宁可填"不确定"，不要编造。"""


def truncate(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text[:limit]


def _join_joblist(joblist: Any) -> str:
    """把宣讲会详情里的 JobList 列表压成一段「岗位@城市」文本。"""
    if not isinstance(joblist, list):
        return ""
    out = []
    for job in joblist:
        if not isinstance(job, dict):
            continue
        title = job.get("jobname") or job.get("job_name") or job.get("stationname") \
            or job.get("pname") or job.get("name") or job.get("position") or ""
        loc = job.get("city_id_name") or job.get("city_name") or job.get("province_id_name") \
            or job.get("address") or job.get("place") or ""
        title = str(title).strip()
        loc = str(loc).strip()
        if title:
            out.append(f"{title}@{loc}" if loc else title)
    return "；".join(out)


def extract_companies(items: list[dict]) -> list[dict]:
    """按企业名称去重，保留每家最完整的一条公告正文。"""
    by_name: dict[str, dict] = {}
    for item in items:
        name = (item.get("com_id_name") or "").strip()
        if not name:
            continue
        existing = by_name.get(name)
        text = truncate(item.get("content") or item.get("remarks") or "")
        if existing is None or len(text) > len(existing["text"]):
            by_name[name] = {"name": name, "text": text, "title": item.get("title", "")}
    return list(by_name.values())


def extract_preach_companies(items: list[dict]) -> list[dict]:
    """按企业名称去重，从宣讲会记录构造用于分析「工作地」的文本。"""
    by_name: dict[str, dict] = {}
    for item in items:
        name = (item.get("com_id_name") or "").strip()
        if not name:
            continue
        parts = [str(item.get("title") or "")]
        parts.append(str(item.get("purpose") or ""))
        if item.get("province_id_name") or item.get("city_id_name") or item.get("address"):
            parts.append("宣讲地点：" + "，".join(
                x for x in [item.get("province_id_name", ""), item.get("city_id_name", ""),
                            item.get("address", "")] if x))
        job_part = _join_joblist(item.get("JobList"))
        if job_part:
            parts.append("招聘岗位：" + job_part)
        parts.append(crawler.plain_text(item.get("remarks") or ""))
        text = truncate("\n".join(p for p in parts if p), limit=1000)
        existing = by_name.get(name)
        if existing is None or len(text) > len(existing["text"]):
            by_name[name] = {"name": name, "text": text, "title": item.get("title", "")}
    return list(by_name.values())


def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def call_api(api_key: str, model: str, name: str, text: str, max_retries: int = 3) -> dict:
    user = f"企业名称：{name}\n\n招聘公告节选：\n{text if text else '（无正文）'}"
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                BASE_URL + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 400,
                },
                timeout=90,
            )
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [限流] {name}，等待 {wait}s 重试")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            return parse_json(content)
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                return {"company_type": "分析失败", "is_state_owned": None, "confidence": "",
                        "locations": [], "evidence": f"网络错误: {exc}", "_raw": ""}
            time.sleep(3 * (attempt + 1))
    return {"company_type": "分析失败", "is_state_owned": None, "confidence": "",
            "locations": [], "evidence": "多次重试失败", "_raw": ""}


def parse_json(content: str) -> dict:
    raw = content
    match = re.search(r"\{.*\}", content, re.S)
    if match:
        content = match.group(0)
    try:
        data = json.loads(content)
        data["_raw"] = ""
        return data
    except json.JSONDecodeError:
        return {"company_type": "解析失败", "is_state_owned": None, "confidence": "",
                "locations": [], "evidence": "", "_raw": raw[:500]}


def build_outputs(workdir: Path, companies: list[dict], cache: dict, model: str, input_name: str,
                  source: str = "enrollment") -> dict:
    """汇总生成 CSV + Markdown 报告，返回统计信息。source 用于标题说明数据来源。"""
    rows = []
    seen = set()
    for company in companies:
        name = company["name"]
        seen.add(name)
        result = cache.get(name, {})
        rows.append({
            "企业名称": name,
            "企业类型": result.get("company_type", ""),
            "是否国企": {True: "是", False: "否"}.get(result.get("is_state_owned"), "未知"),
            "置信度": result.get("confidence", ""),
            "工作地点": "、".join(result.get("locations") or []),
            "判断依据": result.get("evidence", ""),
        })
    # 缓存中有但本次企业列表没有的（例如换了输入文件），也一并纳入报告
    for name, result in cache.items():
        if name in seen:
            continue
        rows.append({
            "企业名称": name,
            "企业类型": result.get("company_type", ""),
            "是否国企": {True: "是", False: "否"}.get(result.get("is_state_owned"), "未知"),
            "置信度": result.get("confidence", ""),
            "工作地点": "、".join(result.get("locations") or []),
            "判断依据": result.get("evidence", ""),
        })

    csv_path = workdir / CSV_NAME
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    type_counts = Counter(r["企业类型"] for r in rows)
    so_rows = [r for r in rows if r["是否国企"] == "是"]
    loc_counts = Counter(loc.strip() for r in rows for loc in r["工作地点"].split("、") if loc.strip())

    md = ["# 企业性质与" + ("工作地流动" if source == "preach" else "工作地点") + "分析报告", "",
          f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
          f"- 数据来源：{input_name}（企业名取自公告的「单位」字段，已去重）",
          f"- 分析模型：{model}（硅基流动 API）",
          f"- 企业总数：{len(rows)} 家，其中国企/央企系：{len(so_rows)} 家", "",
          "## 企业类型分布", "", "| 类型 | 数量 |", "|---|---:|"]
    for type_name, count in type_counts.most_common():
        md.append(f"| {type_name or '（未分析）'} | {count} |")
    md.extend(["", "## 工作地点分布（前 30）", "", "| 城市 | 企业数 |", "|---|---:|"])
    for loc, count in loc_counts.most_common(30):
        md.append(f"| {loc} | {count} |")
    md.extend(["", "## 全部企业明细", "", "| 企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据 |", "|---|---|---|---|---|---|"])
    for r in rows:
        md.append(f"| {r['企业名称']} | {r['企业类型']} | {r['是否国企']} | {r['置信度']} | "
                  f"{r['工作地点'] or '-'} | {r['判断依据']} |")
    md_path = workdir / MD_NAME
    md_path.write_text("\n".join(md), encoding="utf-8")

    return {"total": len(rows), "state_owned": len(so_rows),
            "csv": str(csv_path), "md": str(md_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="", help="原始数据 JSON 路径（默认自动找最新的）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--limit", type=int, default=0, help="只分析前 N 家企业（0=全部）")
    parser.add_argument("--source", default="enrollment", choices=["enrollment", "preach"],
                        help="分析的数据来源：enrollment=招聘信息(默认)，preach=宣讲会")
    args = parser.parse_args()

    api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        print("错误：未设置环境变量 SILICONFLOW_API_KEY。请到 https://cloud.siliconflow.cn 获取密钥后：", file=sys.stderr)
        print('  PowerShell:  $env:SILICONFLOW_API_KEY="***"', file=sys.stderr)
        return 2

    workdir = Path(__file__).resolve().parent.parent
    if args.input:
        input_path = Path(args.input)
    else:
        # 按数据源选择对应的原始数据文件，避免误取只含"宣讲会"的文件导致读空"招聘信息"。
        if args.source == "preach":
            pattern = "宣讲会_*_原始数据.json"
        else:
            pattern = "武汉理工大学招聘信息_*_原始数据.json"
        candidates = sorted(glob.glob(str(workdir / pattern)), key=os.path.getmtime, reverse=True)
        if not candidates:  # 兜底：任一新旧兼容
            candidates = sorted(glob.glob(str(workdir / "*_原始数据.json")), key=os.path.getmtime, reverse=True)
        if not candidates:
            print("错误：找不到 原始数据.json，请先运行 crawler.py", file=sys.stderr)
            return 2
        input_path = Path(candidates[0])

    print(f"输入文件：{input_path.name}")
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if args.source == "preach":
        companies = extract_preach_companies(data.get("宣讲会", []))
        print(f"来源：宣讲会（工作地流动分析）")
    else:
        companies = extract_companies(data.get("招聘信息", []))
    print(f"去重后待分析企业：{len(companies)} 家")
    if args.limit:
        companies = companies[: args.limit]
        print(f"按 --limit 只分析前 {len(companies)} 家")

    cache_path = workdir / CACHE_NAME
    cache = load_cache(cache_path)
    todo = [c for c in companies if c["name"] not in cache]
    print(f"缓存已有 {len(companies) - len(todo)} 家，本次需分析 {len(todo)} 家")

    total = len(todo)
    for index, company in enumerate(todo, 1):
        result = call_api(api_key, args.model, company["name"], company["text"])
        cache[company["name"]] = result
        if index % 10 == 0 or index == total:
            save_cache(cache_path, cache)
        locs = "、".join(result.get("locations") or []) or "-"
        print(f"  [{index}/{total}] {company['name']} -> {result.get('company_type')} | {locs}")
        time.sleep(0.3)
    save_cache(cache_path, cache)

    stats = build_outputs(workdir, companies, cache, args.model, input_path.name, args.source)
    print(f"\n完成。国企/央企系 {stats['state_owned']}/{stats['total']} 家")
    print(f"输出：{Path(stats['csv']).name}、{Path(stats['md']).name}、{cache_path.name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户已中止，已分析结果保存在缓存中，可重新运行继续。", file=sys.stderr)
