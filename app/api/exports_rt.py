"""导出（CSV / Markdown / Excel / ICS）与 Markdown 报告反向导入 —— HTTP 壳。

文件定位在 `services/exporting.py`，报告解析与落盘在 `services/reports.py`，
这里只负责把取到的东西变成 HTTP 响应（send_file / json）。

报告导入的意义：允许用户在别处生成分析结果后回填成本地缓存，
避免必须联网跑一遍 LLM 才能继续使用企业筛选与推荐。
"""

from __future__ import annotations

import io
from datetime import datetime

import exports
from flask import Blueprint, jsonify, request, send_file
from services import ServiceError, exporting, reports
from services.preaches import matched_favs

bp = Blueprint("exports_rt", __name__)


@bp.route("/api/export/<kind>")
def api_export(kind):
    try:
        path, mime = exporting.resolve_export_file(kind)
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return send_file(path, mimetype=mime + "; charset=utf-8", as_attachment=True,
                     download_name=path.name)


@bp.route("/api/recruitments/export")
def api_export_recruitments():
    try:
        path = exporting.latest_recruit_csv()
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return send_file(path, mimetype="text/csv; charset=utf-8", as_attachment=True,
                     download_name=path.name)


@bp.route("/api/import/md/template")
def api_import_md_template():
    """下载一份「导入报告 Markdown」模板供参考。"""
    return send_file(io.BytesIO(reports.IMPORT_TEMPLATE.encode("utf-8")),
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
    try:
        result = reports.import_report(text)
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return jsonify({"ok": True, **result})


@bp.route("/api/preach/favs/export")
def api_export_preach_favs():
    """导出收藏的宣讲会为 Excel（.xlsx）/ 降级 CSV。"""
    matched = matched_favs()
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
    matched = matched_favs()
    if not matched:
        return jsonify({"ok": False, "error": "尚无收藏的宣讲会"}), 404
    fmt = datetime.now().strftime("%Y%m%d")
    bio = io.BytesIO(exports.build_preach_ics(matched))
    return send_file(bio, mimetype="text/calendar",
                     as_attachment=True, download_name=f"收藏宣讲会_{fmt}.ics")
