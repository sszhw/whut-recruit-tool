"""简历 Word 导出的两个版本 —— 同一套排版，区别只在「要不要挑内容」。

| | HR 版（render_hr_docx） | 完整版（render_full_docx） |
|---|---|---|
| 读者 | 真人 HR | 自己留档 / 交需要全量信息的系统 |
| 内容 | 挑着写：高中不上、绩点排名不放、外语按语种归并 | 档案里有什么写什么，一条不落（含家庭成员、学号） |
| 排版 | 分区标题 + 右对齐时间 + 要点 + 右上照片 | 完全相同 |

**为什么完整版也用这套排版**

它原先叫「可解析版」：一行一项、纯段落、禁表格，专供招聘系统的附件解析器抽字段
（表格会把字段名和值拆进不同文本节点，是解析错位的头号来源）。但档案的定位已经
变成「网申要填的全部字段存一份」，而「全量」和「一行一项」放一起就成了一坨没人
愿意打开看的文本——信息全了，可用性没了。所以它改成复用本模块的版式，
**代价是它不再是给解析器用的**：要喂给系统自动解析，请用 `.md` / `.txt`
（那两种仍是纯文本，见 `profile.render_resume`）。

**HR 版刻意的几条取舍**（完整版不做这些取舍，故保留在本模块）

1. **高中不上简历**：有更高学历时，"普通高中"只会占地方。按学历字段里的
   "高中"字样整条跳过。
2. **不放绩点与排名**：档案里本科"学分绩点 2.76"是 5 分制原值（折百分制并不好看），
   硕士"3.76"与"排名 30"在源文件里都标了 ⚠️ 编造。给 HR 的文档不能带存疑数字。

两个渲染器共用的取值清洗：带 ⚠️ / ⛔ / 见§ 的「待核实」值一律当空值处理，
不进任何一份 Word 文档。

数据一律来自 `data/投递档案.json`；本模块不 import flask。
"""

from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from utils.docx import (
    ACCENT,
    BODY_FONT,
    GREY,
    HEAD_FONT,
    TEXT,
    set_cell_margins,
    set_cell_valign,
    set_para_border,
    set_run_font,
    strip_table_borders,
)

from services import profile as prof_svc

# 版式常量。A4 + 窄边距：简历内容密度高，留白过多反而显得空。
PAGE_W_CM = 21.0
PAGE_H_CM = 29.7
MARGIN_X_CM = 1.7
MARGIN_TOP_CM = 1.5
MARGIN_BOTTOM_CM = 1.3
USABLE_CM = PAGE_W_CM - MARGIN_X_CM * 2      # 17.6
BODY_PT = 9.5
PHOTO_W_CM, PHOTO_H_CM = 2.8, 3.7            # 照片外框上限，等比缩放不裁切

# 档案里用这些标记标注「待核实 / 编造 / 见附录」。它们是给自己看的，
# 绝不能出现在给 HR 的文档里——所以取值时一律当空值处理。
_SKIP_HINTS = ("⛔", "⚠", "见 §", "见§")
_LEADING_BULLET = re.compile(r"^[-•*·]\s*")
_LABELED = re.compile(r"^([^：:]{2,14})[：:]\s*(.+)$", re.S)


# ---------------------------------------------------------------- 取值与清洗


def _s(value) -> str:
    return str(value or "").strip()


def _clean(value) -> str:
    """取值并剔掉带「待核实」标记的内容。"""
    text = _s(value)
    return "" if any(h in text for h in _SKIP_HINTS) else text


def _pick(item: dict, *keys: str) -> str:
    """按顺序取第一个非空值。

    同一含义在不同招聘系统的字段名不一样（「奖励批准单位」/「获奖单位」），
    所以给多个候选键而不是写死一个。
    """
    for key in keys:
        value = _clean(item.get(key))
        if value:
            return value
    return ""


def _field(profile: dict, module_key: str, field_key: str) -> str:
    """从 `fields` 型模块里取一个字段值。"""
    for row in profile.get(module_key) or []:
        if _s(row.get("k")) == field_key:
            return _clean(row.get("v"))
    return ""


