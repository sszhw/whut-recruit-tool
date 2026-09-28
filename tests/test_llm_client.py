"""统一 LLM 客户端：重试、超时、HTTP 错误与文案归一化。

全程 monkeypatch 掉 requests.post，**不打任何真实外网**；重试等待也替换成记录器，
避免因退避把用例拖成几十秒。
"""

from __future__ import annotations

import inspect

import llm_client
import pytest
import requests
import resume
import settings

BASE = "https://llm.example.com/v1"


class _FakeResp:
    """够用的 Response 替身：只实现 client 真正用到的 ok / status_code / json / text。"""

    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload


def _ok(content: str = "你好") -> _FakeResp:
    """一条正常的补全响应。"""
    return _FakeResp(200, {"choices": [{"message": {"content": content}}]})


def _cfg(**kw) -> llm_client.LLMConfig:
    """测试用配置：不读 config.json，避免依赖本机密钥。"""
    fields = {"api_key": "sk-test", "model": "test-model", "base_url": BASE}
    fields.update(kw)
    return llm_client.LLMConfig(**fields)


def _company(name: str = "甲企业") -> dict:
    """resume.company_lines 需要的完整字段（缺一个就 KeyError）。"""
    return {"name": name, "type": "央企", "so": "是", "locations": ["武汉"],
            "text": "招聘机械工程师", "title": "校招", "count": 1}


@pytest.fixture()
def stub(monkeypatch):
    """安装假的 requests.post：queue 里放响应或异常，最后一项会被反复使用。"""
    def _install(queue):
        calls: list[dict] = []
        sleeps: list[float] = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(llm_client.requests, "post", fake_post)
        monkeypatch.setattr(llm_client, "_pause", sleeps.append)
        return calls, sleeps
    return _install


# ---------------------------------------------------------------- 成功路径

def test_chat_success(stub):
    calls, _ = stub([_ok("推荐结果")])
    assert llm_client.chat([{"role": "user", "content": "hi"}], _cfg()) == "推荐结果"
    # URL 与鉴权头沿用 OpenAI 兼容协议，换厂商只改 base_url
    assert calls[0]["url"] == BASE + "/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert calls[0]["json"]["model"] == "test-model"


def test_chat_sends_extra_payload_fields(stub):
    """response_format 这类厂商扩展字段可以透传，不必为它再开一个参数。"""
    calls, _ = stub([_ok()])
    llm_client.chat([{"role": "user", "content": "hi"}], _cfg(),
                    response_format={"type": "json_object"})
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}


def test_complete_returns_status_and_raw(stub):
    stub([_ok("内容")])
    resp = llm_client.complete([{"role": "user", "content": "hi"}], _cfg())
    assert resp.status_code == 200 and resp.content == "内容"
    assert resp.raw["choices"][0]["message"]["content"] == "内容"


# ---------------------------------------------------------------- 重试

def test_retry_then_success(stub):
    """网络抖动后恢复：第二次成功，且退避按 3s 线性增长。"""
    calls, sleeps = stub([requests.ConnectionError("connection reset"), _ok()])
    assert llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=3)) == "你好"
    assert len(calls) == 2
    assert sleeps == [3.0]


def test_rate_limit_uses_longer_backoff(stub):
    """429 的退避比网络抖动更久（厂商多按分钟限流）。"""
    _, sleeps = stub([_FakeResp(429, {"error": {"message": "rate limited"}}), _ok()])
    llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=3))
    assert sleeps == [5.0]


def test_network_error_exhausted(stub):
    """重试耗尽：抛 LLMError，kind 为 network，文案里带出重试次数。"""
    calls, sleeps = stub([requests.ConnectionError("connection reset")])
    with pytest.raises(llm_client.LLMError) as ei:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=3))
    assert ei.value.kind == "network"
    assert ei.value.status_code is None
    assert "网络错误" in ei.value.message and "已重试 2 次" in ei.value.message
    assert len(calls) == 3 and sleeps == [3.0, 6.0]


