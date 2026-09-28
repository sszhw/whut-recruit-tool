#!/usr/bin/env python3
"""统一后台任务中心：任务状态机 + 输出采集 + 历史落盘 + 进度解析。

设计上与 HTTP 层解耦（不 import server）：数据目录、上限、子进程工作目录全部由构造参数
注入，因此可以脱离 Flask 直接实例化做单元测试（见 tests/test_task_manager.py）。

任务记录字段遵循状态机：queued → running → succeeded / failed / cancelled，
含进度、成功/失败摘要、开始结束时间与耗时、日志文件路径。

子进程通过 stdout 上的 **JSONL 事件协议**上报进度（见下方「事件协议」）：
`parse_progress()` 优先读结构化事件，读不到才回落到按 `[3/120]` 抠文本的老办法，
因此没改造过的脚本依然能被正确展示。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from utils import io as io_utils

# 注：本模块曾用 crawler.write_json 落盘历史。改成 utils.io 是为了打断
# crawler ↔ taskcenter 的循环导入——crawler 需要 import 这里的事件协议。

APP_DIR = Path(__file__).resolve().parent          # 代码目录（子进程工作目录）
ROOT = APP_DIR.parent                             # 项目根目录
DATA = ROOT / "data"                              # 默认数据目录

DEFAULT_HISTORY_PATH = DATA / "任务历史.json"
DEFAULT_LOG_DIR = DATA / "任务日志"
DEFAULT_HISTORY_MAX = 200
DEFAULT_LOG_MAX = 200

# 任务 ID 前缀（ASCII，避免中文出现在 URL / 文件名 / HTML id 中）
KIND_SLUGS = {"抓取": "crawl", "招聘更新": "recruit-update", "分析": "analyze",
              "宣讲会检查": "preach-check", "工作地流动": "flow"}

_PROGRESS_PAIR = re.compile(r"(\d+)\s*/\s*(\d+)")
_PROGRESS_STEP = re.compile(r"^\s*\[(\d+)\]")
_ERROR_HINTS = ("错误", "失败", "Traceback", "Error", "error", "Exception", "未设置")
_PROGRESS_SCAN = 80          # 只回看末尾若干行找进度：长任务输出可达数千行

# ============================================================ 事件协议（子进程 → 任务中心）
#
# 为什么需要它：靠正则从 `[3/120]` 里抠进度，等于把「界面进度条」耦合在脚本的
# print 文案上——改一个字进度就断，而且没法表达「当前阶段」「出错原因」这类语义。
#
# 约定：子进程在 stdout 上**额外**写一行一个 JSON 对象（JSONL），任务中心逐行识别，
# 认不出来的行一律按普通文本处理，所以老脚本 / 混排输出都不会失联：
#
#   {"type":"progress","done":3,"total":120}   进度
#   {"type":"stage","name":"抓取详情"}         进入新阶段
#   {"type":"log","text":"……"}                一行日志
#   {"type":"error","message":"……"}           出错原因（用于任务卡片摘要）
#
# 未登记的 type 必须被**忽略**而不是报错：协议是单向演进的，新版脚本加的事件
# 不该让旧版任务中心崩掉。

EVENT_PROGRESS = "progress"
EVENT_STAGE = "stage"
EVENT_LOG = "log"
EVENT_ERROR = "error"


def _as_number(value: object) -> float | None:
    """事件字段转数字；非数字返回 None。

    bool 是 int 的子类，`done=True` 这种脏数据不该被当成 1，这里显式排除。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _progress_dict(done: float, total: float) -> dict:
    done_i, total_i = max(int(done), 0), max(int(total), 0)
    return {"done": done_i, "total": total_i,
            "percent": min(100, round(done_i * 100 / total_i)) if total_i > 0 else 0}


def _is_tty(stream: object) -> bool:
    """stream 是否连着终端（StringIO / 已关闭的流一律按非终端处理）。"""
    try:
        return bool(stream.isatty())       # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


