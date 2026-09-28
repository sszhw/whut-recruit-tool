"""analyze.py 已迁到统一 LLMClient 的回归测试。

目的：锁住「企业分析不再自备一套 requests + 重试 + 错误文案」这件事。
此前 analyze.call_api 自己写 requests.post、只认 429 一种可重试状态码、
超时固定 90s，且模块级 BASE_URL 只认 LLM_BASE_URL / SILICONFLOW_BASE_URL
（resume 只认后者）——同一份配置两边会解析出不同地址。现在统一走 llm_client。

测试全程 monkeypatch llm_client，不发真实请求。
"""

from __future__ import annotations

import inspect

import analyze
import llm_client
import pytest
import settings


def _ok_json() -> str:
    return '```json\n{"company_type":"央企","is_state_owned":true,"confidence":"高",' \
           '"locations":["武汉"],"evidence":"国务院国资委监管"}\n```'


def test_call_api_signature_unchanged():
    """analyze_preach.py:203 按位置参数调用，签名不能变。"""
    params = list(inspect.signature(analyze.call_api).parameters)
    assert params == ["api_key", "model", "name", "text", "max_retries"]


def test_no_module_level_base_url():
    """模块级 BASE_URL 是环境变量口径分裂的根源，必须已移除。

    地址 / Key / 模型统一来自 settings 的当前厂商，由 llm_client.from_settings 解析。
    """
    assert not hasattr(analyze, "BASE_URL")


def test_call_api_success_parses_json(monkeypatch):
    """成功路径：LLM 返回裹在 ```json 代码块里的对象，应被容错解析。"""
    seen = {}

    def fake_chat(messages, config, **kwargs):
        seen["messages"] = messages
        seen["config"] = config
        seen["kwargs"] = kwargs
        return _ok_json()

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    result = analyze.call_api("k", "m", "中国建筑", "招聘公告正文")

    assert result["company_type"] == "央企"
    assert result["is_state_owned"] is True
    assert result["locations"] == ["武汉"]
    assert result["_raw"] == ""
    # 系统提示词 + 用户消息两条，温度与长度沿用原参数
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert seen["kwargs"]["temperature"] == 0.1
    assert seen["kwargs"]["max_tokens"] == 400
    # 调用点传入的 api_key / model / max_retries 必须真的进了 config
    assert seen["config"].api_key == "k"
    assert seen["config"].model == "m"


def test_call_api_uses_settings_when_key_empty(monkeypatch):
    """调用点不传 Key 时，由 settings 当前厂商补齐（CLI 场景靠 config.json）。"""
    monkeypatch.setattr(settings, "get_llm", lambda: {"api_key": "cfg-key", "model": "cfg-model",
                                                      "base_url": "https://example.invalid/v1",
                                                      "label": "测试厂商"})
    seen = {}
    monkeypatch.setattr(llm_client, "chat", lambda messages, config, **kw: (
        seen.setdefault("config", config), _ok_json())[1])
    analyze.call_api("", "", "某企业", "")

    assert seen["config"].api_key == "cfg-key"
    assert seen["config"].model == "cfg-model"


def test_call_api_failure_normalized(monkeypatch):
    """LLMError 必须被吃掉转成「分析失败」结构——批处理里一家失败不能中断整批。"""
    def boom(messages, config, **kwargs):
        raise llm_client.LLMError("API Key 无效或已失效（HTTP 401）：Invalid API key",
                                  kind="http", status_code=401)

    monkeypatch.setattr(llm_client, "chat", boom)
    result = analyze.call_api("k", "m", "某企业", "")

    assert result["company_type"] == "分析失败"
    assert result["is_state_owned"] is None
    assert result["locations"] == []
    assert "API Key 无效或已失效" in result["evidence"]
    assert "_raw" in result


@pytest.mark.parametrize("detail,kind", [
    ("网络错误：connection refused", "network"),
    ("请求超时（180s 无响应）", "network"),
    ("模型返回结构异常（缺少 choices[0].message.content）", "response"),
    ("未配置 API Key", "config"),
])
def test_call_api_failure_shapes(monkeypatch, detail, kind):
    """各类 LLMError 都要转成同构的失败结果，字段一个都不能少（上层按 key 取值）。"""
    monkeypatch.setattr(llm_client, "chat",
                        lambda *a, **kw: (_ for _ in ()).throw(llm_client.LLMError(detail, kind=kind)))
    result = analyze.call_api("k", "m", "某企业", "")

    assert set(result) == {"company_type", "is_state_owned", "confidence", "locations", "evidence", "_raw"}
    assert detail in result["evidence"]


def test_max_retries_passthrough(monkeypatch):
    """max_retries 要透传给 client——重试策略在 client 里，analyze 不再自己退避。"""
    seen = {}
    monkeypatch.setattr(llm_client, "from_settings",
                        lambda **kw: seen.setdefault("kw", kw) or llm_client.LLMConfig())
    monkeypatch.setattr(llm_client, "chat", lambda *a, **kw: _ok_json())
    analyze.call_api("k", "m", "某企业", "", max_retries=1)

    assert seen["kw"]["max_retries"] == 1
