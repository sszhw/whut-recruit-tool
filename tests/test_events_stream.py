"""SSE 事件流测试。

SSE 是长连接，不能进普通的全路由冒烟（会把测试挂死），所以单独覆盖。

**读取方式是被约束的**：流在推完首屏后会进入 1 秒休眠等下一轮检测，
所以测试不能在读完想要的事件后继续 `next()`——那会阻塞到下一个心跳（20 秒）。
服务端专门发了 `ready` 帧作为首屏批次的边界，测试见到它就停。

这里要守住的三件事：
1. 首帧必须是完整的 `status` 事件——前端靠它渲染首屏，缺了就白屏；
2. `status` / `tasks` 事件体必须与同名 REST 接口**同口径**——两边各写一份迟早漂移，
   表现为「手动刷新的」和「自动推送的」不一致；
3. 连接必须能立刻关闭，否则前端每开一个标签页就永久占住一个服务端线程。
"""

from __future__ import annotations

import json
from itertools import islice

import server

# 首屏帧数上限（retry / status / tasks / ready 各占若干 chunk），
# 只是防御性上限，正常会在 ready 处提前停止
_MAX_CHUNKS = 12


def _read_first_batch(client) -> list[tuple[str, dict]]:
    """读首屏批次：一直读到 ready 帧为止，绝不越过它继续 next()。"""
    resp = client.get("/api/events", buffered=False)
    assert resp.status_code == 200, f"SSE 未建立：HTTP {resp.status_code}"
    assert "text/event-stream" in resp.headers["Content-Type"]

    events: list[tuple[str, dict]] = []
    current: str | None = None
    try:
        for raw in islice(resp.response, _MAX_CHUNKS):
            # 一次 yield 就是一整帧（"event: x\ndata: {...}\n\n"），
            # 必须按行拆开——把整块当一行会把 event 和 data 粘成一个字符串
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            for line in text.splitlines():
                if line.startswith("event: "):
                    current = line[7:].strip()
                elif line.startswith("data: ") and current:
                    events.append((current, json.loads(line[6:])))
                    current = None
            if events and events[-1][0] == "ready":
                break
    finally:
        resp.close()
    return events


def _single(client, name: str) -> dict | None:
    for n, payload in _read_first_batch(client):
        if n == name:
            return payload
    return None


def test_first_frame_is_status():
    """首帧必须是 status，且含前端首屏需要的字段。"""
    events = _read_first_batch(server.app.test_client())
    assert events, "SSE 没有发出任何事件"
    name, payload = events[0]
    assert name == "status", f"首帧应为 status，实际是 {name}"
    for key in ("recruit_count", "preach_count", "analyzed_count", "has_api_key", "outputs"):
        assert key in payload, f"status 事件缺少字段 {key}"


def test_ready_frame_terminates_first_batch():
    """ready 帧必须存在——它是首屏批次的边界，测试与前端都依赖它。"""
    names = [n for n, _ in _read_first_batch(server.app.test_client())]
    assert names[-1] == "ready", f"首屏批次应以 ready 结尾，实际 {names}"


def test_status_event_matches_rest_payload():
    """推送的 status 与 /api/status 必须同口径（同源构造，不可各写一份）。"""
    client = server.app.test_client()
    rest = client.get("/api/status").get_json()
    streamed = _single(client, "status")
    assert streamed is not None, "SSE 未发出 status 事件"
    # 任务日志可能在这两次调用之间增长，只比对结构性字段
    for key in ("recruit_count", "preach_count", "fair_count", "analyzed_count",
                "has_api_key", "provider", "llm_model", "master_files",
                "coverage_start", "coverage_end", "data_updated"):
        assert streamed.get(key) == rest.get(key), f"{key} 口径不一致：{streamed.get(key)} vs {rest.get(key)}"


def test_tasks_event_matches_rest_payload():
    client = server.app.test_client()
    rest = client.get("/api/tasks").get_json()
    streamed = _single(client, "tasks")
    assert streamed is not None, "SSE 未发出 tasks 事件"
    assert streamed["count"] == rest["count"]
    assert [t["id"] for t in streamed["tasks"]] == [t["id"] for t in rest["tasks"]]


def test_stream_closes_promptly():
    """连接必须能被立刻关闭——否则每个标签页都永久占一个服务端线程。"""
    client = server.app.test_client()
    resp = client.get("/api/events", buffered=False)
    for _ in islice(resp.response, 1):
        break
    resp.close()
    assert True


def test_revision_is_stable_when_nothing_changes():
    """没变化时指纹必须稳定——否则服务端会每秒重发全量 payload，比轮询还糟。"""
    from services import status as st

    assert st.status_revision() == st.status_revision()


def test_revision_changes_when_task_appears():
    """任务状态变化必须被指纹捕获，否则前端收不到任务进度。"""
    from extensions import tasks
    from services import status as st

    before = st.status_revision()
    with tasks.lock:
        tasks.tasks["_probe"] = {"id": "_probe", "kind": "测试", "status": "running",
                                 "running": True, "lines": ["第一行"],
                                 "progress": {"done": 1, "total": 2}, "started_at": 0}
    try:
        after = st.status_revision()
        assert after != before, "新增任务后指纹没变，SSE 不会推送"
        # 日志增长也要能反映出来（任务进度靠这个推）
        with tasks.lock:
            tasks.tasks["_probe"]["lines"].append("第二行")
        assert st.status_revision() != after, "任务日志增长后指纹没变"
    finally:
        with tasks.lock:
            tasks.tasks.pop("_probe", None)
    assert st.status_revision() == before, "清理探针后指纹应回到原值"
