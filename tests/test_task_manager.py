"""后台任务管理器测试：进度解析、日志落盘、历史持久化、失败/取消状态机、并发保护。

注意：这里覆盖过一次真实 bug —— `start()` 在持有不可重入 Lock 时调用 `public()` 会死锁，
因此 `test_duplicate_kind_start_does_not_deadlock` 用带超时的线程显式守护该行为。

后半部分的 JSONL 用例守护另一条契约：脚本在命令行里人盯着跑时**不许**多打一行 JSON，
只有在 stdout 被管道接管（任务中心）时才额外输出事件行。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import server
import taskcenter

OK_CODE = (
    "import time\n"
    "for i in (1, 2, 3):\n"
    "    print(f'[{i}/3] 处理第{i}项', flush=True)\n"
    "    time.sleep(0.1)\n"
    "print('全部完成 中文输出正常', flush=True)\n"
)
FAIL_CODE = (
    "import sys\n"
    "print('开始', flush=True)\n"
    "print('错误：未设置环境变量 LLM_API_KEY', file=sys.stderr, flush=True)\n"
    "sys.exit(2)\n"
)
SLEEP_CODE = "import time; print('长任务开始', flush=True); time.sleep(30)"

# 走 JSONL 协议的子进程：阶段 → 逐条进度 → 一行日志
JSONL_CODE = """
import json, time

def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)

emit(type="stage", name="抓取列表")
for i in (1, 2, 3):
    emit(type="progress", done=i, total=3)
    print(f"  第{i}项完成", flush=True)
    time.sleep(0.05)
emit(type="log", text="汇总完成")
"""
# 事件行里夹一个任务中心不认识的 type（协议单向演进时旧版不该崩）
UNKNOWN_EVENT_CODE = """
import json

print(json.dumps({"type": "heartbeat", "ts": 1}), flush=True)
print("[2/10] 老式文本进度", flush=True)
"""
ERROR_EVENT_CODE = """
import json, sys

