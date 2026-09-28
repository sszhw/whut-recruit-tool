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
_SKIP_RULES = {
    "/api/llm/models/<pid>",
    # SSE 长连接：永远不会自己结束，GET 遍历会挂住；它有 test_events_stream.py 单独覆盖
    "/api/events",
}

# 参与「空体 POST 冒烟」的路由：无副作用、不打外网。
# 其余 POST 不在此列是因为它们会真起后台任务、写 config.json 或改收藏，
# 冒烟测试不该产生这些副作用。
_POST_SAFE = (
    "/api/import/md",            # 空正文 → 400
    "/api/preach/fav",           # 空 id → 400（在动收藏数据之前就拦下）
    "/api/preach/unfav",
    "/api/resume/extract",       # 无文件 → 400
    "/api/resume/recommend",     # 测试内把 API Key 置空 → 400
    "/api/task/stop",            # 空 task_id → 400
)

# 有副作用的 POST：会起后台任务 / 写 config.json / 改收藏 / 打外网，不参与空体冒烟。
# 在这里登记而不是"默认跳过"，是为了让新增路由时被迫想清楚它属于哪一类。
_POST_SIDE_EFFECT = (
    "/api/analyze",          # 起 LLM 分析任务
    "/api/board/items",      # 新增看板条目（会落盘）
    "/api/board/items/<item_id>/delete",  # 删除看板条目（会落盘）
    "/api/board/items/<item_id>/note",    # 更新看板备注（会落盘）
    "/api/board/items/<item_id>/status",  # 更新看板阶段（会落盘）
    "/api/config",           # 写 config.json
    "/api/config/key",       # 写 API Key
    "/api/crawl",            # 起抓取任务
    "/api/llm/test",         # 打真实外网
    "/api/preach/check",     # 起宣讲会检查任务
    "/api/preach/fav-all",   # 批量改收藏（会落盘）
    "/api/preach/flow",      # 起工作地流动分析任务
    "/api/preach/unfav-all",  # 批量改收藏（会落盘）
    "/api/recruit/update",   # 起增量更新任务
)


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


def test_post_routes_never_500(monkeypatch):
    """POST 路由的空体冒烟：只断言「不 5xx」。

    POST 的 5xx 通常不是业务错误，而是**接线错误**——例如路由传给服务的关键字
    参数名与函数签名对不上（TypeError）。GET 冒烟遍历不到 POST，必须单独覆盖。

    需要 API Key 的路由先把 Key 置空，强制走「未配置 → 400」分支：
    既让结果确定，也避免测试真的去烧 token / 依赖外网。
    """
    import services.recommend as rec_svc

    monkeypatch.setattr(rec_svc, "get_llm",
                        lambda: {"api_key": "", "label": "测试厂商", "model": "m", "base_url": ""})
    client = server.app.test_client()
    failures: list[str] = []
    for rule in _POST_SAFE:
        resp = client.post(rule, json={})
        if resp.status_code >= 500:
            failures.append(f"{rule}  ->  HTTP {resp.status_code}")
    assert not failures, "以下 POST 路由返回 5xx（多半是接线错误）：\n" + "\n".join(failures)


def test_all_post_routes_are_classified():
    """每个 POST 路由都必须明确归入「可安全冒烟」或「有副作用」两类之一。

    新增 POST 路由时若忘了分类，这条会失败，逼作者想清楚它有没有副作用，
    而不是默认被排除在冒烟之外、悄悄失去覆盖。
    """
    classified = set(_POST_SAFE) | set(_POST_SIDE_EFFECT)
    actual = sorted(r.rule for r in server.app.url_map.iter_rules()
                    if "POST" in r.methods and not r.rule.startswith("/static/"))
    unknown = [r for r in actual if r not in classified]
    assert not unknown, f"未分类的 POST 路由（请在 _POST_SAFE 或 _POST_SIDE_EFFECT 中登记）：{unknown}"
