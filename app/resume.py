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

import json
import os
import re
from pathlib import Path

import llm_client  # 统一 LLM 调用：超时/重试/错误文案不再各写一份
import repository as repo  # 统一数据访问层：候选企业跨全部招聘原始文件合并去重
import settings
from utils import io as io_utils
from utils import text as text_utils

BASE_URL = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
# 图片/扫描件 OCR 用的视觉模型（可在界面里改）
VISION_MODEL = os.environ.get("RESUME_VISION_MODEL", "Qwen/Qwen3-VL-32B-Instruct")
# 默认模型名统一取自 settings（此前此处硬编码了一份，与 analyze 各写各的，
# 结果 analyze 认 LLM_MODEL 环境变量、resume 不认，同一份配置两边模型不一致）
DEFAULT_MODEL = settings.DEFAULT_MODEL

CACHE_NAME = "企业分析_缓存.json"

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


OCR_PROMPT = ("请完整、准确地识别这张图片中的文字内容（这是一份简历）。"
              "按原有的段落结构逐行输出，保留序号/列表/分隔符，不要添加任何解释或评论。")


def ocr_image_bytes(api_key: str, model: str, image_bytes: bytes, mime: str = "image/png",
                    base_url: str = None) -> str:
    """用当前厂商的视觉模型识别图片里的文字。base_url 为空时用模块默认（硅基流动）。"""
    cfg = llm_client.from_settings(base_url=base_url or BASE_URL, api_key=api_key, model=model,
                                   # 扫描件是逐页调用的，每页都重试会把一次上传拖成好几分钟，
                                   # 因此沿用原先「失败即抛」的语义，只发一次
                                   max_retries=1)
    return llm_client.chat([llm_client.vision_message(OCR_PROMPT, image_bytes, mime)],
                           cfg, temperature=0.1, max_tokens=2000)


def ocr_image_file(api_key: str, model: str, path: str, base_url: str = None) -> str:
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "bmp": "image/bmp", "webp": "image/webp"}.get(Path(path).suffix.lower().lstrip("."),
                                                          "image/png")
    data = Path(path).read_bytes()
    return ocr_image_bytes(api_key, model, data, mime, base_url=base_url)


def extract_text(api_key: str, path: str, ext: str, vision_model: str | None = None,
                 base_url: str = None) -> str:
    """按扩展名提取文字，返回纯文本。找不到文字则返回空串。base_url 为空时用模块默认（硅基流动）。"""
    base_url = (base_url or BASE_URL).rstrip("/")
    vision_model = vision_model or VISION_MODEL
    ext = ext.lower().lstrip(".")
    if ext in {"png", "jpg", "jpeg", "bmp", "webp"}:
        return ocr_image_file(api_key, vision_model, path, base_url=base_url)
    if ext == "pdf":
        text = extract_pdf(path)
        if text:
            return text
        # 扫描件：渲染成图片做 OCR
        pages = pdf_to_pngs(path)
        parts = []
        for page in pages:
            parts.append(ocr_image_bytes(api_key, vision_model, page, "image/png", base_url=base_url))
        return "\n\n".join(parts).strip()
    if ext == "docx":
        return extract_docx(path)
    raise ValueError(f"不支持的格式：.{ext}（请使用 PDF / .docx / 图片，或将旧版 .doc 另存为 .docx）")


# ---------------------------------------------------------------- 汇总候选企业

# 注：原先的 latest_raw_json() 已删除（无任何调用点）。
# 它属于「读最新的那个原始数据文件」的旧口径，与统一后的 repository 合并去重相悖；
# resume 的候选企业现已由 build_companies() 走 repository 全量合并提供。


def load_cache(workdir: Path) -> dict:
    """读分析结果缓存；缺失或损坏一律返回 {}（实现见 utils.io.load_json_dict）。"""
    return io_utils.load_json_dict(Path(workdir) / CACHE_NAME)


def plain_text(value: str) -> str:
    """HTML → 纯文本。

    原实现只去标签并替换四个实体，比 crawler 版少掉了 script/style 剥离、
    块级标签转换行与 \\xa0 处理——同一段正文在不同模块被洗成不同结果。
    现统一到 `utils.text.strip_html`。
    """
    return text_utils.strip_html(value)


