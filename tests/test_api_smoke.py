"""全路由冒烟测试 —— 重构安全网。

自动遍历 Flask 路由表，对每个 GET 路由发一次请求，断言：

- 不返回 5xx（路由依然存在，且处理函数不会崩）；
- 声称 JSON 的响应必须能被 `get_json()` 解析。

这样后续把 `server.py` 拆成 Blueprint 时，任何 URL 丢失、改名或意外 500
都会被立刻发现，不必为每个端点手写用例。

注意：会真实访问外网的路由在此跳过（它有自己的容错分支，且依赖用户 Key）。
"""

from __future__ import annotations

import re
from urllib.parse import quote

import server

# 路径参数的示例值。task_id 故意填不存在的，
# 用来一并验证「查不到 → 回落」的分支不会炸。
_PATH_ARGS = {
    "task_id": "no_such_task_id",
    "pid": "siliconflow",
    "kind": "company",
}
_DEFAULT_ARG = "x"

# 会打到真实外网 / 有独立容错路径的路由，不参与冒烟
_SKIP_RULES = {"/api/llm/models/<pid>"}


def _concrete_url(rule: str) -> str:
    """把 Flask 路由规则中的 <converter:name> 替换成具体值。"""
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>",
                  lambda m: quote(_PATH_ARGS.get(m.group(1), _DEFAULT_ARG)),
                  rule)


def _smoke_rules() -> list[str]:
    return sorted(
        r.rule for r in server.app.url_map.iter_rules()
        if "GET" in r.methods
        and not r.rule.startswith("/static/")
        and r.rule not in _SKIP_RULES
    )


def test_smoke_covers_reasonable_number_of_routes():
    """路由表至少覆盖这些端点 —— 防止重构时整块 Blueprint 忘记注册。"""
    rules = _smoke_rules()
    assert len(rules) >= 20, f"路由数异常偏少（{len(rules)} 条），可能有 Blueprint 未注册"


def test_get_routes_never_500():
    client = server.app.test_client()
    failures: list[str] = []
    for rule in _smoke_rules():
        resp = client.get(_concrete_url(rule))
        if resp.status_code >= 500:
            failures.append(f"{rule}  ->  {_concrete_url(rule)}  HTTP {resp.status_code}")
    assert not failures, "以下 GET 路由返回 5xx：\n" + "\n".join(failures)


def test_json_responses_are_parseable():
    """声明了 JSON 的响应必须能解析；文件下载类（csv/xlsx/ics）放行。"""
    client = server.app.test_client()
    bad: list[str] = []
    for rule in _smoke_rules():
        if not rule.startswith("/api/"):
            continue
        resp = client.get(_concrete_url(rule))
        if "application/json" not in (resp.content_type or ""):
            continue  # 文件下载或非 JSON 错误页，放行
        try:
            resp.get_json()
        except Exception as e:  # noqa: BLE001 - 冒烟测试需要捕获任何解析异常
            bad.append(f"{rule}  ->  JSON 解析失败：{e}")
    assert not bad, "以下路由返回了无法解析的 JSON：\n" + "\n".join(bad)


def test_root_page_serves_html():
    resp = server.app.test_client().get("/")
    assert resp.status_code == 200
    assert "text/html" in (resp.content_type or "")