def _one_ym(part: str) -> str:
    """单段日期：`2024-09-01` → `2024.09`；`预计 2027-06` → `预计 2027.06`。"""
    text = _s(part)
    if not text:
        return ""
    # 先把「预计」剥下来再匹配，否则前缀会让下面那个 `^\\d{4}` 匹配失败，
    # 结果拼出「预计 预计 2027-06」。
    prefix = ""
    if text.startswith("预计"):
        prefix, text = "预计 ", text[2:].strip()
    match = re.match(r"^(\d{4})\s*[-./]\s*(\d{1,2})", text)
    if match:
        return f"{prefix}{match.group(1)}.{match.group(2).zfill(2)}"
    # 「至今」「待定」这类非日期值原样保留——吞掉它比显示得难看严重得多
    return (prefix + text).strip()


def _ym(value) -> str:
    """日期归一化，支持区间：`2024.07 -- 2025.03` → `2024.07 – 2025.03`。

    区间分隔符只认 `--` / `—` / `~` / `至`，**不认单个 `-`**：
    单个 `-` 是 `2024-09-01` 的年月日分隔符，拆了就废了。
    `至` 后面必须紧跟年份才算分隔符，否则「2026.01 -- 至今」会被拆成
    「2026.01 – 今」——「至今」是内容，不是分隔符。
    """
    text = _s(value)
    if not text:
        return ""
    parts = re.split(r"\s*(?:--|—|–|~|～|至(?=\s*\d{4}))\s*", text)
    return " – ".join(x for x in (_one_ym(p) for p in parts) if x)


def _period(start, end) -> str:
    a, b = _ym(start), _ym(end)
    if a and b:
        return f"{a} – {b}"
    return a or b


def _region(value) -> str:
    """`福建省-泉州市` → `福建泉州`。层级再多只取前两级，否则头部会被地名撑长。"""
    text = _s(value)
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"[-—/]", text) if p.strip()]
    out = [re.sub(r"(省|市|自治区|特别行政区|自治州|地区)$", "", p) for p in parts[:2]]
    return "".join(out) or text


def _short_level(value) -> str:
    """`硕士研究生` → `硕士`；`本科（学士）` → `本科`。行内更省地方。"""
    text = _s(value)
    for full, short in (("博士研究生", "博士"), ("硕士研究生", "硕士"),
                        ("本科", "本科"), ("专科", "专科")):
        if full in text:
            return short
    return text


def _bullets(text) -> list[str]:
    """把多行要点拆成列表，去掉行首的 `- ` / `• ` 标记。"""
    out = []
    for raw in re.split(r"[\r\n]+", _s(text)):
        line = _LEADING_BULLET.sub("", raw.strip()).strip()
        if line:
            out.append(line)
    return out


# ---------------------------------------------------------------- 排版原语


def _para(doc, *, before=0, after=1.5, line=1.12, indent=0.0, hanging=0.0,
          keep_next=False, align=None):
    """建一个已设好段距的段落 —— 简历全靠紧致的段距把内容压进一页。"""
    para = doc.add_paragraph()
    pf = para.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line
    if indent:
        pf.left_indent = Cm(indent)
    if hanging:
        pf.first_line_indent = Cm(-hanging)
    if keep_next:
        pf.keep_with_next = True
    if align is not None:
        para.alignment = align
    return para


def _heading(doc, text):
    """分区标题：深蓝加粗 + 一条下划线。"""
    para = _para(doc, before=9, after=3.5, keep_next=True)
    set_run_font(para.add_run(text), size=11.5, bold=True, color=ACCENT, name=HEAD_FONT)
    set_para_border(para, edge="bottom", color=ACCENT, width_pt=1.0, space=2)
    return para


def _entry(doc, left_parts, right_text=""):
    """条目标题行：左边「主体 + 说明」，右边时间用右对齐制表位顶到页边。

    用制表位而不是表格：条目多的时候表格会互相挤压，制表位按段落排，
    跨页与自动换行都更稳。
    """
    para = _para(doc, before=3, after=1, keep_next=True)
    para.paragraph_format.tab_stops.add_tab_stop(Cm(USABLE_CM), WD_TAB_ALIGNMENT.RIGHT)
    for text, bold in left_parts:
        if not text:
            continue
        set_run_font(para.add_run(text), size=10, bold=bold, color=TEXT)
    if right_text:
        set_run_font(para.add_run("\t" + right_text), size=9, color=GREY)
    return para