def test_server_error_retried_client_error_not(stub):
    """5xx 值得重试，4xx 重试多少次结果都一样——白白拖慢逐家企业跑的分析任务。"""
    calls, _ = stub([_FakeResp(500, {"error": {"message": "boom"}})])
    with pytest.raises(llm_client.LLMError) as ei:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=2))
    assert ei.value.status_code == 500 and len(calls) == 2

    calls2, sleeps2 = stub([_FakeResp(401, {"error": {"message": "invalid key"}})])
    with pytest.raises(llm_client.LLMError) as ei2:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=3))
    assert ei2.value.status_code == 401
    assert len(calls2) == 1 and sleeps2 == []


# ---------------------------------------------------------------- 超时与错误归一化

def test_timeout_is_network_error(stub):
    """超时单独归一成「超时」文案，而不是笼统的「网络错误」。"""
    stub([requests.Timeout("read timed out")])
    with pytest.raises(llm_client.LLMError) as ei:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=1))
    assert ei.value.kind == "network"
    assert "超时" in ei.value.message


def test_http_error_message_normalized(stub):
    """非 2xx 翻译成中文 + 厂商原始提示；不再出现写死的厂商名。"""
    stub([_FakeResp(401, {"error": {"message": "Invalid API key"}})])
    with pytest.raises(llm_client.LLMError) as ei:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=1))
    assert ei.value.status_code == 401
    assert "API Key 无效或已失效" in ei.value.message
    assert "Invalid API key" in ei.value.message


def test_http_error_falls_back_to_plain_text(stub):
    """响应体不是 JSON（网关常见的 HTML 报错页）时退回原文，不能崩在 json() 上。"""
    stub([_FakeResp(502, None, text="<html>bad gateway</html>")])
    with pytest.raises(llm_client.LLMError) as ei:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=1))
    assert "服务端暂时不可用" in ei.value.message
    assert "bad gateway" in ei.value.message


def test_broken_success_response_is_an_error(stub):
    """2xx 但结构不对也算失败——否则 KeyError 会炸在业务代码里。"""
    stub([_FakeResp(200, {"unexpected": True})])
    with pytest.raises(llm_client.LLMError) as ei:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=1))
    assert ei.value.kind == "response"
    assert "结构异常" in ei.value.message


def test_non_json_body_is_an_error(stub):
    stub([_FakeResp(200, None, text="not json")])
    with pytest.raises(llm_client.LLMError) as ei:
        llm_client.chat([{"role": "user", "content": "hi"}], _cfg(max_retries=1))
    assert "不是合法 JSON" in ei.value.message


def test_missing_config_fails_fast(stub):
    """配置缺失不该发请求：直接抛，kind 为 config。"""
    calls, _ = stub([_ok()])
    for cfg in (_cfg(api_key=""), _cfg(base_url=""), _cfg(model="")):
        with pytest.raises(llm_client.LLMError) as ei:
            llm_client.chat([{"role": "user", "content": "hi"}], cfg)
        assert ei.value.kind == "config"
    assert calls == []


def test_from_settings_falls_back_to_current_provider(monkeypatch):
    """显式传入的字段优先；缺省的模型名回落到 settings.DEFAULT_MODEL（唯一来源）。"""
    monkeypatch.setattr(llm_client.settings, "get_llm",
                        lambda: {"provider": "siliconflow", "label": "硅基流动",
                                 "base_url": "https://api.siliconflow.cn/v1",
                                 "api_key": "sk-cfg", "model": ""})
    cfg = llm_client.from_settings(api_key="sk-explicit")
    assert cfg.api_key == "sk-explicit"
    assert cfg.base_url == "https://api.siliconflow.cn/v1"
    assert cfg.model == settings.DEFAULT_MODEL


# ---------------------------------------------------------------- 连通性自检

def test_ping_ok(stub):
    stub([_ok("hi")])
    res = llm_client.ping(_cfg())
    assert res == {"ok": True, "status": 200, "error": ""}


