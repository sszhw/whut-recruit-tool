"""投递档案：结构化简历数据的持久化，以及档案全文纯文本（.md / .txt）的生成。

**为什么要有这一层**

校招网申要填的东西和简历不是一回事。简历只写「教育 / 项目 / 技能 / 自我评价」，
而网申系统还要「家庭成员、生源地、政治面貌、紧急联系人、附件清单」——这些字段
在简历文件里根本没有，却每次都要手打一遍。这份档案把两类信息放在同一处，
需要时能分别取用。

**为什么分模块存，而不是一个大 flat dict**

不同招聘系统的字段集差得很远（同一份信息，国家管网要 26 项教育字段，
中国移动的家庭成员表单要 10 项）。固定 schema 会让「加一个系统特有的字段」
变成改代码。所以这里只定义模块的**种类**，不定义字段：

- `fields` 一条条 `{k, v}`，可自由增删键（基本信息、求职意向、计算机技能）
- `items` 可重复的卡片，每张卡里字段自由（教育经历、家庭成员、项目、外语、获奖、附件）
- `lines` 纯文本段落列表（自我评价、个人爱好）

**纯文本导出（`.md` / `.txt`）为什么单独生成**

很多校招系统支持「上传附件简历 → 自动解析填表」。解析错位的常见原因是
排版花哨（表格、分栏、文本框）让解析器认不出哪个词是字段名。所以这两种
格式刻意保持「一行一项、字段：值、日期 yyyy-MM-dd」，不加任何排版修饰。

Word 导出**不在这里**：两份 `.docx`（HR 版 / 完整版）同属一套排版，都放在
`services/resume_hr.py`。它们的内容取舍与本模块的纯文本导出一致
（`include_field` 是唯一一处取舍，双方共用）。

**导出即全量：不加任何按字段名的过滤**

早先这里会剔掉学号、紧急联系人、证书扫描件文件名、内部备注，以及整个
「家庭成员」模块，理由是「它们不是简历内容，会干扰系统解析」。但档案的定位
本来就是「网申要填的全部字段存一份」——导出时少一块，就得回头翻原始材料，
反而更麻烦。所以现在**所有模块、所有非空字段都进文档**。

只丢掉两类东西：

- **本地文件指针**：`照片` 字段的值是 `data/简历照片.png` 这类文件名，
  它指向的是本机文件，写进文档只是噪音（照片本身也进不了纯文本导出）。
- **内部批注**：值里带 ⚠️ / ⛔ / 见 § 的行是写给自己看的（「这个绩点是折算值」
  「扫描件缺失」），整条丢掉——把标记删掉、留半句批注在文档里比不写更糟。

`form_only` 标记只影响界面提示（模块条上的「· 填表」），不参与导出取舍。

本模块不 import flask，与 services/ 下其它模块同一口径（可被 CLI / 定时任务复用）。
读取一律零抛出：档案坏了不该让「我的」整页打不开。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from settings import DATA
from utils.io import load_json_dict, write_json_atomic

from services import ServiceError

PROFILE_PATH = DATA / "投递档案.json"
_SCHEMA_VERSION = 1

# 模块定义：顺序即界面与导出的顺序。
# title_keys 用于在卡片列表里拼标题（按顺序取第一个非空值）；
# form_only=True 的模块是网申表单字段（界面标「· 填表」），但**照样进导出**。
MODULES: list[dict] = [
    {"key": "basic",       "name": "基本信息", "kind": "fields", "form_only": False, "title_keys": []},
    {"key": "education",   "name": "教育经历", "kind": "items",  "form_only": False, "title_keys": ["学校名称", "学历"]},
    {"key": "projects",    "name": "项目经验", "kind": "items",  "form_only": False, "title_keys": ["项目名称", "时间"]},
    {"key": "languages",   "name": "外语水平", "kind": "items",  "form_only": False, "title_keys": ["外语语种", "外语水平"]},
    {"key": "awards",      "name": "获奖信息", "kind": "items",  "form_only": False, "title_keys": ["获奖时间", "其他获奖名称"]},
    {"key": "intent",      "name": "求职意向", "kind": "fields", "form_only": False, "title_keys": []},
    {"key": "skills",      "name": "计算机技能", "kind": "fields", "form_only": False, "title_keys": []},
    {"key": "summary",     "name": "自我评价", "kind": "lines",  "form_only": False, "title_keys": []},
    {"key": "hobbies",     "name": "个人爱好", "kind": "lines",  "form_only": False, "title_keys": []},
    # 家庭成员是网申表单字段，不是简历内容——界面上标出来，但导出时一并写出
    {"key": "family",      "name": "家庭成员", "kind": "items",  "form_only": True,  "title_keys": ["亲属姓名", "亲属关系"]},
]

MODULE_KEYS = [m["key"] for m in MODULES]
_KINDS = {m["key"]: m["kind"] for m in MODULES}

_LOCK = threading.RLock()


# ---------------------------------------------------------------- 归一化


def _s(value) -> str:
    """任意值 → 去空白字符串；None / 非字符串一律安全降级。"""
    return str(value if value is not None else "").strip()


def _norm_fields(raw) -> list[dict]:
    """`fields` 模块：只保留有键名的行；空值行保留（可能只是还没填）。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        key = _s(row.get("k"))
        if not key:
            continue
        out.append({"k": key, "v": _s(row.get("v"))})
    return out


