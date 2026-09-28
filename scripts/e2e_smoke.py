"""端到端冒烟：用真实 config.json 的 Key 跑一遍主要链路（网页功能 + 真实 LLM 调用）。

目的：证明「配好 Key 之后工具真的能用」，而不只是单测绿。

用法（项目根目录，配好 config.json 里的 API Key 之后）：

    python scripts/e2e_smoke.py

会真实调用大模型（分析 2 家企业 + 一次简历推荐），会读写 data/ 下的缓存与收藏。
收藏与看板条目用完即还原，但**求职偏好会被写成「武汉 / 黑名单中公教育」**——
这是为了让第 10 组能验证偏好真的生效；跑完想复原，去页面「求职偏好」里清空即可。

为什么必须有它：分层之后每层单测都把自己那一层的入参打桩了，
「接口接线」类 bug（例如 `--source` 默认值不在 repository.KINDS 里）
没有任何一层单测能发现——它只在 web 路径的参数组合下暴露。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from server import create_app  # noqa: E402

app = create_app()
c = app.test_client()
FAILS = []


def show(title, obj, limit=300):
    s = json.dumps(obj, ensure_ascii=False)
    print(f"  {title}: {s[:limit]}{' …' if len(s) > limit else ''}")


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  → " + str(extra)))
    if not cond:
        FAILS.append(name)


def get(path, **kw):
    r = c.get(path, **kw)
    try:
        return r.status_code, r.get_json()
    except Exception:
        return r.status_code, r.data[:200]


print("=" * 70)
print("1) 状态与数据规模")
code, st = get("/api/status")
check("/api/status 200", code == 200, code)
check("已识别到 API Key", st.get("has_api_key") is True, st.get("has_api_key"))
show("主库", {k: st.get(k) for k in ("recruit_count", "preach_count", "fair_count",
                                     "analyzed_count", "unanalyzed_count")})

print("=" * 70)
print("2) LLM 连通性（真实调用硅基流动）")
r = c.post("/api/llm/test", json={})
res = r.get_json()
check("/api/llm/test 连通", bool(res.get("ok")), res)
show("ping", {k: res.get(k) for k in ("ok", "status", "error")})

print("=" * 70)
print("3) 企业列表 / 企业详情页")
code, comp = get("/api/companies?size=3")
check("/api/companies 200 且有数据", code == 200 and comp.get("total", 0) > 0, comp)
first = (comp.get("rows") or [{}])[0]
cname = first.get("name") or first.get("单位名称") or ""
show("候选企业数", comp.get("total"))
if cname:
    code, det = get(f"/api/company/{cname}")
    check(f"企业详情页 /api/company/{cname[:12]}…", code == 200 and det.get("ok") is not False, code)
    show("详情字段", list(det.keys())[:10])

print("=" * 70)
print("4) 宣讲会（周日历视图依赖这个接口）")
code, pr = get("/api/preachs?size=5")
check("/api/preachs 200 且有数据", code == 200 and pr.get("total", 0) > 0, pr)
show("宣讲会总数", pr.get("total"))
row0 = (pr.get("rows") or [{}])[0]
show("单条字段", list(row0.keys())[:14])

print("=" * 70)
print("5) 求职偏好（本轮新增）")
code, pf = get("/api/prefs")
check("GET /api/prefs", code == 200 and pf.get("ok") is True, pf)
r = c.post("/api/prefs", json={"target_cities": ["武汉"], "blacklist": ["中公教育"]})
saved = r.get_json()
check("POST /api/prefs 保存", r.status_code == 200 and saved["prefs"]["target_cities"] == ["武汉"], saved)
show("保存结果", saved["prefs"])

print("=" * 70)
print("6) 重分析：先看规模")
code, stale = get("/api/analyze/stale")
check("GET /api/analyze/stale", code == 200 and stale.get("ok") is True, stale)
show("过期统计", {k: stale.get(k) for k in ("stale", "total", "prompt_version")})

print("=" * 70)
print("7) 真实 AI 分析（limit=2，会真的调 LLM）")
r = c.post("/api/analyze", json={"limit": 2})
start = r.get_json()
check("POST /api/analyze 起任务", bool(start.get("ok")), start)
task_id = (start.get("task") or {}).get("id")
print(f"  task_id = {task_id}")
final = {}
if task_id:
    for _ in range(60):
        time.sleep(1)
        code, lg = get(f"/api/task/{task_id}/log?tail=200")
        final = (lg or {}).get("task") or {}
        if not final.get("running"):
            break
    show("任务终态", {k: final.get(k) for k in ("status", "exit_code", "progress", "error")})
    for line in (final.get("lines") or [])[-12:]:
        print("   |", line)
    # 任务中心的终态名是 succeeded / failed（不是 done）
    check("分析任务成功结束", final.get("status") == "succeeded" and final.get("exit_code") == 0,
          (final.get("status"), final.get("error")))

print("=" * 70)
print("8) 再看一次过期规模（分析过之后应该变化）")
code, stale2 = get("/api/analyze/stale")
show("过期统计", {k: stale2.get(k) for k in ("stale", "total")})
check("分析后过期条目减少或未增加", stale2.get("stale", 10**9) <= stale.get("stale", 0),
      (stale.get("stale"), stale2.get("stale")))

print("=" * 70)
print("9) 投递看板")
code, bi = get("/api/board/items")
check("GET /api/board/items", code == 200, code)
# 真实载荷形状跟 ui.html.createBoardItem() 一致：source_type + source_id + snapshot
pid = str(row0.get("ID") or row0.get("id") or "")
r = c.post("/api/board/items", json={
    "source_type": "宣讲会", "source_id": pid,
    "snapshot": {"unit": row0.get("单位名称") or "测试企业",
                 "title": row0.get("标题") or "宣讲会",
                 "link": row0.get("原网页") or ""},
    "note": "端到端冒烟"})
created = r.get_json()
check("POST /api/board/items 新增", r.status_code in (200, 201) and bool(created.get("ok")), created)
item_id = (created.get("item") or {}).get("id")
if item_id:
    # 阶段流转有约束：关注只能 → 投递/放弃，不能跳到面试
    r = c.post(f"/api/board/items/{item_id}/status", json={"stage": "面试"})
    check("禁止跳阶段 关注→面试", r.status_code == 400, r.get_json())
    r = c.post(f"/api/board/items/{item_id}/status", json={"stage": "投递"})
    ok1 = r.status_code == 200 and bool(r.get_json().get("ok"))
    r = c.post(f"/api/board/items/{item_id}/status", json={"stage": "面试"})
    ok2 = r.status_code == 200 and bool(r.get_json().get("ok"))
    check("合法流转 关注→投递→面试", ok1 and ok2, (ok1, ok2))
    c.post(f"/api/board/items/{item_id}/delete", json={})
code, bs = get("/api/board/stats")
show("看板统计", bs.get("counts") if isinstance(bs.get("counts"), dict) else bs)

print("=" * 70)
print("10) 投递推荐（真实 LLM + 偏好黑名单生效）")
resume_text = ("张三，武汉理工大学 计算机科学与技术 本科。熟悉 Python、Java，"
               "做过后端开发与数据分析项目。期望工作地：武汉。期望企业性质：国企央企。")
r = c.post("/api/resume/recommend", data={"resume_text": resume_text,
                                          "work_place": "武汉", "company_type": "国企央企"})
rec = r.get_json()
check("POST /api/resume/recommend 200", r.status_code == 200, r.status_code)
if r.status_code == 200:
    # 推荐清单在 result 里（result.recommendations），顶层是元信息
    recs = (rec.get("result") or {}).get("recommendations") or []
    show("推荐条数", len(recs))
    show("来源", rec.get("source"))
    show("候选总量", (rec.get("companies_total"), rec.get("companies_count")))
    show("使用了偏好", {k: v for k, v in (rec.get("prefs_applied") or {}).items()
                    if k in ("work_place_from_prefs", "company_type_from_prefs",
                             "blocked_companies", "blocked_preachs")})
    show("宣讲会推荐", len(rec.get("recommended_preachs") or []))
    for t in recs[:5]:
        print("   -", t.get("company"), "| score", t.get("score"),
              "| breakdown", t.get("breakdown"), "| match", t.get("match"))
    check("推荐结果非空", len(recs) > 0, rec.get("error"))
    check("黑名单里的企业未出现在结果",
          all("中公教育" not in (t.get("company") or "") for t in recs),
          [t.get("company") for t in recs])

print("=" * 70)
print("11) 宣讲会收藏 + 导出（xlsx / ics）")
# 收藏为空时导出接口按设计返回 404「尚无收藏的宣讲会」，所以先收藏一场
r = c.post("/api/preach/fav", json={"id": pid})
check("收藏一场宣讲会", r.status_code == 200 and bool(r.get_json().get("ok")), r.get_json())
code, favlist = get("/api/preach/favs")
show("收藏数", favlist.get("count"))
r = c.get("/api/preach/favs/export")
check("收藏导出 xlsx", r.status_code == 200 and len(r.data) > 0, r.status_code)
show("xlsx", (r.headers.get("Content-Type"), len(r.data)))
r = c.get("/api/preach/favs/export-ics")
check("收藏导出 ics", r.status_code == 200 and b"BEGIN:VCALENDAR" in r.data, r.status_code)
show("ics", (r.headers.get("Content-Type"), len(r.data)))
c.post("/api/preach/unfav", json={"id": pid})     # 还原现场

print("=" * 70)
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败: {FAILS}")
sys.exit(1 if FAILS else 0)