def _labeled(doc, label, value, *, indent=0.42, label_cm=2.35):
    """`研究方向    新能源动力系统控制` —— 标签靠制表位对齐成一列。"""
    if not _s(value):
        return None
    para = _para(doc, after=1.2, indent=indent)
    para.paragraph_format.tab_stops.add_tab_stop(Cm(label_cm))
    set_run_font(para.add_run(label), size=BODY_PT, bold=True, color=TEXT)
    set_run_font(para.add_run("\t" + _s(value)), size=BODY_PT, color=TEXT)
    return para


def _label_cm(labels) -> float:
    """标签列宽：按本分区里最长的标签算。

    制表位是绝对的：标签比制表位还长时，值不会「挤在标签后面」，
    而是跳到下一个默认制表位——档案里有「所有教育类型最高学位标识」这种
    13 字的字段名，写死 2.35 cm 会让那一行的值跑到页面中间去。
    中文 9.5pt 一个字约 0.34 cm，这里按 0.36 估并留 0.3 cm 余量。
    """
    longest = max((len(_s(x)) for x in labels), default=0)
    return min(6.0, max(2.35, longest * 0.36 + 0.3))


def _bullet(doc, text, *, indent=0.62, hanging=0.36):
    """要点行。形如「关键词：说明」的会在冒号处加粗关键词——HR 扫读全靠这个。"""
    para = _para(doc, after=1.5, indent=indent, hanging=hanging)
    set_run_font(para.add_run("•  "), size=BODY_PT, color=ACCENT, bold=True)
    match = _LABELED.match(_s(text))
    if match:
        set_run_font(para.add_run(match.group(1) + "："), size=BODY_PT, bold=True, color=TEXT)
        set_run_font(para.add_run(match.group(2)), size=BODY_PT, color=TEXT)
    else:
        set_run_font(para.add_run(_s(text)), size=BODY_PT, color=TEXT)
    return para


def _text(doc, value, *, size=BODY_PT, color=TEXT, after=1.5, indent=0.0, bold=False):
    para = _para(doc, after=after, indent=indent)
    set_run_font(para.add_run(_s(value)), size=size, color=color, bold=bold)
    return para


def _add_photo(run, path):
    """插入照片并等比缩放到 `PHOTO_W_CM × PHOTO_H_CM` 的框内。

    只给 width 或只给 height，另一个由 Word 等比算——同时给两个会拉伸变形。
    """
    from docx.image.image import Image as DocxImage

    info = DocxImage.from_file(str(path))
    ratio = info.height / info.width if info.width else 1.33
    if ratio * float(Cm(PHOTO_W_CM)) > float(Cm(PHOTO_H_CM)):
        run.add_picture(str(path), height=Cm(PHOTO_H_CM))
    else:
        run.add_picture(str(path), width=Cm(PHOTO_W_CM))


# ---------------------------------------------------------------- 头部


def _header(doc, profile, photo_path, *, contact_only=False):
    """姓名 + 基本信息 + 右侧照片。两列无边框表格是 Word 里放照片的标准做法。

    `contact_only=True`（完整版用）：只留电话 / 邮箱一行。完整版下面紧跟一个
    「基本信息」分区会把性别、出生日期、籍贯等逐条列出，头部再摘要一遍就是
    同一页里来回说两遍。
    """
    name = _field(profile, "basic", "姓名") or "个人简历"

    line2 = []
    sex = _field(profile, "basic", "性别")
    born = _ym(_field(profile, "basic", "出生日期"))
    age = _field(profile, "basic", "年龄")
    if born and age:
        born = f"{born}（{age} 岁）"
    political = _field(profile, "basic", "政治面貌")
    nation = _field(profile, "basic", "民族")
    for value in (sex, born, political, nation):
        if value:
            line2.append(value)

    line3 = []
    phone = _field(profile, "basic", "移动电话") or _field(profile, "basic", "手机号")
    mail = _field(profile, "basic", "电子邮箱")
    if phone:
        line3.append(f"电话 {phone}")
    if mail:
        line3.append(f"邮箱 {mail}")

    line4 = []
    if not contact_only:
        live = _region(_field(profile, "basic", "现居住地") or _field(profile, "basic", "通信地址"))
        home = _region(_field(profile, "basic", "籍贯"))
        if home:
            line4.append(f"籍贯 {home}")
        # 现居与籍贯相同时只留一条，否则头部会出现「现居 福建泉州 · 籍贯 福建泉州」
        if live and live != home:
            line4.append(f"现居 {live}")

    table = doc.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    strip_table_borders(table)
    left, right = table.rows[0].cells
    left.width = Cm(USABLE_CM - PHOTO_W_CM - 0.5)
    right.width = Cm(PHOTO_W_CM + 0.5)
    for cell in (left, right):
        set_cell_margins(cell)
    set_cell_valign(left, "center")
    set_cell_valign(right, "top")

    para = left.paragraphs[0]
    para.paragraph_format.space_after = Pt(2.5)
    set_run_font(para.add_run(name), size=20, bold=True, color=TEXT, name=HEAD_FONT)

    for parts, size in ((line2, 9.5), (line3, 9.5), (line4, 9.5)):
        if not parts:
            continue
        para = left.add_paragraph()
        para.paragraph_format.space_after = Pt(1)
        para.paragraph_format.line_spacing = 1.1
        set_run_font(para.add_run("　·　".join(parts)), size=size, color=GREY)

    if photo_path is not None:
        para = right.paragraphs[0]
        para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        para.paragraph_format.space_after = Pt(0)
        try:
            _add_photo(para.add_run(), photo_path)
        except Exception:      # 图片损坏 / 格式不受支持时不要毁掉整份简历
            pass


