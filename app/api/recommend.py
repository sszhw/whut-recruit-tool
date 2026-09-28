"""简历解析与投递推荐。

两条互补的推荐路径：
1. 企业推荐 —— 主库全量候选排序后取前 N 条送 LLM（`resume.build_companies`）；
2. 宣讲会推荐 —— 基于「工作地流动」映射的本地规则匹配（`_recommend_preachs`）。

LLM 不可用时整体降级为本地关键词匹配，保证功能不中断。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import repository as repo
import resume
from dataloaders import load_cache, load_preachs
from flask import Blueprint, jsonify, request
from settings import DATA, WORKDIR, get_llm

bp = Blueprint("recommend", __name__)

ALLOWED_EXT = {"pdf", "docx", "png", "jpg", "jpeg", "bmp", "webp"}

# 送入 LLM 的候选企业条数上限（受模型上下文限制；排序取自主库全量候选，避免样本偏小）
RESUME_PROMPT_LIMIT = 80


def save_upload(file_storage) -> Path:
    ext = Path(file_storage.filename).suffix.lower()
    tmp = WORKDIR / f"_upload_tmp_{int(time.time() * 1000)}{ext}"
    file_storage.save(tmp)
    return tmp


def _extract_upload(api_key: str, file_storage, base_url: str = None) -> tuple:
    """保存临时文件并提取文字，返回 (text, err)。无论成功与否都会清理临时文件。"""
    ext = Path(file_storage.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        return "", f"不支持的格式 .{ext}，请用 PDF / .docx / 图片"
    tmp = save_upload(file_storage)
    try:
        text = resume.extract_text(api_key, str(tmp), ext, request.form.get("vision_model") or None,
                                   base_url=base_url)
    except ValueError as exc:
        return "", str(exc)
    except Exception as exc:  # noqa: BLE001  文件来源多样，统一转成可读错误
        return "", f"解析失败：{exc}"
    finally:
        tmp.unlink(missing_ok=True)
    return text, ""


@bp.route("/api/resume/extract", methods=["POST"])
def api_resume_extract():
    llm = get_llm()
    if not llm["api_key"]:
        return jsonify({"ok": False, "error": f"请先在设置中配置 {llm['label']} API Key"}), 400
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "请选择要上传的简历文件"}), 400
    text, err = _extract_upload(llm["api_key"], file, base_url=llm["base_url"])
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if not text.strip():
        return jsonify({"ok": False, "error": "未能从该文件中提取到文字（可能是扫描件或空白），请直接粘贴简历文字"}), 400
    return jsonify({"ok": True, "text": text, "chars": len(text)})


@bp.route("/api/resume/companies")
def api_resume_companies():
    """候选企业统计（统一口径：主库全部招聘公告去重后的企业数）。"""
    all_companies = resume.build_companies(DATA, max_items=0)
    analyzed = sum(1 for c in all_companies if c["type"] or c["locations"])
    return jsonify({"count": len(all_companies), "analyzed": analyzed})


def _build_so_map() -> dict[str, str]:
    """企业分析缓存 → 企业名 → 国企标签（是/否/空串）。"""
    cache = load_cache()
    return {name: {True: "是", False: "否"}.get(info.get("is_state_owned"), "")
            for name, info in cache.items()}


def _recommend_preachs(text: str, target_cities: list[str], company_type: str = "",
                       limit: int = 40) -> list[dict]:
    """基于「轨迹流动」工作地映射，推荐求职者可参加的宣讲会。

    匹配规则：宣讲会企业的工作地城市与目标工作地命中 → 再按企业性质（若有）过滤 → 结合日期排序。
    未填目标城市时，从简历文本里嗅探城市；仍无则展示全部有工作地映射的场次。
    """
    rows = load_preachs()
    so_map = _build_so_map()
    if not target_cities:
        target_cities = sorted({c for r in rows for c in (r.get("work_cities") or []) if c in text})
    want_state = any(k in (company_type or "") for k in ("国企", "央企", "事业"))
    want_private = any(k in (company_type or "") for k in ("民营", "私企", "外企", "互联网"))
    today = datetime.now().strftime("%Y-%m-%d")

    matched = []
    for r in rows:
        wc = r.get("work_cities") or []
        if not wc:
            continue  # 无工作地映射的场次无法基于轨迹流动推荐
        if target_cities and not any(c in w or w in c for c in target_cities for w in wc):
            continue
        so = so_map.get(r["单位名称"], "")
        if want_state and so == "否":
            continue  # 已分析出是非国企，与期望冲突，排除
        if want_private and so == "是":
            continue
        matched.append({
            "单位名称": r["单位名称"],
            "举办日期": r.get("举办日期", ""),
            "宣讲时间": r["宣讲时间"],
            "宣讲会地点": r["宣讲会地点"],
            "公司地点": r["公司地点"],
            "工作地城市": wc,
            "线下/线上": r["线下/线上"],
            "原网页": r["原网页"],
            "正文": r.get("正文", ""),
            "企业性质": so,
            "reason": (f"工作地 {r['公司地点']} 与目标地命中" if target_cities else "有明确工作地映射")
                      + ("，且企业性质符合期望" if (want_state or want_private) else ""),
        })
    matched.sort(key=lambda x: (x["举办日期"] < today, x["举办日期"]))
    return matched[:limit]


@bp.route("/api/resume/recommend", methods=["POST"])
def api_resume_recommend():
    llm = get_llm()
    if not llm["api_key"]:
        return jsonify({"ok": False, "error": f"请先在设置中配置 {llm['label']} API Key"}), 400
    # 统一口径：候选企业来自主库合并后的全部招聘公告（不再是「最新那个文件」）；
    # 送入 LLM 的条数受上下文限制，但排序基于全量，并向界面回报真实总量。
    all_companies = resume.build_companies(DATA, max_items=0)
    if not all_companies:
        return jsonify({"ok": False, "error": "没有可推荐的企业数据，请先运行「抓取」与「企业分析」"}), 400
    companies = all_companies[:RESUME_PROMPT_LIMIT]

    model = (request.form.get("model") or "").strip() or llm["model"]
    work_place = (request.form.get("work_place") or "").strip()
    company_type = (request.form.get("company_type") or "").strip()
    text = (request.form.get("resume_text") or "").strip()
    file = request.files.get("file")
    if file and file.filename:
        # 文件文字提取走视觉模型 OCR（同样用当前配置的 LLM 密钥/地址）；推荐文本生成走当前配置的 LLM
        extracted, err = _extract_upload(llm["api_key"], file, base_url=llm["base_url"])
        if err:
            # 文件解析失败：若用户已粘贴文字，则用粘贴文字继续
            if not text:
                return jsonify({"ok": False, "error": err}), 400
        elif not extracted.strip():
            if not text:
                return jsonify({"ok": False, "error": "未能从文件提取到文字，请粘贴简历文字或换用其他格式"}), 400
        else:
            text = text or extracted
    if not text.strip():
        return jsonify({"ok": False, "error": "请上传简历文件，或直接粘贴简历文字"}), 400

    result = resume.recommend(llm["api_key"], model, text, companies, work_place=work_place,
                              company_type=company_type, base_url=llm["base_url"])
    source = "ai"
    note = ""
    if result.get("error"):
        # AI 不可用（余额不足/网络异常/模型被禁用）→ 降级为本地关键词匹配，保证有可用推荐
        source = "offline"
        note = f"AI 模型暂不可用（{result['error']}），已用本地关键词匹配生成推荐（充值后自动恢复 AI）。"
        result = resume.keyword_recommend(text, companies, work_place=work_place,
                                          company_type=company_type)
        result["note"] = note
    target_cities = resume.parse_target_cities(work_place)
    recommended_preachs = _recommend_preachs(text, target_cities, company_type)
    summary = repo.master_summary()
    return jsonify({"ok": True, "result": result, "source": source, "companies_count": len(companies),
                    "companies_total": len(all_companies),
                    "data_source": {"recruit_count": summary["recruit_count"],
                                    "coverage_start": summary["coverage_start"],
                                    "coverage_end": summary["coverage_end"],
                                    "updated": summary["last_update"]},
                    "recommended_preachs": recommended_preachs,
                    "target_work_place": work_place, "target_company_type": company_type,
                    "model": model, "resume_chars": len(text)})
