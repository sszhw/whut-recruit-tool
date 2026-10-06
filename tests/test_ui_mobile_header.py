"""手机顶栏（竖屏）的契约与行为测试。

背景：顶栏是 `position: sticky` 的，而在手机上它一度长到 8 行、约 460px——
标题一行、数据时间一行、徽标一行，再加上 4 个被 `button.btn { width: 100% }`
拉成全宽的按钮各占一行。结果就是「上面这个框挡住了大半个界面」。

修法是把顶栏分成两级：常驻的 `.hd-bar`（标题 / 数据新鲜度 / Key 状态 / 更新数据）
与次要动作 `.hd-more`（数据维护 / 任务中心 / 设置 / 主题）。宽屏下两个容器都是
`display: contents`（盒子拆掉，子元素照旧平铺，逐像素不变）；手机上 `.hd-bar`
固定两行网格、`.hd-more` 收进「⋯」浮层。

这里守三类回归：

1. **宽屏不许被改样**：两个容器必须还是 `display: contents`，「⋯」与遮罩必须 `display:none`。
   哪天有人给它们加上 `display:block`，桌面顶栏就会多出两个块级盒子、断成两行。
2. **窄屏两行不许退化成多行**：`.hd-bar` 必须是写死两行的 `grid-template-areas`，
   而不是靠 flex 换行——换行在 320px 上会把「⋯」甩到第二行开头（实测过）。
   同时「顶栏按钮不许被全局 `button.btn{width:100%}` 拉满」这条覆盖必须存在。
3. **浮层开关的状态一致性**（真行为，node 里跑）：class、遮罩、`aria-expanded`
   三者同进同退；点浮层外 / Esc 关闭，点浮层内不关（否则点一次菜单等于开完立刻关）。

静态断言只能证明「写了这行 CSS」，证明不了「点一下真的开、再点一下真的关」，
所以第 3 类把 `ui.html` 里的那段代码**原样抽出来**在 node 里跑（不抄实现）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "app" / "ui.html"
MOBILE_MAX = 640  # 与 ui.html 里的窄屏断点一致

HARNESS = r"""
// 由 tests/test_ui_mobile_header.py 写入临时目录后执行
// argv[2] = app/ui.html 路径
const html = require('fs').readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

// 「更多」浮层这一段是自洽的：一个函数 + 两个 document 监听器，不依赖别处的变量。
// 用行号切片而不是正则抽函数，是为了把**监听器**也一起测到（它们不是具名函数）。
const START = script.indexOf('// ---- 顶栏「更多」浮层');
const END = script.indexOf('function taskCard(', START);
if (START < 0 || END < START) { console.error('MISSING 顶栏「更多」浮层区块'); process.exit(2); }
const block = script.slice(START, END);
if (!/function toggleHdMore\s*\(/.test(block)) { console.error('MISSING toggleHdMore'); process.exit(2); }

const els = {};
function mkEl(id) {
  const set = new Set();
  return {
    id, attrs: {},
    classList: {
      contains(n) { return set.has(n); },
      add(n) { set.add(n); },
      remove(n) { set.delete(n); },
      toggle(n, v) { const on = (v === undefined) ? !set.has(n) : !!v; on ? set.add(n) : set.delete(n); },
    },
    _has(n) { return set.has(n); },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
  };
}
function $(id) { return (els[id] = els[id] || mkEl(id)); }

const listeners = {};
const document = { addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); } };

const util = new Function('$', 'document', block + '\nreturn { toggleHdMore };')($, document);
const { toggleHdMore } = util;

const out = [];
const check = (name, ok, extra) => out.push([name, !!ok, extra || '']);

const panel = () => els.hdMore;
const button = () => els.btnHdMore;
const mask = () => els.hdMask;
const isOpen = () => !!(panel() && panel()._has('open'));
const aria = () => (button() ? button().getAttribute('aria-expanded') : null);
const maskShown = () => !!(mask() && mask()._has('show'));

// 桩：closest 只认浮层自己与「⋯」按钮，其余一律当作浮层外
const INSIDE = { closest: s => (s === '#hdMore' || s === '#btnHdMore') ? {} : null };
const OUTSIDE = { closest: () => null };
const NO_CLOSEST = {};   // 纯文本节点之类：没有 closest，代码里必须判空而不是直接调

const fireClick = t => (listeners.click || []).forEach(fn => fn({ target: t }));
const fireEsc = () => (listeners.keydown || []).forEach(fn => fn({ key: 'Escape' }));
const fireKey = k => (listeners.keydown || []).forEach(fn => fn({ key: k }));