# ---------------------------------------------------------------- 各分区


def _intent(doc, profile):
    """求职意向：只在真有内容时出现，空板块比没有更糟。"""
    wanted = []
    for key, label in (("期望岗位", "期望岗位"), ("期望工作地点", "期望城市"),
                       ("期望薪资", "期望薪资")):
        value = _field(profile, "intent", key)
        if value:
            wanted.append(f"{label} {value}")
    if not wanted:
        return
    _heading(doc, "求职意向")
    _text(doc, "　|　".join(wanted))


def _education(doc, rows):
    for item in rows:
        school = _pick(item, "学校名称", "毕业院校", "学校")
        level = _pick(item, "学历")
        if not school or "高中" in level:
            continue
        major = _pick(item, "专业")
        # 学校与学历之间用全角空格断开：两个 run 直接相连会成为
        # 「武汉理工大学本科」，看不出这是两个层次的信息。
        head = " · ".join(x for x in (_short_level(level), major) if x)
        left = [(school, True)] + ([("　" + head, False)] if head else [])
        _entry(doc, left,
               _period(_pick(item, "入学日期"),
                       _pick(item, "毕业/预计毕业日期", "毕业时间", "学位授予日期")))
        _labeled(doc, "研究方向", _pick(item, "研究方向"))
        _labeled(doc, "主修课程", _pick(item, "所学主要课程"))


def _projects(doc, rows):
    for item in rows:
        name = _pick(item, "项目名称", "项目")
        if not name:
            continue
        _entry(doc, [(name, True)], _ym(_pick(item, "时间")))
        _labeled(doc, "项目简介", _pick(item, "项目简介", "项目描述"))
        for line in _bullets(_pick(item, "负责工作", "工作内容")):
            _bullet(doc, line)


def _skills(doc, profile):
    rows = []
    lang = _field(profile, "skills", "开发语言")
    other_lang = _field(profile, "skills", "其他类开发语言")
    joined = " / ".join(x for x in (lang, other_lang) if x and x != lang)
    if lang:
        rows.append(("开发语言", joined or lang))
    for key, label in (("掌握程度", "掌握程度"), ("其他技能", "其他技能"),
                       ("特长", "专业特长"), ("计算机水平", "计算机水平")):
        value = _field(profile, "skills", key)
        # 「计算机水平：其他」这类值没有信息量，跳过
        if value and value not in ("其他", "无"):
            rows.append((label, value))

    # 外语按语种归并成一行：「英语」下挂 CET6 / CET4 两个等级，
    # 各自占一行会出现两条一模一样的「外语水平」标签。
    tongues: dict[str, list[str]] = {}
    for item in profile.get("languages") or []:
        tongue = _pick(item, "外语语种", "外语水平") or "外语"
        grade = _pick(item, "外语水平")
        score = _pick(item, "成绩")
        match = re.match(r"^(\d{2,3})", score)
        bits = " · ".join(x for x in (grade if grade != tongue else "",
                                      f"{match.group(1)} 分" if match else "") if x)
        if bits:
            tongues.setdefault(tongue, []).append(bits)
    for tongue, bits in tongues.items():
        rows.append((tongue, "　｜　".join(bits)))

    if not rows:
        return
    _heading(doc, "专业技能")
    for label, value in rows:
        _labeled(doc, label, value)