def _merged_recruit_items(workdir: Path) -> list[dict]:
    """跨全部招聘信息原始数据文件合并、按 ID 去重（与页面展示、企业分析同一口径）。"""
    if Path(workdir).resolve() == repo.DATA.resolve():
        return repo.raw_items("recruit")
    return repo.raw_items_in("recruit", Path(workdir))


def build_companies(workdir: Path, max_items: int = 60) -> list[dict]:
    """汇总正在校招的企业清单：名称 + 类型/国企/地点(来自分析缓存) + 岗位摘要(来自原始数据)。

    统一口径：候选企业来自 repository 合并后的**全部**招聘公告（不再是"最新那个文件"），
    max_items=0 表示不截断（用于统计企业总数）。
    """
    cache = load_cache(workdir)
    companies: dict[str, dict] = {}

    for item in _merged_recruit_items(workdir):
        name = (item.get("com_id_name") or "").strip()
        if not name:
            continue
        c = companies.setdefault(name, {"name": name, "type": "", "so": "", "locations": [],
                                        "evidence": "", "text": "", "title": "", "count": 0})
        c["count"] += 1
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

    # 排序：有完整画像(类型/地点)的优先 → 有正文的 → 公告数多的；保持相对顺序
    def rank(c: dict) -> tuple:
        return (0 if c["type"] or c["locations"] else 1,
                0 if c["text"] else 1,
                -int(c.get("count") or 0))

    ordered = sorted(companies.values(), key=rank)
    return ordered[:max_items] if max_items else ordered


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
3. 对于每家企业，说明为什么匹配（结合简历里的具体点），给出建议投递的岗位方向，并给出一个**可横向比较的匹配分数**。

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
    {{"company": "企业名", "match": "高|中|低", "location": "工作地点", "position": "建议岗位",
      "reason": "为什么匹配/投它的理由", "score": 78,
      "breakdown": {{"major": 30, "skill": 25, "location": 20, "nature": 3}},
      "matched": {{"keywords": ["机械", "仿真"], "cities": ["武汉"]}}}}
  ]
}}
分数规则（很重要，界面要靠它排序和画对比图）：
- score 是 0-100 的整数，必须等于 breakdown 四项之和；
- breakdown 各维度也是 0-100 的整数，建议上限：major 30、skill 30（合计不超过 50）、location 30、nature 20；
- 分数必须能横向比较：同一份简历下不同企业的分差要反映真实差距，强烈匹配的给 80 以上，
  一般的给 40~70，勉强相关的低于 40。**不要所有企业都给 90 分**，也不要全部挤在同一档；
- matched.keywords 填简历与企业真实重合的关键词原文（如 "机械"）、cities 填命中的城市名；没有就给空数组。
其他规则：recommendations 最多 10 条，按 score 从高到低排序；若某条信息未知可留空，不要编造企业名；没有合适的企业时 recommendations 返回空数组。必须严格遵从【求职要求】里的目标工作地与目标企业性质，不符合的不要推荐；若要求为空则忽略。"""


# ---------------------------------------------------------------- 推荐可解释性：统一打分口径
# AI、离线关键词兜底、宣讲会三条路径共用这套 0-100 分口径，
# 这样「A 比 B 更值得投」才真的可比：分差来自同一套算法的重合度差异，而不是各说各话。
MAX_SCORE = 100
EXPLAIN_DIMENSIONS = ["keyword", "location", "nature"]
_DIMENSION_CAPS = {"keyword": 50, "location": 30, "nature": 20}
_KEYWORD_POINTS = 10                      # 每个命中关键词
_NATURE_POINTS = {"state": 20, "private": 10}
MATCH_HIGH, MATCH_MID = 70, 40            # 档位门槛：≥70 高、≥40 中、否则低

EXPLAIN_NOTE = "匹配分 = keyword + location + nature，满分 100；档位：≥70 高、≥40 中、否则低。"
EXPLAIN_NOTE_AI = (EXPLAIN_NOTE +
                   "本结果由 AI 模型按「专业/技能/地点/企业性质」评估后折算，分数可横向比较。")
EXPLAIN_NOTE_OFFLINE = (EXPLAIN_NOTE +
                        "本结果由本地规则计算：命中关键词每个 +10（最多 5 个）、目标城市命中 +30、"
                        "企业性质符合 +20（国企）/+10（非国企）。")


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
              top: int = 10, max_retries: int = 3, base_url: str = None) -> dict:
    """调用 LLM 文本模型，返回推荐结果 JSON。失败返回 {"error": ...}。
    base_url 为空时用模块默认（硅基流动），可由上层传入当前配置的厂商 Base URL。"""
    if not companies:
        return {"error": "没有可推荐的企业数据，请先运行「抓取」与「企业分析」"}
    cfg = llm_client.from_settings(base_url=base_url or BASE_URL, api_key=api_key, model=model,
                                   max_retries=max_retries)
    requirements = _requirements_text(work_place, company_type)
    messages = [
        {"role": "system", "content": "你只输出要求格式的 JSON，不输出任何额外文字。"},
        {"role": "user", "content": RECOMMEND_PROMPT.format(resume=resume_text[:1800],
                                                            requirements=requirements,
                                                            companies=company_lines(companies))},
    ]
    try:
        content = llm_client.chat(messages, cfg, temperature=0.2, max_tokens=2000)
    except llm_client.LLMError as exc:
        # 失败统一成 {"error": ...}：services 层据此降级为本地关键词匹配，
        # 提示语里填的就是这里的归一化文案（不再写死厂商名，换厂商也不会变成假话）
        err = {"error": exc.message}
        if exc.status_code:
            err["status_code"] = exc.status_code
        return err
    return normalize_explanations(parse_recommend_json(content), companies,
                                  work_place=work_place, company_type=company_type,
                                  resume_text=resume_text)


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


def _as_score(value, cap: int) -> int:
    """把任意值收敛成 [0, cap] 的整数：模型常返回字符串、小数、负数或远超上限的数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(cap, int(value)))


