"""投递看板 HTTP 接口。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from services import ServiceError
from services import board as svc

bp = Blueprint("board", __name__)


def _error(exc: ServiceError):
    return jsonify({"ok": False, "error": str(exc)}), exc.status


@bp.route("/api/board/items")
def api_board_items():
    try:
        rows = svc.list_items(request.args.get("stage", ""))
    except ServiceError as exc:
        return _error(exc)
    return jsonify({"ok": True, "items": rows, "stages": svc.STAGES})


@bp.route("/api/board/stats")
def api_board_stats():
    try:
        stats = svc.statistics()
    except ServiceError as exc:
        return _error(exc)
    return jsonify({"ok": True, **stats})


@bp.route("/api/board/items", methods=["POST"])
def api_board_create():
    try:
        item = svc.create_item(request.get_json(silent=True) or {})
    except ServiceError as exc:
        return _error(exc)
    return jsonify({"ok": True, "item": item}), 201


@bp.route("/api/board/items/<item_id>/status", methods=["POST"])
def api_board_status(item_id: str):
    data = request.get_json(silent=True) or {}
    try:
        item = svc.update_status(item_id, data.get("stage", ""))
    except ServiceError as exc:
        return _error(exc)
    return jsonify({"ok": True, "item": item})


@bp.route("/api/board/items/<item_id>/note", methods=["POST"])
def api_board_note(item_id: str):
    data = request.get_json(silent=True) or {}
    try:
        item = svc.update_note(item_id, data.get("note", ""))
    except ServiceError as exc:
        return _error(exc)
    return jsonify({"ok": True, "item": item})


@bp.route("/api/board/items/<item_id>/delete", methods=["POST"])
def api_board_delete(item_id: str):
    try:
        item = svc.delete_item(item_id)
    except ServiceError as exc:
        return _error(exc)
    return jsonify({"ok": True, "item": item})
