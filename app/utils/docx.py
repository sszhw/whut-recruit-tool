"""python-docx 排版小工具 —— 两份简历导出器共用。

**为什么单独抽一层**

`services/resume_hr.py` 的两份简历（挑着写的「HR 版」与一条不落的「完整版」）对排版的
要求几乎相反（前者要极简、后者要好看），但底层有三件事是一致的：中文字体、段落边线、
单元格宽度。放在一处才不会出现「A 版补了中文字体、B 版忘了补」这种只在别人电脑上
才暴露的问题。

**中文字体的坑（本项目踩过）**

python-docx 的 `run.font.name` 只写 `w:ascii` 与 `w:hAnsi`，中文字符走的是
`w:eastAsia`。不补这个属性，中文在别人的 Word / WPS 上会掉回默认字体，
导出的文档看着像没设过字体。`set_run_font()` 一次把四个属性都写全。

本模块只依赖 python-docx，不 import flask，也不 import services。
"""

from __future__ import annotations

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

# 中文正文字体。微软雅黑在 Windows 与多数中文 Office 里都有；缺字体时
# Word 会按字符集 fallback，不会出现方块。西文用同一套，避免中英混排跳字。
BODY_FONT = "微软雅黑"
HEAD_FONT = "微软雅黑"

# HR 版的强调色。深蓝在黑白打印下会转成中灰，仍然可读——这是刻意选的，
# 不用彩色，避免简历被黑白打印后标题变成一片浅灰看不清。
ACCENT = "1F4E79"
TEXT = "1A1A1A"
BLACK = "000000"
GREY = "595959"
RULE = "BFBFBF"


def set_run_font(run, *, size=None, bold=None, italic=None, name=None, color=None):
    """给 run 设字体：ascii / hAnsi / eastAsia / cs 四件套 + 字号字重颜色。

    颜色必须显式给：默认模板的 Heading 样式带主题蓝（#2E74B5），
    复用样式而不覆盖颜色会让简历标题是蓝的。
    """
    name = name or BODY_FONT
    run.font.name = name
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(color)

    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), name)


def set_para_border(para, *, edge="bottom", color=RULE, width_pt=1.0, space=2):
    """给段落加一条边线——简历分区标题下面那条横线靠它。

    `w:sz` 的单位是 1/8 pt，所以 1pt 要写 8。
    """
    ppr = para._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    el = OxmlElement(f"w:{edge}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(int(round(width_pt * 8))))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color)
    pbdr.append(el)


def strip_table_borders(table):
    """去掉表格的全部边框。

    头部「姓名 + 照片」用无边框表格做左右分栏是 Word 里的标准做法；
    python-docx 默认样式本来就没边框，但显式写一遍更保险——
    文档被套用别的模板样式时不会突然冒出一圈格线。
    """
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "none")
        el.set(qn("w:sz"), "0")
        el.set(qn("w:space"), "0")
        borders.append(el)
    tbl_pr.append(borders)


def set_cell_margins(cell, *, top=0, start=0, bottom=0, end=0):
    """设置单元格内边距（单位 dxa，1/20 pt）。头部表格要贴边，全靠它。"""
    tc_pr = cell._tc.get_or_add_tcPr()
    mar = OxmlElement("w:tcMar")
    for tag, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        el = OxmlElement(f"w:{tag}")
        el.set(qn("w:w"), str(int(value)))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tc_pr.append(mar)


def set_cell_valign(cell, align="center"):
    """设置单元格垂直对齐。照片列用 center，才能跟左边的姓名块对齐。"""
    tc_pr = cell._tc.get_or_add_tcPr()
    el = OxmlElement("w:vAlign")
    el.set(qn("w:val"), align)
    tc_pr.append(el)