print(json.dumps({"type": "error", "message": "错误：配额已用尽"}, ensure_ascii=False), flush=True)
sys.exit(3)
"""


class _FakeStream:
    """stdout 替身：可指定 isatty，用来在进程内模拟「终端 / 管道」两种运行方式。"""

    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self.chunks: list[str] = []

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    @property
    def text(self) -> str:
        return "".join(self.chunks)


@pytest.fixture()
def tm(tmp_path, monkeypatch):
    # 路径改为显式注入（TaskManager 不再依赖某个模块的全局变量），
    # 同时对 server 上的常量做兼容 patch：这几个断言仍以它们为基准。
    monkeypatch.setattr(server, "TASK_HISTORY_PATH", tmp_path / "任务历史.json")
    monkeypatch.setattr(server, "TASK_LOG_DIR", tmp_path / "任务日志")
    manager = server.TaskManager(history_path=server.TASK_HISTORY_PATH,
                                 log_dir=server.TASK_LOG_DIR)
    yield manager
    with manager.lock:
        for task in manager.tasks.values():
            if task["running"]:
                try:
                    task["proc"].terminate()
                except OSError:
                    pass


def run(tm, code: str, kind: str = "测试", title: str = "测试任务", timeout: float = 20.0) -> dict:
    res = tm.start(kind, [sys.executable, "-u", "-c", code], dict(os.environ), title=title)
    assert res["ok"] is True, res
    tid = res["task"]["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = tm.get(tid)
        if not cur["running"]:
            return cur
        time.sleep(0.1)
    raise AssertionError("任务未在超时内结束")


# ---------------- 进度解析（纯函数，无需子进程）

def test_parse_progress_variants():
    assert server._parse_progress(["[6/10] 处理中"])["percent"] == 60
    assert server._parse_progress(["  已抓取详情 8/40"])["done"] == 8
    assert server._parse_progress(["已读取 3/40 页"])["total"] == 40
    step = server._parse_progress(["[6] 已更新数据文件"])
    assert step["done"] == 6 and step["total"] == 0 and step["percent"] == 0
    assert server._parse_progress(["没有进度信息"]) == {"done": 0, "total": 0, "percent": 0}
    assert server._parse_progress([]) == {"done": 0, "total": 0, "percent": 0}


def test_parse_progress_uses_latest_match():
    assert server._parse_progress(["[1/10] a", "[10/10] b"])["percent"] == 100
    assert server._parse_progress(["8/40", "[2] 阶段"])["total"] == 0      # 后面的阶段号覆盖


# ---------------- 成功路径

def test_success_records_status_progress_duration_and_log(tm):
    cur = run(tm, OK_CODE, title="成功任务")
    assert cur["status"] == "succeeded"
    assert cur["exit_code"] == 0
    assert cur["title"] == "成功任务"
    assert cur["progress"] == {"done": 3, "total": 3, "percent": 100}
    assert isinstance(cur["duration"], (int, float)) and cur["duration"] >= 0
    assert cur["finished"] and not cur["error"]
    assert (server.TASK_LOG_DIR / cur["log_file"]).exists()

    lines = tm.read_log(cur["id"], tail=0)
    assert len(lines) == 4
    assert "全部完成 中文输出正常" in lines[-1]        # 子进程 UTF-8 管道未乱码
    assert not any("\ufffd" in ln for ln in lines)


def test_history_persisted_to_disk_and_reloadable(tm):
    cur = run(tm, OK_CODE)
    path = Path(server.TASK_HISTORY_PATH)
    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))["tasks"]
    assert any(r["id"] == cur["id"] and r["status"] == "succeeded" for r in saved)

    # 模拟服务重启：新实例只有历史记录，没有内存运行态（注意指向同一份历史文件）
    restarted = server.TaskManager(history_path=server.TASK_HISTORY_PATH,
                                   log_dir=server.TASK_LOG_DIR)
    assert restarted.running() == []
    rec = restarted.get(cur["id"])
    assert rec is not None and rec["status"] == "succeeded"
    assert len(restarted.read_log(cur["id"], tail=2)) == 2      # 重启后仍能回看日志
    assert any(r["id"] == cur["id"] for r in restarted.history_list(limit=20))


def test_history_list_is_sorted_desc_and_limited(tm):
    run(tm, OK_CODE, title="第一个")
    time.sleep(1.1)
    run(tm, OK_CODE, title="第二个")
    rows = tm.history_list(limit=10)
    assert [r["title"] for r in rows][:2] == ["第二个", "第一个"]
    assert tm.history_list(limit=1)[0]["title"] == "第二个"


# ---------------- 失败 / 取消

def test_failed_task_keeps_exit_code_and_error_summary(tm):
    cur = run(tm, FAIL_CODE, title="失败任务")
    assert cur["status"] == "failed"
    assert cur["exit_code"] == 2
    assert "LLM_API_KEY" in cur["error"]


def test_cancelled_status_is_preserved(tm):
    res = tm.start("可取消", [sys.executable, "-u", "-c", SLEEP_CODE], dict(os.environ), title="取消任务")
    tid = res["task"]["id"]
    time.sleep(0.8)
    with tm.lock:
        tm.tasks[tid]["status"] = "cancelled"          # 与 /api/task/stop 一致
        tm.tasks[tid]["proc"].terminate()
    deadline = time.time() + 15
    while time.time() < deadline and tm.get(tid)["running"]:
        time.sleep(0.1)
    cur = tm.get(tid)
    assert cur["status"] == "cancelled"                # 不被 _pump 覆盖成 failed
    assert cur["exit_code"] != 0
    assert not cur["error"]


# ---------------- 并发保护（死锁回归）

def test_duplicate_kind_start_does_not_deadlock(tm):
    first = tm.start("测试", [sys.executable, "-u", "-c", SLEEP_CODE], dict(os.environ), title="并发A")
    assert first["ok"] is True
    holder: dict = {}

    def call_second():
        holder["res"] = tm.start("测试", [sys.executable, "-u", "-c", "print(1)"],
                                 dict(os.environ), title="并发B")

    thread = threading.Thread(target=call_second, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "同类型任务重复启动时发生死锁（Lock 不可重入）"
    assert holder["res"]["ok"] is False
    assert "已有" in holder["res"]["error"]
    assert holder["res"]["task"]["id"] == first["task"]["id"]


def test_task_ids_are_ascii_and_unique(tm):
    ids = [tm.start(f"类型{i}", [sys.executable, "-u", "-c", "print(1)"], dict(os.environ),
                    title=f"任务{i}")["task"]["id"] for i in range(3)]
    assert len(set(ids)) == 3
    assert all(i.isascii() for i in ids)
    assert all(i.startswith("task_") for i in ids)     # 未登记的 kind → 兜底前缀


# ---------------- JSONL 事件协议

def test_emitter_stays_silent_on_a_terminal():
    """命令行里人盯着跑：一行 JSON 都不该多出来（CLI 输出必须与改造前一致）。"""
    stream = _FakeStream(tty=True)
    emit = taskcenter.EventEmitter.for_stdout(stream=stream)
    assert emit.enabled is False
    emit.progress(1, 3)
    emit.stage("抓取详情")
    assert stream.text == ""


def test_emitter_emits_jsonl_when_piped():
    stream = _FakeStream(tty=False)
    emit = taskcenter.EventEmitter.for_stdout(stream=stream)
    assert emit.enabled is True
    emit.progress(2, 5)
    emit.stage("写回主库")
    emit.log("一行日志")
    emit.error("错误：配额不足")
    rows = [json.loads(line) for line in stream.text.splitlines()]
    assert [r["type"] for r in rows] == ["progress", "stage", "log", "error"]
    assert rows[0]["done"] == 2 and rows[0]["total"] == 5


def test_jsonl_flag_forces_events_even_on_a_terminal():
    stream = _FakeStream(tty=True)
    emit = taskcenter.EventEmitter.for_stdout(force=True, stream=stream)
    emit.progress(1, 1)
    assert json.loads(stream.text)["type"] == "progress"


def test_bar_draws_carriage_return_only_on_terminal(capsys):
    """进度条：终端里 \\r 原地刷新；被管道接管时只发事件，不把日志刷成几千行同一句。"""
    terminal = taskcenter.EventEmitter.for_stdout(stream=_FakeStream(tty=True))
    terminal.bar(2, 10, "  已读取", " 页")
    terminal.bar_end()
    assert capsys.readouterr().out == "\r  已读取 2/10 页\n"

    piped = taskcenter.EventEmitter.for_stdout(stream=_FakeStream(tty=False))
    piped.bar(2, 10, "  已读取", " 页")
    piped.bar_end()
    assert capsys.readouterr().out == ""


def test_parse_event_rejects_non_event_lines():
    assert taskcenter.parse_event("  已抓取详情 8/40") is None
    assert taskcenter.parse_event("{坏掉的 JSON") is None
    assert taskcenter.parse_event('{"done": 1}') is None          # 没有 type 字段
    assert taskcenter.parse_event('{"type": "future", "x": 1}') == {"type": "future", "x": 1}


def test_parse_progress_prefers_jsonl_event():
    """结构化事件优先于文本：脚本改了 print 文案也不影响进度条。"""
    lines = ["[1/9] 老式文本进度", '{"type": "progress", "done": 30, "total": 120}']
    assert server._parse_progress(lines) == {"done": 30, "total": 120, "percent": 25}


def test_parse_progress_falls_back_to_text_without_progress_event():
    """只有 stage/log 事件（没报进度）时，文本正则必须照旧生效。"""
    lines = ['{"type": "stage", "name": "抓取列表"}', "  已抓取详情 8/40"]
    assert server._parse_progress(lines)["done"] == 8


def test_unknown_event_type_never_counts_as_progress():
    lines = ['{"type": "future", "done": 99, "total": 100}', "[2/10] 老式文本进度"]
    assert server._parse_progress(lines) == {"done": 2, "total": 10, "percent": 20}


def test_jsonl_task_reports_structured_progress(tm):
    cur = run(tm, JSONL_CODE, title="JSONL 任务")
    assert cur["status"] == "succeeded"
    assert cur["progress"] == {"done": 3, "total": 3, "percent": 100}

    lines = tm.read_log(cur["id"], tail=0)
    assert not any(line.lstrip().startswith("{") for line in lines)   # 事件原文不进界面日志
    assert "[阶段] 抓取列表" in lines
    assert "  第3项完成" in lines and "汇总完成" in lines


def test_unknown_event_type_does_not_break_task(tm):
    """未知 type 被忽略：任务照常完成，进度回落到文本推断。"""
    cur = run(tm, UNKNOWN_EVENT_CODE, title="未知事件")
    assert cur["status"] == "succeeded"
    assert cur["progress"] == {"done": 2, "total": 10, "percent": 20}
    assert not any(line.lstrip().startswith("{") for line in cur["lines"])


def test_jsonl_error_event_becomes_task_error(tm):
    cur = run(tm, ERROR_EVENT_CODE, title="JSONL 错误")
    assert cur["status"] == "failed"
    assert cur["exit_code"] == 3
    assert cur["error"] == "错误：配额已用尽"


def test_cli_scripts_keep_jsonl_flag_and_help():
    """四个 CLI 都要能 --help，且都提供 --jsonl（默认靠 tty 判定，不强制）。"""
    app_dir = Path(server.__file__).resolve().parent
    for name in ("crawler.py", "check_update.py", "analyze.py", "analyze_preach.py"):
        out = subprocess.run([sys.executable, str(app_dir / name), "--help"],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, f"{name} --help 失败：{out.stderr}"
        assert "--jsonl" in out.stdout, f"{name} 缺少 --jsonl 开关"
