"""统一的 LLM 调用客户端（OpenAI 兼容协议的 /chat/completions）。

背景：此前企业分析（analyze）、简历解析/推荐（resume）、连通性测试（api.settings_cfg）
各写了一份 requests.post，超时、重试、错误文案三套互不相同——
analyze 认 LLM_BASE_URL 环境变量而 resume 只认 SILICONFLOW_BASE_URL；
推荐失败返回 {"error": "硅基流动返回 401：..."}（把厂商名写死在文案里，换厂商就成了假话），
OCR 失败却直接抛 RuntimeError、网络异常又原样往上冒。改超时、换厂商要改三个地方。

本模块把这些收敛成一处：配置解析、超时、重试退避、错误归一化。
调用点只负责拼 prompt 与解析业务结果。

**依赖方向**：只依赖标准库 + requests + settings。settings 是全项目依赖终点，
反向 import 业务模块（repository / resume / analyze）会成环，因此这里也不 import flask，
这样 CLI / 定时任务 / Web 都能用同一个 client。

**失败约定**：网络异常、超时、HTTP 非 2xx、响应结构异常、配置缺失，一律抛 `LLMError`，
由调用点捕获后转成各自的呈现形式——analyze 转成「分析失败」结构、resume 转成
{"error": ...}、settings_cfg 转成 {"ok": False, ...}。内部只有一种失败表示，
不会每个调用点各发明一种。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass

import requests
import settings

# 超时按「慢模型 + 长 prompt」取值。此前 analyze 是 90s、resume 是 180s，
# 统一取较宽松的 180s，需要快的场景（连通性自检）由调用点显式压低。
DEFAULT_TIMEOUT = 180.0
# 默认重试次数沿用各调用点原先的 max_retries=3
DEFAULT_MAX_RETRIES = 3
# 网络类失败的退避基数（秒）：第 n 次等待 base * n，避免抖动时把请求砸向刚恢复的服务
RETRY_BACKOFF = 3.0
# 限流退避基数（秒）：厂商多按分钟限流，等比等待比网络错误的退避更久才可能成功
RATE_LIMIT_BACKOFF = 5.0

# 常见状态码 → 人能看懂的中文。没有映射的按 5xx / 4xx 归类给一句兜底话
_HTTP_HINTS = {
    400: "请求参数有误",
    401: "API Key 无效或已失效",
    403: "API Key 没有访问该模型的权限",
    404: "接口或模型不存在",
    422: "请求体不合法",
    429: "请求过于频繁，已被限流",
}


class LLMError(Exception):
    """LLM 调用失败的统一表示。

    kind 用于区分失败来源：`config`（配置缺失，重试无用）、`network`（连不上/超时）、
    `http`（服务端非 2xx）、`response`（2xx 但结构不是预期的补全结果）。
    """

    def __init__(self, detail: str, kind: str = "http", status_code: int | None = None,
                 retries: int = 0) -> None:
        super().__init__(detail)
        self.detail = detail          # 不带重试计数的原始文案
        self.kind = kind
        self.status_code = status_code
        self.retries = retries

    @property
    def message(self) -> str:
        """归一化后的可读文案：主体 + 已重试次数（没重试过就不带，免得误导）。"""
        return self.detail + (f"（已重试 {self.retries} 次）" if self.retries else "")

    def __str__(self) -> str:
        return self.message


@dataclass
class LLMConfig:
    """一次 LLM 调用的连接参数（不含 prompt，prompt 由调用点拼）。"""

    api_key: str = ""
    model: str = ""
    base_url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES


@dataclass
class LLMResponse:
    """一次成功的补全结果。status_code / raw 保留下来是为了连通性自检要回报真实状态码。"""

    content: str
    status_code: int
    raw: dict


def from_settings(base_url: str = "", api_key: str = "", model: str = "",
                  timeout: float = DEFAULT_TIMEOUT,
                  max_retries: int = DEFAULT_MAX_RETRIES) -> LLMConfig:
    """按 settings 里当前配置的厂商组装配置，显式传入的字段优先。

    调用点常常已经从别处拿到了 Key/模型（例如用户在页面上临时选了另一个厂商），
    所以三个字段都允许覆盖；缺省部分才回落到配置里的当前厂商。
    """
    llm = settings.get_llm()
    return LLMConfig(
        api_key=(api_key or llm["api_key"]).strip(),
        model=(model or llm["model"] or settings.DEFAULT_MODEL).strip(),
        base_url=(base_url or llm["base_url"]).strip().rstrip("/"),
        timeout=timeout,
        max_retries=max_retries,
    )


def complete(messages: list[dict], config: LLMConfig, temperature: float = 0.2,
             max_tokens: int = 2000, **extra) -> LLMResponse:
    """发一次 chat 补全并按重试策略重试；成功返回 LLMResponse，失败抛 LLMError。

    extra 透进请求体（如 response_format / top_p），避免为此再开一个参数。
    """
    _require(config)
    payload = {"model": config.model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    payload.update(extra)

    attempts = max(1, int(config.max_retries))
    for attempt in range(attempts):
        try:
            status, data = _post(config, payload)
            return LLMResponse(content=_content(data), status_code=status, raw=data)
        except LLMError as exc:
            exc.retries = attempt
            if attempt == attempts - 1 or not _retryable(exc):
                raise
            _pause(_backoff(exc, attempt))
    raise LLMError("LLM 调用失败", kind="response")  # 不可达：attempts >= 1，循环必 return 或 raise


def chat(messages: list[dict], config: LLMConfig, temperature: float = 0.2,
         max_tokens: int = 2000, **extra) -> str:
    """只要文本的快捷入口，等价于 `complete(...).content`。"""
    return complete(messages, config, temperature=temperature, max_tokens=max_tokens, **extra).content


def ping(config: LLMConfig, timeout: float = 30.0) -> dict:
    """连通性自检：发一条最小请求，返回 {"ok": bool, "status": int | None, "error": str}。

    不重试——用户点「测试连接」要的是立刻知道 Key 对不对，重试只会让他多等几十秒；
    网络层就失败时 status 为 None，调用方据此决定是否下发该字段。
    """
    probe = LLMConfig(api_key=config.api_key, model=config.model, base_url=config.base_url,
                      timeout=timeout, max_retries=1)
    try:
        resp = complete([{"role": "user", "content": "hi"}], probe, temperature=0.0, max_tokens=4)
    except LLMError as exc:
        return {"ok": False, "status": exc.status_code, "error": exc.message}
    return {"ok": True, "status": resp.status_code, "error": ""}


def vision_message(prompt: str, image_bytes: bytes, mime: str = "image/png") -> dict:
    """构造一条带图片的多模态 user 消息（图片以 base64 data URL 内联）。

    图片怎么塞进 content 属于协议细节，放在 client 里，业务侧只管「这段提示词 + 这张图」。
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "content": prompt},
    ]}


