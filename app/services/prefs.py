"""个人求职偏好：目标城市 / 岗位 / 薪资下限 / 黑名单的持久化与全局复用。

**为什么单独存一份 `data/求职偏好.json`，而不是塞进 `config.json`：**
两者的生命周期与语义都不同。`config.json` 是「本机服务配置」，含 API Key 且已被
gitignore；偏好是**用户数据**，要跟着人走、要能单独备份与清空。混在一起会让
「恢复默认配置」顺手删掉求职偏好，也会让「备份配置」等于备份密钥。
分开存之后，删配置不会丢偏好，删偏好也不会碰密钥。

读取必须零抛出：推荐链路每一跳都会读偏好，一个坏文件不该让整个推荐功能不可用
（与本项目其它数据文件同一口径，见 `utils.io.load_json_dict`）。

本模块不 import flask —— 它要能被定时任务 / CLI 直接复用。
"""

from __future__ import annotations

import re
import threading
from datetime import datetime

from settings import DATA
from utils.io import load_json_dict, write_json_atomic

PREFS_PATH = DATA / "求职偏好.json"
_SCHEMA_VERSION = 1

# 可写字段（version / updated_at 由本模块自己维护，不接受外部传入）
FIELDS = ("target_cities", "target_positions", "salary_min", "blacklist", "company_type")

# 与 `resume.parse_target_cities` 同一套分隔符。这里不 import resume 是因为
# resume 牵着 PDF/LLM 那一串重依赖，而偏好是「读个配置」级别的东西，
# 不该被它们拖住——三行正则的代价远小于多一层依赖。
_SPLIT = re.compile(r"[，、,;；\s/]+")

# 「目标企业性质」下拉候选：与 ui.html 的 #companyType 选项保持一致，
# 放在服务端是为了让偏好页与推荐页共用同一份枚举，不会各自漂移。
COMPANY_TYPES = ["国企央企", "事业单位", "民营", "外企", "互联网"]

_LOCK = threading.RLock()


def empty_prefs() -> dict:
    """一份全新的空偏好（调用方可安全修改返回值）。"""
    return {"version": _SCHEMA_VERSION, "target_cities": [], "target_positions": [],
            "salary_min": 0, "blacklist": [], "company_type": "", "updated_at": ""}


def _norm_list(value) -> list[str]:
    """归一化成字符串列表：去空白、丢空项、去重且保持原有顺序。

    顺带接受单个字符串（前端偶发把一项当字符串传），按分隔符拆开，
    避免出现 "武汉、深圳" 被当成一整个城市名而永远匹配不上。
    """
    if isinstance(value, str):
        value = _SPLIT.split(value)
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _norm_salary(value) -> int:
    """薪资下限归一化：非数字一律 0（宁可不筛，也不能让一个脏值把结果全滤掉）。"""
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def normalize(raw) -> dict:
    """把任意来源（文件 / 请求体）的偏好归一化成完整结构；非 dict 一律当空偏好。"""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "version": _SCHEMA_VERSION,
        "target_cities": _norm_list(raw.get("target_cities")),
        "target_positions": _norm_list(raw.get("target_positions")),
        "salary_min": _norm_salary(raw.get("salary_min")),
        "blacklist": _norm_list(raw.get("blacklist")),
        "company_type": str(raw.get("company_type") or "").strip(),
        "updated_at": str(raw.get("updated_at") or "").strip(),
    }


def load_prefs() -> dict:
    """读偏好；文件缺失 / 损坏 / 顶层不是 dict 一律返回空偏好，绝不抛异常。"""
    with _LOCK:
        data = load_json_dict(PREFS_PATH)
    return normalize(data)


def save_prefs(patch) -> dict:
    """部分更新偏好：只改传入的字段，其余保留；**空数组表示清空该项**。

    返回保存后的完整偏好（前端拿它直接刷新表单，不必再发一次 GET）。
    """
    current = load_prefs()
    raw = patch if isinstance(patch, dict) else {}
    merged = dict(current)
    for field in FIELDS:
        if field in raw:
            merged[field] = raw[field]
    merged = normalize(merged)
    merged["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(PREFS_PATH, merged)
    return merged


def cities_to_text(cities) -> str:
    """偏好里的城市列表 → 「目标工作地」文案（与页面输入框同一格式，可被解析回去）。"""
    return "、".join(_norm_list(cities))


def blacklist_hit(name, blacklist) -> bool:
    """企业名是否命中黑名单：子串 + 忽略大小写。

    子串匹配是刻意的——用户写「某外包」是想挡掉「某外包科技（武汉）」这类
    带后缀的写法，只按全等匹配会形同虚设。
    """
    target = str(name or "").strip().lower()
    if not target:
        return False
    for keyword in _norm_list(blacklist):
        if keyword.lower() in target:
            return True
    return False


def split_blacklisted(rows, blacklist, key: str = "单位名称") -> tuple[list[dict], list[str]]:
    """把命中黑名单的条目挑出来，返回 `(保留下来的条目, 被剔除的企业/单位名)`。

    **必须在排序与截断之前调用**：否则黑名单里的企业会先占掉 Top-N 名额，
    用户看到的是「推荐条数变少了」，而不是「黑名单生效了」——那会被当成 bug。
    """
    if not _norm_list(blacklist):
        return list(rows or []), []
    kept: list[dict] = []
    blocked: list[str] = []
    for row in rows or []:
        name = str((row.get(key) if isinstance(row, dict) else row) or "").strip()
        if blacklist_hit(name, blacklist):
            if name and name not in blocked:
                blocked.append(name)
            continue
        kept.append(row)
    return kept, blocked


def defaults() -> dict:
    """给前端的初始结构：空偏好模板 + 下拉候选（不含任何用户数据）。"""
    return {"prefs": empty_prefs(), "company_types": list(COMPANY_TYPES)}
