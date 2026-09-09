#!/usr/bin/env python3
"""上传简历 → 硅基流动(SiliconFlow) AI 分析 → 推荐适合投递的企业。

支持输入：
    - PDF（含文字层或扫描件，扫描件自动渲染成图片做 OCR）
    - Word（.docx）
    - 图片（.png/.jpg/.jpeg/.bmp/.webp，用视觉模型 OCR）
    - 直接粘贴简历文字

工作原理：
    1. 从文件中提取纯文本（PDF/Word 用本地库，图片用硅基流动视觉模型 OCR）；
    2. 从本目录的 企业分析_缓存.json + *_原始数据.json 汇总出「正在校招的企业清单」；
    3. 把简历 + 企业清单交给硅基流动文本模型，让其选出最匹配、最值得投递的企业并说明理由。
"""

from __future__ import annotations

import base64
import glob
import io
import json
import os
import re
import time
from pathlib import Path

import requests

BASE_URL = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
# 图片/扫描件 OCR 用的视觉模型（可在界面里改）
VISION_MODEL = os.environ.get("RESUME_VISION_MODEL", "Qwen/Qwen3-VL-32B-Instruct")
DEFAULT_MODEL = "Qwen/Qwen2.5-72B-Instruct"

CACHE_NAME = "企业分析_缓存.json"
RAW_GLOB = "*_原始数据.json"

# ---------------------------------------------------------------- 文字提取

def extract_pdf(path: str) -> str:
    """提取 PDF 文字层；若为空（扫描件）返回空串。"""
    import fitz  # PyMuPDF

    text_parts = []
    with fitz.open(path) as doc:
        for page in doc:
            text_parts.append(page.get_text("text"))
    return "\n".join(text_parts).strip()


def pdf_to_pngs(path: str, dpi: int = 200, max_pages: int = 8) -> list[bytes]:
    """把 PDF 逐页渲染成 PNG 字节，用于扫描件 OCR。"""
    import fitz  # PyMuPDF

    pages = []
    with fitz.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            pages.append(pix.tobytes("png"))
            if len(pages) >= max_pages:
                break
    return pages


def extract_docx(path: str) -> str:
    """提取 Word(.docx) 段落与表格文字。"""
    from docx import Document

    doc = Document(path)
    lines: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            lines.append(text)
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def ocr_image_bytes(api_key: str, model: str, image_bytes: bytes, mime: str = "image/png") -> str:
    """用硅基流动视觉模型识别图片里的文字。"""
    prompt = ("请完整、准确地识别这张图片中的文字内容（这是一份简历）。"
              "按原有的段落结构逐行输出，保留序号/列表/分隔符，不要添加任何解释或评论。")
    b64 = base64.b64encode(image_bytes).decode()
    content = [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "content": prompt},
    ]
    resp = requests.post(
        BASE_URL + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": 2000,
        },
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"视觉模型返回 {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


def ocr_image_file(api_key: str, model: str, path: str) -> str:
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "bmp": "image/bmp", "webp": "image/webp"}.get(Path(path).suffix.lower().lstrip("."),
                                                          "image/png")
    data = Path(path).read_bytes()
    return ocr_image_bytes(api_key, model, data, mime)


def extract_text(api_key: str, path: str, ext: str, vision_model: str | None = None) -> str:
    """按扩展名提取文字，返回纯文本。找不到文字则返回空串。"""
    vision_model = vision_model or VISION_MODEL
    ext = ext.lower().lstrip(".")
    if ext in {"png", "jpg", "jpeg", "bmp", "webp"}:
        return ocr_image_file(api_key, vision_model, path)
    if ext == "pdf":
        text = extract_pdf(path)
        if text:
            return text
        # 扫描件：渲染成图片做 OCR
        pages = pdf_to_pngs(path)
        parts = []
        for page in pages:
            parts.append(ocr_image_bytes(api_key, vision_model, page, "image/png"))
        return "\n\n".join(parts).strip()
    if ext == "docx":
        return extract_docx(path)
    raise ValueError(f"不支持的格式：.{ext}（请使用 PDF / .docx / 图片，或将旧版 .doc 另存为 .docx）")


# ---------------------------------------------------------------- 汇总候选企业

def latest_raw_json(workdir: Path) -> Path | None:
    # 简历投递推荐面向"招聘信息"中的企业：优先取招聘信息原始数据文件，避免误取只含"宣讲会"的文件。
    candidates = sorted(glob.glob(str(workdir / "武汉理工大学招聘信息_*_原始数据.json")),
                        key=os.path.getmtime, reverse=True)
    if not candidates:
        candidates = sorted(glob.glob(str(workdir / RAW_GLOB)), key=os.path.getmtime, reverse=True)
    return Path(candidates[0]) if candidates else None


