"""Markdown 分析报告导入解析测试（表头兼容、其它表格忽略、模板往返）。"""

from __future__ import annotations

import server

CANONICAL = """# 企业性质与工作地点分析报告

- 生成时间：2026-09-09 15:00
- 数据来源：合并 3 个招聘信息文件
- 分析模型：deepseek-chat

## 一、类型分布

| 类型 | 数量 |
|---|---|
| 央企 | 1 |
| 民企 | 1 |

## 二、全部企业明细

| 企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据 |
|---|---|---|---|---|---|
| 中国建筑第三工程局 | 央企 | 是 | 高 | 武汉、深圳 | 央企子公司，总部武汉 |
| 某科技公司 | 民企 | 否 | 中 | 北京 | 民营互联网企业，总部北京 |
"""

VARIANT = """> ⚠️ 使用提示：本文件由联网核实后重新导出

# 企业分析报告（核实修正版）

- 关键词：校园招聘
- 核实方式：官网对照

## 三、全部企业明细（核实修正版）

| 企业名称 | 类型 | 是否国企 | 置信度 | 工作地点 | 判断依据 |
|---|---|---|---|---|---|
| 中证股转科技 | 国企子公司 | 是 | 高 | 北京 | 【核查修正：股转公司全资子公司】 |
| 长江存储 | 混合所有制 | 否 | 中 | 武汉 | 【核查修正：国资参股但非国有控股】 |
| 某无地点单位 | 其他 | - | 低 | - | 未获取到工作地 |
"""


def test_parse_canonical_report():
    parsed = server.parse_report_md(CANONICAL)
    entries = parsed["entries"]
    assert set(entries) == {"中国建筑第三工程局", "某科技公司"}
    zj = entries["中国建筑第三工程局"]
    assert zj["company_type"] == "央企"
    assert zj["is_state_owned"] is True
    assert zj["confidence"] == "高"
    assert zj["locations"] == ["武汉", "深圳"]
    assert "央企子公司" in zj["evidence"]
    assert parsed["bad_rows"] == 0


def test_type_distribution_table_is_ignored():
    """「类型分布」表格（不含 企业名称/工作地点 列）不会被当成企业明细。"""
    entries = server.parse_report_md(CANONICAL)["entries"]
    assert "央企" not in entries and "民企" not in entries
    assert all(name for name in entries)


def test_parse_variant_headers_and_annotated_report():
    """表头用「是否国企 / 判断依据」、标题被改写、依据带【核查修正】也能解析。"""
    parsed = server.parse_report_md(VARIANT)
    entries = parsed["entries"]
    assert set(entries) == {"中证股转科技", "长江存储", "某无地点单位"}
    assert entries["中证股转科技"]["is_state_owned"] is True
    assert entries["中证股转科技"]["company_type"] == "国企子公司"
    assert entries["长江存储"]["is_state_owned"] is False
    assert entries["长江存储"]["locations"] == ["武汉"]
    assert entries["某无地点单位"]["is_state_owned"] is None      # "-" → 未知
    assert entries["某无地点单位"]["locations"] == []              # "-" → 空列表
    assert "核查修正" in entries["中证股转科技"]["evidence"]
    assert parsed["meta"].get("核实方式") == "官网对照"


def test_comma_and_slash_separated_locations():
    md = """| 企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据 |
|---|---|---|---|---|---|
| 甲 | 央企 | 是 | 高 | 武汉, 深圳、成都 | 多基地 |
"""
    entries = server.parse_report_md(md)["entries"]
    assert entries["甲"]["locations"] == ["武汉", "深圳", "成都"]


def test_import_template_roundtrip():
    """下载的模板本身可以被导入（保证模板与解析器一致）。"""
    parsed = server.parse_report_md(server._IMPORT_TEMPLATE)
    assert len(parsed["entries"]) == 2
    assert parsed["entries"]["中国建筑第三工程局"]["is_state_owned"] is True


def test_empty_or_garbage_input():
    assert server.parse_report_md("")["entries"] == {}
    assert server.parse_report_md("随便一段文字\n没有表格\n")["entries"] == {}
