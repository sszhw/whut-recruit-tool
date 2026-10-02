"""推荐页「简历来源」的**行为**测试（node + DOM 桩），不只是读文件做字符串断言。

为什么值得单独跑一次 node：这段逻辑的坑不在「有没有写」，而在**优先级和覆盖规则**——

- 切到推荐页会自动读一次档案（档案是数据源，改了就该立刻生效）；
- 但自动读**不许**顶掉用户自己粘的简历，也不许把用户手动删掉的那几行补回来；
- 而手动点「读取简历档案」是明确的强制替换意图，必须覆盖。

三条规则互相冲突，只有真跑一遍才分得清谁先谁后。`tests/test_ui_contract.py` 那种
静态断言只能证明「写了 if (!manual)」，证明不了「自动读没把用户粘的简历冲掉」。
所以这里把 ui.html 里的几个函数原样抽出来，用桩替换 DOM / 网络后在 node 里跑。

函数名改了会在第一步就报错（找不到函数），不会静默变成「测了个空」。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "app" / "ui.html"

# 被测函数：从 ui.html 里原样抽取，不抄一份实现（抄一份就会各自漂移）
WANTED_FUNCS = [
    "setResumeProfileMsg",
    "profileTextIsEmpty",
    "refreshResumePayloadMsg",
    "fillResumeFromProfile",
    "clearResumeText",
]

HARNESS = r"""
// 由 tests/test_ui_resume_source.py 写入临时目录后执行，argv[2] = app/ui.html 路径
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

