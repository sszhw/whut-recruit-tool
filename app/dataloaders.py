"""页面数据的读取与派生：招聘 / 双选会 / 宣讲会列表、收藏、分析缓存。

从原 `server.py` 抽出的第二层。这里全是「把原始数据整理成前端要的行」的逻辑，
不涉及 HTTP 语义，因此蓝图可以直接调用，也可以单独测试。

所有 RU 都走 `repository` 的统一口径（跨全部原始文件合并 + 按 ID 去重），
并且派生结果由 `repository.cached_derived` 按「文件签名 + 日期」缓存——
列表页、筛选器、导出、推荐、行动中心共用同一份缓存，避免重复清洗上千条正文。
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import analyze_preach
import crawler
import repository as repo
from settings import CACHE_PATH, DATA, FAV_PATH, WORK_FLOW_CSV
from utils import io as io_utils

# 《宣讲会_工作地流动.csv》的解析结果缓存：(签名, 单位名 → 工作地城市列表)
_work_map_cache: tuple[str, dict[str, list[str]]] | None = None


def _work_flow_signature() -> str:
    """《宣讲会_工作地流动.csv》的签名（mtime+size），用于缓存失效判断。"""
    try:
        st = WORK_FLOW_CSV.stat()
    except OSError:
        return "-"
    return f"{st.st_mtime_ns}:{st.st_size}"


def _company_work_map() -> dict[str, list[str]]:
    """单位名称 → 工作地城市列表。

    优先读取本项目已生成的《宣讲会_工作地流动.csv》（数百家单位均已映射）。
    文件缺失时返回空 dict，由调用方回退到 `analyze_preach.infer_work_cities` 逐条推断。

    结果按 CSV 签名缓存：数据健康 / 宣讲会列表 / 筛选器等多个接口都会读它，
    避免每次请求重复解析同一份 CSV。
    """
    global _work_map_cache
    sig = _work_flow_signature()
    if _work_map_cache and _work_map_cache[0] == sig:
        return _work_map_cache[1]
    mapping: dict[str, list[str]] = {}
    if WORK_FLOW_CSV.exists():
        try:
            with WORK_FLOW_CSV.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    name = (row.get("单位名称") or "").strip()
                    if not name:
                        continue
                    cities = [c.strip() for c in (row.get("工作地城市") or "").split("、") if c.strip()]
                    mapping[name] = cities
        except (OSError, csv.Error):
            mapping = {}
    _work_map_cache = (sig, mapping)
    return mapping


def _build_recruit_rows(today_str: str) -> list[dict]:
    rows = []
    for item in repo.raw_items("recruit"):
        add_date = crawler.time_text(item.get("addtime"), with_time=False)
        rows.append({
            "发布日期": crawler.time_text(item.get("addtime")),
            "发布日期日": add_date,
            "今日更新": bool(add_date) and add_date == today_str,
            "标题": item.get("title", ""),
            "单位": item.get("com_id_name", ""),
            "原网页": item.get("httpurl") or f"https://scc.whut.edu.cn/#/recruitmentInformation/notice?type=enrollment&id={item.get('id','')}",  # noqa: E501
            "ID": item.get("id", ""),
            "正文": crawler.plain_text(item.get("remarks") or item.get("content") or "")[:600],
        })
    rows.sort(key=lambda r: r["发布日期"], reverse=True)
    return rows


def load_recruitments() -> list[dict]:
    """招聘信息列表（统一口径：跨全部原始文件合并、按 ID 去重）。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    return repo.cached_derived("recruit_rows", "recruit",
                               lambda: _build_recruit_rows(today_str), extra=today_str)


def load_fairs() -> list[dict]:
    """双选会列表（统一口径：合并去重）。"""
    def build() -> list[dict]:
        rows = []
        for f in repo.raw_items("fair"):
            rows.append({
                "标题": f.get("title", ""),
                "地点": f.get("field_id_name", ""),
                "举办时间": f"{crawler.time_text(f.get('start_time'))} 至 {crawler.time_text(f.get('end_time'))}",
                "参会单位数": f.get("verify_count", ""),
                "原网页": f"https://scc.whut.edu.cn/#/doubleElection/{f.get('id','')}",
            })
        return rows
    return repo.cached_derived("fair_rows", "fair", build)