// ---- 1. 初始态：三个东西都没开
check('初始 未打开', !isOpen());
check('初始 遮罩未显示', !maskShown());

// ---- 2. 打开：class / 遮罩 / aria 三处都要动
toggleHdMore(true);
check('打开 → 浮层 open', isOpen());
check('打开 → 遮罩 show', maskShown());
check('打开 → aria-expanded=true', aria() === 'true', 'aria=' + aria());

// ---- 3. 重复打开是幂等的（SSE 重算 / 连点两下不该把它关掉）
toggleHdMore(true);
check('重复打开 仍为开', isOpen() && maskShown() && aria() === 'true');

// ---- 4. 关闭：三处一起复位
toggleHdMore(false);
check('关闭 → 浮层收起', !isOpen());
check('关闭 → 遮罩隐藏', !maskShown());
check('关闭 → aria-expanded=false', aria() === 'false', 'aria=' + aria());

// ---- 5. 无参调用＝取反：点一下开，再点一下关
toggleHdMore();
check('无参第一次 → 开', isOpen() && maskShown());
toggleHdMore();
check('无参第二次 → 关', !isOpen() && !maskShown());

// ---- 6. 点浮层里（含「⋯」自己）：监听器不许关，交给按钮自己的 onclick 决定
toggleHdMore(true);
fireClick(INSIDE);
check('点浮层内 不关', isOpen(), '点了一下菜单项就被关掉，等于点不开');
fireClick({ closest: s => (s === '#btnHdMore' ? {} : null) });
check('点「⋯」 不关', isOpen(), '监听器与按钮 onclick 打架，一次点击会开→关');

// ---- 7. 点浮层外：关。遮罩接住的就是这一下
fireClick(OUTSIDE);
check('点浮层外 → 关', !isOpen() && !maskShown());

// ---- 8. 关着的时候点外面：不该抛错，也不该被打开
fireClick(OUTSIDE);
check('关着点外面 保持关', !isOpen());

// ---- 9. 目标没有 closest（文本节点）：不能抛错，且应视为浮层外
toggleHdMore(true);
let threw = null;
try { fireClick(NO_CLOSEST); } catch (e) { threw = e; }
check('目标无 closest 不抛错', threw === null, String(threw));
check('目标无 closest 视为浮层外 → 关', !isOpen());

// ---- 10. Esc 关闭；浮层没开时按 Esc 不抛错
toggleHdMore(true);
fireEsc();
check('Esc → 关', !isOpen() && !maskShown());
threw = null;
try { fireEsc(); } catch (e) { threw = e; }
check('关着按 Esc 不抛错', threw === null, String(threw));

// ---- 11. 其他键不关（顶栏浮层不该吞掉所有按键）
toggleHdMore(true);
fireKey('a');
check('按字母键 不关', isOpen());

// ---- 12. 缺元素时直接返回：老页面 / 片段化 HTML 不该炸
//      （把 stub 换成"永远查不到"，再调一次）
const utilNoEl = new Function('$', 'document', block + '\nreturn { toggleHdMore };')(
  () => null, { addEventListener() {} });
threw = null;
try { utilNoEl.toggleHdMore(true); } catch (e) { threw = e; }
check('元素缺失 不抛错', threw === null, String(threw));

