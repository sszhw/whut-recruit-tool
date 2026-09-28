"""导出（CSV / Markdown / Excel / ICS）与 Markdown 报告反向导入。

报告导入的意义：允许用户在别处生成分析结果后回填成本地缓存，
避免必须联网跑一遍 LLM 才能继续使用企业筛选与推荐。
"""

from __future__ import annotations

import glob
import io
import os
import re
from datetime import datetime
from pathlib import Path

import analyze
import exports
import repository as repo
from dataloaders import load_cache, load_preach_favs, load_preachs
from flask import Blueprint, jsonify, request, send_file
from settings import CACHE_PATH, DATA, get_llm

bp = Blueprint("exports_rt", __name__)

# 导入模板：让用户按「全部企业明细」表格格式手写/复用导出报告
_IMPORT_TEMPLATE = "# 企业性质与工作地点分析报告\n" + \
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


def _md_cell(cols: dict, cells: list[str], key: str, default: str = "") -> str:
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

        name = _md_cell(cols, cells, "name")
        if not name or name in ("企业名称",):
            continue
        so_raw = _md_cell(cols, cells, "so")
        so = True if so_raw == "是" else (False if so_raw == "否" else None)
        loc_str = _md_cell(cols, cells, "loc")
        locs = [c.strip() for c in loc_str.replace("、", ",").split(",") if c.strip()] if loc_str and loc_str != "-" else []  # noqa: E501
        entries[name] = {
            "company_type": _md_cell(cols, cells, "type"),
            "is_state_owned": so,
            "confidence": _md_cell(cols, cells, "conf"),
            "locations": locs,
            "evidence": _md_cell(cols, cells, "evidence"),
            "_raw": "",
        }
    return {"entries": entries, "meta": meta, "bad_rows": bad}


@bp.route("/api/export/<kind>")
def api_export(kind):
    if kind == "csv":
        path = DATA / analyze.CSV_NAME
        mime = "text/csv"
    elif kind == "md":
        path = DATA / analyze.MD_NAME
        mime = "text/markdown"
    else:
        return jsonify({"ok": False, "error": "未知导出类型"}), 400
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在，请先运行分析"}), 404
    return send_file(path, mimetype=mime + "; charset=utf-8", as_attachment=True, download_name=path.name)


@bp.route("/api/recruitments/export")
def api_export_recruitments():
    # 原先用「有没有最新原始数据文件」兜底；改成直接问主库有没有记录，
    # 避免在没有当天快照、主库仍有历史数据时误报 404。
    if not repo.master_summary()["recruit_count"]:
        return jsonify({"ok": False, "error": "无数据"}), 404
    csv_path = sorted(glob.glob(str(DATA / "*_招聘信息.csv")), key=os.path.getmtime, reverse=True)
    if csv_path:
        return send_file(csv_path[0], mimetype="text/csv; charset=utf-8", as_attachment=True,
                         download_name=Path(csv_path[0]).name)
    return jsonify({"ok": False, "error": "CSV 不存在"}), 404


@bp.route("/api/import/md/template")
def api_import_md_template():
    """下载一份「导入报告 Markdown」模板供参考。"""
    return send_file(io.BytesIO(_IMPORT_TEMPLATE.encode("utf-8")),
                     mimetype="text/markdown; charset=utf-8", as_attachment=True,
                     download_name="导入报告模板.md")


@bp.route("/api/import/md", methods=["POST"])
def api_import_report():
    """导入 Markdown 分析报告，重建「企业分析」缓存并同步刷新 CSV / MD 报告。"""
    text = ""
    if request.files.get("file"):
        text = request.files["file"].read().decode("utf-8", errors="replace")
    elif request.is_json and request.get_json(silent=True):
        text = (request.get_json(silent=True) or {}).get("text", "")
    text = (text or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请选择或粘贴要导入的 Markdown 报告"}), 400

    parsed = parse_report_md(text)
    if not parsed["entries"]:
        return jsonify({"ok": False, "error": "未识别到「全部企业明细」表格。请按模板格式：企业名称 | 类型 | 国企 | 置信度 | 工作地点 | 依据"}), 400  # noqa: E501

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

    return jsonify({"ok": True, "imported": len(parsed["entries"]),
                    "total": len(cache), "bad_rows": parsed["bad_rows"]})


def _matched_favs_sorted() -> list[dict]:
    """收藏的宣讲会记录（含过去的，按举办日期排序）。导出类接口共用。"""
    favs = load_preach_favs()
    rows = load_preachs(past=True)
    matched = [r for r in rows if str(r.get("ID", "")) in favs]
    matched.sort(key=lambda r: r.get("举办日期") or "")
    return matched


@bp.route("/api/preach/favs/export")
def api_export_preach_favs():
    """导出收藏的宣讲会为 Excel（.xlsx）/ 降级 CSV。"""
    matched = _matched_favs_sorted()
    if not matched:
        return jsonify({"ok": False, "error": "尚无收藏的宣讲会"}), 404
    fmt = datetime.now().strftime("%Y%m%d")
    try:
        bio = exports.build_preach_favs_xlsx(matched)
        return send_file(bio, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"收藏宣讲会_{fmt}.xlsx")
    except Exception:  # noqa: BLE001  openpyxl 缺失或写表失败 → 降级为 CSV
        bio = exports.build_preach_favs_csv(matched)
        return send_file(bio, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name=f"收藏宣讲会_{fmt}.csv")


@bp.route("/api/preach/favs/export-ics")
def api_export_preach_favs_ics():
    """导出收藏的宣讲会为 iCalendar (.ics)，供日历 App 订阅/导入。"""
    matched = _matched_favs_sorted()
    if not matched:
        return jsonify({"ok": False, "error": "尚无收藏的宣讲会"}), 404
    fmt = datetime.now().strftime("%Y%m%d")
    bio = io.BytesIO(exports.build_preach_ics(matched))
    return send_file(bio, mimetype="text/calendar",
                     as_attachment=True, download_name=f"收藏宣讲会_{fmt}.ics")