// 按「下一个顶格函数声明」切块（与 tests/test_ui_contract.py 的 func_chunks 同一套办法）
const lines = script.split('\n');
const starts = [];
lines.forEach((ln, i) => { if (/^(async\s+)?function\s+\w+\s*\(/.test(ln)) starts.push(i); });
const chunks = {};
starts.forEach((s, n) => {
  const name = lines[s].match(/^(?:async\s+)?function\s+(\w+)/)[1];
  chunks[name] = lines.slice(s, n + 1 < starts.length ? starts[n + 1] : lines.length).join('\n');
});

const wanted = JSON.parse(process.argv[3]);
const missing = wanted.filter(w => !chunks[w]);
if (missing.length) { console.error('MISSING ' + JSON.stringify(missing)); process.exit(2); }

const declLine = lines.find(l => l.startsWith('let RESUME_SRC'));
if (!declLine) { console.error('MISSING RESUME_SRC'); process.exit(2); }
const body = [declLine, ...wanted.map(w => chunks[w])].join('\n') +
  '\nreturn { fillResumeFromProfile, refreshResumePayloadMsg, clearResumeText };';

function harness(profileText, reply) {
  // <input type=file> 的 files 是只读列表：浏览器里 value='' 会同时清掉已选文件，
  // 桩要照这个语义来，否则「清空」用例会假失败（代码依赖的正是这个标准行为）。
  const fileInput = { _v: '' , files: [] };
  Object.defineProperty(fileInput, 'value', {
    get() { return this._v; },
    set(v) { this._v = v; if (!v) this.files = []; },
  });
  const els = {
    resumeText: { value: '' },
    resumePayloadMsg: { textContent: '' },
    resumeProfileMsg: { textContent: '' },
    btnResumeFromProfile: { disabled: false, textContent: '读取简历档案' },
    resumeFile: fileInput,
  };
  let calls = 0;
  const api = async () => { calls++; return reply || { ok: true, text: profileText }; };
  const util = new Function('$', 'api', 'toast', body)(id => els[id] || null, api, () => {});
  return { els, util, calls: () => calls };
}

// 公开仓库：夹具用示例姓名与示例学号，不能出现真实个人信息
const PROFILE = '# 张三\n## 基本信息\n- 姓名：张三\n- 学号：2021000001';
const out = [];
const check = (name, ok, extra) => out.push([name, !!ok, extra || '']);

(async () => {
  // 空框 → 自动读一次，填入档案并如实报出来源
  let h = harness(PROFILE);
  await h.util.fillResumeFromProfile(false);
  check('空框自动读填入档案', h.els.resumeText.value === PROFILE, h.els.resumeText.value.slice(0, 40));
  check('状态行说与档案一致', /与档案一致/.test(h.els.resumeProfileMsg.textContent), h.els.resumeProfileMsg.textContent);
  check('来源提示写明用了简历档案', /本次将用：简历档案/.test(h.els.resumePayloadMsg.textContent), h.els.resumePayloadMsg.textContent);

  // 用户自己粘的内容 → 自动读不许覆盖，且不该白跑一次请求
  h = harness(PROFILE);
  h.els.resumeText.value = '我自己粘的简历';
  await h.util.fillResumeFromProfile(false);
  check('自动读不覆盖用户粘贴的内容', h.els.resumeText.value === '我自己粘的简历');
  check('有内容时自动读不发请求', h.calls() === 0, String(h.calls()));

  // 框里还是上次读的档案、没动过 → 切回来要刷新（档案可能刚改过）
  h = harness(PROFILE);
  await h.util.fillResumeFromProfile(false);
  await h.util.fillResumeFromProfile(false);
  check('没动过的档案文本会被刷新', h.calls() === 2 && h.els.resumeText.value === PROFILE, String(h.calls()));

  // 手动改过 → 不许被补回来，且状态行要改口
  h = harness(PROFILE);
  await h.util.fillResumeFromProfile(false);
  h.els.resumeText.value = PROFILE.replace('- 学号：2021000001', '');
  await h.util.fillResumeFromProfile(false);
  check('手动改过的内容不被自动读补回', !/学号/.test(h.els.resumeText.value));
  check('状态行提示已手动修改', /已手动修改/.test(h.els.resumeProfileMsg.textContent), h.els.resumeProfileMsg.textContent);
  check('来源提示改口为文本框内容', /本次将用：文本框内容/.test(h.els.resumePayloadMsg.textContent), h.els.resumePayloadMsg.textContent);

  // 手动点按钮 = 强制替换，且按钮文案要还原
  await h.util.fillResumeFromProfile(true);
  check('手动点按钮强制换成档案', h.els.resumeText.value === PROFILE);
  check('按钮文案跑完还原', h.els.btnResumeFromProfile.textContent === '读取简历档案' && h.els.btnResumeFromProfile.disabled === false);

  // 空档案：不算读取成功，也不能往框里塞「# 个人简历」这种空壳
  h = harness('# 个人简历');
  const okEmpty = await h.util.fillResumeFromProfile(true);
  check('空档案返回 false', okEmpty === false);
  check('空档案不污染文本框', h.els.resumeText.value === '');
  check('空档案提示可行动', /档案还是空的/.test(h.els.resumeProfileMsg.textContent), h.els.resumeProfileMsg.textContent);

  // 接口失败：不抛异常，报出原因
  h = harness(PROFILE, { ok: false, error: '服务不可用' });
  const okFail = await h.util.fillResumeFromProfile(true);
  check('接口失败返回 false 并报原因', okFail === false && /读取档案失败：服务不可用/.test(h.els.resumeProfileMsg.textContent), h.els.resumeProfileMsg.textContent);

  // 清空：文本框与已选文件一起清（只清文本框的话文件仍然优先，看着清了其实没清）
  h = harness(PROFILE);
  await h.util.fillResumeFromProfile(false);
  h.els.resumeFile.files = [{ name: '简历.pdf' }];
  h.util.refreshResumePayloadMsg();
  check('有文件时来源提示变成文件', /上传的文件/.test(h.els.resumePayloadMsg.textContent), h.els.resumePayloadMsg.textContent);
  h.util.clearResumeText();
  check('清空连已选文件一起清', h.els.resumeText.value === '' && h.els.resumeFile.files.length === 0);
  check('清空后来源提示回到起点', /还没选来源/.test(h.els.resumePayloadMsg.textContent), h.els.resumePayloadMsg.textContent);

  console.log(JSON.stringify(out));
})();
"""


def _node() -> str:
    return shutil.which("node") or shutil.which("node.exe") or ""


@pytest.fixture(scope="module")
def node_result(tmp_path_factory) -> list[list]:
    node = _node()
    if not node:
        pytest.skip("未找到 node，跳过前端行为测试")
    assert UI.is_file(), f"找不到前端文件：{UI}"
    target = tmp_path_factory.mktemp("ui_js") / "resume_source_probe.js"
    target.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(target), str(UI), json.dumps(WANTED_FUNCS, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr[:800]}\n{proc.stdout[:800]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_resume_source_behaviour(node_result):
    failed = [f"{name}（{extra}）" for name, ok, extra in node_result if not ok]
    assert not failed, "简历来源的覆盖规则被改坏了：" + "；".join(failed)


def test_resume_source_cases_are_not_silently_empty(node_result):
    """桩跑空不算通过：用例数少说明抽取逻辑失效（比如函数改名后没被测到）。"""
    assert len(node_result) >= 15, f"实际只跑了 {len(node_result)} 项"
