"""Server-Sent Events：把前端的轮询换成服务端推送。

原先 `ui.html` 有 4 组长驻定时器（任务 2s、流动分析 1.5s、状态 15s、任务中心 5s），
即使什么都没发生也在反复打 HTTP。SSE 之后前端只维持一条连接，
服务端仅在**状态真的变了**时才推送。

为什么是 SSE 而不是 WebSocket：这里只有「服务端 → 前端」的单向推送需求，
SSE 走普通 HTTP、能自动重连（前端 `EventSource` 内建），实现成本低一个量级。

心跳（`: ping`）不可省：中间层（代理 / 浏览器）会掐掉长时间静默的连接。
"""

from __future__ import annotations

import json
import time

from flask import Blueprint, Response, stream_with_context
from services.status import build_status_payload, build_tasks_payload, status_revision

bp = Blueprint("events", __name__)

# 服务端检测间隔：只在进程内比对指纹（stat + 内存），不发 HTTP、不序列化 payload
TICK_SECONDS = 1.0
# 心跳间隔：短于常见代理的 30s 静默超时
HEARTBEAT_SECONDS = 20.0


def _sse(event: str, data: dict) -> str:
    """拼一帧 SSE。data 用紧凑分隔，避免 payload 里的换行破坏帧结构。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _stream():
    """事件流生成器：指纹变了才发，没变就发心跳。"""
    last_rev = None
    last_beat = time.monotonic()
    # 建议重连间隔：服务端重启 / 网络闪断后前端按此退避
    yield "retry: 3000\n\n"
    first = True
    while True:
        try:
            rev = status_revision()
            if rev != last_rev:
                last_rev = rev
                yield _sse("status", build_status_payload())
                yield _sse("tasks", build_tasks_payload())
                if first:
                    # 首屏批次结束的边界帧。
                    # 前端据此把「连接中…」换成实际状态；测试据此停止读取——
                    # 否则会一直阻塞在这一轮末尾的 sleep 上等下一帧。
                    first = False
                    yield _sse("ready", {"ok": True})
        except GeneratorExit:
            raise
        except Exception:  # noqa: BLE001  单帧构造失败不能让整条流断掉
            yield _sse("stream_error", {"message": "状态构造失败，将在下一轮重试"})

        now = time.monotonic()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            yield ": ping\n\n"   # 注释帧：仅用于保活，前端不会收到 event
        time.sleep(TICK_SECONDS)


@bp.route("/api/events")
def api_events():
    """SSE 事件流：status / tasks 两类事件 + 心跳。"""
    return Response(
        stream_with_context(_stream()),
        mimetype="text/event-stream",
        headers={
            # 关掉中间层缓冲，否则事件会被攒着一起发
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
