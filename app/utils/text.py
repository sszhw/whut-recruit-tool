"""文本清洗、截断与 LLM 输出解析。

为什么要单独抽出来：

历史上 `crawler.plain_text` 与 `resume.plain_text` 是两份实现且行为不一致——
- crawler 版：剥离 script/style、`<br>` 与块级标签转换行、`html.unescape`、`\\xa0` 转空格；
- resume 版：只去标签并手工替换四个常见实体。

同一段公告正文在列表页详情页被洗成一种结果、喂给 LLM 时是另一种结果，
会让敏感性依赖正文的企业分析与推荐口径不一致。现统一下沉为本模块的
`strip_html()`（取较完整的 crawler 版）。
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
_BR_TAG = re.compile(r"(?i)<br\s*/?>")
_BLOCK_END = re.compile(r"(?i)</(p|div|li|tr|h[1-6])>")
_ANY_TAG = re.compile(r"(?s)<[^>]+>")
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_INLINE_SPACES = re.compile(r"[ \t]+")


def strip_html(value: Any) -> str:
    """HTML 富文本 → 纯文本：去标签、保留段落换行、压缩行内空白并丢弃空行。"""
    if not value:
        return ""
    text = str(value)
    text = _SCRIPT_STYLE.sub("", text)
    text = _BR_TAG.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = html.unescape(text).replace("\u00a0", " ")
    lines = [_INLINE_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def truncate(text: str | None, limit: int = 1200) -> str:
    """裁剪正文并去掉首尾空白（对 None / 空串安全）。"""
    return (text or "").strip()[:limit]


def extract_json_object(content: str) -> dict | None:
    """从 LLM 回复中抠出第一个顶层 JSON 对象。

    LLM 常在 JSON 外包 ```json 代码块或夹些解释文字，这里容忍这类噪声。
    解析失败返回 None，由调用方决定降级策略——各处 fallback 的字段结构不同，
    不在这里强行统一。
    """
    if not content:
        return None
    match = _JSON_OBJECT.search(content)
    candidate = match.group(0) if match else content
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