console.log(JSON.stringify(out));
"""


def load_ui() -> str:
    return UI.read_text(encoding="utf-8")


def css_text() -> str:
    html = load_ui()
    return "\n".join(re.findall(r"<style[^>]*>([\s\S]*?)</style>", html))


def _blocks_at(width: int) -> list[str]:
    css = css_text()
    out = []
    for m in re.finditer(r"@media \(max-width: " + str(width) + r"px\)\{", css):
        depth = 0
        for j in range(css.index("{", m.start()), len(css)):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append(css[m.start():j + 1])
                    break
    return out


def mobile_block() -> str:
    """取最后一个 ≤640px 媒体查询块（窄屏顶栏规则都在那里，且必须是最后生效的一份）。"""
    blocks = _blocks_at(640)
    assert blocks, "找不到 640px 断点"
    return blocks[-1]


def all_mobile_blocks() -> str:
    """全部 ≤640px 媒体查询块拼接后返回。

    窄屏规则本来就该分散在三处：基础样式管表单与控件尺寸、苹果主题管圆角间距、
    最后一块管顶栏网格。各自写在负责的区域里比硬堆在一起好维护，
    代价是校验 tapping 目标、输入字号这类跨区域的规则时要跨块扫。
    """
    return "\n".join(_blocks_at(640))


def tag_of(wanted_id: str) -> str:
    html = load_ui()
    i = html.find(f'id="{wanted_id}"')
    assert i >= 0, f"找不到 id={wanted_id!r}"
    return html[html.rfind("<", 0, i):html.find(">", i) + 1]


def _node() -> str | None:
    return shutil.which("node")


@pytest.fixture(scope="module")
def node_result(tmp_path_factory) -> list[list]:
    node = _node()
    if not node:
        pytest.skip("未找到 node，跳过前端行为测试")
    assert UI.is_file(), f"找不到前端文件：{UI}"
    target = tmp_path_factory.mktemp("ui_js") / "hd_more_probe.js"
    target.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(target), str(UI)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr[:800]}\n{proc.stdout[:800]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# 1. 宽屏：结构拆了、视觉没拆
# --------------------------------------------------------------------------

def test_desktop_keeps_the_original_single_row():
    """两个分组容器在宽屏下必须把盒子拆掉，否则顶栏会多出两个块级盒子、断成两行。"""
    css = css_text()
    m = re.search(r"\.hd-bar\s*,\s*\.hd-more\s*\{[^}]*\}", css)
    assert m, "缺少 .hd-bar / .hd-more 的基础规则"
    assert "display: contents" in m.group(0), "宽屏两个容器必须 display:contents，否则桌面顶栏会变形"


def test_more_button_and_mask_are_mobile_only():
    css = css_text()
    m = re.search(r"#btnHdMore\s*,\s*\.hd-mask\s*\{([^}]*)\}", css)
    assert m, "缺少 #btnHdMore / .hd-mask 的基础规则"
    assert "display: none" in m.group(1), "「⋯」与遮罩在宽屏必须是隐藏的"
    # 触发器的无障碍契约：说明它控制谁、当前开没开
    btn = tag_of("btnHdMore")
    assert 'aria-controls="hdMore"' in btn and 'aria-expanded="false"' in btn, \
        "「⋯」按钮缺少 aria-controls / 初始 aria-expanded"
    assert "aria-label" in btn, "「⋯」是图标按钮，必须有可读的名字"
    assert "⋯" in load_ui(), "「⋯」不见了"


def test_header_groups_cover_every_control_exactly_once():
    """分组只搬家不改名：id 与 onclick 全部原样，别处引用（activateTab 高亮等）才不会断。"""
    html = load_ui()
    bar_start = html.index('<div class="hd-bar">')
    more_start = html.index('<div class="hd-more" id="hdMore"')
    bar, more = html[bar_start:more_start], html[more_start:html.index("</header>")]
    for token in ("btnQuickUpdate", "btnQuickUpdateStop", "statusBadge", "hdUpdated", "btnHdMore"):
        assert token in bar, f"{token} 应留在常驻组 .hd-bar 里"
    for token in ("btnDataMaintain", "btnTaskCenter", "taskRunningBadge", "btnSettings", "themeSel"):
        assert token in more, f"{token} 应放进次要组 .hd-more 里"
    # 隐藏页高亮逻辑按这两个 id 找按钮，不能因为分组而失效
    assert "'#btnDataMaintain, #btnSettings'" in html or '#btnDataMaintain, #btnSettings' in html, \
        "activateTab 里按 id 找按钮的高亮逻辑被破坏了"


# --------------------------------------------------------------------------
# 2. 窄屏：两行、可点、不被全局规则拉满
# --------------------------------------------------------------------------

def test_mobile_header_is_a_fixed_two_row_grid():
    """不能用 flex 换行：换行在 320px 上会把「⋯」甩到第二行开头、徽标掉到第三行。"""
    block = mobile_block()
    m = re.search(r"\.hd-bar\s*\{([^}]*)\}", block)
    assert m, "窄屏缺少 .hd-bar 规则"
    body = m.group(1)
    assert "display: grid" in body, "窄屏顶栏应为网格布局（换行排布会在窄屏散架）"
    areas = re.search(r"grid-template-areas:\s*([^;]+);", block)
    assert areas, "缺少 grid-template-areas"
    rows = re.findall(r'"([^"]*)"', areas.group(1))
    assert len(rows) == 2, f"顶栏应为写死的两行，实际 {len(rows)} 行"
    names = set(" ".join(rows).split())
    assert {"title", "upd", "more", "meta", "badge"} <= names, f"网格区域缺项：{names}"
    for area in ("title", "upd", "more", "meta", "badge"):
        assert f"grid-area: {area}" in block, f"没有元素落到 {area} 区"


def test_mobile_header_buttons_are_not_stretched_full_width():
    """根因就在这：全局 `button.btn{width:100%}` 会把顶栏按钮拉成全宽、各占一行。

    这里同时校验**级联顺序**：覆盖规则必须写在全局规则之后，
    否则以后有人调整顺序、或改回同名选择器，这行覆盖会静默失效。
    """
    css = css_text()
    global_i = css.find("button.btn { width: 100%")
    assert global_i >= 0, "全局「手机表单按钮全宽」规则不见了，请同步更新本测试"
    override_i = css.find("header .hd-bar button.btn { width: auto")
    assert override_i >= 0, "缺少顶栏按钮的覆盖规则：会被全局 width:100% 拉成全宽堆叠"
    assert override_i > global_i, "覆盖规则必须写在全局规则之后"


def test_mobile_overflow_menu_is_a_popover_closed_by_default():
    block = mobile_block()
    assert re.search(r"\.hd-more\s*\{[^}]*display:\s*none", block, re.S), \
        "浮层默认必须是收起的（display:none），不能占版面"
    assert re.search(r"\.hd-more\.open\s*\{[^}]*display:\s*grid", block, re.S), \
        "缺少展开态"
    m = re.search(r"\.hd-more\s*\{[^}]*\}", block, re.S)
    assert m and re.search(r"top:\s*calc\(100%\s*\+\s*\d+px\)", m.group(0)), \
        "浮层应贴顶栏下沿展开（top: calc(100% + Npx)），并留出一条缝不压住顶栏发丝线"


def test_mobile_mask_sits_below_the_header():
    """遮罩的作用是接住「点浮层外」那一下。它必须压在页面之上、顶栏之下：

    - 压不住页面 → 手指点到浮层下面的导航，会被下面那颗按钮抢走（想切页却触发了数据维护）；
    - 盖住顶栏 → 「⋯」和主操作点不动了。
    """
    block = mobile_block()
    m = re.search(r"\.hd-mask\.show\s*\{([^}]*)\}", block)
    assert m, "缺少遮罩的展开态"
    body = m.group(1)
    assert "position: fixed" in body and "inset: 0" in body, "遮罩要铺满视口"
    mask_z = int(re.search(r"z-index:\s*(\d+)", body).group(1))
    header_z = int(re.search(r"position: sticky; top: [^;]*; z-index:\s*(\d+)",
                             css_text()).group(1))
    assert mask_z < header_z, f"遮罩 z-index({mask_z}) 必须低于顶栏({header_z})，否则顶栏点不动"


def test_menu_items_close_the_menu_when_picked():
    """选中任一项后浮层要自己收起，否则切到「数据维护」后还盖着导航。"""
    html = load_ui()
    more = html[html.index('<div class="hd-more" id="hdMore"'):html.index("</header>")]
    for token in ("btnDataMaintain", "btnTaskCenter", "btnSettings"):
        tag = tag_of(token)
        assert "toggleHdMore(false)" in tag, f"{token} 选中后没有收起浮层"
    assert 'id="themeSel"' in more, "主题选择器应放进浮层"
    assert 'onclick="toggleHdMore(false)"' in tag_of("hdMask"), "遮罩没有绑关闭"


def test_hidden_pages_mark_the_overflow_trigger():
    """「数据维护 / 设置」不占导航位：宽屏靠顶栏按钮高亮，手机上那两颗在浮层里看不见，
    所以「⋯」自己要被标出来，否则切过去以后完全不知道自己在哪一页。"""
    block = mobile_block()
    assert "#btnHdMore.active" in block, "缺少「⋯」的当前页标记样式"
    js = "\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", load_ui()))
    assert re.search(r"btnHdMore'\)[\s\S]{0,80}classList\.toggle\('active'", js), \
        "activateTab 没有把当前页状态同步到「⋯」上"


def test_mobile_tap_targets_are_large_enough():
    """手机上所有可点元素统一到 44px（--tap）。

    依据是 iOS 44pt / Android 48dp 的最小点击面积，取两者下限 44 作为统一刻度。
    此前顶栏按钮写的是 40px、勾选片 .chip 是 38px —— 40 那还不是最糟的，38px 的
    勾选片实际用起来是「点两次命中一次」。纯图标的「⋯」两个方向都得自己撑满。
    """
    block = all_mobile_blocks()
    assert re.search(r"header \.hd-bar button\.btn\s*\{[^}]*min-height:\s*var\(--tap\)", block, re.S), \
        "顶栏按钮未统一到 44px（--tap）"
    assert re.search(r"\.hd-more button\.btn\s*\{[^}]*min-height:\s*var\(--tap\)", block, re.S), \
        "浮层里的项应为 44px（--tap）"
    assert re.search(r"#btnHdMore\s*\{[^}]*width:\s*var\(--tap\)[^}]*height:\s*var\(--tap\)", block, re.S), \
        "「⋯」是纯图标按钮，宽高都要撑到 44×44"
    for sel in (r"\.chip\s*\{", r"\.pfilter-check\s*\{", r"\.favbtn\s*\{"):
        m = re.search(sel + r"[^}]*\}", block, re.S)
        assert m and re.search(r"min-height:\s*var\(--tap\)|min-height:\s*44px", m.group(0)), \
            f"{sel} 的最小点击高度低于 44px"


def test_inputs_do_not_trigger_ios_focus_zoom():
    """输入控件字号必须 ≥16px，否则 iOS Safari / Chrome 会做「聚焦自动放大」。

    表现是每次点输入框整页放大一次再缩回 —— 填一张网申表要抖十几次。
    16px 是唯一解法：text-size-adjust 对这条不生效，禁缩放又伤无障碍。
    选择器里显式带上 .prof-kv 是因为那里有一条 `font: inherit`，简写会把 font-size
    重置成继承值（14px），只有同选择器排在后才能压住。
    """
    block = all_mobile_blocks()
    m = re.search(r"input, select, textarea,[^}]*\{([^}]*)\}", block, re.S)
    assert m and re.search(r"font-size:\s*16px", m.group(1)), "缺少 ≥16px 的输入字号兜底"
    assert ".prof-kv textarea" in block[block.index("input, select, textarea,"):][:200], \
        "必须显式覆盖 .prof-kv textarea 的 `font: inherit`"
    # 手机断点里不能再出现任何小于 16px 的输入字号规则
    for bad in re.finditer(r"(input|select|textarea)[^{]*\{([^}]*)\}", block, re.S):
        fs = re.search(r"font-size:\s*(\d+)px", bad.group(2))
        if fs:
            assert int(fs.group(1)) >= 16, f"手机断点里 {bad.group(0)[:40]} 的字号小于 16px"


def test_safe_area_insets_are_wired_up():
    """viewpor-fit=cover 与安全区：开了一个必须把另一个补齐。

    只加 viewport-fit=cover 不加 env() → 内容被刘海 / 底部手势条压住；
    只加 env() 不加 viewport-fit=cover → env() 恒为 0，写了也白写（还假装已经适配）。
    所以这两条一起验，防止以后有人删掉其中一半。
    """
    html = load_ui()
    vp = re.search(r'<meta name="viewport" content="([^"]*)"', html)
    assert vp and "viewport-fit=cover" in vp.group(1), "viewport 缺 viewport-fit=cover"
    css = css_text()
    assert "env(safe-area-inset-bottom, 0px)" in css, "底部手势条没有补偿"
    assert "env(safe-area-inset-left, 0px)" in css, "横屏刘海左侧没有补偿"
    # fixed 元素不参与 body 的内边距，必须自己补第三条边
    for sel in (".taskpanel", ".cpdrawer"):
        m = re.search(re.escape(sel) + r"\s*\{[^}]*\}", css, re.S)
        assert m and "env(safe-area-inset-right" in m.group(0), \
            f"{sel} 是 fixed 抽屉，补不全横向安全区"


def test_text_size_adjust_is_pinned():
    """横屏 / 系统大字号下 Android Chrome 会把字体按自己的比例再放大一次，
    结果是字号与 8px 间距刻度对不上，卡片里的行高看着忽松忽紧。"""
    css = css_text()
    assert "text-size-adjust: 100%" in css, "缺少 -webkit-text-size-adjust 兜底"


def test_no_hardcoded_color_in_new_header_css():
    """顶栏新样式必须挂主题变量，否则两套配色里必有一套翻车。"""
    block = mobile_block()
    for lit in ("#fff", "#000", "rgb(", "rgba(255,255,255", "#0071e3", "#F5F5F7"):
        assert lit not in block, f"窄屏顶栏样式出现硬编码颜色 {lit!r}"
    css = css_text()
    m = re.search(r"\.hd-bar\s*,\s*\.hd-more\s*\{[^}]*\}", css)
    assert "display: contents" in m.group(0)


# --------------------------------------------------------------------------
# 3. 浮层开关的真行为（node + DOM 桩）
# --------------------------------------------------------------------------

def test_hd_more_toggle_behaviour(node_result):
    failed = [f"{name}（{extra}）" for name, ok, extra in node_result if not ok]
    assert not failed, "顶栏「更多」开关被改坏了：" + "；".join(failed)


def test_hd_more_cases_are_not_silently_empty(node_result):
    """桩跑空不算通过：用例数少说明抽取逻辑失效（区块被挪走或函数改名）。"""
    assert len(node_result) >= 18, f"实际只跑了 {len(node_result)} 项"
