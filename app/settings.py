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

关于 API Key 的保管：默认仍落在 config.json（已 gitignore）。想进一步避免明文落盘
的用户可以 `pip install keyring` 并开启开关（`WHUT_KEYRING=1` 环境变量或 config.json
里的 `use_keyring: true`），之后 Key 会写进系统钥匙串，读取时钥匙串优先。
keyring 是**可选**依赖：没装、装了但后端不可用、或调用抛任何异常，一律静默回落
到 config.json——「配置读写失败」比「Key 明文存盘」严重得多，这是本模块的底线。
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

try:
    import keyring
except ImportError:      # 可选依赖：没装就退化成「明文存 config.json」的老行为
    keyring = None

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


# ---------------------------------------------------------------- 密钥保管（可选）
# 钥匙串里按「服务名 + 字段名」存，字段名（如 api_key / deepseek_api_key）就是条目名，
# 这样各厂商的 Key 互不干扰，也不必再维护一份额外的映射表。
KEYRING_SERVICE = "whut-recruit-tool"
KEYRING_ENV = "WHUT_KEYRING"


def _truthy(value) -> bool:
    """把环境变量 / 配置项的多种写法统一成布尔（"1"/"true"/"yes"/"on" 都算开）。"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _keyring_fields() -> list[str]:
    """所有厂商的 API Key 字段名（去重，保持预设表里的顺序）。"""
    fields: list[str] = []
    for conf in providers.LLM_PROVIDERS.values():
        field = conf["api_key_field"]
        if field not in fields:
            fields.append(field)
    return fields


def keyring_enabled(cfg: dict | None = None) -> bool:
    """是否启用系统钥匙串：`WHUT_KEYRING=1` 或配置项 `use_keyring` 为真才启用。

    **默认关闭**：钥匙串是可选增强，不开就完全不碰它——不装 keyring 的人
    行为必须与现在一模一样。环境变量优先于配置项，方便临时开关与排查。
    """
    if keyring is None:
        return False
    env = os.environ.get(KEYRING_ENV, "").strip()
    if env:
        return _truthy(env)
    return _truthy((cfg or {}).get("use_keyring"))


def _keyring_get(field: str) -> str:
    """读钥匙串；未装 / 后端不可用 / 抛任何异常都返回空串（静默回落）。"""
    kr = keyring
    if kr is None:
        return ""
    try:
        return (kr.get_password(KEYRING_SERVICE, field) or "").strip()
    except Exception:      # noqa: BLE001  钥匙串不可用时必须静默，绝不能打断配置读取
        return ""


def _keyring_set(field: str, secret: str) -> bool:
    """写钥匙串；失败返回 False，由调用方继续把 Key 落到 config.json。"""
    kr = keyring
    if kr is None:
        return False
    try:
        kr.set_password(KEYRING_SERVICE, field, secret)
        return True
    except Exception:      # noqa: BLE001  写不进钥匙串不该让「保存配置」失败
        return False


def _keyring_delete(field: str) -> None:
    """删钥匙串条目；不存在或删不掉都无所谓（Key 以 config.json 为准仍能覆盖）。"""
    kr = keyring
    if kr is None:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, field)
    except Exception:      # noqa: BLE001  多数后端对「删不存在的条目」会抛错，忽略即可
        pass


def _keyring_sync(cfg: dict) -> None:
    """启用钥匙串时把各厂商 Key 同步进钥匙串。

    空值按「删除」处理：否则用户在设置页点了「删除 Key」，config.json 清空了，
    钥匙串里那份还在，下次读取又会冒出来——删除看起来像没生效。
    """
    for field in _keyring_fields():
        if field not in cfg:
            continue                       # 该厂商从未配置过，不动钥匙串
        secret = str(cfg.get(field) or "").strip()
        if secret:
            _keyring_set(field, secret)
        else:
            _keyring_delete(field)


# ---------------------------------------------------------------- 配置管理

def _read_config_file() -> dict:
    """读 config.json 原始内容。文件缺失或损坏一律退化为空配置而非抛错。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def load_config() -> dict:
    """读配置，补齐默认值；启用钥匙串时用钥匙串里的 Key 覆盖文件里的值。

    钥匙串优先于文件：这就是「迁移」的全部含义——迁移后 config.json 里那份旧 Key
    **不会被主动删除**（何时清由用户决定），但读到的永远是钥匙串里的。
    """
    cfg = _read_config_file()
    cfg.setdefault("api_key", os.environ.get("SILICONFLOW_API_KEY", ""))
    cfg.setdefault("model", DEFAULT_MODEL)
    cfg.setdefault("provider", os.environ.get("LLM_PROVIDER", "siliconflow"))
    cfg.setdefault("deepseek_api_key", "")
    cfg.setdefault("deepseek_model", "")
    cfg.setdefault("deepseek_base_url", "https://api.deepseek.com")
    if keyring_enabled(cfg):
        for field in _keyring_fields():
            secret = _keyring_get(field)
            if secret:
                cfg[field] = secret
    return cfg


def save_config(cfg: dict) -> None:
    """写 config.json；启用钥匙串时先把 Key 同步进钥匙串（失败不影响落盘）。

    文件里仍保留一份明文：这是刻意的——不主动删旧 Key，用户确认钥匙串能用之后
    再自己决定要不要清，避免「启用开关 → 文件里的 Key 没了 → 钥匙串也没写进去」。
    """
    if keyring_enabled(cfg):
        _keyring_sync(cfg)
    io_utils.write_json_atomic(CONFIG_PATH, cfg)


def get_api_key() -> str:
    return (load_config().get("api_key") or "").strip()


def keyring_status(cfg: dict | None = None) -> dict:
    """当前密钥的存放位置，供设置页提示（**只回位置，不回显明文**）。

    source: keyring（钥匙串）/ file（config.json）/ env（环境变量）/ none（没配）。
    """
    cfg = load_config() if cfg is None else cfg
    enabled = keyring_enabled(cfg)
    conf = providers.LLM_PROVIDERS[providers.normalize_provider(cfg.get("provider"))]
    field = conf["api_key_field"]
    in_keyring = bool(enabled and _keyring_get(field))
    in_file = bool(str(_read_config_file().get(field) or "").strip())
    if in_keyring:
        source = "keyring"
    elif in_file:
        source = "file"
    elif str(cfg.get(field) or "").strip():
        source = "env"          # 既不在钥匙串也不在文件里，只可能是环境变量给的
    else:
        source = "none"
    return {"available": keyring is not None, "enabled": enabled, "source": source,
            "in_keyring": in_keyring, "in_file": in_file}


# LLM 厂商预设与解析见 providers.py；此处仅重导出以兼容既有引用
LLM_PROVIDERS = providers.LLM_PROVIDERS
_mask = providers.mask


def get_llm() -> dict:
    """按当前配置的厂商返回 base_url / api_key / model / label。"""
    return providers.resolve(load_config())
