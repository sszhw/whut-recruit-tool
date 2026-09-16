"""后台任务管理器测试：进度解析、日志落盘、历史持久化、失败/取消状态机、并发保护。

注意：这里覆盖过一次真实 bug —— `start()` 在持有不可重入 Lock 时调用 `public()` 会死锁，
因此 `test_duplicate_kind_start_does_not_deadlock` 用带超时的线程显式守护该行为。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import server

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


@pytest.fixture()
def tm(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASK_HISTORY_PATH", tmp_path / "任务历史.json")
    monkeypatch.setattr(server, "TASK_LOG_DIR", tmp_path / "任务日志")
    manager = server.TaskManager()
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

    # 模拟服务重启：新实例只有历史记录，没有内存运行态
    restarted = server.TaskManager()
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