def load_cache(workdir: Path) -> dict:
    path = workdir / CACHE_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def plain_text(value: str) -> str:
    value = (value or "")
    value = re.sub(r"(?s)<[^>]+>", "", value)
    value = value.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def build_companies(workdir: Path, max_items: int = 60) -> list[dict]:
    """汇总正在校招的企业清单：名称 + 类型/国企/地点(来自分析缓存) + 岗位摘要(来自原始数据)。"""
    cache = load_cache(workdir)
    raw = latest_raw_json(workdir)
    companies: dict[str, dict] = {}

    if raw:
        try:
            data = json.loads(raw.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {"招聘信息": []}
        for item in data.get("招聘信息", []):
            name = (item.get("com_id_name") or "").strip()
            if not name:
                continue
            c = companies.setdefault(name, {"name": name, "type": "", "so": "", "locations": [],
                                            "evidence": "", "text": "", "title": ""})
            if item.get("title") and not c["title"]:
                c["title"] = str(item["title"]).strip()
            body = plain_text(item.get("content") or item.get("remarks") or "")
            if body and len(body) > len(c["text"]):
                c["text"] = body[:300]

    # 附着分析缓存里的企业画像
    for name, c in companies.items():
        info = cache.get(name)
        if not info:
            continue
        c["type"] = info.get("company_type", "")
        c["so"] = {True: "是", False: "否"}.get(info.get("is_state_owned"), "")
        c["locations"] = info.get("locations") or []
        c["evidence"] = info.get("evidence", "")

    # 排序：有完整画像(类型/地点)的优先，其次有正文的，最后其余；保持相对顺序
    def rank(c: dict) -> tuple:
        return (0 if c["type"] or c["locations"] else 1,
                0 if c["text"] else 1)

    ordered = sorted(companies.values(), key=rank)
    return ordered[:max_items]


def company_lines(companies: list[dict]) -> str:
    lines = []
    for i, c in enumerate(companies, 1):
        info = "、".join(str(x) for x in [c["type"], c["so"], "、".join(c["locations"])] if str(x).strip())
        summary = c["text"] or c["title"] or ""
        summary = summary.replace("\n", " ")[:150]
        meta = f"{c['name']}（{info}）" if info else c["name"]
        lines.append(f"{i}. {meta} | {summary}" if summary else f"{i}. {meta}")
    return "\n".join(lines)


# ---------------------------------------------------------------- AI 推荐

RECOMMEND_PROMPT = """你是资深的校园招聘顾问。给你一位求职者的简历及其求职要求，再给你一份正在校招的「备选企业清单」。请你：
1. 先概括这份简历的核心信息（专业、技能、实习/项目经历、求职意向、倾向城市）；
2. 再从中挑出最适合该求职者投递的企业，最多 10 家，按匹配度从高到低排序；
3. 对于每家企业，说明为什么匹配（结合简历里的具体点），并给出建议投递的岗位方向。

【简历内容】
{resume}

【求职要求】
{requirements}

【备选企业清单】（序号. 企业名（类型｜是否国企｜工作地点）｜招聘摘要）
{companies}

只输出一个合法的 JSON 对象，不要输出任何其他文字。JSON 结构如下：
{{
  "resume_summary": "一段话概括简历：专业/技能/实习/意向岗位/城市倾向",
  "target_positions": ["建议投递的岗位方向1", "岗位方向2", "岗位方向3"],
  "recommendations": [
    {{"company": "企业名", "match": "高|中|低", "location": "工作地点", "position": "建议岗位", "reason": "为什么匹配/投它的理由"}}
  ]
}}
规则：recommendations 最多 10 条，按匹配度排序；若某条信息未知可留空，不要编造企业名；没有合适的企业时 recommendations 返回空数组。必须严格遵从【求职要求】里的目标工作地与目标企业性质，不符合的不要推荐；若要求为空则忽略。"""


def parse_target_cities(text: str) -> list[str]:
    """把用户填写的「目标工作地」解析为城市名列表（支持中英文逗号、顿号、空格、分号分隔）。"""
    if not text:
        return []
    parts = re.split(r"[，、,;；\s/]+", text)
    return [p.strip() for p in parts if p.strip()]


def _requirements_text(work_place: str = "", company_type: str = "") -> str:
    parts = []
    if work_place:
        parts.append(f"目标工作地/倾向城市：{work_place}")
    if company_type:
        parts.append(f"目标企业性质：{company_type}")
    return "；".join(parts) or "（无特别要求）"


def recommend(api_key: str, model: str, resume_text: str, companies: list[dict],
              work_place: str = "", company_type: str = "",
              top: int = 10, max_retries: int = 3) -> dict:
    """调用硅基流动文本模型，返回推荐结果 JSON。失败返回 {"error": ...}。"""
    if not companies:
        return {"error": "没有可推荐的企业数据，请先运行「抓取」与「企业分析」"}
    requirements = _requirements_text(work_place, company_type)
    user = RECOMMEND_PROMPT.format(resume=resume_text[:1800], requirements=requirements,
                                   companies=company_lines(companies))
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                BASE_URL + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你只输出要求格式的 JSON，不输出任何额外文字。"},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 2000,
                },
                timeout=180,
            )
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                body = resp.text
                try:
                    msg = resp.json().get("message", body)
                except Exception:
                    msg = body
                return {"error": f"硅基流动返回 {resp.status_code}：{msg[:200]}",
                        "status_code": resp.status_code}
            content = resp.json()["choices"][0]["message"]["content"].strip()
            return parse_recommend_json(content)
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                return {"error": f"网络错误：{exc}"}
            time.sleep(3 * (attempt + 1))
    return {"error": "多次重试仍失败"}


