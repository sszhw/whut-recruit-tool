"""投递档案 HTTP 接口。

- `GET  /api/profile`              → `{ok, profile, modules}`（`modules` 给模块元信息）
- `POST /api/profile`              → 按模块整体覆盖保存，返回保存后的完整档案
- `GET  /api/profile/render?format=md|txt` → `{ok, text, filename}` 档案全文纯文本
- `GET|POST /api/profile/render.docx`     → 同一份内容的 Word 文档（二进制下载）
- `GET|POST|DELETE /api/profile/photo`    → 个人照片（预览 / 导入 / 删除）

读写与文本生成全在 `services/profile.py`，Word 排版在 `services/resume_hr.py`，
这里只做参数解析与 jsonify。
"""

from __future__ import annotations

from urllib.parse import quote

from flask import Blueprint, jsonify, make_response, request
from services import ServiceError
from services import profile as svc
from services import profile_fill as fill_svc
from services import resume_hr as hr_svc

bp = Blueprint("profile", __name__)


@bp.route("/api/profile")
def api_profile_get():
    """读取当前投递档案；文件缺失/损坏时返回空档案（不会 500）。"""
    return jsonify({
        "ok": True,
        "profile": svc.load_profile(),
        "modules": svc.module_meta(),
        "photo": svc.photo_meta(),
    })


@bp.route("/api/profile", methods=["POST"])
def api_profile_save():
    """保存投递档案（按模块整体覆盖）。"""
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    return jsonify({"ok": True, "profile": svc.save_profile(payload)})


@bp.route("/api/profile/render", methods=["GET", "POST"])
def api_profile_render():
    """生成「档案全文」纯文本；只用于前端预览与 Blob 下载，不落盘。

    GET  渲染已保存的档案；
    POST 渲染请求体里的档案 —— 界面上改了还没保存时用它，
         否则用户点「预览」会拿到旧内容，看着像功能坏了。
    """
    fmt = (request.args.get("format") or "md").strip().lower()
    if fmt not in ("md", "txt"):
        fmt = "md"
    if request.method == "POST":
        payload = request.get_json(force=True, silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        profile = svc.normalize(payload)
    else:
        profile = svc.load_profile()
    return jsonify({
        "ok": True,
        "format": fmt,
        "text": svc.render_resume(profile, fmt),
        "filename": svc.resume_filename(profile, fmt),
    })


@bp.route("/api/profile/render.docx", methods=["GET", "POST"])
def api_profile_render_docx():
    """生成简历 Word 文档并直接下载。

    与 `/api/profile/render` 同一套入参约定：GET 用已保存的档案，
    POST 用请求体里的档案（界面上改了还没保存时也要能导出）。

    `?style=` 选哪一份：

    - `hr`   —— 挑着写的 HR 版：高中不上、绩点排名不放、外语按语种归并；
    - `full` —— **完整版**（默认）：HR 同款排版，但档案里有什么写什么，
      家庭成员、学号、紧急联系人一条不落。

    两份共用同一套版式，区别只在「要不要挑内容」，详见 services/resume_hr.py。
    `plain` 是历史别名（早年那版纯段落、禁表格的「可解析版」），现等价于 `full`。
    """
    style = (request.args.get("style") or "full").strip().lower()
    if style not in ("hr", "full", "plain"):
        style = "full"
    full = style != "hr"

    if request.method == "POST":
        payload = request.get_json(force=True, silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        profile = svc.normalize(payload)
    else:
        profile = svc.load_profile()

    try:
        if full:
            data = hr_svc.render_full_docx(profile, svc.find_photo())
        else:
            data = hr_svc.render_hr_docx(profile, svc.find_photo())
    except ImportError:
        return jsonify({
            "ok": False,
            "error": "缺少 python-docx，无法导出 Word。请先执行 pip install python-docx 后重启服务。",
        }), 503

    filename = svc.resume_filename(profile, "docx")
    filename = filename.replace(".docx", "-完整版.docx" if full else "-HR版.docx")
    resp = make_response(data)
    resp.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # 中文文件名必须走 RFC 5987 的 filename*，否则下载下来是一串乱码
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=\"resume.docx\"; filename*=UTF-8''{quote(filename)}"
    )
    return resp


@bp.route("/api/profile/photo")
def api_profile_photo_get():
    """返回照片二进制供界面预览；没有照片则 404。"""
    path = svc.find_photo()
    if path is None:
        return jsonify({"ok": False, "error": "尚未导入照片"}), 404
    resp = make_response(path.read_bytes())
    resp.headers["Content-Type"] = {
        "png": "image/png", "webp": "image/webp", "bmp": "image/bmp",
    }.get(path.suffix.lstrip(".").lower(), "image/jpeg")
    # no-cache + URL 上的 v=mtime：换照片后立即生效，不用等缓存过期
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/api/profile/photo", methods=["POST"])
def api_profile_photo_upload():
    """导入个人照片（multipart，字段名 file）。"""
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return jsonify({"ok": False, "error": "请选择要导入的照片"}), 400
    try:
        meta = svc.save_photo(upload.filename, upload.read())
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return jsonify({"ok": True, "photo": meta})


@bp.route("/api/profile/photo", methods=["DELETE"])
def api_profile_photo_delete():
    return jsonify({"ok": True, "deleted": svc.delete_photo(), "photo": svc.photo_meta()})


@bp.route("/api/profile/fill", methods=["POST"])
def api_profile_fill():
    """从简历原文识别成档案结构。**只返回建议，不写文件**——
    落盘前必须经用户逐项勾选确认，模型会编造简历里没有的内容。
    """
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请先提供简历文字（上传文件或直接粘贴）"}), 400
    try:
        result = fill_svc.suggest_from_text(text, model=str(payload.get("model") or "").strip())
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return jsonify({"ok": True, **result})