class EventEmitter:
    """结构化事件输出器：一行一个 JSON 对象写进 stdout。

    只在「stdout 不是终端」时启用——那说明输出被任务中心的管道接管了；
    命令行里人盯着跑的时候保持原有中文输出，一行 JSON 都不多打。
    """

    def __init__(self, enabled: bool, stream=None) -> None:
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stdout

    @classmethod
    def for_stdout(cls, force: bool = False, stream=None) -> EventEmitter:
        """按 stdout 是否被人看着决定启用与否；force 供 CLI 的 `--jsonl` 强制打开。"""
        target = stream if stream is not None else sys.stdout
        return cls(force or not _is_tty(target), target)

    def emit(self, event_type: str, **fields: object) -> None:
        """写一条事件。写失败一律吞掉：事件只服务于界面，不能反过来中断采集。"""
        if not self.enabled:
            return
        try:
            self.stream.write(json.dumps({"type": event_type, **fields}, ensure_ascii=False) + "\n")
            self.stream.flush()
        except (OSError, TypeError, ValueError):
            pass

    def progress(self, done: int, total: int = 0) -> None:
        """上报进度（机器模式专用，界面据此画进度条）。"""
        self.emit(EVENT_PROGRESS, done=int(done), total=int(total))

    def stage(self, name: str) -> None:
        """标记进入新阶段（任务日志里会补一行 `[阶段] …`，CLI 下不额外打印）。"""
        self.emit(EVENT_STAGE, name=str(name))

    def log(self, text: str) -> None:
        """输出一行结构化日志；任务日志里以纯文本呈现。"""
        self.emit(EVENT_LOG, text=str(text))

    def error(self, message: str) -> None:
        """上报错误原因，优先作为任务卡片的失败摘要。"""
        self.emit(EVENT_ERROR, message=str(message))

    def bar(self, done: int, total: int, prefix: str, suffix: str = "") -> None:
        """覆盖式进度条：终端里 `\\r` 原地刷新，机器模式只发一条 progress 事件。

        管道里每个 `\\r` 都会被当成一行，逐条 print 会把任务日志刷成几千行同一句话，
        所以被接管时干脆不打印，只上报结构化进度。
        """
        if self.enabled:
            self.progress(done, total)
        else:
            print(f"\r{prefix} {done}/{total}{suffix}", end="", flush=True)

    def bar_end(self) -> None:
        """进度条收尾换行——只有终端里真的画过进度条才需要。"""
        if not self.enabled:
            print()


# 「没有接管 stdout」的调用点（被别的脚本当库调用时）复用它，省得判空
DISABLED_EMITTER = EventEmitter(False)