def _valid_score(value) -> bool:
    """分数是否可直接采用：必须是 0-100 的整数。

    模型给字符串、小数、负数或越界值都算「没给」——与其猜它的意思，不如整条走本地规则。
    """
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SCORE


def _str_list(value) -> list[str]:
    """只保留字符串元素的列表；模型偶尔塞对象/数字，直接给前端会渲染崩。"""
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if isinstance(x, str) and x.strip()]


def match_level(score: int) -> str:
    """档位只由分数决定。

    模型经常在 reason 里写「非常匹配」却给个低分（或反过来），界面要同时展示档位和分数条，
    两者必须自洽，所以档位一律由分数反推，模型给的 match 不参与决策。
    """
    return "高" if score >= MATCH_HIGH else ("中" if score >= MATCH_MID else "低")


def explain_meta(note: str = "") -> dict:
    """分数口径说明，界面拿它渲染图例（有哪些维度、满分多少、分数怎么来的）。"""
    return {"dimensions": list(EXPLAIN_DIMENSIONS), "max_score": MAX_SCORE,
            "note": note or EXPLAIN_NOTE}


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


def _nature_flags(candidate: str, resume_text: str = "") -> tuple[bool, bool]:
    """解析企业性质倾向（是否想进国企 / 是否想进非国企）。未指定时从简历里嗅探。"""
    n = (candidate or "").lower()
    if not n:
        return (any(k in resume_text for k in ("国企", "央企", "编制", "稳定", "体制")),
                any(k in resume_text for k in ("外企", "私企", "互联网", "高薪", "民企")))
    if ("国企" in n) or ("央" in n) or ("事业" in n):
        return True, False
    if ("民营" in n) or ("私企" in n) or ("外企" in n) or ("互联网" in n):
        return False, True
    return False, False


