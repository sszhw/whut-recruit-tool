"""从简历原文识别并结构化为投递档案。

**为什么走 LLM 而不是正则**

简历排版没有统一格式：同样是「教育经历」，有人按时间倒序、有人先硕士后本科；
「项目经历」有的写「项目简介 / 负责工作」四段，有的只有一段话。
正则只能覆盖自己见过的那一种排版，换个模板就全漏。

**为什么识别结果不直接写入档案**

LLM 会编。让它补全「简历里没写」的字段，是这类功能最危险的地方——
它会顺手造一个「专业排名 30/100」「GPA 3.8」出来，而用户根本发现不了，
直到背调对不上。所以这里**只返回建议，不落盘**：由界面把「当前值 → 识别值」
摊开给用户逐项勾选，且**默认不勾选会覆盖已有内容的项**。

本模块不 import flask。
"""

from __future__ import annotations

import json
import re

import llm_client
from settings import get_llm

from services import ServiceError
from services import profile as prof_svc

# 送进 LLM 的最大字符数。简历一般 2~5 千字，截断保护的是上下文与费用，
# 不是精度——真被截断时提示语会说明。
_MAX_CHARS = 12000

# 给模型的字段指引：不是强制 schema，只是告诉它每个模块通常装什么。
# 与 prof_svc.MODULES 的 key 一一对应，改动时两边要同步。
_FIELD_HINTS = {
    "basic": "个人基本信息。建议字段：姓名、性别、出生日期、政治面貌、证件类型、证件号码、民族、籍贯、"
             "生源地、现居住地、婚姻状况、健康状况、身高、体重、电子邮箱、移动电话、通信地址、"
             "毕业院校、就读院校所在城市、是否为应届毕业生、毕业时间、最高学历院系、学历、学位、专业、研究方向",
    "education": "教育经历。每条建议字段：学校名称、入学日期、毕业/预计毕业日期、国家（地区）、教育类型、"
                 "学科门类、学历、专业、专业方向、研究方向、学位、所学主要课程、学分绩点、"
                 "专业成绩排名(%)、是否第一学历",
    "projects": "项目 / 实习经历。每条建议字段：时间、项目名称、项目简介、负责工作（多条要点用 \\n 连接，"
                "每行以 - 开头）",
    "languages": "外语水平。每条建议字段：外语语种、外语水平、成绩、是否第一外语",
    "awards": "获奖信息。每条建议字段：获奖时间、奖项名称、奖励级别、奖励批准单位、个人/集体",
    "intent": "求职意向。建议字段：期望工作地点、期望岗位、期望薪资、是否接受岗位调剂、到岗时间",
    "skills": "技能。建议字段：开发语言、掌握程度、其他技能、特长、计算机水平",
    "summary": "自我评价 / 个人优势。每条一段话，保留原文的关键技术栈与能力描述",
    "hobbies": "个人爱好 / 兴趣爱好",
    "family": "家庭成员。每条建议字段：亲属姓名、亲属关系、亲属出生日期、亲属性别、亲属政治面貌、"
              "亲属职位、亲属工作单位、现居住地址、亲属联系电话",
}

_SYSTEM = (
    "你是简历信息抽取助手。你的任务是把一段简历原文整理成结构化的 JSON，用于预填招聘网站的在线申请表。\n"
    "严格要求：\n"
    "1. 只输出 JSON 本身，不要解释、不要 markdown 代码块围栏、不要任何多余文字。\n"
    "2. 只提取简历原文里**确实写了**的信息。任何原文没有的内容一律留空字符串，"
    "**严禁推测、补全或编造**（尤其是排名、绩点、薪资、证书编号、亲属信息）。\n"
    "3. 日期统一成 yyyy-MM-dd；只有年月的写成 yyyy-MM。\n"
    "4. 字段名用中文，与下面给出的建议字段保持一致；建议字段之外的信息也可以加，宁可多一个字段也不要丢信息。\n"
    "5. 值里的换行写成 \\n。\n"
)


def _schema_block() -> str:
    """把模块结构描述给模型：`kind` 决定它该输出数组还是对象数组。"""
    lines = []
    for mod in prof_svc.MODULES:
        key, kind, name = mod["key"], mod["kind"], mod["name"]
        hint = _FIELD_HINTS.get(key, name)
        if kind == "fields":
            shape = '[{"k":"字段名","v":"值"}, ...]'
        elif kind == "lines":
            shape = '["一段话", "另一段话"]'
        else:
            shape = '[{"字段1":"值","字段2":"值"}, ...]'
        lines.append(f'- "{key}"（{name}）：{shape}\n  内容：{hint}')
    return "\n".join(lines)


def build_messages(text: str) -> list[dict]:
    """构造给 LLM 的消息；抽出来是为了能单独测试提示词，不必真的调模型。"""
    schema = _schema_block()
    keys = "、".join(prof_svc.MODULE_KEYS)
    user = (
        f"请从下面的简历原文中提取信息，输出一个 JSON 对象，"
        f"只包含这些 key（没有内容的 key 直接省略）：{keys}。\n\n"
        f"各 key 的结构与内容要求：\n{schema}\n\n"
        f"简历原文：\n{text}"
    )
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def _parse_json(raw: str) -> dict:
    """从模型输出里取出 JSON 对象。

    模型经常自作主张包一层 ```json 围栏，有时还会在前后加一句「好的，如下」。
    这里逐级降级，最后才抛错。
    """
    s = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fenced:
        s = fenced.group(1).strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
    try:
        data = json.loads(s)
    except (ValueError, TypeError) as exc:
        raise ServiceError(f"模型返回的不是合法 JSON（{exc}），请重试或换一个模型") from exc
    if not isinstance(data, dict):
        raise ServiceError("模型返回的 JSON 顶层不是对象，请重试")
    return data


def suggest_from_text(text: str, model: str = "") -> dict:
    """把简历原文识别成档案结构；**只返回建议，不写文件**。"""
    text = (text or "").strip()
    if len(text) < 20:
        raise ServiceError("简历文字太少，识别不出内容")
    llm = get_llm()
    if not llm["api_key"]:
        raise ServiceError(f"请先在「设置」中配置 {llm['label']} API Key")

    truncated = len(text) > _MAX_CHARS
    config = llm_client.from_settings(base_url=llm["base_url"], api_key=llm["api_key"],
                                      model=(model or "").strip() or llm["model"])
    try:
        raw = llm_client.chat(build_messages(text[:_MAX_CHARS]), config,
                              temperature=0.1, max_tokens=4000)
    except llm_client.LLMError as exc:
        raise ServiceError(f"模型调用失败：{exc}") from exc

    suggestion = prof_svc.normalize(_parse_json(raw))
    filled = sum(len(v) for k, v in suggestion.items() if isinstance(v, list))
    return {
        "suggestion": suggestion,
        "model": config.model,
        "chars": len(text),
        "truncated": truncated,
        "items": filled,
    }
