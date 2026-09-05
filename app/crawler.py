#!/usr/bin/env python3
"""抓取武汉理工大学就业信息网指定日期后的招聘信息、双选会和宣讲会。

宣讲会( preachMeeting )：
    - 列表端点  /preach/getlist
    - 详情端点  /preach/detail
    - 线下宣讲会 air_type == 0（必有实体宣讲地点）；空中/线上 air_type 为 1/4（无地点）。
    - 链接（无外链时）：https://scc.whut.edu.cn/#/preachMeeting/{id}/1
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://scc.whut.edu.cn/mobile.php"
SITE_URL = "https://scc.whut.edu.cn"
SCHOOL_ID = "b525083d-b83c-4c7e-892f-29909421d961"
SCHOOL_CODE = 10497


def timestamp(value: Any) -> int:
    try:
        number = int(float(value or 0))
        return number // 1000 if number > 10_000_000_000 else number
    except (TypeError, ValueError):
        return 0


def time_text(value: Any, with_time: bool = True) -> str:
    value = timestamp(value)
    if not value:
        return ""
    fmt = "%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d"
    return datetime.fromtimestamp(value).strftime(fmt)


def plain_text(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text).replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


class WhutClient:
    def __init__(self, timeout: int = 30, pause: float = 0.08) -> None:
        self.timeout = timeout
        self.pause = pause
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; WHUT-public-info-archiver/1.0)",
                "Referer": SITE_URL + "/",
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.auth = ""
        self.auth_expiry = 0

    @property
    def common(self) -> dict[str, Any]:
        return {
            "login_user_id": 1,
            "login_admin_school_id": SCHOOL_ID,
            "login_admin_school_code": SCHOOL_CODE,
            "school_id": SCHOOL_ID,
        }

    def refresh_auth(self) -> None:
        response = self.session.get(
            BASE_URL + "/wx/getselock",
            params={k: v for k, v in self.common.items() if k != "school_id"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        if payload.get("code") != 0 or not data.get("lock"):
            raise RuntimeError(f"获取接口访问令牌失败：{payload}")
        self.auth = data["lock"]
        self.auth_expiry = timestamp(data.get("expTime"))

    def post(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        if not self.auth or time.time() + 60 >= self.auth_expiry:
            self.refresh_auth()
        payload = {**data, **self.common}
        for attempt in range(3):
            response = self.session.post(
                BASE_URL + endpoint,
                json=payload,
                headers={"auth": self.auth},
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") == 0:
                time.sleep(self.pause)
                return result
            if "认证" in str(result.get("msg", "")) and attempt < 2:
                self.refresh_auth()
                continue
            raise RuntimeError(f"接口 {endpoint} 返回错误：{result}")
        raise RuntimeError(f"接口 {endpoint} 多次请求失败")

    def list_all(self, endpoint: str, page_size: int) -> list[dict[str, Any]]:
        first = self.post(endpoint, {"page": 1, "size": page_size})["data"]
        items = list(first.get("list") or [])
        pages = int(first.get("allpage") or 1)
        print(f"  {endpoint}: 共 {first.get('count', len(items))} 条，{pages} 页")
        for page in range(2, pages + 1):
            data = self.post(endpoint, {"page": page, "size": page_size})["data"]
            items.extend(data.get("list") or [])
            print(f"\r  已读取 {page}/{pages} 页", end="", flush=True)
        if pages > 1:
            print()
        # 置顶项目可能重复出现，按 id 去重。
        return list({str(item.get("id")): item for item in items}.values())


def enrich(client: WhutClient, items: list[dict[str, Any]], endpoint: str, workers: int = 8) -> None:
    total = len(items)

    def fetch(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | Exception]:
        try:
            data = client.post(endpoint, {"id": item["id"]}).get("data") or {}
            return item, data
        except Exception as exc:  # 单条详情失败不影响其余结果
            return item, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in items]
        for index, future in enumerate(as_completed(futures), 1):
            item, result = future.result()
            if isinstance(result, Exception):
                item["detail_error"] = str(result)
            else:
                item.update(result)
            print(f"\r  已抓取详情 {index}/{total}", end="", flush=True)
    if total:
        print()


def recruitment_row(item: dict[str, Any]) -> dict[str, Any]:
    item_id = item.get("id", "")
    return {
        "发布日期": time_text(item.get("addtime")),
        "今日更新": "是" if time_text(item.get("addtime"), with_time=False)
                      == datetime.now().strftime("%Y-%m-%d") else "",
        "标题": item.get("title", ""),
        "单位": item.get("com_id_name", ""),
        "招聘类型": item.get("enrollment_type", ""),
        "浏览量": item.get("viewcount", ""),
        "发布者": item.get("create_name", ""),
        "原网页": item.get("httpurl") or f"{SITE_URL}/#/recruitmentInformation/notice?type=enrollment&id={item_id}",
        "正文": plain_text(item.get("remarks") or item.get("content")),
        "ID": item_id,
        "抓取异常": item.get("detail_error", ""),
    }


def fair_row(item: dict[str, Any]) -> dict[str, Any]:
    item_id = item.get("id", "")
    return {
        "发布日期": time_text(item.get("addtime")),
        "开始时间": time_text(item.get("start_time")),
        "结束时间": time_text(item.get("end_time")),
        "标题": item.get("title", ""),
        "地点": item.get("field_id_name", ""),
        "报名截止": time_text(item.get("enterend_time")),
        "参会单位数": item.get("verify_count", ""),
        "联系人": item.get("contacts", ""),
        "发布者": item.get("create_name", ""),
        "原网页": f"{SITE_URL}/#/doubleElection/{item_id}",
        "正文": plain_text(item.get("remarks")),
        "ID": item_id,
        "抓取异常": item.get("detail_error", ""),
    }


def _int_or(value: Any, default: int = -1) -> int:
    """安全转 int；None / 空串 / 非数字返回 default。"""
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def preach_row(item: dict[str, Any]) -> dict[str, Any]:
    """宣讲会单条记录。线下/线上由 air_type 区分；地点用 城市+地址 拼出。"""
    item_id = item.get("id", "")
    hold_date = item.get("hold_date", "")
    start = item.get("hold_starttime", "")
    end = item.get("hold_endtime", "")
    meeting_time = f"{hold_date} {start}~{end}".strip(" ~")
    city = item.get("city_id_name", "") or ""
    venue = (item.get("address", "") or "").strip()
    venue_full = "，".join(part for part in (city, venue) if part)
    return {
        "宣讲时间": meeting_time,
        "单位名称": item.get("com_id_name", ""),
        "宣讲会地点": venue_full,
        "城市": city,
        "线下/线上": "线下" if _int_or(item.get("air_type"), 0) == 0 else "线上",
        "标题": item.get("title", ""),
        "浏览量": item.get("viewcount", ""),
        "原网页": item.get("httpurl") or f"{SITE_URL}/#/preachMeeting/{item_id}/1",
        "正文": plain_text(item.get("remarks") or item.get("purpose") or ""),
        "ID": item_id,
        "抓取异常": item.get("detail_error", ""),
    }


def list_preach_year(
    client: WhutClient,
    target_year: int,
    offline_only: bool = True,
    page_size: int = 100,
    max_pages: int = 30,
) -> list[dict[str, Any]]:
    """抓取指定年份的宣讲会。列表按举办日期倒序，越过今年窗口后自动停止。

    offline_only=True 时只保留 air_type==0（线下，有实体地点）。
    """
    result: list[dict[str, Any]] = []
    total_pages = 1
    for page in range(1, max_pages + 1):
        data = client.post("/preach/getlist", {"page": page, "size": page_size})["data"]
        items = list(data.get("list") or [])
        total_pages = int(data.get("allpage") or 1)
        all_past = True  # 整页是否全部早于目标年份（列表按日期倒序，一旦整页都在过去即可停止）

        for item in items:
            try:
                year = int(str(item.get("hold_date", ""))[:4] or 0)
            except ValueError:
                year = 0
            if year < target_year:
                continue  # 早于目标年份，跳过
            all_past = False  # 存在目标年份或更晚的记录，还没越过窗口
            if year != target_year:
                continue  # 未来年份（置顶）不纳入，但不视为"已越过"
            if offline_only and _int_or(item.get("air_type")) != 0:
                continue  # 空中/线上宣讲会，排除
            result.append(item)

        print(f"\r  宣讲会：已读取 {page}/{total_pages} 页，命中 {len(result)} 条", end="", flush=True)
        if all_past:
            break
    if result:
        print()
    # 置顶可能重复，按 id 去重。
    return list({str(item.get("id")): item for item in result}.values())


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["发布日期", "标题"]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def md_escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")


def write_markdown(
    path: Path,
    start_text: str,
    end_text: str,
    recruitments: list[dict[str, Any]],
    fairs: list[dict[str, Any]],
    preachs: list[dict[str, Any]] | None = None,
) -> None:
    lines = [
        "# 武汉理工大学招聘信息与双选会汇总",
        "",
        f"- 数据来源：[{SITE_URL}]({SITE_URL}/#/home)",
        f"- 发布日期范围：{start_text} 至 {end_text}",
        f"- 招聘信息：{len(recruitments)} 条",
        f"- 双选会：{len(fairs)} 条",
        f"- 宣讲会：{len(preachs or [])} 条",
        "",
        "## 招聘信息",
        "",
        "| 序号 | 发布日期 | 标题 | 单位 | 原网页 |",
        "|---:|---|---|---|---|",
    ]
    for index, row in enumerate(recruitments, 1):
        lines.append(
            f"| {index} | {md_escape(row['发布日期'])} | {md_escape(row['标题'])} | "
            f"{md_escape(row['单位'])} | [查看]({row['原网页']}) |"
        )
    lines.extend(["", "## 双选会", "", "| 序号 | 发布日期 | 举办时间 | 标题 | 地点 | 原网页 |", "|---:|---|---|---|---|---|"])
    for index, row in enumerate(fairs, 1):
        period = f"{row['开始时间']} 至 {row['结束时间']}"
        lines.append(
            f"| {index} | {md_escape(row['发布日期'])} | {md_escape(period)} | "
            f"{md_escape(row['标题'])} | {md_escape(row['地点'])} | [查看]({row['原网页']}) |"
        )
    preachs = preachs or []
    if preachs:
        lines.extend(["", "## 宣讲会", "",
                      "| 序号 | 宣讲时间 | 单位名称 | 宣讲会地点 | 线下/线上 | 原网页 |",
                      "|---:|---|---|---|---|---|"])
        for index, row in enumerate(preachs, 1):
            lines.append(
                f"| {index} | {md_escape(row['宣讲时间'])} | {md_escape(row['单位名称'])} | "
                f"{md_escape(row['宣讲会地点'])} | {md_escape(row['线下/线上'])} | [查看]({row['原网页']}) |"
            )
    lines.extend(["", "## 正文详情", ""])
    for kind, rows in (("招聘信息", recruitments), ("双选会", fairs), ("宣讲会", preachs)):
        lines.extend([f"### {kind}", ""])
        for index, row in enumerate(rows, 1):
            lines.extend(
                [
                    f"#### {index}. {row['标题']}",
                    "",
                    f"发布日期：{row.get('发布日期', '')}  ",
                    f"原网页：{row['原网页']}",
                    "",
                    row.get("正文") or "（网页接口未提供正文）",
                    "",
                ]
            )
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def load_existing_ids(output: Path, key: str, pattern: str) -> set[str]:
    """读取已有原始数据文件中某类记录的 id 集合，用于增量爬取去重（跳过已爬取过的记录）。"""
    ids: set[str] = set()
    for path in output.glob(pattern):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get(key, []) or []:
            iid = item.get("id")
            if iid:
                ids.add(str(iid))
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-06-01", help="开始日期，格式 YYYY-MM-DD")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="结束日期，格式 YYYY-MM-DD")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent.parent), help="输出目录")
    parser.add_argument("--page-size", type=int, default=500, help="列表分页大小")
    parser.add_argument("--preach-year", type=int, default=datetime.now().year,
                        help="抓取的宣讲会年份（默认今年）")
    parser.add_argument("--all-types", action="store_true",
                        help="默认只抓线下(air_type==0)宣讲会；加上此参数则同时抓空中/线上宣讲会")
    parser.add_argument("--force", action="store_true",
                        help="默认跳过已爬取过的记录（按 ID 去重）；加上此参数则强制重新抓取全部")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    client = WhutClient()

    if args.force:
        existing_recruit = set()
        existing_fair = set()
        existing_preach = set()
    else:
        existing_recruit = load_existing_ids(output, "招聘信息", "武汉理工大学招聘信息_*_原始数据.json")
        existing_fair = load_existing_ids(output, "双选会", "武汉理工大学招聘信息_*_原始数据.json")
        existing_preach = load_existing_ids(output, "宣讲会", "宣讲会_*_原始数据.json")
        print(f"已存在：招聘信息 {len(existing_recruit)} 条，双选会 {len(existing_fair)} 条，"
              f"宣讲会 {len(existing_preach)} 条（本次将跳过这些 ID）")

    print("读取招聘信息列表……")
    recruitment_all = client.list_all("/enrollment/getlist", args.page_size)
    recruitment = [item for item in recruitment_all
                   if start_ts <= timestamp(item.get("addtime")) <= end_ts
                   and str(item.get("id")) not in existing_recruit]
    recruitment.sort(key=lambda x: timestamp(x.get("addtime")), reverse=True)
    print(f"  日期范围内 {len(recruitment)} 条（已跳过已爬取 {len([i for i in recruitment_all if str(i.get('id')) in existing_recruit])}），开始读取详情……")
    enrich(client, recruitment, "/enrollment/detail")

    print("读取双选会列表……")
    fair_all = client.list_all("/jobfair/getlist", args.page_size)
    fairs = [item for item in fair_all
             if start_ts <= timestamp(item.get("addtime")) <= end_ts
             and str(item.get("id")) not in existing_fair]
    fairs.sort(key=lambda x: timestamp(x.get("addtime")), reverse=True)
    print(f"  日期范围内 {len(fairs)} 条，开始读取详情……")
    enrich(client, fairs, "/jobfair/detail")

    print(f"读取 {args.preach_year} 年宣讲会列表（{'仅线下' if not args.all_types else '线下+线上'}）……")
    preachs = list_preach_year(client, args.preach_year, offline_only=not args.all_types)
    preachs = [item for item in preachs if str(item.get("id")) not in existing_preach]
    preachs.sort(key=lambda x: str(x.get("hold_date", "")) + str(x.get("hold_starttime", "")))
    print(f"  {args.preach_year} 年筛选出 {len(preachs)} 条（已跳过已爬取），开始读取详情……")
    enrich(client, preachs, "/preach/detail")

    recruitment_rows = [recruitment_row(item) for item in recruitment]
    fair_rows = [fair_row(item) for item in fairs]
    preach_rows = [preach_row(item) for item in preachs]
    prefix = f"武汉理工大学招聘信息_{args.start}_至_{args.end}"
    write_csv(output / f"{prefix}_招聘信息.csv", recruitment_rows)
    write_csv(output / f"{prefix}_双选会.csv", fair_rows)
    write_csv(output / f"{prefix}_宣讲会_{args.preach_year}年.csv", preach_rows)
    write_markdown(output / f"{prefix}.md", args.start, args.end, recruitment_rows, fair_rows, preach_rows)
    (output / f"{prefix}_原始数据.json").write_text(
        json.dumps({"招聘信息": recruitment, "双选会": fairs, "宣讲会": preachs},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"完成：招聘信息 {len(recruitment)} 条，双选会 {len(fairs)} 条，宣讲会 {len(preachs)} 条。")
    print(f"输出目录：{output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户已中止。", file=sys.stderr)
        raise SystemExit(130)
