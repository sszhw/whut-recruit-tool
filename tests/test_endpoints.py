"""HTTP 路由级冒烟测试（Flask test_client）：不启动子进程、不写数据。

覆盖点：
- 路由表里该有的端点都在、「一键抓取今日」下线后不能再被访问到（404 → 统一 JSON 错误）；
- 「更新招聘信息（增量）」端点确实以 `check_update.py --kind recruit` 起后台任务，kind＝招聘更新；
- `/api/status` 暴露 `update_task`，供前端把更新任务渲染到抓取页日志区。
"""

from __future__ import annotations

import server

EXPECTED_ROUTES = [
    "/api/status", "/api/health", "/api/actions", "/api/tasks",
    "/api/recruitments", "/api/preachs", "/api/preach/favs", "/api/preachs/filters",
    "/api/fairs", "/api/companies", "/api/companies/filters", "/api/stats",
    "/api/llm/catalog", "/api/resume/companies", "/api/flow",
    "/api/crawl", "/api/crawl/dates", "/api/recruit/update", "/api/preach/check", "/api/analyze",
]


def test_route_table():
    rules = {r.rule for r in server.app.url_map.iter_rules()}
    missing = [r for r in EXPECTED_ROUTES if r not in rules]
    assert not missing, f"缺少路由：{missing}"
    assert "/api/crawl/today" not in rules, "旧「抓取今日」端点应已下线"


def test_removed_today_endpoint_is_json_404():
    resp = server.app.test_client().post("/api/crawl/today")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["ok"] is False and body["error"], body


def test_recruit_update_starts_incremental_update_script(monkeypatch):
    captured: dict = {}

    def fake_start(kind, cmd, env, title=""):
        captured.update(kind=kind, cmd=list(cmd), title=title)
        return {"ok": True, "task": {"id": "recruit-update_test", "kind": kind, "title": title}}

    monkeypatch.setattr(server.tasks, "start", fake_start)
    resp = server.app.test_client().post("/api/recruit/update")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert captured["kind"] == "招聘更新"
    assert captured["cmd"][-2:] == ["--kind", "recruit"]
    assert captured["cmd"][-3].endswith("check_update.py")
    assert "更新招聘信息" in captured["title"]


def test_recruit_update_conflict_returns_409(monkeypatch):
    monkeypatch.setattr(server.tasks, "start",
                        lambda *a, **k: {"ok": False, "error": "已有招聘更新任务在运行"})
    resp = server.app.test_client().post("/api/recruit/update")
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False and "在运行" in body["error"]


def test_status_exposes_update_task():
    resp = server.app.test_client().get("/api/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "update_task" in payload
    assert "recruit_count" in payload


def test_task_id_slugs_are_ascii_registered():
    """新增任务类型必须登记 ASCII slug，避免中文进入 URL / 文件名 / HTML id。"""
    assert server.KIND_SLUGS.get("招聘更新") == "recruit-update"
    assert all(v.isascii() for v in server.KIND_SLUGS.values())