def parse_recommend_json(content: str) -> dict:
    match = re.search(r"\{.*\}", content, re.S)
    if match:
        content = match.group(0)
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return {"error": "AI 返回的 JSON 不是对象"}
        recs = data.get("recommendations")
        if recs is not None and not isinstance(recs, list):
            data["recommendations"] = []
        return data
    except json.JSONDecodeError as exc:
        return {"error": f"无法解析 AI 返回结果：{exc}", "_raw": content[:400]}


# ---------------------------------------------------------------- 本地关键词匹配（离线兜底）

JOB_KEYWORDS = [
    ("算法", "算法工程师"), ("机器学习", "算法工程师"), ("深度学习", "算法工程师"),
    ("开发", "开发工程师"), ("后端", "后端开发"), ("前端", "前端开发"), ("软件", "软件开发"),
    ("嵌入式", "嵌入式开发"), ("硬件", "硬件工程师"), ("芯片", "芯片/数字 IC"), ("半导体", "半导体"),
    ("电力电子", "电力电子研发"), ("逆变器", "电力电子研发"), ("储能", "储能/新能源"),
    ("光伏", "光伏/新能源"), ("新能源", "新能源"), ("电网", "电网/能源"), ("电力", "电力系统"),
    ("自动化", "自动化/控制"), ("控制", "控制工程师"), ("电气", "电气工程师"), ("机械", "机械工程师"),
    ("土木", "土木工程"), ("化工", "化工/材料"), ("环保", "环保/环境工程"), ("汽车", "汽车/整车"),
    ("通信", "通信工程师"), ("计算机", "软件/IT"), ("大数据", "大数据"), ("云计算", "云计算"),
    ("数据", "数据分析"), ("运维", "运维/DevOps"), ("测试", "测试工程师"), ("质量", "质量管理"),
    ("金融", "金融/投资"), ("银行", "银行"), ("证券", "证券/金融"), ("投资", "金融/投资"),
    ("财务", "财务/会计"), ("会计", "财务/会计"), ("审计", "审计"), ("保险", "保险"),
    ("市场", "市场/营销"), ("营销", "市场/营销"), ("销售", "销售"), ("运营", "运营"),
    ("电商", "电商/运营"), ("直播", "电商/直播"), ("供应链", "供应链/物流"), ("物流", "供应链/物流"),
    ("人事", "HR/人事"), ("人力资源", "HR/人事"), ("管理培训", "管培生"), ("管培", "管培生"),
    ("日语", "外语"), ("英语", "外语"), ("翻译", "翻译"),
    ("python", "Python 开发"), ("c++", "C++ 开发"), ("java", "Java 开发"), ("matlab", "MATLAB/仿真"),
]


def _build_reason(comp: dict, keywords: list, loc: str, wants_state: bool, wants_private: bool) -> str:
    segs = []
    if keywords:
        segs.append("匹配关键词：" + "、".join(keywords[:4]))
    if loc:
        segs.append(f"工作地点 {loc} 与求职城市吻合")
    if comp.get("type"):
        segs.append(f"企业类型：{comp['type']}")
    if comp.get("so") and (wants_state or wants_private):
        good = (wants_state and comp.get("so") == "是") or (wants_private and comp.get("so") == "否")
        segs.append(f"{'国企/央企' if comp.get('so') == '是' else '非国企'}性质{'符合期望' if good else ''}")
    return "；".join(s for s in segs if s) or "与简历部分经历相关"