def _awards(doc, rows):
    """同年同名的奖项合并成一行（两次学业奖学金不必占两行）。"""
    merged: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    for item in rows:
        name = _pick(item, "其他获奖名称", "奖项名称", "荣誉名称")
        if not name:
            continue
        unit = _pick(item, "奖励批准单位", "获奖单位", "授予单位")
        key = (name, unit)
        if key not in merged:
            merged[key] = []
            order.append(key)
        when = _ym(_pick(item, "获奖时间", "获奖日期"))
        if when and when not in merged[key]:
            merged[key].append(when)

    if not order:
        return
    _heading(doc, "荣誉奖项")
    for name, unit in order:
        para = _para(doc, after=1.2, indent=0.42)
        para.paragraph_format.tab_stops.add_tab_stop(Cm(USABLE_CM), WD_TAB_ALIGNMENT.RIGHT)
        set_run_font(para.add_run("•  "), size=BODY_PT, bold=True, color=ACCENT)
        set_run_font(para.add_run(name), size=BODY_PT, color=TEXT)
        # 单位已含在奖项名里就不再重复括号
        if unit and unit not in name:
            set_run_font(para.add_run(f"（{unit}）"), size=BODY_PT, color=GREY)
        whens = merged[(name, unit)]
        if whens:
            set_run_font(para.add_run("\t" + "、".join(whens)), size=9, color=GREY)


def _summary(doc, lines):
    if not lines:
        return
    _heading(doc, "自我评价")
    for line in lines:
        if _s(line) and not any(h in _s(line) for h in _SKIP_HINTS):
            _bullet(doc, line, indent=0.42, hanging=0.36)


def _hobbies(doc, lines):
    text = "、".join(_s(x) for x in lines if _s(x))
    if not text:
        return
    _heading(doc, "兴趣爱好")
    _text(doc, text, indent=0.42, after=0)


# ---------------------------------------------------------------- 入口


def _setup_document():
    """建 A4 文档，设好页边距与正文默认样式；两个渲染器共用。

    不设 Normal 的话默认是 Calibri，中文会 fallback 到系统默认，
    打印出来与标题的观感对不上。rPr 在个别模板里可能缺失，取不到就跳过。
    """
    doc = Document()

    section = doc.sections[0]
    section.page_width = Cm(PAGE_W_CM)
    section.page_height = Cm(PAGE_H_CM)
    section.top_margin = Cm(MARGIN_TOP_CM)
    section.bottom_margin = Cm(MARGIN_BOTTOM_CM)
    section.left_margin = Cm(MARGIN_X_CM)
    section.right_margin = Cm(MARGIN_X_CM)

    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(BODY_PT)
    rpr = getattr(normal.element, "rPr", None)
    rfonts = getattr(rpr, "rFonts", None) if rpr is not None else None
    if rfonts is not None:
        rfonts.set(qn("w:eastAsia"), BODY_FONT)
    return doc