def score_company(resume_text: str, comp: dict, target_cities: list[str],
                  company_type: str = "") -> dict:
    """按统一口径给单个企业打分，返回中间结果（供离线推荐与归一化兜底共用）。

    三个维度各有一个上限，加起来就是 0-100 的 `score`：
    keyword 每个命中关键词 +10（最多 5 个 → 50）、location 命中目标城市 +30、
    nature 企业性质符合期望 +20（国企）/+10（非国企）。
    同一份简历下所有企业走同一套算法，所以分数差异反映的是真实重合度差异，可以横向比较。
    企业本身不在清单里（comp 为空）时结果全 0，调用方据此判断要不要改用别的值。
    """
    resume_low = (resume_text or "").lower()
    locations = [str(x) for x in (comp.get("locations") or [])]
    blob = " ".join([str(comp.get("name") or ""), str(comp.get("type") or ""),
                     str(comp.get("title") or ""), str(comp.get("text") or ""),
                     " ".join(locations)]).lower()

    keywords: list[str] = []
    positions: list[str] = []
    for term, pos in JOB_KEYWORDS:
        if term in resume_low and term in blob:
            keywords.append(term)
            positions.append(pos)

    cities: list[str] = []
    for city in target_cities:
        if any(city in work or work in city for work in locations):
            cities.append(city)

    wants_state, wants_private = _nature_flags(company_type, resume_text)
    nature = 0
    if wants_state and comp.get("so") == "是":
        nature = _NATURE_POINTS["state"]
    elif wants_private and comp.get("so") == "否":
        nature = _NATURE_POINTS["private"]

    breakdown = {
        "keyword": min(len(keywords) * _KEYWORD_POINTS, _DIMENSION_CAPS["keyword"]),
        "location": _DIMENSION_CAPS["location"] if cities else 0,
        "nature": nature,
    }
    return {
        "score": sum(breakdown.values()),
        "breakdown": breakdown,
        "matched": {"keywords": keywords, "cities": cities},
        "matched_positions": positions,
        "location": cities[0] if cities else "",
    }


def _rec_out(rec: dict, breakdown: dict, matched: dict) -> dict:
    """推荐条目的对外形状：AI 与离线两条路径都从这里出场，字段集合因此必然一致。

    分数恒等于各维度之和（维度先按各自上限夹紧），界面画条形图时才不会出现
    「柱子加起来跟总分对不上」的错觉。
    """
    bd = {dim: _as_score(breakdown.get(dim, 0), _DIMENSION_CAPS[dim]) for dim in EXPLAIN_DIMENSIONS}
    score = sum(bd.values())
    return {
        "company": str(rec.get("company") or ""),
        "match": match_level(score),
        "location": str(rec.get("location") or ""),
        "position": str(rec.get("position") or ""),
        "reason": str(rec.get("reason") or ""),
        "score": score,
        "breakdown": bd,
        "matched": {"keywords": _str_list(matched.get("keywords")),
                    "cities": _str_list(matched.get("cities"))},
    }


def _fold_breakdown(raw) -> dict | None:
    """把模型给的 breakdown 折叠成本地口径的三个维度；缺维度或全是 0 时返回 None。

    prompt 让模型按 major/skill/location/nature 四项给更细的判断，但界面只有三根柱子，
    所以 major+skill 合并成 keyword；模型也可能直接给 keyword（幂等场景下就是自己上次的产物）。
    三个维度缺一个就必须整条重算：只给一半维度的分数跟「全维度」的分数放一起比没有意义，
    可比性要求同一份结果里的每条都出自同一套算法。
    """
    if not isinstance(raw, dict):
        return None
    has_keyword = any(isinstance(raw.get(k), (int, float)) and not isinstance(raw.get(k), bool)
                      for k in ("keyword", "major", "skill"))
    if not has_keyword or not _valid_score(raw.get("location")) or not _valid_score(raw.get("nature")):
        return None
    keyword = raw.get("keyword")
    if keyword is None:
        keyword = (_as_score(raw.get("major"), _DIMENSION_CAPS["keyword"])
                   + _as_score(raw.get("skill"), _DIMENSION_CAPS["keyword"]))
    bd = {
        "keyword": _as_score(keyword, _DIMENSION_CAPS["keyword"]),
        "location": _as_score(raw.get("location"), _DIMENSION_CAPS["location"]),
        "nature": _as_score(raw.get("nature"), _DIMENSION_CAPS["nature"]),
    }
    return bd if any(bd.values()) else None


def _clean_matched(raw) -> dict | None:
    """matched 只保留两个字符串列表；空的或格式不对的返回 None，交给本地规则重算。"""
    if not isinstance(raw, dict):
        return None
    out = {"keywords": _str_list(raw.get("keywords")), "cities": _str_list(raw.get("cities"))}
    return out if (out["keywords"] or out["cities"]) else None


def _find_company(name: str, by_name: dict[str, dict]) -> dict | None:
    """按企业名找候选企业：先用全等，再退一步做包含匹配。

    模型常把「某某集团」写成简称或加后缀，全等匹配会漏掉，漏了就只能靠本地规则兜底。
    """
    comp = by_name.get(name)
    if comp is not None:
        return comp
    for full, item in by_name.items():
        if full and (full in name or name in full):
            return item
    return None


