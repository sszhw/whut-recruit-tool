"""宣讲会「冲突开关」的**行为**测试（node + DOM 桩），不只是读文件做字符串断言。

这段代码的坑不在「有没有写按钮」，而在开关的**状态一致性**：

- 按钮文案、`aria-expanded`、明细容器的 `display` 三者必须同进同退——
  只改其中一两个，就会出现「按钮写着关闭、明细却是收起的」这种看起来像坏了的界面；
- **行内标记（列表竖条 / 日历红边 / ⚠️ 冲突 标签）与明细共用同一个开关**：
  默认全部隐藏，点开才亮。两者的状态必须一致，否则会出现「标记亮着但明细已关」；
- 无冲突时必须把三者一起复位，否则下次有冲突时用户看到的是上一次的残留文案；
- 91 处冲突 = 91 张卡片，明细必须按天分组，否则平铺出来没法看。

`tests/test_ui_contract.py` 里的静态断言只能证明「写了 `textContent = ...`」，
证明不了「点一下真的开、再点一下真的关」。所以这里把 ui.html 里的函数**原样抽出来**
（不抄实现，抄一份就会各自漂移），用桩替换 DOM 后在 node 里跑真行为。

函数名改了会在第一步就报 MISSING，不会静默变成「测了个空」。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "app" / "ui.html"

# 被测函数：从 ui.html 里原样抽取
WANTED_FUNCS = [
    "renderPreachConflictBar",
    "syncPreachConflictMarkers",
    "togglePreachConflictList",
    "preachConflictListHtml",
    "preachConflictCard",
    "pcdayLabel",
    "pcwMinStr",
    "pcwPad2",
    "esc",
    "companyLink",
]

HARNESS = r"""
// 由 tests/test_ui_preach_conflict.py 写入临时目录后执行，
// argv[2] = app/ui.html 路径，argv[3] = 要抽取的函数名 JSON
const html = require('fs').readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