def _require(config: LLMConfig) -> None:
    """配置缺失是最不值得重试的失败：直接抛，别浪费一次网络往返。"""
    if not config.api_key:
        raise LLMError("未配置 API Key", kind="config")
    if not config.base_url:
        raise LLMError("未配置 Base URL", kind="config")
    if not config.model:
        raise LLMError("未配置模型名", kind="config")


def _post(config: LLMConfig, payload: dict) -> tuple[int, dict]:
    """发一次请求，把「网络异常 / 非 2xx / 非 JSON」统一归一化成 LLMError。"""
    url = config.base_url.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=config.timeout,
        )
    except requests.Timeout as exc:
        raise LLMError(f"请求超时（{config.timeout:.0f}s 无响应）", kind="network") from exc
    except requests.RequestException as exc:
        raise LLMError(f"网络错误：{exc}", kind="network") from exc

    if not resp.ok:
        raise LLMError(_http_detail(resp), kind="http", status_code=resp.status_code)
    try:
        return resp.status_code, resp.json()
    except ValueError as exc:
        raise LLMError("服务端返回的内容不是合法 JSON", kind="response",
                       status_code=resp.status_code) from exc


def _content(data: dict) -> str:
    """从补全响应里取 assistant 文本；结构不对也算失败（否则上层会拿到 KeyError）。"""
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("模型返回结构异常（缺少 choices[0].message.content）", kind="response") from exc


def _http_detail(resp) -> str:
    """把非 2xx 响应翻译成一句中文 + 厂商给的原始提示，方便用户自助排查。"""
    code = resp.status_code
    hint = _HTTP_HINTS.get(code, "服务端暂时不可用" if code >= 500 else "请求被拒绝")
    detail = _error_detail(resp)
    return f"{hint}（HTTP {code}）" + (f"：{detail}" if detail else "")


def _error_detail(resp) -> str:
    """尽量抠出厂商返回的 message；响应体不是 JSON 就退回截断后的原文。"""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300]
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")[:300]
    return str(err or body.get("message") or "")[:300]


def _retryable(exc: LLMError) -> bool:
    """只有「可能自己好」的失败才值得重试。

    401/403/404 重试多少次结果都一样，而分析任务是逐家企业跑的，
    无效 Key 时无脑重试会把整批任务拖成几倍时长。
    """
    if exc.kind == "network":
        return True
    return exc.status_code == 429 or (exc.status_code or 0) >= 500


def _backoff(exc: LLMError, attempt: int) -> float:
    """退避时长：限流等比等得更久，网络抖动按 3s 线性增长。"""
    base = RATE_LIMIT_BACKOFF if exc.status_code == 429 else RETRY_BACKOFF
    return base * (attempt + 1)


def _pause(seconds: float) -> None:
    """重试前等待。单独抽成函数，测试可以替换掉真实 sleep 而不必 monkeypatch 全局 time。"""
    time.sleep(seconds)