def normalize_explanations(result: dict, companies: list[dict], work_place: str = "",
                           company_type: str = "", resume_text: str = "") -> dict:
    """把推荐结果规范成「可比较」的形状：补齐 score/breakdown/matched，并按分数重排。

    模型经常漏字段、给超范围的分数、或者在 reason 里写「高度匹配」却给个低分，
    而界面要靠分数排序、画对比图，所以这里统一兜底：
    - 分数缺失 / 非数字 / 越界 / breakdown 缺失 → 用本地规则重算（与离线路径同一套算法）；
    - match 档位一律由分数推出，保证「分高的排前面、档位高的分也高」；
    - 幂等：已经规范化的结果再跑一次，所有字段都不变。
    """
    if not isinstance(result, dict) or result.get("error"):
        return result
    recs = result.get("recommendations")
    if not isinstance(recs, list):
        recs = []
        result["recommendations"] = recs

    text = resume_text or str(result.get("resume_summary") or "")
    by_name = {str(c.get("name") or "").strip(): c for c in (companies or []) if isinstance(c, dict)}
    req_cities = parse_target_cities(work_place)

    out = []
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        comp = _find_company(str(rec.get("company") or "").strip(), by_name)
        # 未填目标工作地时退回简历里提到的城市，与离线路径口径一致
        cities = req_cities or [c for c in (comp or {}).get("locations") or [] if c and c in text]
        offline = score_company(text, comp or {}, cities, company_type)
        # 分数与三维拆解必须同时合法才采信模型：只信一半会出现「80 分但拆不出来」的怪结果
        breakdown = _fold_breakdown(rec.get("breakdown"))
        use_ai = _valid_score(rec.get("score")) and breakdown is not None
        if not use_ai:
            breakdown = offline["breakdown"]
        matched = (_clean_matched(rec.get("matched")) if use_ai else None) or offline["matched"]
        out.append(_rec_out(rec, breakdown, matched))

    out.sort(key=lambda r: r["score"], reverse=True)
    result["recommendations"] = out
    return result


def keyword_recommend(resume_text: str, companies: list[dict], work_place: str = "",
                      company_type: str = "", top: int = 10) -> dict:
    """无需 API 的本地关键词匹配推荐（离线兜底）。

    依据企业文本与简历在「岗位关键词」「工作地点城市」「企业性质倾向」上的真实重合度打分，
    确定性、可复现。算法见 `score_company`：三维合计 0-100，分数≥70 高、≥40 中、否则低。
    work_place/company_type 为用户明确指定的目标工作地/企业性质，优先采用。
    """
    resume_text = resume_text or ""
    resume_low = resume_text.lower()

    all_cities = {str(c) for comp in companies for c in (comp.get("locations") or [])}
    pref_cities = [c for c in all_cities if c and c in resume_text]
    # 用户填写的目标工作地优先；未填则退回简历里提到的城市
    target_cities = parse_target_cities(work_place) or pref_cities
    wants_state, wants_private = _nature_flags(company_type, resume_text)

    scored = []
    for comp in companies:
        s = score_company(resume_text, comp, target_cities, company_type)
        if s["score"] <= 0:
            continue
        locations = comp.get("locations") or []
        loc = s["location"] or (locations[0] if locations else "")
        scored.append(_rec_out({
            "company": comp.get("name", ""),
            "location": loc,
            "position": s["matched_positions"][0] if s["matched_positions"]
                        else (comp.get("title") or "校招岗位"),
            "reason": _build_reason(comp, s["matched"]["keywords"], loc, wants_state, wants_private),
        }, s["breakdown"], s["matched"]))

    scored.sort(key=lambda x: x["score"], reverse=True)

    found_pos: list[str] = []
    for term, pos in JOB_KEYWORDS:
        if term in resume_low and pos not in found_pos:
            found_pos.append(pos)

    # 过一遍归一化：与 AI 路径共用同一个出口，两条路径对外字段集合完全一致
    return normalize_explanations({
        "resume_summary": " ".join(resume_text.split())[:80] or "（未能识别简历内容）",
        "target_positions": found_pos[:5],
        "recommendations": scored[:top],
    }, companies, work_place=work_place, company_type=company_type, resume_text=resume_text)


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