def keyword_recommend(resume_text: str, companies: list[dict], work_place: str = "",
                      company_type: str = "", top: int = 10) -> dict:
    """无需 API 的本地关键词匹配推荐（离线兜底）。

    依据企业文本与简历在「岗位关键词」「工作地点城市」「企业性质倾向」上的真实重合度打分，
    确定性、可复现。算法：重合一个关键词 +2，地点命中 +3，性质倾向 +1~2；分数≥8 高、≥4 中、否则低。
    work_place/company_type 为用户明确指定的目标工作地/企业性质，优先采用。
    """
    resume_text = resume_text or ""
    resume_low = resume_text.lower()

    all_cities = {str(c) for comp in companies for c in (comp.get("locations") or [])}
    pref_cities = [c for c in all_cities if c and c in resume_text]
    # 用户填写的目标工作地优先；未填则退回简历里提到的城市
    target_cities = parse_target_cities(work_place) or pref_cities

    def _nature_flags(candidate: str) -> tuple[bool, bool]:
        n = (candidate or "").lower()
        if not n:
            # 未指定则从简历里嗅探倾向
            return (any(k in resume_text for k in ("国企", "央企", "编制", "稳定", "体制")),
                    any(k in resume_text for k in ("外企", "私企", "互联网", "高薪", "民企")))
        if ("国企" in n) or ("央" in n) or ("事业" in n):
            return True, False
        if ("民营" in n) or ("私企" in n) or ("外企" in n) or ("互联网" in n):
            return False, True
        return False, False

    wants_state, wants_private = _nature_flags(company_type)

    scored = []
    for comp in companies:
        locations = comp.get("locations") or []
        blob = " ".join([comp.get("name", ""), comp.get("type", ""), comp.get("title", ""),
                         comp.get("text", ""), " ".join(locations)]).lower()
        score = 0
        matched_positions: list[str] = []
        matched_keywords: list[str] = []
        for term, pos in JOB_KEYWORDS:
            if term in resume_low and term in blob:
                score += 2
                matched_positions.append(pos)
                matched_keywords.append(term)
        loc = ""
        for city in target_cities:
            for work in locations:
                if city in work or work in city:
                    score += 3
                    loc = loc or city
                    break
        if wants_state and comp.get("so") == "是":
            score += 2
        if wants_private and comp.get("so") == "否":
            score += 1
        if score <= 0:
            continue
        loc = loc or (locations[0] if locations else "")
        scored.append({
            "company": comp.get("name", ""),
            "location": loc,
            "position": matched_positions[0] if matched_positions else (comp.get("title") or "校招岗位"),
            "reason": _build_reason(comp, matched_keywords, loc, wants_state, wants_private),
            "match": "高" if score >= 8 else ("中" if score >= 4 else "低"),
            "_score": score,
        })

    scored.sort(key=lambda x: x["_score"], reverse=True)
    top_recs = scored[:top]
    for r in top_recs:
        r.pop("_score", None)

    found_pos: list[str] = []
    for term, pos in JOB_KEYWORDS:
        if term in resume_low and pos not in found_pos:
            found_pos.append(pos)

    return {
        "resume_summary": " ".join(resume_text.split())[:80] or "（未能识别简历内容）",
        "target_positions": found_pos[:5],
        "recommendations": [{"company": r["company"], "match": r["match"], "location": r["location"],
                             "position": r["position"], "reason": r["reason"]} for r in top_recs],
    }


# ---------------------------------------------------------------- 命令行（便于本地测试）

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="简历文件路径（pdf/docx/图片）")
    parser.add_argument("--text", default="", help="直接粘贴简历文字（优先级低于文件）")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        cfg = Path(__file__).resolve().parent.parent / "config.json"
        if cfg.exists():
            api_key = str(json.loads(cfg.read_text(encoding="utf-8")).get("api_key", "")).strip()
    if not api_key:
        print("错误：未配置 API Key（写进 config.json 或设置 SILICONFLOW_API_KEY）", file=os.sys.stderr)
        return 2

    workdir = Path(__file__).resolve().parent.parent / "data"
    companies = build_companies(workdir)
    print(f"候选企业：{len(companies)} 家")

    resume = args.text.strip()
    if args.file:
        p = Path(args.file)
        ext = p.suffix.lower().lstrip(".")
        print(f"解析文件 {p.name}（.{ext}）…")
        resume = extract_text(api_key, str(p), ext)
        print(f"提取到 {len(resume)} 字简历文字")
    if not resume:
        print("错误：没有得到简历文字。请提供文件或 --text", file=os.sys.stderr)
        return 2
    print("调用模型生成推荐…")
    result = recommend(api_key, args.model, resume, companies)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