def _save(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def render_hr_docx(profile: dict, photo_path=None) -> bytes:
    """生成排版版简历（挑着写的那一份），返回 .docx 字节流。

    `photo_path` 传 None 或指向不存在的文件时不插图，其余排版不变。
    """
    doc = _setup_document()

    _header(doc, profile, photo_path)

    _intent(doc, profile)

    for title, key, renderer in (
        ("教育经历", "education", _education),
        ("项目经验", "projects", _projects),
    ):
        rows = profile.get(key) or []
        if not rows:
            continue
        _heading(doc, title)
        renderer(doc, rows)

    _skills(doc, profile)
    _awards(doc, profile.get("awards") or [])
    _summary(doc, profile.get("summary") or [])
    _hobbies(doc, profile.get("hobbies") or [])

    return _save(doc)


# ---- 完整版：同一套排版，内容不挑 ----------------------------------------
#
# 与 `render_hr_docx` 反过来：那边是「哪些不要」，这边是「照单全收」。
# 所以这里不用 _education / _skills 那些手写映射（每个函数都在做取舍与归并），
# 而是按 `profile.MODULES` 通用遍历——加一个模块就自动出现在文档里，
# 不会出现「档案里加了模块，导出忘了补」的漏项。

# 头部已经原样写出的字段：完整版的「基本信息」分区里不再重复列一遍。
# 其余字段（含证件号码、学号、紧急联系人、生源地口径等）一条不落。
_HEADER_FIELDS = {"姓名", "移动电话", "手机号", "电子邮箱"}


def _full_fields(doc, rows):
    """`fields` 型模块：逐条「标签 · 值」，标签靠制表位对齐。"""
    kept = [r for r in rows if prof_svc.include_field(r["k"], r["v"])
            and _s(r["k"]) not in _HEADER_FIELDS]
    if not kept:
        return
    width = _label_cm(r["k"] for r in kept)
    for row in kept:
        _labeled(doc, row["k"], row["v"], label_cm=width)


def _full_lines(doc, rows):
    """`lines` 型模块：每条一段，与 HR 版的自我评价同一种圆点样式。"""
    for line in rows:
        if prof_svc.include_field("", line):
            _bullet(doc, line, indent=0.42, hanging=0.36)


def _full_items(doc, mod, rows):
    """`items` 型模块：每条一张卡片 —— 标题行 + 该条全部字段。

    字段顺序按卡片里的原有顺序（档案 JSON 的顺序就是用户录入的顺序），
    不做任何重排，这样导出的文档和界面里看到的是同一个次序。
    """
    kept = [item for item in rows
            if any(prof_svc.include_field(k, v) for k, v in item.items())]
    if not kept:
        return
    width = _label_cm(k for item in kept for k, v in item.items()
                      if prof_svc.include_field(k, v))
    for index, item in enumerate(kept, 1):
        fields = [(k, v) for k, v in item.items() if prof_svc.include_field(k, v)]
        # 标题用过滤后的字段拼：带 ⚠️ 的标题字段本身就被拦掉了，
        # 拿原 item 拼会把标记写进标题——那是全文唯一漏网的位置。
        title = _s(prof_svc.item_title(dict(fields), mod)) or "（未命名）"
        _entry(doc, [(f"{index}. {title}", True)])
        for key, value in fields:
            points = _bullets(value)
            if len(points) > 1:
                # 多行值（项目「负责工作」的要点）：标签单独一行，要点逐条圆点
                para = _para(doc, after=1.2, indent=0.42)
                set_run_font(para.add_run(key), size=BODY_PT, bold=True, color=TEXT)
                for point in points:
                    _bullet(doc, point, indent=0.62, hanging=0.36)
            else:
                _labeled(doc, key, value, label_cm=width)


def render_full_docx(profile: dict, photo_path=None) -> bytes:
    """生成「完整版」简历：HR 版的排版 + 档案里的全部信息。

    - 头部与 HR 版同款（姓名大字 + 右上照片 + 电话 / 邮箱），
      性别、籍贯这些挪到紧随其后的「基本信息」分区；
    - 每个模块都出一个分区，模块内的每条字段原样写出（含家庭成员、学号、
      紧急联系人、证书编号、扫描件文件名）；
    - 唯一会少的东西是 `profile.include_field` 拦掉的两类：
      本地文件指针（`照片` 字段的值是本机文件名）与带 ⚠️ / ⛔ 的内部批注。

    **它不再是给招聘系统解析器用的那一版**：含无边框表格与图片，
    要喂自动解析请用 `.md` / `.txt`。
    """
    doc = _setup_document()
    _header(doc, profile, photo_path, contact_only=True)

    for mod in prof_svc.MODULES:
        rows = profile.get(mod["key"]) or []
        if not rows:
            continue
        key, kind, name = mod["key"], mod["kind"], mod["name"]
        if kind == "fields":
            if key == "basic":
                rows = [r for r in rows if _s(r["k"]) not in _HEADER_FIELDS]
            if not any(prof_svc.include_field(r["k"], r["v"]) for r in rows):
                continue
            _heading(doc, name)
            _full_fields(doc, rows)
        elif kind == "lines":
            if not any(prof_svc.include_field("", x) for x in rows):
                continue
            _heading(doc, name)
            _full_lines(doc, rows)
        else:
            if not any(prof_svc.include_field(k, v) for item in rows
                       for k, v in item.items()):
                continue
            _heading(doc, name)
            _full_items(doc, mod, rows)

    return _save(doc)