def _norm_items(raw) -> list[dict]:
    """`items` 模块：每条是一个自由 schema 的卡片；丢掉空键与空卡片。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        card = {_s(k): _s(v) for k, v in item.items() if _s(k)}
        if card:
            out.append(card)
    return out


def _norm_lines(raw) -> list[str]:
    """`lines` 模块：纯文本段落；空行丢掉。"""
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        return []
    return [line for line in (_s(x) for x in raw) if line]


def empty_profile() -> dict:
    """一份全新的空档案（调用方可安全修改返回值）。"""
    profile = {"version": _SCHEMA_VERSION, "updated_at": ""}
    for mod in MODULES:
        profile[mod["key"]] = [] if mod["kind"] in ("fields", "items") else []
    return profile


def normalize(raw) -> dict:
    """把任意来源（文件 / 请求体）的档案归一化成完整结构；非 dict 一律当空档案。"""
    raw = raw if isinstance(raw, dict) else {}
    profile: dict = {"version": _SCHEMA_VERSION, "updated_at": _s(raw.get("updated_at"))}
    for mod in MODULES:
        key, kind = mod["key"], mod["kind"]
        value = raw.get(key)
        if kind == "fields":
            profile[key] = _norm_fields(value)
        elif kind == "items":
            profile[key] = _norm_items(value)
        else:
            profile[key] = _norm_lines(value)
    return profile


def load_profile() -> dict:
    """读档案；文件缺失 / 损坏 / 顶层不是 dict 一律返回空档案，绝不抛异常。"""
    with _LOCK:
        data = load_json_dict(PROFILE_PATH)
    return normalize(data)


def save_profile(payload) -> dict:
    """按模块整体覆盖保存：传哪个模块就改哪个，没传的原样保留。

    刻意不做「字段级 merge」——前端每次都提交整个模块数组，逐项合并反而会让
    「删掉一行」变成无法表达的操作（传少了分不清是删除还是没提交）。
    返回保存后的完整档案，前端省掉一次回查。
    """
    current = load_profile()
    raw = payload if isinstance(payload, dict) else {}
    merged = dict(current)
    for key in MODULE_KEYS:
        if key in raw:
            merged[key] = raw[key]
    merged = normalize(merged)
    merged["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(PROFILE_PATH, merged)
    return merged


# ---------------------------------------------------------------- 个人照片
#
# 单独存文件而不是塞进 JSON：照片是二进制，base64 进 JSON 会让档案文件膨胀
# 几十倍，而且每次读档案都要解一遍。文件名固定成「简历照片.<ext>」，换格式时
# 先删旧文件，所以 data/ 下永远只会存在一张。

PHOTO_STEM = "简历照片"
PHOTO_EXTS = ("jpg", "jpeg", "png", "webp", "bmp")
MAX_PHOTO_BYTES = 5 * 1024 * 1024


def find_photo():
    """返回当前照片路径；没有则 None。"""
    for ext in PHOTO_EXTS:
        path = DATA / f"{PHOTO_STEM}.{ext}"
        if path.is_file():
            return path
    return None


def photo_meta() -> dict:
    """给前端与导出用的照片元信息。"""
    path = find_photo()
    if path is None:
        return {"exists": False, "filename": "", "bytes": 0, "ext": "", "url": ""}
    stat = path.stat()
    return {
        "exists": True,
        "filename": path.name,
        "bytes": stat.st_size,
        "ext": path.suffix.lstrip(".").lower(),
        # 用 mtime 当版本号：换了照片 URL 就变，绕开浏览器缓存显示旧图
        "url": f"/api/profile/photo?v={int(stat.st_mtime)}",
    }


def save_photo(filename: str, blob: bytes) -> dict:
    """保存个人照片；格式或大小不合规抛 ServiceError。"""
    ext = (Path(filename or "").suffix or "").lstrip(".").lower()
    if ext == "jpe":
        ext = "jpeg"
    if ext not in PHOTO_EXTS:
        raise ServiceError(f"照片格式只支持 {'/'.join(PHOTO_EXTS)}，当前是 .{ext or '未知'}")
    if not blob:
        raise ServiceError("照片内容为空")
    if len(blob) > MAX_PHOTO_BYTES:
        raise ServiceError(f"照片不能超过 {MAX_PHOTO_BYTES // 1024 // 1024} MB")

    with _LOCK:
        delete_photo()
        path = DATA / f"{PHOTO_STEM}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    return photo_meta()


def delete_photo() -> bool:
    """删除照片文件；本来就没有则返回 False。"""
    path = find_photo()
    if path is None:
        return False
    path.unlink()
    return True


def module_meta() -> list[dict]:
    """给前端的模块元信息（不含任何用户数据）。"""
    return [{"key": m["key"], "name": m["name"], "kind": m["kind"],
             "form_only": m["form_only"], "title_keys": list(m["title_keys"])} for m in MODULES]


def item_title(item: dict, mod: dict) -> str:
    """卡片标题：把 title_keys 里的非空值用「 · 」连起来。

    取多个而不是只取第一个，是因为只取一个会重名——两条教育经历的「学校名称」
    都是武汉理工大学，列表里会变成两个「武汉理工大学」分不清本科还是硕士。
    """
    parts = [_s(item.get(k)) for k in mod.get("title_keys", [])]
    parts = [p for p in parts if p]
    if parts:
        return " · ".join(parts)
    for value in item.values():
        if value:
            return value
    return "（未命名）"


def field_value(profile: dict, module_key: str, field_key: str) -> str:
    """从 fields 模块里按字段名取值（简历标题要用「姓名」）。"""
    for row in profile.get(module_key, []):
        if isinstance(row, dict) and _s(row.get("k")) == field_key:
            return _s(row.get("v"))
    return ""


# ---------------------------------------------------------------- 复制用文本


def module_copy_text(profile: dict, module_key: str) -> str:
    """整个模块 → 「字段：值」多行文本，用于整块复制到网申系统。"""
    mod = next((m for m in MODULES if m["key"] == module_key), None)
    if not mod:
        return ""
    kind = mod["kind"]
    if kind == "fields":
        return "\n".join(f"{r['k']}：{r['v']}" for r in profile.get(module_key, []) if r.get("v", "").strip())
    if kind == "lines":
        return "\n".join(profile.get(module_key, []))
    blocks = []
    for index, item in enumerate(profile.get(module_key, []), 1):
        lines = [f"{k}：{v}" for k, v in item.items() if v]
        if lines:
            blocks.append(f"【{mod['name']} {index}】" + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 纯文本导出


# 导出时唯一会丢的两类东西。
#
# 1) 本地文件指针：`照片` 的值就是 `data/简历照片.png` 的文件名，指的是本机文件，
#    写进文档只是噪音（照片本身也进不了文本导出）。
# 2) 内部批注：值里带 ⚠️ / ⛔ / 见 § 的行是写给自己看的（「这个绩点是折算值」
#    「扫描件缺失」）。整条丢掉，而不是把标记删掉留半句批注——那比不写更糟。
_SKIP_KEYS = {"照片"}
_INTERNAL_MARKERS = ("⚠", "⛔", "见 §", "见§")


def include_field(key: str, value) -> bool:
    """该字段值是否应出现在**任何**导出文档里。

    **不按字段名过滤**——学号、紧急联系人、证书编号、家庭成员都是网申要填的
    信息，正是档案存在的意义。只有两类丢掉：

    - `key` 是本地文件指针（`照片` 的值是本机文件名，照片本身也进不了导出）；
    - `value` 含 ⚠️ / ⛔ / 见§ —— 那是写给自己看的批注，整条丢掉而不是把标记
      抠掉留半句批注（那比不写更糟）。

    `services/resume_hr.py` 的完整版遍历 `MODULES` 时调用的就是它，
    所以两条导出链路（纯文本 / Word）的取舍永远一致。
    """
    text = _s(value)
    if not text:
        return False
    if _s(key) in _SKIP_KEYS:
        return False
    return not any(marker in text for marker in _INTERNAL_MARKERS)


def _blocks(profile: dict) -> list[tuple[str, str]]:
    """档案全文 → 结构化块序列 `(类型, 文本)`。

    类型：`h1` / `h2` / `h3` / `kv` / `t`。md 与 txt 两种格式共用同一份块序列，
    保证内容永远一致，不会出现「改了 md 忘了改 txt」。

    遍历**全部模块**（含家庭成员这类网申表单字段），取舍只有 `include_field` 那一处。
    """
    out: list[tuple[str, str]] = []

    name = field_value(profile, "basic", "姓名")
    out.append(("h1", name or "个人简历"))

    for mod in MODULES:
        key, kind = mod["key"], mod["kind"]
        rows = profile.get(key, [])
        if not rows:
            continue
        out.append(("h2", mod["name"]))

        if kind == "fields":
            for row in rows:
                if include_field(row["k"], row["v"]):
                    out.append(("kv", f"{row['k']}：{row['v']}"))
        elif kind == "lines":
            for line in rows:
                if include_field("", line):
                    out.append(("t", line))
        else:
            for index, item in enumerate(rows, 1):
                out.append(("h3", f"{index}. {item_title(item, mod)}"))
                for field, value in item.items():
                    if not include_field(field, value):
                        continue
                    # 多行值（如项目「负责工作」的要点列表）单独成段，
                    # 否则「字段名：\n- 要点」会被解析器当成两个字段。
                    if "\n" in value:
                        out.append(("kv", f"{field}："))
                        out.append(("t", value))
                    else:
                        out.append(("kv", f"{field}：{value}"))
    return out


def render_resume(profile: dict, fmt: str = "md") -> str:
    """生成档案全文的纯文本（`.md` / `.txt`）。

    刻意不做任何排版美化：一行一项、字段名用全角冒号、日期保持 yyyy-MM-dd。
    校招系统的附件解析器最认这种结构，花哨排版反而会导致字段错位。
    内容上不做取舍——档案里有几项就导几项，见 `_blocks`。

    要看排版好的版本请用 `services/resume_hr.py`（HR 版 / 完整版两份 `.docx`）。
    """
    blocks = _blocks(profile)
    lines: list[str] = []

    def blank() -> None:
        """标题前补一个空行——紧贴上一段的 `## xxx` 会被不少 Markdown 解析器
        当成普通文本，导致整段落进错误的字段。"""
        if lines and lines[-1] != "":
            lines.append("")

    if fmt == "txt":
        for kind, text in blocks:
            if kind == "h1":
                lines += [text, "=" * (len(text) * 2), ""]
            elif kind == "h2":
                blank()
                lines.append(f"【{text}】")
            elif kind == "h3":
                blank()
                lines.append(text)
            elif kind == "kv":
                lines.append(text)
            else:
                lines += [text, ""]
        return "\n".join(lines).strip() + "\n"

    for kind, text in blocks:
        if kind == "h1":
            lines += [f"# {text}", ""]
        elif kind in ("h2", "h3"):
            blank()
            mark = "##" if kind == "h2" else "###"
            lines += [f"{mark} {text}", ""]
        elif kind == "kv":
            lines.append(f"- {text}")
        else:
            lines += [text, ""]
    return "\n".join(lines).strip() + "\n"


def resume_filename(profile: dict, fmt: str) -> str:
    """导出文件名：带上姓名，避免下载一堆都叫「简历.md」分不清。"""
    name = field_value(profile, "basic", "姓名") or "简历"
    ext = {"txt": "txt", "docx": "docx"}.get((fmt or "").lower(), "md")
    return f"{name}-投递简历-{datetime.now().strftime('%Y%m%d')}.{ext}"


# Word 导出不在这里：HR 版与完整版共用一套排版，都放在 `services/resume_hr.py`。
# 早年这里还有一个「纯段落、禁表格」的可解析版 `.docx`，后来按用户要求改成
# HR 同款排版 + 档案全量内容（就是那边的 `render_full_docx`），便不再需要。
# 要喂招聘系统自动解析，用本模块的 `.md` / `.txt`。