// 按花括号配对数出每个函数的完整范围，**只取函数自己**：
// ui.html 里两段函数之间还夹着顶格 const（如 REDUCED_MOTION 取 window），
// 用「切到下一个函数声明」的老办法会把这些行一起带进来，在 node 里直接炸。
// 花括号在字符串/模板字面量里都是成对的，所以逐个字符计数是安全的。
const lines = script.split('\n');
const starts = [];
lines.forEach((ln, i) => { if (/^(async\s+)?function\s+\w+\s*\(/.test(ln)) starts.push(i); });
const names = starts.map(i => lines[i].match(/^(?:async\s+)?function\s+(\w+)/)[1]);
function chunkOf(name) {
  const s = starts[names.indexOf(name)];
  if (s == null) return '';
  // opened 判定要看「本行出现过 {」，不能看净深度：单行函数
  // （togglePreachConflictList 就是）在同一行里 { 和 } 都出现，净深度是 0，
  // 于是会一路吞掉后面的 const 和下一个函数。
  let depth = 0, end = -1, opened = false;
  for (let k = s; k < lines.length; k++) {
    for (const ch of lines[k]) { if (ch === '{') depth++; else if (ch === '}') depth--; }
    if (lines[k].includes('{')) opened = true;
    if (opened && depth === 0) { end = k; break; }
  }
  return end < 0 ? '' : lines.slice(s, end + 1).join('\n');
}

const wanted = JSON.parse(process.argv[3]);
const missing = wanted.filter(w => !chunkOf(w));
if (missing.length) { console.error('MISSING ' + JSON.stringify(missing)); process.exit(2); }

// 函数间夹着的顶格 const 要显式带上（只带被测代码真正用到的那几条）
const WANTED_CONSTS = ['const PCDAY_WEEK'];
const consts = WANTED_CONSTS.map(c => {
  const m = script.match(new RegExp('^' + c + '.*$', 'm'));
  if (!m) { console.error('MISSING CONST ' + c); process.exit(2); }
  return m[0];
});

const body = [
  'const state = { pConflicts: [], pConflictIds: new Set(), pConflictUnknown: 0, pConflictOpen: false };',
  'const els = {};',
  "function $ (id){ return (els[id] = els[id] || { id, style: {}, textContent: '', innerHTML: '', attrs: {},",
  '  setAttribute(k, v){ this.attrs[k] = v; }, getAttribute(k){ return this.attrs[k]; } }); }',
  // body.classList 的桩：syncPreachConflictMarkers 靠它切显隐，
  // 抽出来单独看，才能断言「开关一关标记真的灭了」，而不是只看按钮文字。
  'const bodyCls = new Set();',
  'const document = { body: { classList: { toggle(n, v){ if (v) bodyCls.add(n); else bodyCls.delete(n); },',
  '  contains(n){ return bodyCls.has(n); } } } };',
  ...consts,
  ...wanted.map(chunkOf),
  'return { state, els, bodyCls, renderPreachConflictBar, togglePreachConflictList, preachConflictListHtml };',
].join('\n');

const util = new Function(body)();

const mk = (date, s, e, a, b) => ({ date, start: s, end: e, overlap: Math.floor((e - s) / 2),
  a: { '单位名称': a, '宣讲会地点': a + '楼' }, b: { '单位名称': b, '宣讲会地点': b + '楼' } });

const out = [];
const check = (name, ok, extra) => out.push([name, !!ok, extra || '']);
const views = () => {
  const bar = util.els.preachConflictBar, list = util.els.preachConflictList, btn = util.els.preachConflictToggle;
  return { bar: bar && bar.style.display, list: list && list.style.display,
           html: (list && list.innerHTML) || '', label: btn && btn.textContent,
           aria: btn && btn.getAttribute('aria-expanded') };
};
// 行内标记（列表竖条 / 日历红边 / ⚠️ 冲突 标签）的显隐开关
const marks = () => util.bodyCls.has('pconf-on');

// ---- 有冲突：初始收起，点一下展开，再点收回
util.state.pConflicts = [
  mk('2026-10-05', 540, 660, '甲公司', '乙公司'),
  mk('2026-10-05', 600, 720, '丙公司', '丁公司'),
  mk('2026-10-06', 840, 900, '戊公司', '己公司'),
];
util.renderPreachConflictBar();
let v = views();
check('有冲突时提醒条出现', v.bar === '', String(v.bar));
check('初始按钮文案是「显示冲突」', v.label === '显示冲突', v.label);
check('初始不渲染明细', v.list === 'none' && v.html === '');
check('初始 aria-expanded=false', v.aria === 'false', String(v.aria));
check('初始隐藏行内冲突标记', marks() === false, String(marks()));

util.togglePreachConflictList();                       // 点一下 = 显示冲突
v = views();
check('点一下明细展开', v.list === '', String(v.list));
check('展开后按钮文案变「关闭冲突」', v.label === '关闭冲突', v.label);
check('展开后 aria-expanded=true', v.aria === 'true', String(v.aria));
check('展开后才亮出行内冲突标记', marks() === true, String(marks()));
check('明细含全部冲突卡片', (v.html.match(/class="pccard"/g) || []).length === 3,
      String((v.html.match(/class="pccard"/g) || []).length));
check('展开时按钮与容器状态一致', v.list === '' && v.label === '关闭冲突');

util.togglePreachConflictList();                       // 再点一下 = 关闭冲突
v = views();
check('再点一下明细收回', v.list === 'none', String(v.list));
check('收回后按钮文案回到「显示冲突」', v.label === '显示冲突', v.label);
check('收回后 aria-expanded=false', v.aria === 'false', String(v.aria));
check('收回后行内冲突标记一并隐藏', marks() === false, String(marks()));
check('收回后清空明细 DOM', v.html === '');

// ---- 展开状态要能扛过重算：改筛选后重新扫描，不该把用户展开的明细关掉
util.togglePreachConflictList();
util.renderPreachConflictBar();                        // 模拟 loadPreachs 里的重渲染
v = views();
check('重算后保持展开（不被关掉）', v.list === '' && v.label === '关闭冲突');
check('重算后行内标记仍是亮的', marks() === true, String(marks()));

// ---- 按天分组：2 个日期 → 2 组，组标题带当天处数与星期
check('明细按天分组', (v.html.match(/class="pccday"/g) || []).length === 2,
      String((v.html.match(/class="pccday"/g) || []).length));
check('分组标题写明日期与星期', /2026-10-05 · 周一/.test(v.html) && /2026-10-06 · 周二/.test(v.html));
check('分组标题带当天处数', /2 处/.test(v.html) && /1 处/.test(v.html));
check('卡片里不再重复日期', !/2026-10-05<\/div>/.test(v.html) && v.html.indexOf('🕒') > 0);

// ---- 转义：企业名是外部数据，拼进 innerHTML 前必须过 esc()
util.state.pConflicts = [mk('2026-10-05', 540, 660, '<img src=x onerror=1>', '正常公司')];
util.renderPreachConflictBar();
v = views();
check('企业名里的尖括号被转义', v.html.indexOf('<img src=x') < 0 && v.html.indexOf('&lt;img') > 0);

// ---- 时间待定的场次：如实说明，不能假装没有
util.state.pConflictUnknown = 4;
util.renderPreachConflictBar();
v = views();
check('时间不明场次会如实提示', /另有 4 场因时间格式不明未参与检测/.test(v.html));

// ---- 冲突清空（改筛选 / 收藏取消）：条、按钮、明细一起复位，不留残留文案
util.state.pConflicts = [];
util.state.pConflictUnknown = 0;
util.renderPreachConflictBar();
v = views();
check('无冲突时提醒条隐藏', v.bar === 'none', String(v.bar));
check('无冲突时按钮文案复位', v.label === '显示冲突', v.label);
check('无冲突时明细清空并收起', v.list === 'none' && v.html === '');
check('无冲突时 aria-expanded=false', v.aria === 'false', String(v.aria));
check('无冲突后状态复位为收起', util.state.pConflictOpen === false);
check('无冲突时行内标记也一并隐藏', marks() === false, String(marks()));

// ---- 复位之后再出现冲突：仍是收起态，不该自动弹开一屏，也不该顺手把标记点亮
util.state.pConflicts = [mk('2026-10-05', 540, 660, '甲公司', '乙公司')];
util.renderPreachConflictBar();
v = views();
check('再次出现冲突时仍是收起态', v.list === 'none' && v.label === '显示冲突');
check('再次出现冲突时标记仍是隐藏的', marks() === false, String(marks()));

console.log(JSON.stringify(out));
"""


def _node() -> str:
    return shutil.which("node") or shutil.which("node.exe") or ""


@pytest.fixture(scope="module")
def node_result(tmp_path_factory) -> list[list]:
    node = _node()
    if not node:
        pytest.skip("未找到 node，跳过前端行为测试")
    assert UI.is_file(), f"找不到前端文件：{UI}"
    target = tmp_path_factory.mktemp("ui_js") / "preach_conflict_probe.js"
    target.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(target), str(UI), json.dumps(WANTED_FUNCS, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr[:800]}\n{proc.stdout[:800]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_conflict_toggle_behaviour(node_result):
    failed = [f"{name}（{extra}）" for name, ok, extra in node_result if not ok]
    assert not failed, "冲突开关的行为被改坏了：" + "；".join(failed)


def test_conflict_toggle_cases_are_not_silently_empty(node_result):
    """桩跑空不算通过：用例数少说明抽取逻辑失效（比如函数改名后没被测到）。"""
    assert len(node_result) >= 30, f"实际只跑了 {len(node_result)} 项"