def parse_event(line: str) -> dict | None:
    """把一行输出解释成事件；不是本协议的 JSONL（普通中文输出 / 坏 JSON）返回 None。

    只对以 `{` 开头且以 `}` 结尾的行做 JSON 解析：脚本正文里大量的中文与表格行
    不该为解析付代价，而 `print('{"a":1}')` 这类被误认的普通输出也只是多一次尝试。
    """
    text = (line or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        return None
    return data


def parse_progress_jsonl(lines: list[str]) -> dict | None:
    """取最后一条 progress 事件转成进度字典；没有则返回 None（交由文本回落处理）。"""
    for line in reversed(lines[-_PROGRESS_SCAN:]):
        event = parse_event(line)
        if event is None or event.get("type") != EVENT_PROGRESS:
            continue
        done = _as_number(event.get("done"))
        if done is None:
            continue
        return _progress_dict(done, _as_number(event.get("total")) or 0)
    return None


def parse_progress_text(lines: list[str]) -> dict:
    """从任务输出里推断进度：优先 `[3/120]` / `已抓取详情 3/120`，其次 `[3]` 阶段号。"""
    for line in reversed(lines[-_PROGRESS_SCAN:]):
        m = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return {"done": done, "total": total, "percent": min(100, round(done * 100 / total))}
        m = _PROGRESS_PAIR.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0 and done <= total:
                return {"done": done, "total": total, "percent": min(100, round(done * 100 / total))}
        m = _PROGRESS_STEP.search(line)
        if m:
            return {"done": int(m.group(1)), "total": 0, "percent": 0}
    return {"done": 0, "total": 0, "percent": 0}


def parse_progress(lines: list[str]) -> dict:
    """推断进度：结构化 progress 事件优先，否则回落到文本正则。

    两条路并存不是冗余而是兼容：没改造过的脚本（或改了一半、只在部分阶段发事件）
    仍靠文本推断，界面不会退化成「进度永远 0」。
    """
    return parse_progress_jsonl(lines) or parse_progress_text(lines)


def error_summary(lines: list[str]) -> str:
    """从任务输出末尾提取一句错误摘要，用于任务卡片展示。"""
    for line in reversed(lines[-40:]):
        text = line.strip()
        if text and any(h in text for h in _ERROR_HINTS):
            return text[:200]
    return ""


class TaskManager:
    """后台任务：运行状态在内存，历史记录落盘（服务重启后仍可查看/回看日志）。"""

    def __init__(self, history_path: Path | None = None, log_dir: Path | None = None,
                 workdir: Path | None = None, history_max: int = DEFAULT_HISTORY_MAX,
                 log_max: int = DEFAULT_LOG_MAX) -> None:
        self.history_path = Path(history_path or DEFAULT_HISTORY_PATH)
        self.log_dir = Path(log_dir or DEFAULT_LOG_DIR)
        self.workdir = Path(workdir or APP_DIR)
        self.history_max = history_max
        self.log_max = log_max
        self.lock = threading.Lock()
        self.tasks: dict[str, dict] = {}
        self.history: list[dict] = self._load_history()

    # ---------------- 历史记录落盘

    def _load_history(self) -> list[dict]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return []
        rows = data.get("tasks") if isinstance(data, dict) else data
        return [r for r in (rows or []) if isinstance(r, dict)]

    def _cleanup_logs(self) -> None:
        """调用方需持有 lock：清理超出上限的任务日志文件（仍在运行的任务日志不删）。"""
        try:
            active = {Path(t.get("log_file") or "").name for t in self.tasks.values()
                      if t.get("log_file")}
            logs = sorted(self.log_dir.glob("*.log"), key=os.path.getmtime, reverse=True)
            for path in logs[self.log_max:]:
                if path.name in active:
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _save_history(self) -> None:
        """调用方需持有 lock。"""
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            io_utils.write_json_atomic(self.history_path, {"tasks": self.history[:self.history_max]})
        except OSError:
            pass
        self._cleanup_logs()

    def _progress_of(self, task: dict) -> dict:
        """任务进度：结构化事件（_pump 消费 JSONL 时写入）优先，否则按文本推断。"""
        return task.get("_progress") or parse_progress(task.get("lines") or [])

    def _snapshot(self, task: dict) -> dict:
        """把运行中的任务转成可持久化的记录（不含子进程句柄与日志正文）。"""
        return {
            "id": task["id"], "kind": task["kind"], "title": task.get("title") or task["kind"],
            "status": task.get("status", "queued"), "exit_code": task.get("exit_code"),
            "started": task.get("started"), "finished": task.get("finished"),
            "started_at": task.get("started_at"), "duration": task.get("duration"),
            "progress": self._progress_of(task),
            "error": task.get("error", ""),
            "log_file": Path(task["log_file"]).name if task.get("log_file") else "",
        }

    def _sync(self, task: dict) -> None:
        """调用方需持有 lock：把内存任务同步进历史列表并落盘。"""
        snap = self._snapshot(task)
        for i, row in enumerate(self.history):
            if row.get("id") == snap["id"]:
                self.history[i] = snap
                break
        else:
            self.history.insert(0, snap)
        self.history = self.history[:self.history_max]
        self._save_history()

    # ---------------- 启动 / 收集输出

    def start(self, kind: str, cmd: list[str], env: dict, title: str = "") -> dict:
        with self.lock:
            for task in self.tasks.values():
                if task["kind"] == kind and task["running"]:
                    return {"ok": False, "error": f"已有{kind}任务在运行", "task": self._public_nolock(task)}
            now = time.time()
            # 毫秒 + 冲突自增，确保同一秒内启动的不同类型任务不会共用 ID（否则内存记录与日志文件会互相覆盖）
            base = f"{KIND_SLUGS.get(kind, 'task')}_{int(now * 1000)}"
            task_id = base
            seq = 1
            while task_id in self.tasks or any(r.get("id") == task_id for r in self.history):
                seq += 1
                task_id = f"{base}_{seq}"
            env = dict(env)
            env["PYTHONUNBUFFERED"] = "1"  # 让子进程 stdout 实时刷新，界面日志即时可见
            # 强制子进程以 UTF-8 输出。Windows 中文环境下 Python 默认用 GBK(cp936) 写 stdout，
            # 而本进程按 utf-8 解码（errors="replace"），会导致中文全部变成 � 乱码。
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            proc = subprocess.Popen(
                cmd, cwd=str(self.workdir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            log_file = self.log_dir / f"{task_id}.log"
            try:
                log_handle = log_file.open("w", encoding="utf-8")
            except OSError:
                log_handle = None
            task = {"id": task_id, "kind": kind, "title": title or kind, "running": True,
                    "status": "running", "exit_code": None, "started": datetime.now().strftime("%H:%M:%S"),
                    "started_at": now, "proc": proc, "lines": [], "log_file": log_file,
                    "log_handle": log_handle, "error": "", "_last_sync": now}
            self.tasks[task_id] = task
            self._sync(task)
        threading.Thread(target=self._pump, args=(task,), daemon=True).start()
        return {"ok": True, "task": self.public(task)}

    def _append_line(self, task: dict, line: str) -> None:
        """调用方需持有 lock：追加一行人类可读输出（内存日志 + 日志文件）。"""
        task["lines"].append(line)
        if len(task["lines"]) > 5000:
            del task["lines"][:2500]
        handle = task.get("log_handle")
        if handle:
            try:
                handle.write(line + "\n")
                handle.flush()
            except (OSError, ValueError):
                task["log_handle"] = None

    def _apply_event(self, task: dict, event: dict) -> None:
        """调用方需持有 lock：消费一条 JSONL 事件，只落结构化字段。

        事件原文不进界面日志——那是给人看的，JSON 行是噪音；
        需要展示的语义（阶段名、日志文本、错误原因）在这里还原成中文一行。
        未登记的 type 静默忽略：协议单向演进，新事件不该把旧任务中心搞崩。
        """
        etype = event.get("type")
        if etype == EVENT_PROGRESS:
            done = _as_number(event.get("done"))
            if done is None:
                return
            task["_progress"] = _progress_dict(done, _as_number(event.get("total")) or 0)
        elif etype == EVENT_STAGE:
            name = str(event.get("name") or "").strip()
            if name:
                self._append_line(task, f"[阶段] {name}")
        elif etype == EVENT_LOG:
            text = str(event.get("text") or "").strip()
            if text:
                self._append_line(task, text)
        elif etype == EVENT_ERROR:
            message = str(event.get("message") or "").strip()
            if message:
                task["_error"] = message
                self._append_line(task, message)

    def _pump(self, task: dict) -> None:
        proc = task["proc"]
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            event = parse_event(line)
            with self.lock:
                if event is None:
                    self._append_line(task, line)
                else:
                    self._apply_event(task, event)
                # 进度最多每 3 秒落盘一次，避免高频写文件
                now = time.time()
                if now - task.get("_last_sync", now) > 3:
                    task["_last_sync"] = now
                    self._sync(task)
        proc.wait()
        with self.lock:
            task["running"] = False
            task["exit_code"] = proc.returncode
            task["finished"] = datetime.now().strftime("%H:%M:%S")
            task["duration"] = round(time.time() - float(task.get("started_at") or time.time()), 1)
            cancelled = task.get("status") == "cancelled"
            task["status"] = "cancelled" if cancelled else ("succeeded" if proc.returncode == 0 else "failed")
            if proc.returncode != 0 and not cancelled:
                # 结构化 error 事件比正则猜出来的那句话准，猜不到时再退回文本摘要
                task["error"] = task.get("_error") or error_summary(task["lines"]) or f"退出码 {proc.returncode}"
            handle = task.get("log_handle")
            if handle:
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass
                task["log_handle"] = None
            self._sync(task)

    def _public_nolock(self, task: dict, tail: int = 0) -> dict:
        # 调用方可能已持有 lock（Lock 不可重入），故单独提供无锁版本
        lines = task["lines"][-tail:] if tail else list(task["lines"])
        return {"id": task["id"], "kind": task["kind"], "title": task.get("title") or task["kind"],
                "running": task["running"], "status": task.get("status", "running"),
                "exit_code": task["exit_code"], "started": task.get("started"),
                "finished": task.get("finished"), "started_at": task.get("started_at"),
                "duration": task.get("duration"), "progress": self._progress_of(task),
                "error": task.get("error", ""),
                "log_file": Path(task["log_file"]).name if task.get("log_file") else "",
                "lines": lines}

    def public(self, task: dict, tail: int = 0) -> dict:
        with self.lock:
            return self._public_nolock(task, tail)

    def latest(self, kind: str) -> dict | None:
        with self.lock:
            candidates = [t for t in self.tasks.values() if t["kind"] == kind]
        return max(candidates, key=lambda t: t["started_at"]) if candidates else None

    def running(self) -> list[dict]:
        with self.lock:
            tasks = [t for t in self.tasks.values() if t["running"]]
        return [self.public(t) for t in tasks]

    def get(self, task_id: str) -> dict | None:
        """先查内存运行态，再回落到历史记录（服务重启后仍可取到）。"""
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                return self._public_nolock(task)
            for row in self.history:
                if row.get("id") == task_id:
                    return dict(row)
        return None

    def read_log(self, task_id: str, tail: int = 300) -> list[str]:
        """读取任务日志文件（历史任务也可读）。"""
        path = self.log_dir / f"{task_id}.log"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-tail:] if tail else lines

    def history_list(self, limit: int = 60) -> list[dict]:
        """运行中的任务 + 历史任务（合并、按开始时间倒序）。"""
        with self.lock:
            live_ids = set(self.tasks)
            live = [self._snapshot(t) for t in self.tasks.values()]
            past = [dict(r) for r in self.history if r.get("id") not in live_ids]
        rows = live + past
        rows.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return rows[:limit]