def _build_preach_rows(today_str: str) -> list[dict]:
    """构造全部宣讲会行（不过滤过去的场次）。开销较大，由 load_preachs 走缓存调用。"""
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    soon_end = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    work_map = _company_work_map()
    rows = []
    for item in repo.raw_items("preach"):
        hold_date = item.get("hold_date", "")
        start = item.get("hold_starttime", "")
        end = item.get("hold_endtime", "")
        period = f"{hold_date} {start}~{end}".strip(" ~")
        city = item.get("city_id_name", "") or ""
        venue = (item.get("address", "") or "").strip()
        venue_full = "，".join(part for part in (city, venue) if part)
        name = item.get("com_id_name", "") or ""
        # 工作地城市：优先用工作地流动 CSV 映射，缺失则离线推断
        if name in work_map:
            work_cities = work_map[name]
        else:
            title = item.get("title", "")
            text = "\n".join(str(x) for x in [
                title, item.get("purpose", ""), item.get("address", ""),
                "、".join(str(j.get("city_id_name", "")) for j in (item.get("JobList") or []) if isinstance(j, dict)),
            ] if x)
            work_cities = analyze_preach.infer_work_cities(name, title, text)
        work_cities = [c for c in work_cities if c and c != "未确定"]
        # 提醒标注：今日新出(addtime==今天) / 3天内开始(举办日期在未来3天内)
        # addtime 缺失/非数字时留空，不要回退成 0——那会显示成 1970 年的假日期
        add_date = ""
        raw_add = item.get("addtime")
        if raw_add:
            try:
                add_date = datetime.fromtimestamp(int(raw_add)).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError, OverflowError):
                add_date = ""
        new_today = bool(add_date) and add_date == today_str
        soon3 = bool(hold_date) and today_str <= hold_date <= soon_end
        reminds = []
        if new_today:
            reminds.append("今日新出")
        if soon3:
            reminds.append("3天内开始")
        rows.append({
            "宣讲时间": period,
            "举办日期": hold_date,
            "开始时间": start,
            "结束时间": end,
            "单位名称": name,
            "宣讲会地点": venue_full,
            "城市": city,
            "场馆": venue,
            "线下/线上": "线下" if crawler._int_or(item.get("air_type"), 0) == 0 else "线上",
            "标题": item.get("title", ""),
            "公司地点": "、".join(work_cities) if work_cities else "未确定",
            "work_cities": work_cities,
            "原网页": item.get("httpurl") or f"https://scc.whut.edu.cn/#/preachMeeting/{item.get('id','')}/1",
            "ID": item.get("id", ""),
            "正文": crawler.plain_text(item.get("remarks") or item.get("purpose") or "")[:600],
            "今日新出": new_today,
            "3天内开始": soon3,
            "提醒": reminds,
        })
    return rows


def load_preachs(past: bool = False) -> list[dict]:
    """宣讲会列表：来自 repository（跨全部宣讲会文件合并、按 ID 去重）。

    行构建（工作地 CSV 映射 / 离线推断 / 正文清洗）按
    「数据文件签名 + 当天日期 + 工作地流动 CSV 签名」缓存。
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    extra = f"{today_str}|{_work_flow_signature()}"
    all_rows = repo.cached_derived("preach_rows", "preach",
                                   lambda: _build_preach_rows(today_str), extra=extra)
    if past:
        return list(all_rows)
    # 默认只看当天及以后（过去的宣讲会隐藏）
    return [r for r in all_rows if not (r["举办日期"] and r["举办日期"].strip() < today_str)]


def load_preach_favs() -> set[str]:
    """读取收藏的宣讲会 ID 集合。"""
    data = io_utils.load_json_dict(FAV_PATH)
    return set(data.get("ids") or [])


def save_preach_favs(ids: set[str]) -> None:
    """持久化收藏的宣讲会 ID（原子写，防止并发请求写坏收藏文件）。"""
    io_utils.write_json_atomic(FAV_PATH, {"ids": sorted(ids)})


def load_cache() -> dict:
    """读企业分析缓存；缺失或损坏返回 {}。"""
    return io_utils.load_json_dict(CACHE_PATH)


def work_undetermined_count() -> int:
    """工作地未确定的宣讲会企业数（工作地流动 CSV 映射缺失的企业）。"""
    work_map = _company_work_map()
    names = {str(item.get("com_id_name") or "").strip() for item in repo.raw_items("preach")}
    names.discard("")
    return sum(1 for name in names if not work_map.get(name))


def source_files_dir() -> Path:
    """数据目录（供导出/下载等需要拼路径的场景使用）。"""
    return DATA
