#!/usr/bin/env python3
"""统一后台任务中心：任务状态机 + 输出采集 + 历史落盘 + 进度解析。

设计上与 HTTP 层解耦（不 import server）：数据目录、上限、子进程工作目录全部由构造参数
注入，因此可以脱离 Flask 直接实例化做单元测试（见 tests/test_task_manager.py）。

任务记录字段遵循状态机：queued → running → succeeded / failed / cancelled，
含进度、成功/失败摘要、开始结束时间与耗时、日志文件路径。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import crawler

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


def parse_progress(lines: list[str]) -> dict:
    """从任务输出里推断进度：优先 `[3/120]` / `已抓取详情 3/120`，其次 `[3]` 阶段号。"""
    for line in reversed(lines[-80:]):
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
            crawler.write_json(self.history_path, {"tasks": self.history[:self.history_max]})
        except OSError:
            pass
        self._cleanup_logs()

    def _snapshot(self, task: dict) -> dict:
        """把运行中的任务转成可持久化的记录（不含子进程句柄与日志正文）。"""
        return {
            "id": task["id"], "kind": task["kind"], "title": task.get("title") or task["kind"],
            "status": task.get("status", "queued"), "exit_code": task.get("exit_code"),
            "started": task.get("started"), "finished": task.get("finished"),
            "started_at": task.get("started_at"), "duration": task.get("duration"),
            "progress": parse_progress(task.get("lines") or []),
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

    def _pump(self, task: dict) -> None:
        proc = task["proc"]
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            with self.lock:
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
                task["error"] = error_summary(task["lines"]) or f"退出码 {proc.returncode}"
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
                "duration": task.get("duration"), "progress": parse_progress(task["lines"]),
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
