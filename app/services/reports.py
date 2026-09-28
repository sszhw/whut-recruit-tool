"""Markdown 分析报告的解析与反向导入。

报告导入的意义：允许用户在别处生成分析结果后回填成本地缓存，
避免必须联网跑一遍 LLM 才能继续使用企业筛选与推荐。

`parse_report_md` 是纯文本解析（无 IO），因此可以在测试里直接喂字符串断言；
`import_report` 才负责落盘与重建 CSV / MD 报告。
"""

from __future__ import annotations

import re

import analyze
from dataloaders import load_cache
from settings import CACHE_PATH, DATA, get_llm

from services import ServiceError

# 导入模板：让用户按「全部企业明细」表格格式手写/复用导出报告
IMPORT_TEMPLATE = "# 企业性质与工作地点分析报告\n" + \
                  "\n" + \
                  "- 生成时间：2026-09-09 15:00\n" + \
                  "- 数据来源：自定义导入\n" + \
                  "- 分析模型：自定义\n" + \
                  "- 企业总数：2\n" + \
                  "\n" + \
                  "## 全部企业明细\n" + \
                  "\n" + \
                  "| 企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据 |\n" + \
                  "|---|---|---|---|---|---|\n" + \
                  "| 中国建筑第三工程局 | 央企 | 是 | 高 | 武汉、深圳 | 央企子公司，总部武汉 |\n" + \
                  "| 某科技公司 | 民企 | 否 | 中 | 北京 | 民营互联网企业，总部北京 |\n"


def md_cell(cols: dict, cells: list[str], key: str, default: str = "") -> str:
    """按表头列索引从 Markdown 表格行中取值。

    原实现是在循环体内定义闭包 g()，既每轮重建函数对象，又会把 cols / cells
    变成延迟绑定的闭包变量（ruff B023）。改为纯函数后行为一致且可单测。
    """
    i = cols.get(key) if cols else None
    return cells[i].strip() if (i is not None and i < len(cells)) else default


def parse_report_md(text: str) -> dict:
    """解析导入的 Markdown 报告，从「全部企业明细」表格重建企业分析缓存。

    返回 {"entries": {企业名: 记录}, "meta": {元信息}, "bad_rows": 忽略行数}。
    列顺序（表头自动识别，兼容导出报告的「企业名称|类型|国企|置信度|工作地点|依据」）：
      企业名称、类型、国企、置信度、工作地点、依据
    """
    entries: dict[str, dict] = {}
    meta: dict[str, str] = {}
    cols = None          # 列名 -> 下标
    in_detail = False
    bad = 0
    for raw in text.splitlines():
        line = raw.strip()
        # 元信息行：- 生成时间：xxx / - 数据来源：xxx 等
        m = re.match(r"^-\s*([^：:]+?)[：:]\s*(.*)$", line)
        if m and not line.startswith("|"):
            meta[m.group(1).strip()] = m.group(2).strip()
            continue
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # 跳过分隔行 |---|
        if cells and all(set(c) <= set("-: ") for c in cells if c):
            continue
        if not in_detail:
            joined = "".join(cells)
            if ("企业名称" in joined and ("国企" in joined or "是否国企" in joined)
                    and "工作地点" in joined):
                cols = {}
                for i, h in enumerate(cells):
                    if h == "企业名称":
                        cols["name"] = i
                    elif h in ("国企", "是否国企"):
                        cols["so"] = i
                    elif "类型" in h:
                        cols["type"] = i
                    elif "置信度" in h:
                        cols["conf"] = i
                    elif "工作地点" in h:
                        cols["loc"] = i
                    elif h in ("依据", "判断依据"):
                        cols["evidence"] = i
                in_detail = True
            continue

        name = md_cell(cols, cells, "name")
        if not name or name in ("企业名称",):
            continue
        so_raw = md_cell(cols, cells, "so")
        so = True if so_raw == "是" else (False if so_raw == "否" else None)
        loc_str = md_cell(cols, cells, "loc")
        locs = [c.strip() for c in loc_str.replace("、", ",").split(",") if c.strip()] if loc_str and loc_str != "-" else []  # noqa: E501
        entries[name] = {
            "company_type": md_cell(cols, cells, "type"),
            "is_state_owned": so,
            "confidence": md_cell(cols, cells, "conf"),
            "locations": locs,
            "evidence": md_cell(cols, cells, "evidence"),
            "_raw": "",
        }
    return {"entries": entries, "meta": meta, "bad_rows": bad}


def import_report(text: str) -> dict:
    """导入报告文本 → 合并进企业分析缓存 → 重建 CSV / MD 报告。

    返回 {"imported": 本次导入条数, "total": 缓存总数, "bad_rows": 忽略行数}。
    报告重建失败不影响导入结果（缓存已经落盘，重新导出即可）。
    """
    text = (text or "").strip()
    if not text:
        raise ServiceError("请选择或粘贴要导入的 Markdown 报告")
    parsed = parse_report_md(text)
    if not parsed["entries"]:
        raise ServiceError("未识别到「全部企业明细」表格。请按模板格式："
                           "企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据")

    cache = load_cache()
    cache.update(parsed["entries"])
    analyze.save_cache(CACHE_PATH, cache)
    # 用缓存重建 CSV / Markdown，让导出与统计保持一致
    model = parsed["meta"].get("分析模型", "") or get_llm()["model"]
    src = parsed["meta"].get("数据来源", "") or "导入的分析报告"
    try:
        analyze.build_outputs(DATA, [{"name": n} for n in cache], cache, model, src)
    except Exception:  # noqa: BLE001  缓存已更新，报告重建失败不影响导入结果
        pass
    return {"imported": len(parsed["entries"]), "total": len(cache), "bad_rows": parsed["bad_rows"]}