def test_ping_failure_keeps_status_code(stub):
    stub([_FakeResp(403, {"error": {"message": "no permission"}})])
    res = llm_client.ping(_cfg())
    assert res["ok"] is False and res["status"] == 403
    assert "没有访问该模型的权限" in res["error"]


def test_ping_network_failure_has_no_status(stub):
    """网络层就失败时没有 HTTP 状态码，调用方据此决定是否下发 status 字段。"""
    stub([requests.ConnectionError("dns failure")])
    res = llm_client.ping(_cfg())
    assert res["ok"] is False and res["status"] is None and "网络错误" in res["error"]


def test_ping_does_not_retry(stub):
    """点「测试连接」要立刻知道结果，重试只会让用户干等。"""
    calls, sleeps = stub([_FakeResp(500, {"error": {"message": "boom"}})])
    assert llm_client.ping(_cfg())["ok"] is False
    assert len(calls) == 1 and sleeps == []


# ---------------------------------------------------------------- 多模态

def test_vision_message_inlines_image_as_data_url():
    msg = llm_client.vision_message("识别文字", b"\x89PNG", "image/png")
    assert msg["role"] == "user"
    parts = msg["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1]["content"] == "识别文字"


# ---------------------------------------------------------------- resume 迁移后的对外契约

def _params(fn) -> list:
    return [(p.name, p.default) for p in inspect.signature(fn).parameters.values()]


def test_resume_public_signatures_unchanged():
    """services 与前端依赖这三个函数，重构不得改变签名。"""
    empty = inspect.Parameter.empty
    assert _params(resume.extract_text) == [
        ("api_key", empty), ("path", empty), ("ext", empty),
        ("vision_model", None), ("base_url", None)]
    assert _params(resume.recommend) == [
        ("api_key", empty), ("model", empty), ("resume_text", empty), ("companies", empty),
        ("work_place", ""), ("company_type", ""), ("top", 10), ("max_retries", 3),
        ("base_url", None)]
    assert _params(resume.keyword_recommend) == [
        ("resume_text", empty), ("companies", empty),
        ("work_place", ""), ("company_type", ""), ("top", 10)]


def test_recommend_returns_parsed_result(stub):
    stub([_ok('{"resume_summary": "机械硕士", "recommendations": []}')])
    out = resume.recommend("sk", "m", "简历正文", [_company()])
    assert out == {"resume_summary": "机械硕士", "recommendations": []}


def test_recommend_error_shape_unchanged(stub):
    """失败返回 {"error": ...}（HTTP 错误还带 status_code），上层据此降级为关键词匹配。"""
    stub([_FakeResp(401, {"error": {"message": "bad key"}})])
    out = resume.recommend("sk", "m", "简历正文", [_company()])
    assert out["error"] and out["status_code"] == 401
    # 文案归一化：不再把某个厂商名写死在提示里
    assert "硅基流动" not in out["error"]

    stub([requests.ConnectionError("reset")])
    out = resume.recommend("sk", "m", "简历正文", [_company()], max_retries=1)
    assert "网络错误" in out["error"] and "status_code" not in out


def test_recommend_without_companies_short_circuits(stub):
    """没有候选企业时不发请求——原行为保留。"""
    calls, _ = stub([_ok()])
    out = resume.recommend("sk", "m", "简历正文", [])
    assert out["error"].startswith("没有可推荐的企业数据")
    assert calls == []


def test_ocr_uses_vision_model_and_no_retry(stub):
    """OCR 走同一个 client：多模态消息 + 失败即抛（扫描件逐页调用，逐页重试太慢）。"""
    calls, _ = stub([_ok("张三 简历")])
    assert resume.ocr_image_bytes("sk", "vl-model", b"\x89PNG") == "张三 简历"
    assert calls[0]["json"]["model"] == "vl-model"
    assert calls[0]["json"]["messages"][0]["content"][0]["type"] == "image_url"

    stub([_FakeResp(429, {"error": {"message": "rate limited"}})])
    with pytest.raises(llm_client.LLMError):
        resume.ocr_image_bytes("sk", "vl-model", b"\x89PNG")
