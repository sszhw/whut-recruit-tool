"""LLM 厂商与本机配置：厂商清单、模型刷新、连通性测试、config.json 读写。

安全性说明：配置页列出的 Key 一律经 `_mask()` 脱敏回显，
日志也不记录完整密钥；`config.json` 已在 .gitignore 中排除。
"""

from __future__ import annotations

import requests
from flask import Blueprint, jsonify, request
from settings import LLM_PROVIDERS, _mask, get_llm, load_config, save_config

bp = Blueprint("settings", __name__)


@bp.route("/api/llm/catalog")
def api_llm_catalog():
    """返回各厂家预设 + 每家已保存的 Key/模型/BaseURL，供「预设供应商」卡片网格与配置表单使用。"""
    cfg = load_config()
    llm = get_llm()
    providers = []
    for pid, conf in LLM_PROVIDERS.items():
        key = (cfg.get(conf["api_key_field"], "") or "").strip()
        model = (cfg.get(conf["model_field"], "") or "").strip()
        saved_base = (cfg.get(conf.get("base_url_field") or "", "") or "").strip()
        providers.append({
            "id": pid, "label": conf["label"], "logo": conf.get("logo", ""),
            "desc": conf.get("desc", ""), "base_url": conf["base_url"],
            "default_model": conf["default_model"], "models": conf.get("models", []),
            "context_window": conf.get("context_window"),
            "docs_url": conf.get("docs_url", ""), "compute_url": conf.get("compute_url", ""),
            "api_key_set": bool(key), "api_key_masked": _mask(key),
            "model": model, "base_url_saved": saved_base,
            "current": pid == cfg.get("provider", ""),
        })
    return jsonify({
        "providers": providers,
        "current": {"provider": llm["provider"], "model": llm["model"],
                    "label": llm["label"], "base_url": llm["base_url"]},
    })


@bp.route("/api/llm/models/<pid>")
def api_llm_models(pid):
    """用已保存的 Key + BaseURL 调用 /models，刷新该厂家的模型清单。"""
    conf = LLM_PROVIDERS.get(pid)
    if not conf:
        return jsonify({"ok": False, "error": "未知厂商"}), 404
    cfg = load_config()
    key = (cfg.get(conf["api_key_field"], "") or "").strip()
    base = (cfg.get(conf.get("base_url_field") or "", "") or conf["base_url"]).rstrip("/")
    if not key:
        return jsonify({"ok": False, "error": "请先填写该厂商的 API Key", "models": conf.get("models", [])})
    if not base:
        return jsonify({"ok": False, "error": "请先填写 Base URL", "models": conf.get("models", [])})
    try:
        r = requests.get(base + "/models", headers={"Authorization": "Bearer " + key}, timeout=20)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        return jsonify({"ok": True, "models": ids or conf.get("models", [])})
    except Exception as e:  # noqa: BLE001  连通性问题一律降级为「保留预设清单 + 提示」
        return jsonify({"ok": False, "error": str(e), "models": conf.get("models", [])})


@bp.route("/api/llm/test", methods=["POST"])
def api_llm_test():
    """测试连接：用当前填写的 Key/BaseURL/模型向该厂商发一条最小请求。"""
    payload = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    provider = str(payload.get("provider") or "").strip() or cfg.get("provider", "siliconflow")
    conf = LLM_PROVIDERS.get(provider) or LLM_PROVIDERS["siliconflow"]
    key = str(payload.get("api_key") or "").strip() or (cfg.get(conf["api_key_field"], "") or "").strip()
    base = (str(payload.get("base_url") or "").strip()
            or (cfg.get(conf.get("base_url_field") or "", "") or "").strip()
            or conf["base_url"]).rstrip("/")
    model = str(payload.get("model") or "").strip() or conf["default_model"]
    if not key:
        return jsonify({"ok": False, "error": "未填写 API Key"})
    if not base:
        return jsonify({"ok": False, "error": "未填写 Base URL"})
    try:
        r = requests.post(base + "/chat/completions",
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                          json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
                          timeout=30)
        return jsonify({"ok": r.ok, "status": r.status_code,
                        "error": ("" if r.ok else r.text[:300])})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/config", methods=["POST"])
def api_config():
    payload = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    if "provider" in payload and str(payload["provider"]).strip() in LLM_PROVIDERS:
        cfg["provider"] = str(payload["provider"]).strip()
    provider = cfg.get("provider", "siliconflow")
    if provider not in LLM_PROVIDERS:
        provider = "siliconflow"
    conf = LLM_PROVIDERS[provider]
    # 所选模型写入对应厂家（由所选模型决定厂家）的 model 字段
    if "model" in payload and str(payload["model"]).strip():
        cfg[conf["model_field"]] = str(payload["model"]).strip()
    # 通用字段：api_key / base_url 按当前厂商写入独立字段（空值不动，保留已保存）
    if "api_key" in payload and str(payload["api_key"]).strip():
        cfg[conf["api_key_field"]] = str(payload["api_key"]).strip()
    if "base_url" in payload and str(payload["base_url"]).strip():
        cfg[conf.get("base_url_field") or ""] = str(payload["base_url"]).strip()
    save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/api/config/key", methods=["POST", "DELETE"])
def api_config_key_delete():
    """删除某厂商已保存的 API Key（仅清空本机 config.json 中的密钥，不影响 Base URL/模型）。"""
    payload = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    provider = str(payload.get("provider") or "").strip() or cfg.get("provider", "siliconflow")
    conf = LLM_PROVIDERS.get(provider)
    if not conf:
        return jsonify({"ok": False, "error": "未知厂商"}), 404
    cfg[conf["api_key_field"]] = ""
    save_config(cfg)
    return jsonify({"ok": True, "provider": provider})
