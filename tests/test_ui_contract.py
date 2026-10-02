"""前端契约测试 —— 宣讲会周日历 / 时间冲突检测 / 首次使用向导。

前端是单文件 `app/ui.html`（原生 HTML/CSS/JS，无构建），改动没有编译器兜底：
一个拼错的 id、一个忘了转义的 `${}`、一处写死的颜色，都要等人在浏览器里点到
那一屏才会发现。这里把「新增功能的接口面」固化成断言，用读文件的方式在 CI 里
就能挡住三类回归：

1. **DOM 契约**：新增的 id 必须存在且全局唯一（`$('x')` 取不到会静默变 undefined）。
2. **JS 契约**：新增函数必须有定义；往 innerHTML 里拼数据的函数必须调用 `esc()`（XSS）。
3. **主题契约**：新增片段不得出现硬编码颜色，否则三套主题（apple / light / dark）
   里总有一套会变成白底白字。

另外顺带守住两条边界：不新增后端路由（只用既有 /api/preachs、/api/preach/favs、
/api/status），以及 `<script>` 整体语法可解析（TDZ 之外的低级语法错误）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "app" / "ui.html"

# 新增的 DOM id：周日历视图 / 冲突提醒 / 上手向导
NEW_IDS = [
    "pcViewList",            # 视图切换：列表
    "pcViewWeek",            # 视图切换：周日历
    "preachListView",        # 列表视图容器（表格 + 筛选 + 分页）
    "preachWeekView",        # 周日历视图容器
    "pcwRange",              # 周日历当前日期范围
    "pcwGrid",               # 周日历网格（7 列 × 时间轴）
    "preachConflictBar",     # 冲突提醒条（无冲突时隐藏）
    "preachConflictCount",   # 冲突处数
    "preachConflictToggle",  # 显示 / 关闭冲突明细的开关
    "preachConflictList",    # 冲突对明细容器
    "setupGuide",            # 上手向导卡片
    "setupGuideProgress",    # 向导进度文本
    "setupGuideSteps",       # 向导三步列表
    "btnDismissGuide",       # 「不再显示」
]

# 新增的 JS 函数：写进测试等于给前端函数也建了一份「导出清单」
NEW_FUNCS = [
    # 周日历
    "setPreachView", "syncPreachView", "refreshPreachView", "shiftPreachWeek",
    "loadPreachWeek", "renderPreachWeek", "parsePreachSlot", "pcwPackLanes",
    "pcwMonday", "pcwDateStr", "pcwMinStr", "pcwToMin", "pcwPad2", "pcwAddDays", "pcwDayIndex",
    # 冲突检测
    "scanPreachConflicts", "renderPreachConflictBar", "togglePreachConflictList",
    "syncPreachConflictMarkers",
    "preachConflictListHtml", "preachConflictCard", "pcdayLabel",
    # 上手向导
    "loadSetupGuide", "dismissSetupGuide", "setupGuideDismissed",
]

# 这些函数往 innerHTML 里拼了数据，必须逐个字段过 esc()
HTML_BUILDING_FUNCS = ["renderPreachWeek", "preachConflictListHtml", "preachConflictCard", "loadSetupGuide"]

# 新增片段允许出现的接口（其余 /api/... 都算越界新增后端依赖）
ALLOWED_ENDPOINTS = {"/api/preachs", "/api/preach/favs", "/api/status"}

# 硬编码颜色：三套主题下总有一套会踩雷
COLOR_LITERALS = ["#fff", "#FFF", "#000", "rgb(", "RGB(", "hsl(", "white;", "black;"]


def load_ui() -> str:
    assert UI.is_file(), f"找不到前端文件：{UI}"
    return UI.read_text(encoding="utf-8")


def script_of(html: str) -> str:
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "ui.html 里没有 <script> 块"
    return "\n".join(blocks)


def all_ids(html: str) -> list[str]:
    """抽取全部静态 id；模板串里的 `id="${x}"` 是运行时占位符，不参与唯一性检查。"""
    found = re.findall(r"""(?<![\w-])id\s*=\s*["']([^"']+)["']""", html)
    return [x for x in found if "${" not in x]


def func_chunks(script: str) -> dict[str, str]:
    """把顶层函数切成 {函数名: 函数体}。

    只认顶格声明的 `function` / `async function`——ui.html 里所有顶层函数都是这个
    写法，按「下一个顶格函数声明」切分足够稳定，也不必去写括号匹配器（会被字符串
    和模板串里的花括号骗到）。
    """
    lines = script.splitlines()
    start_idx = [i for i, ln in enumerate(lines)
                 if re.match(r"^(async\s+)?function\s+\w+\s*\(", ln)]
    chunks: dict[str, str] = {}
    for n, i in enumerate(start_idx):
        name = re.match(r"^(?:async\s+)?function\s+(\w+)", lines[i]).group(1)
        end = start_idx[n + 1] if n + 1 < len(start_idx) else len(lines)
        chunks[name] = "\n".join(lines[i:end])
    return chunks


def slice_between(text: str, start_marker: str, end_marker: str) -> str:
    i = text.find(start_marker)
    j = text.find(end_marker, i)
    assert i >= 0, f"缺少片段起始标记：{start_marker!r}"
    assert j > i, f"缺少片段结束标记：{end_marker!r}"
    return text[i:j]


# --------------------------------------------------------------------------
# 1. DOM 契约
# --------------------------------------------------------------------------

def test_new_dom_ids_exist_and_unique():
    html = load_ui()
    ids = all_ids(html)
    for wanted in NEW_IDS:
        hits = [x for x in ids if x == wanted]
        assert len(hits) == 1, f"id={wanted!r} 应恰好出现 1 次，实际 {len(hits)} 次"


def test_all_ids_globally_unique():
    """全文件 id 唯一 —— `$('x')` 只返回第一个，重名会让后半个功能静默失效。"""
    html = load_ui()
    ids = all_ids(html)
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    assert not dupes, f"存在重复 id：{dupes}"


def tag_of(html: str, wanted_id: str) -> str:
    """取包含 `id="wanted_id"` 的那一对尖括号（属性顺序不敏感）。"""
    i = html.find(f'id="{wanted_id}"')
    assert i >= 0, f"找不到 id={wanted_id!r}"
    start = html.rfind("<", 0, i)
    end = html.find(">", i)
    assert start >= 0 and end > i, f"id={wanted_id!r} 不在标签里"
    return html[start:end + 1]


def test_view_switch_defaults_to_list():
    """默认必须是列表：既有用户不该一进页面就被切成日历。"""
    html = load_ui()
    assert re.search(r'class="[^"]*\bon\b', tag_of(html, "pcViewList")), "列表按钮初始未选中"
    assert not re.search(r'class="[^"]*\bon\b', tag_of(html, "pcViewWeek")), "周日历按钮初始不应选中"
    assert 'display:none' in tag_of(html, "preachWeekView"), "周日历初始未隐藏"
    assert "pView: 'list'" in html, "state.pView 默认应为 list"


def test_conflict_bar_hidden_until_conflict():
    """无冲突时不许常驻一条「无冲突」横幅。"""
    html = load_ui()
    bar = slice_between(html, '<div class="pcbar" id="preachConflictBar"', '</div>')
    assert 'style="display:none"' in bar, "冲突提醒条应默认隐藏"
    script = script_of(html)
    body = func_chunks(script)["renderPreachConflictBar"]
    assert "style.display = 'none'" in body, "无冲突时必须把提醒条收起来"


def test_conflict_toggle_is_an_explicit_switch():
    """开关按钮的文案与 aria 三态必须成套：文案、aria-expanded、容器 display 同进同退。

    这里只做静态体检（真行为在 tests/test_ui_preach_conflict.py 里跑 node）。
    静态能守住的是「有没有成套写」：只改文案不改 aria，读屏用户听到的就是错的。
    """
    html = load_ui()
    btn = tag_of(html, "preachConflictToggle")
    assert 'aria-controls="preachConflictList"' in btn, "开关要声明它控制哪个容器"
    assert 'aria-expanded="false"' in btn, "初始必须是收起态"
    # 按钮文案在标签外面，要连文字一起取
    label = slice_between(html, 'id="preachConflictToggle"', "</button>")
    assert "显示冲突" in label, "初始文案应是「显示冲突」，不能是含义含糊的「查看冲突」"
    assert "关闭冲突" not in label

    body = func_chunks(script_of(html))["renderPreachConflictBar"]
    assert "显示冲突" in body and "关闭冲突" in body, "两个方向的文案都要在渲染函数里"
    assert body.count("aria-expanded") >= 2, "显示与关闭两条分支都要同步 aria-expanded"
    assert body.count("btn.textContent") >= 2, "两条分支都要同步按钮文案"
    assert "list.innerHTML = ''" in body, "收起后必须清掉明细 DOM，不能只隐藏"


def test_conflict_markers_hidden_until_switch_on():
    """行内冲突标记默认**不显示**，由「显示冲突」那个开关统一点亮。

    守两件事：

    1. 默认态不能有亮着的标记 —— 满屏整行标红 + ⚠️ 标签，会让人以为这些场次
       本身有问题，而它们只是「和另一场撞了时间」。用户没点开之前不该被抢注意力。
    2. 显隐必须靠 CSS 类切，而不是「重渲染时决定要不要写这个 class」——
       后者每切一次开关都得重跑一次 /api/preachs，而且筛选 / 翻页 / 收藏
       任何一处漏改，就会出现「标记亮着但明细已关」的错位。
    """
    html = load_ui()
    css = slice_between(html, "<style", "</style>")
    for sel in ("body:not(.pconf-on) tr.pcrow-conflict > td",
                "body:not(.pconf-on) .pconf-flag",
                "body:not(.pconf-on) .pcw-ev.conflict"):
        assert sel in css, f"缺少默认隐藏规则：{sel}"
    # 日历卡片要退回它本来的颜色；线上卡片是 --accent2，不能一律退回 --accent
    assert "body:not(.pconf-on) .pcw-ev.conflict.online" in css, "线上卡片会被错染成线下色"

    # ⚠️ 冲突 标签必须有专属类：提醒列也用 .tag.remind，不能靠 .tag.remind 一刀切
    assert 'class="tag remind pconf-flag"' in html, "冲突标签缺少 pconf-flag 专属类"
    assert ".pconf-flag" in css

    script = script_of(html)
    chunks = func_chunks(script)
    assert "syncPreachConflictMarkers" in chunks, "缺少行内标记的同步函数"
    assert "classList.toggle('pconf-on'" in chunks["syncPreachConflictMarkers"], \
        "标记显隐应由 body 上的类控制"
    assert "syncPreachConflictMarkers" in chunks["renderPreachConflictBar"], \
        "开关与行内标记必须由同一个渲染函数同步，否则两处会各自漂移"

    # 标记本身仍要照常写进 DOM（只是被 CSS 藏起来）——否则开关打开时没东西可亮
    rows = chunks["loadPreachs"]
    assert "pcrow-conflict" in rows, "列表行必须照常写上冲突类"
    assert "pConflictOpen" not in rows, \
        "显隐不该由重渲染决定：那样切一次开关就要重跑一次 /api/preachs"


# --------------------------------------------------------------------------
# 2. JS 契约
# --------------------------------------------------------------------------

def test_new_js_functions_defined():
    script = script_of(load_ui())
    chunks = func_chunks(script)
    missing = [f for f in NEW_FUNCS if f not in chunks]
    assert not missing, f"以下函数未定义：{missing}"


def test_html_building_functions_escape():
    """往 innerHTML 拼数据的新函数必须调用 esc()（企业名、地点、时间都是外部数据）。"""
    chunks = func_chunks(script_of(load_ui()))
    for name in HTML_BUILDING_FUNCS:
        body = chunks.get(name, "")
        assert body, f"{name} 未找到"
        assert "esc(" in body, f"{name} 往 innerHTML 拼了数据却没有 esc()"


def test_preach_rows_escape_before_conflict_mark():
    """列表行里新增的冲突标记不能顺手把转义也改掉。"""
    script = script_of(load_ui())
    body = func_chunks(script)["loadPreachs"]
    assert "esc(x['宣讲时间']" in body, "列表行的时间字段必须过 esc()"
    assert 'pcrow-conflict' in body, "冲突场次需要视觉标记"


def test_week_calendar_uses_large_page_size():
    """一周场次很容易超过默认 size=50，取数时必须放大，否则日历会静默缺场次。"""
    body = func_chunks(script_of(load_ui()))["loadPreachWeek"]
    assert "size:200" in body, "周日历应使用 size=200 取该周全部场次"
    assert "show_past:'1'" in body, "周日历必须含过往日，否则本周已过去的场次会消失"


def test_undated_sessions_never_dropped():
    """解析不出时间的场次必须进「时间待定」，不能静默丢弃。"""
    script = script_of(load_ui())
    week = func_chunks(script)["renderPreachWeek"]
    assert "时间待定" in week, "周日历缺少「时间待定」兜底区"
    assert "pcw-nodate" in week, "解析不出日期的场次也要兜底列出"
    parse = func_chunks(script)["parsePreachSlot"]
    assert "开始时间" in parse and "结束时间" in parse, "解析应优先用结构化时间字段"
    assert "PCW_RANGE_RE" in parse, "解析应能从「宣讲时间」文本里兜底抠时间段"


def test_conflict_range_covers_favs_and_visible():
    """冲突检测范围 = 收藏场次 + 当前可见场次，且只走既有接口。"""
    body = func_chunks(script_of(load_ui()))["scanPreachConflicts"]
    assert "/api/preach/favs" in body, "冲突检测必须覆盖收藏场次"
    assert "visibleRows" in body, "冲突检测必须覆盖当前可见场次"
    assert "rangeOk" in body, "解析不出时间段的场次不参与判定"
    assert "pConflictUnknown" in body, "未参与检测的场次要有计数并如实说明"


def test_guide_reads_real_status_and_persists_dismiss():
    """向导依据 /api/status 的真实状态；手动关闭必须写 localStorage。"""
    script = script_of(load_ui())
    body = func_chunks(script)["loadSetupGuide"]
    assert "/api/status" in body
    for field in ("has_api_key", "recruit_count", "analyzed_count"):
        assert field in body, f"向导未依据 {field} 判断完成情况"
    assert "localStorage" in func_chunks(script)["dismissSetupGuide"]
    assert "localStorage" in func_chunks(script)["setupGuideDismissed"]
    assert "box.style.display = 'none'" in body, "三步完成后向导应自动不再出现"


# --------------------------------------------------------------------------
# 3. 主题契约
# --------------------------------------------------------------------------

def test_no_hardcoded_color_in_new_snippets():
    html = load_ui()
    script = script_of(html)
    snippets = {
        "新增 CSS": slice_between(html,
                                "/* ===================== 宣讲会周日历 / 冲突提醒 / 上手向导",
                                "/* ===================== 响应式"),
        "周日历 HTML": slice_between(html, '<div id="preachWeekView"', '</div>\n  </div>'),
        "向导卡片 HTML": slice_between(html, '<div class="card" id="setupGuide"', '</div>'),
        "新增 JS": slice_between(script, "// ================= 宣讲会周日历 =================",
                                 "// ---- Resume / 投递推荐"),
    }
    for label, text in snippets.items():
        for lit in COLOR_LITERALS:
            assert lit not in text, f"{label} 出现硬编码颜色 {lit!r}，主题切换会失效"


def test_new_css_only_uses_theme_variables():
    """新增样式必须挂主题变量，不能自带颜色。"""
    css = slice_between(load_ui(),
                        "/* ===================== 宣讲会周日历 / 冲突提醒 / 上手向导",
                        "/* ===================== 响应式")
    for token in ("--accent", "--danger", "--card-bg", "--border", "--muted"):
        assert token in css, f"新增 CSS 未使用主题变量 {token}"


# --------------------------------------------------------------------------
# 4. 边界：不新增后端路由 / 脚本可解析
# --------------------------------------------------------------------------

def test_new_js_adds_no_backend_route():
    """三项功能只用既有接口，避免与并行的后端改动抢文件。"""
    new_js = slice_between(script_of(load_ui()),
                           "// ================= 宣讲会周日历 =================",
                           "// ---- Resume / 投递推荐")
    used = set(re.findall(r"/api/[a-zA-Z0-9_\-/]+", new_js))
    stray = {u for u in used if u not in ALLOWED_ENDPOINTS}
    assert not stray, f"新增代码引用了约定外的接口（等于要新增后端路由）：{sorted(stray)}"


def test_script_block_is_parseable(tmp_path):
    """<script> 整体语法可解析 —— 括号不匹配 / 低级语法错误在这里就拦住。"""
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("未找到 node，跳过语法检查")
    target = tmp_path / "ui_script_dump.js"
    target.write_text(script_of(load_ui()), encoding="utf-8")
    out = subprocess.run([node, "--check", str(target)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"node --check 失败：{out.stderr}"


def test_module_importable_without_app():
    """本测试只依赖标准库，不该被 app/ 的导入拖住。"""
    assert sys.version_info >= (3, 10)


# --------------------------------------------------------------------------
# 5.「我的」子视图：排列顺序 / 默认页 / 推荐页直接读简历档案
# --------------------------------------------------------------------------

MINE_IDS = [
    "resumeProfileMsg",      # ① 档案来源状态行
    "btnResumeFromProfile",  # 「读取简历档案」按钮
    "btnResumeClear",        # 清空来源（含已选文件）
    "resumePayloadMsg",      # 「本次将用哪份简历」
]

MINE_FUNCS = [
    "setResumeProfileMsg", "profileTextIsEmpty", "refreshResumePayloadMsg",
    "fillResumeFromProfile", "clearResumeText",
]


def mine_subnav(html: str) -> str:
    return slice_between(html, 'aria-label="我的：子视图切换"', "</div>")


def test_mine_subnav_order_is_usage_order():
    """顺序 = 使用顺序：简历档案是数据源，排第一；投递推荐是它的下游消费者，排最后。"""
    views = re.findall(r"setMineView\('(\w+)'\)", mine_subnav(load_ui()))
    assert views == ["profile", "board", "prefs", "rec"], f"子导航顺序不对：{views}"


def test_mine_default_view_is_the_first_tab():
    """默认必须落在子导航第一项，否则「排在最前」和「打开看到的」是两回事。"""
    html = load_ui()
    assert "DEFAULT_MINE_VIEW = 'profile'" in html
    assert "setMineView(DEFAULT_MINE_VIEW)" in html, "初始化没用默认视图常量"
    assert "if (!map[view]) view = DEFAULT_MINE_VIEW;" in html, "未知视图名要回退到默认页"
    assert re.search(r'class="[^"]*\bon\b', tag_of(html, "mineViewProfile")), "简历档案按钮初始未选中"
    assert not re.search(r'class="[^"]*\bon\b', tag_of(html, "mineViewRec")), "投递推荐按钮初始不应选中"
    # 静态 display 初值要与 JS 默认一致，否则首屏会先闪一下推荐页再跳走
    assert "display:none" in tag_of(html, "mineRecView")
    assert "display:none" not in tag_of(html, "mineProfileView")


def test_mine_ids_and_funcs_exist():
    html = load_ui()
    ids = all_ids(html)
    for wanted in MINE_IDS:
        assert ids.count(wanted) == 1, f"id={wanted!r} 应恰好出现 1 次，实际 {ids.count(wanted)} 次"
    chunks = func_chunks(script_of(html))
    missing = [f for f in MINE_FUNCS if f not in chunks]
    assert not missing, f"以下函数未定义：{missing}"


def test_recommend_can_read_profile():
    """推荐页直接调档案：读档案渲染接口，结果落进文本框（用户得看得见拿什么在匹配）。"""
    body = func_chunks(script_of(load_ui()))["fillResumeFromProfile"]
    assert "/api/profile/render" in body and "format=md" in body, "读的应是档案渲染接口"
    assert "box.value = text" in body, "读到的档案必须填进文本框，不能只在后台悄悄拼一份"
    assert "if (!manual){" in body, "自动读要单独判定，不能顺带覆盖用户内容"
    assert "box.value === RESUME_SRC.text" in body, "手动改过的档案文本不该被自动读覆盖"
    assert "profileTextIsEmpty" in body, "空档案不能当成读取成功"
    assert "btn.textContent = '读取简历档案'" in body, "按钮文案跑完要还原，不能留「读取中…」"


def test_recommend_falls_back_to_profile_when_input_empty():
    """三条来源全空时自动读档案 —— 「一键直出」，不要求用户先手点一次读取。"""
    chunks = func_chunks(script_of(load_ui()))
    assert "fillResumeFromProfile(true)" in chunks["resumeRecommend"]
    assert "fillResumeFromProfile(false)" in chunks["setMineView"], "切到推荐页应自动读一次（非覆盖式）"


def test_payload_msg_names_the_source():
    """「本次将用哪份简历」必须如实报出，来源选错时才有线索可查。"""
    body = func_chunks(script_of(load_ui()))["refreshResumePayloadMsg"]
    for token in ("本次将用", "简历档案", "文本框内容", "file.name"):
        assert token in body, f"来源提示缺少 {token!r}"
