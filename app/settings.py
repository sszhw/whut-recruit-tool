"""路径常量、配置读写、LLM 解析与结构化日志。

这是从原 `server.py` 抽出的最底层模块（阶段 2 拆分的第一步）。

**约束：本模块只依赖 `providers` 与 `utils`（均只用标准库），不得 import 任何业务模块。**

这是全项目的依赖终点：谁都可以 import settings，settings 不能反向依赖谁。
尤其不能碰 analyze / crawler / repository——它们 import requests，
一旦 settings 牵上它们，「读个配置」就要先装网络库；更危险的是 repository
早晚要 import settings 拿 DATA 路径，那时 settings → analyze → repository → settings
会直接成环。为此 DEFAULT_MODEL 定义在**本模块**，analyze 反向引用。

命名为 settings 而非 config，是为了跟项目根的 `config.json`（含 API Key，已 gitignore）
区分开：前者是代码模块，后者是运行时产物。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import providers
from utils import io as io_utils

# ---------------------------------------------------------------- LLM 默认值
# 定义在这里而不是 analyze，是因为配置层要在零业务依赖下知道默认模型名。
# analyze / resume 均从此引用，避免"两处各写一个默认模型"（见 utils.text 同款重复）。
DEFAULT_MODEL = os.environ.get("LLM_MODEL") or "Qwen/Qwen2.5-72B-Instruct"

# ---------------------------------------------------------------- 路径

SCRIPT_DIR = Path(__file__).resolve().parent          # 代码所在目录（app/）
ROOT = SCRIPT_DIR.parent                              # 项目根（数据/配置所在）
WORKDIR = SCRIPT_DIR                                  # 兼容旧引用：指代码目录
DATA = ROOT / "data"                                  # 抓取/分析/收藏/缓存统一存放
CONFIG_PATH = ROOT / "config.json"
CACHE_PATH = DATA / "企业分析_缓存.json"
FAV_PATH = DATA / "收藏_宣讲会.json"
WORK_FLOW_CSV = DATA / "宣讲会_工作地流动.csv"
INDEX_HTML = WORKDIR / "ui.html"

# ---------------------------------------------------------------- 任务

TASK_HISTORY_PATH = DATA / "任务历史.json"     # 后台任务历史（服务重启后仍可查看）
TASK_LOG_DIR = DATA / "任务日志"               # 每个任务一份独立日志文件
TASK_HISTORY_MAX = 200                         # 历史记录上限
TASK_LOG_MAX = 200                             # 任务日志文件保留上限（超出按修改时间清理）

MAX_UPLOAD_BYTES = 20 * 1024 * 1024           # 单请求体上限 20MB（简历 / 报告导入）

# ---------------------------------------------------------------- 结构化日志
# 每个请求一行 JSON（JSON Lines）落盘到 data/服务日志.jsonl，带请求号 rid / 方法 / 路径 / 状态码 / 耗时。
# 控制台只打印 ASCII 摘要——Windows 控制台是 GBK，直接输出中文会抛 UnicodeEncodeError。
LOG_PATH = DATA / "服务日志.jsonl"
LOG_MAX_BYTES = 2 * 1024 * 1024          # 单文件上限，超出滚动保留一份旧日志
_LOG_LOCK = threading.Lock()
logger = logging.getLogger("whut")


def log_event(event: str, **fields) -> None:
    """写一条结构化日志。fields 会被 JSON 序列化，非可序列化对象自动转 str。"""
    now = datetime.now()
    rec = {"ts": now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}",
           "event": event}
    rec.update(fields)
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return
    try:
        with _LOG_LOCK:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                try:
                    LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.stem + ".old.jsonl"))
                except OSError:
                    LOG_PATH.unlink(missing_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
    try:  # 控制台摘要：只输出 ASCII 字段，避免 GBK 终端乱码
        summary = " ".join(f"{k}={v}" for k, v in fields.items()
                           if isinstance(v, (int, float)) or (isinstance(v, str) and v.isascii()))
        print(f"[{event}] {summary}", flush=True)
    except (OSError, UnicodeEncodeError):
        pass


# ---------------------------------------------------------------- 配置管理

def load_config() -> dict:
    """读 config.json，补齐默认值。文件缺失或损坏一律退化为空配置而非抛错。"""
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
    cfg.setdefault("api_key", os.environ.get("SILICONFLOW_API_KEY", ""))
    cfg.setdefault("model", DEFAULT_MODEL)
    cfg.setdefault("provider", os.environ.get("LLM_PROVIDER", "siliconflow"))
    cfg.setdefault("deepseek_api_key", "")
    cfg.setdefault("deepseek_model", "")
    cfg.setdefault("deepseek_base_url", "https://api.deepseek.com")
    return cfg


def save_config(cfg: dict) -> None:
    """写 config.json（原子写：临时文件 + os.replace，避免写一半被杀导致配置损坏）。"""
    io_utils.write_json_atomic(CONFIG_PATH, cfg)


def get_api_key() -> str:
    return (load_config().get("api_key") or "").strip()


# LLM 厂商预设与解析见 providers.py；此处仅重导出以兼容既有引用
LLM_PROVIDERS = providers.LLM_PROVIDERS
_mask = providers.mask


def get_llm() -> dict:
    """按当前配置的厂商返回 base_url / api_key / model / label。"""
    return providers.resolve(load_config())
