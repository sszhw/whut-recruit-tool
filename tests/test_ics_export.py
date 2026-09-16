"""iCalendar (.ics) 导出测试：转义、按字节折行、VEVENT 块字段。"""

from __future__ import annotations

import re

import server


def test_ics_escape_special_chars():
    assert server._ics_escape("a,b;c") == "a\\,b\\;c"
    assert server._ics_escape("反斜杠\\与逗号,分号;") == "反斜杠\\\\与逗号\\,分号\\;"
    assert server._ics_escape("普通文本") == "普通文本"


def test_ics_fold_keeps_lines_within_75_bytes_and_unfolds():
    line = "SUMMARY:" + "武汉理工大学宣讲会（东风汽车集团2027届校园招聘专场）" * 6
    folded = server._ics_fold(line)
    physical = folded.split("\r\n")
    assert len(physical) > 1
    for part in physical:
        assert len(part.encode("utf-8")) <= 75, part
    assert all(p.startswith(" ") for p in physical[1:])          # 续行以空格开头
    assert folded.replace("\r\n ", "") == line                   # 还原后与原行一致


def test_ics_fold_leaves_short_line_untouched():
    assert server._ics_fold("UID:abc") == "UID:abc"


def _row(**kw):
    base = {
        "ID": "de37526b-97d4-6c2e-d526-e7e3e0cecde5", "单位名称": "东风汽车集团",
        "标题": "东风汽车2027届校园招聘", "举办日期": "2026-09-20",
        "开始时间": "19:00", "结束时间": "21:00",
        "宣讲会地点": "武汉市，马房山校区，东风厅", "线下/线上": "线下",
        "原网页": "https://scc.whut.edu.cn/#/preachMeeting/x/1",
    }
    base.update(kw)
    return base


def test_event_block_is_list_of_lines_with_required_fields():
    lines = server._preach_ics_event(_row())
    assert isinstance(lines, list)
    assert lines[0] == "BEGIN:VEVENT"
    assert lines[-1] == "END:VEVENT"
    joined = "\n".join(lines)
    assert "BEGIN:VEVENT" in joined and "END:VEVENT" in joined
    assert "DTSTART:20260920T190000" in joined
    assert "DTEND:20260920T210000" in joined
    assert re.search(r"^UID:[A-Za-z0-9._-]+-whutrecruit$", joined, re.M)
    assert "TRANSP:OPAQUE" in joined
    assert "LOCATION:" in joined and "URL:" in joined


def test_summary_escapes_commas_in_company_and_title():
    lines = server._preach_ics_event(_row(标题="机械,结构 专场"))
    summary = next(ln for ln in lines if ln.startswith("SUMMARY:"))
    assert "\\," in summary
    assert "东风汽车集团" in summary


def test_all_day_event_when_no_start_time():
    lines = server._preach_ics_event(_row(开始时间="", 结束时间=""))
    joined = "\n".join(lines)
    assert "DTSTART;VALUE=DATE:20260920" in joined
    assert "DTEND;VALUE=DATE:20260920" in joined


def test_end_before_start_is_normalized_to_one_hour():
    lines = server._preach_ics_event(_row(开始时间="19:00", 结束时间="18:00"))
    joined = "\n".join(lines)
    assert "DTSTART:20260920T190000" in joined
    assert "DTEND:20260920T200000" in joined


def test_invalid_date_falls_back_to_date_value():
    lines = server._preach_ics_event(_row(举办日期="", 开始时间=""))
    joined = "\n".join(lines)
    assert "DTSTART;VALUE=DATE:19700101" in joined


def test_every_physical_line_within_rfc_limit_after_full_export():
    """完整导出时的每一条物理行（含折行）都必须 ≤ 75 字节。"""
    blocks = [["BEGIN:VCALENDAR", "VERSION:2.0"]] + [server._preach_ics_event(_row())] + [["END:VCALENDAR"]]
    body = "".join(server._ics_fold(line) + "\r\n" for block in blocks for line in block)
    for line in body.split("\r\n"):
        if line:
            assert len(line.encode("utf-8")) <= 75, line
