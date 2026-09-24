#!/usr/bin/env python3
"""导出：收藏宣讲会 Excel / CSV、宣讲会 ICS 日历。

与 HTTP 层解耦：这里只产出字节流，不碰 request / response / send_file，
路由层负责把字节流包装成下载响应。因此导出逻辑可脱离 Web 层直接测试。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone

PREACH_FAV_HEADERS = ["宣讲时间", "举办日期", "单位名称", "宣讲会地点", "城市",
                      "线下/线上", "标题", "公司地点（工作地）", "链接"]


def preach_fav_row(r: dict) -> list[str]:
    """宣讲会记录 → 导出行（与 PREACH_FAV_HEADERS 顺序一致）。"""
    return [r.get("宣讲时间", ""), r.get("举办日期", ""), r.get("单位名称", ""),
            r.get("宣讲会地点", ""), r.get("城市", ""), r.get("线下/线上", ""),
            r.get("标题", ""), r.get("公司地点", ""), r.get("原网页", "")]


def build_preach_favs_xlsx(rows: list[dict]) -> io.BytesIO:
    """收藏宣讲会 → xlsx 工作簿字节流（openpyxl 不可用时抛出，由调用方降级 CSV）。"""
    from openpyxl import Workbook  # noqa: PLC0415  可选依赖，按需导入
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415

    wb = Workbook()
    ws = wb.active
    ws.title = "收藏宣讲会"
    header_fill = PatternFill("solid", fgColor="4F8EF7")
    header_font = Font(bold=True, color="FFFFFF")
    ws.append(PREACH_FAV_HEADERS)
    for c in ws[1]:
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"
    for r in rows:
        ws.append(preach_fav_row(r))
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col) + 4
        ws.column_dimensions[col[0].column_letter].width = min(max(width, 10), 60)
    for row in ws.iter_rows(min_row=2):
        cell = row[8]                                   # 「原网页」列设为超链接
        if cell.value:
            try:
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"
            except (ValueError, AttributeError):
                pass
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


def build_preach_favs_csv(rows: list[dict]) -> io.BytesIO:
    """收藏宣讲会 → 带 BOM 的 CSV 字节流（openpyxl 不可用时的降级方案）。"""
    buf = io.StringIO()
    buf.write("\ufeff")                                 # UTF-8 BOM，Excel 打开不乱码
    w = csv.writer(buf)
    w.writerow(PREACH_FAV_HEADERS)
    for r in rows:
        w.writerow(preach_fav_row(r))
    bio = io.BytesIO(buf.getvalue().encode("utf-8"))
    bio.seek(0)
    return bio


# ------------------------------------------------------------------ ICS 日历

def ics_escape(value: str) -> str:
    """转义 ICS 文本值中的反斜杠 / 分号 / 逗号。"""
    return (str(value)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,"))


def ics_fold(line: str, limit: int = 74) -> str:
    """ICS 行按 75 字节（含换行）上限折行，续行以空格开头。"""
    if len(line.encode("utf-8")) <= limit:
        return line
    out: list[str] = []
    cur = ""
    cur_bytes = 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if cur_bytes + b > limit:
            out.append(cur)
            cur = " " + ch
            cur_bytes = 1 + b
        else:
            cur += ch
            cur_bytes += b
    if cur:
        out.append(cur)
    return "\r\n".join(out)


def preach_ics_event(r: dict) -> list[str]:
    """把一场宣讲会转换成 VEVENT 的各行（未折行）。"""
    name = str(r.get("单位名称", "")).strip()
    title = str(r.get("标题", "")).strip()
    summary = name if not title or title == name else f"{name}（{title}）"
    location = str(r.get("宣讲会地点", "")).strip()
    url = str(r.get("原网页", "")).strip()
    online = str(r.get("线下/线上", "")).strip()

    hold_date = str(r.get("举办日期", "")).strip()
    start_time = str(r.get("开始时间", "")).strip()
    end_time = str(r.get("结束时间", "")).strip()

    # 事件时间：默认用当地时间（浮动时间），无具体时刻则以全天事件呈现
    try:
        if hold_date and start_time:
            start_dt = datetime.strptime(f"{hold_date} {start_time}", "%Y-%m-%d %H:%M")
            if end_time:
                end_dt = datetime.strptime(f"{hold_date} {end_time}", "%Y-%m-%d %H:%M")
                if end_dt <= start_dt:
                    end_dt = start_dt + timedelta(hours=1)
            else:
                end_dt = start_dt + timedelta(hours=1)
            dtstart = f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}"
            dtend = f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}"
        else:
            d = hold_date.replace("-", "") or "19700101"
            dtstart = f"DTSTART;VALUE=DATE:{d}"
            dtend = f"DTEND;VALUE=DATE:{d}"
    except ValueError:
        d = hold_date.replace("-", "") or "19700101"
        dtstart = f"DTSTART;VALUE=DATE:{d}"
        dtend = f"DTEND;VALUE=DATE:{d}"

    uid_base = str(r.get("ID", "")) or url or name
    uid = re.sub(r"[^A-Za-z0-9._-]", "-", uid_base) + "-whutrecruit"
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    desc_parts = []
    if title and title != name:
        desc_parts.append(f"宣讲标题：{title}")
    if title is not None and title == name:
        desc_parts.append(f"单位名称：{name}")
    if location:
        desc_parts.append(f"地点：{location}")
    if online:
        desc_parts.append(f"形式：{online}")
    if url:
        desc_parts.append(f"链接：{url}")
    description = "；".join(desc_parts) or summary

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        dtstart,
        dtend,
        f"SUMMARY:{ics_escape(summary)}",
        f"DESCRIPTION:{ics_escape(description)}",
    ]
    if location:
        lines.append(f"LOCATION:{ics_escape(location)}")
    if url:
        lines.append(f"URL:{url}")
    lines.append("TRANSP:OPAQUE")
    lines.append("END:VEVENT")
    return lines


def build_preach_ics(rows: list[dict]) -> bytes:
    """宣讲会列表 → ICS 日历文件内容（UTF-8 字节，CRLF 换行）。"""
    blocks = [
        ["BEGIN:VCALENDAR"],
        ["VERSION:2.0"],
        ["PRODID:-//WHUT Recruit Tool//Preach Favorites//ZH"],
        ["CALSCALE:GREGORIAN"],
        ["METHOD:PUBLISH"],
        ["X-WR-CALNAME:WHUT 宣讲会收藏"],
    ]
    for r in rows:
        blocks.append(preach_ics_event(r))
    blocks.append(["END:VCALENDAR"])
    body_lines = [folded for block in blocks for folded in (ics_fold(line) for line in block)]
    return ("\r\n".join(body_lines) + "\r\n").encode("utf-8")
