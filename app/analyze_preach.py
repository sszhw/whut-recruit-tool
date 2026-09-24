#!/usr/bin/env python3
"""宣讲会企业「工作地流动」分析。

输入：宣讲会原始数据（默认取 宣讲会_*_原始数据.json，或 --input 指定）
输出（写到项目根目录）：
    宣讲会_工作地流动.csv     逐企业：单位名称 → 工作地城市
    宣讲会_工作地流动报告.md   工作地流向分布 + 明细

两种分析方式（--method）：
    offline（默认，无需 API）：依据企业名称中的城市/省市与企业总部映射推断工作地。
    ai（需要硅基流动余额，--ai）：复用 analyze.py 的 AI 接口。

用法：
    python analyze_preach.py                    # 全部离线
    python analyze_preach.py --ai               # 走 AI（余额充足时）
    python analyze_preach.py --input 其他.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ 城市库

# 常见城市名（用于从企业名称里提取工作地）。按"是否更常见"排列，匹配左起最长。
CITY_TOKENS = [
    "北京", "上海", "深圳", "广州", "武汉", "成都", "重庆", "天津", "南京", "杭州", "西安", "苏州",
    "郑州", "长沙", "青岛", "大连", "厦门", "宁波", "无锡", "合肥", "福州", "济南", "佛山", "东莞",
    "珠海", "惠州", "中山", "江门", "南昌", "昆明", "贵阳", "南宁", "哈尔滨", "长春", "沈阳", "石家庄",
    "太原", "呼和浩特", "兰州", "西宁", "银川", "乌鲁木齐", "拉萨", "海口", "三亚", "襄阳", "十堰",
    "宜昌", "荆州", "黄石", "咸宁", "随州", "孝感", "黄冈", "鄂州", "荆门", "南阳", "洛阳", "常州",
    "嘉兴", "南通", "扬州", "绍兴", "台州", "温州", "金华", "芜湖", "马鞍山", "泉州", "漳州",
    "烟台", "潍坊", "威海", "保定", "唐山", "秦皇岛", "柳州", "绵阳", "德阳", "宜宾", "泸州", "岳阳",
    "常德", "株洲", "湘潭", "汕头", "湛江", "潮州", "揭阳", "梅州", "茂名", "包头", "鄂尔多斯", "大同",
    "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "大庆", "齐齐哈尔", "佳木斯", "牡丹江", "九江",
    "景德镇", "赣州", "宜春", "上饶", "徐州", "连云港", "盐城", "淮安", "泰州", "宿迁", "廊坊",
]

# 省份/直辖市 → 省会（用于名称只含省份、不含地市的本地单位兜底推断）。
PROVINCE_TO_CITY = {
    "北京": "北京", "上海": "上海", "天津": "天津", "重庆": "重庆",
    "湖北": "武汉", "广东": "广州", "河南": "郑州", "河北": "石家庄", "山东": "济南",
    "江苏": "南京", "浙江": "杭州", "安徽": "合肥", "福建": "福州", "江西": "南昌",
    "湖南": "长沙", "陕西": "西安", "山西": "太原", "四川": "成都", "广西": "南宁",
    "云南": "昆明", "贵州": "贵阳", "甘肃": "兰州", "辽宁": "沈阳", "吉林": "长春",
    "黑龙江": "哈尔滨", "内蒙古": "呼和浩特", "宁夏": "银川", "青海": "西宁",
    "新疆": "乌鲁木齐", "海南": "海口", "西藏": "拉萨",
}

# 企业名称关键词 → 工作地城市（针对名称不含地名但工作地很明确的大企业/集团）。
CORP_HQ: list[tuple[str, list[str]]] = [
    ("东风汽车", ["武汉", "十堰"]), ("猛士", ["武汉", "襄阳"]), ("岚图", ["武汉"]), ("华为", ["深圳"]),
    ("比亚迪", ["深圳"]), ("腾讯", ["深圳"]), ("中兴", ["深圳"]), ("大疆", ["深圳"]), ("迈瑞", ["深圳"]),
    ("海信", ["青岛"]), ("海尔", ["青岛"]), ("京东方", ["北京"]), ("宁德时代", ["宁德"]), ("宁德新能源", ["宁德"]),
    ("宁德润智", ["宁德"]), ("中航锂电", ["常州"]), ("蔚来", ["上海"]), ("理想", ["北京", "常州"]),
    ("小鹏", ["广州"]), ("吉利", ["杭州"]), ("长城汽车", ["保定"]),     ("奇瑞", ["芜湖"]), ("宇通", ["郑州"]),
    ("长安", ["重庆"]), ("上汽", ["上海"]), ("广汽", ["广州"]), ("一汽", ["长春"]), ("中国重汽", ["济南"]),
    ("陕汽", ["西安"]), ("潍柴", ["潍坊"]), ("三一", ["长沙"]), ("徐工", ["徐州"]), ("中联重科", ["长沙"]),
    ("格力", ["珠海"]), ("美的", ["佛山"]), ("TCL", ["惠州"]), ("创维", ["深圳"]), ("小米", ["北京"]),
    ("OPPO", ["东莞"]), ("维沃", ["东莞"]), ("vivo", ["东莞"]), ("荣耀", ["深圳"]),
    ("海康威视", ["杭州"]), ("宇视", ["杭州"]), ("大华", ["杭州"]), ("浙江大华", ["杭州"]),
    ("中建", ["北京"]), ("中交", ["北京"]), ("中铁", ["北京"]), ("中国铁建", ["北京"]), ("中国中铁", ["北京"]),
    ("中冶", ["北京"]), ("中国电建", ["北京"]), ("中国能建", ["北京"]), ("中核", ["北京"]),
    ("中广核", ["深圳"]), ("国家电网", ["北京"]), ("南方电网", ["广州"]), ("中国移动", ["北京"]),
    ("中国联通", ["北京"]), ("中国电信", ["北京"]), ("中国石化", ["北京"]), ("中国石油", ["北京"]),
    ("中海油", ["北京"]), ("华润", ["深圳"]), ("保利", ["广州"]), ("招商局", ["深圳"]),
    ("中国宝武", ["上海"]), ("宝钢", ["上海"]), ("鞍钢", ["鞍山"]), ("首钢", ["北京"]),
    ("中国电科", ["北京"]), ("中电科", ["北京"]), ("中国电子", ["北京"]), ("航天", ["北京"]),
    ("航空工业", ["北京"]), ("中国船舶", ["上海"]), ("中国兵器", ["北京"]),
    ("国家能源", ["北京"]), ("华能", ["北京"]), ("大唐", ["北京"]), ("华电", ["北京"]),
    ("国家电投", ["北京"]), ("三峡", ["武汉"]), ("长航", ["武汉"]), ("湖北能源", ["武汉"]),
    ("长江存储", ["武汉"]), ("武汉新芯", ["武汉"]), ("楚兴", ["武汉"]), ("中元通信", ["武汉"]),
    ("深南电路", ["深圳"]), ("共济", ["深圳"]), ("新凯来", ["深圳"]), ("信锐", ["深圳"]),
    ("基恩士", ["上海"]), ("南瑞继保", ["南京"]), ("国网南瑞", ["南京"]), ("南瑞", ["南京"]),
    ("金山办公", ["珠海"]), ("明源云", ["深圳"]), ("金蝶", ["深圳"]), ("用友", ["北京"]),
    ("顺丰", ["深圳"]), ("菜鸟", ["杭州"]), ("阿里巴巴", ["杭州"]), ("阿里", ["杭州"]),
    ("网易", ["杭州"]), ("字节", ["北京"]), ("拼多多", ["上海"]), ("美团", ["北京"]),
    ("贝壳", ["北京"]), ("京东", ["北京"]), ("百度", ["北京"]),
    ("正邦", ["南昌"]), ("牧原", ["南阳"]), ("温氏", ["云浮"]), ("海大", ["广州"]),
    ("领益", ["东莞"]), ("立讯", ["东莞", "昆山"]), ("歌尔", ["潍坊"]),
    ("长电科技", ["江阴"]), ("通富微电", ["南通"]), ("华天科技", ["天水"]),
    ("中芯", ["上海"]), ("长鑫", ["合肥"]), ("晶盛", ["杭州"]), ("禾迈", ["杭州"]),
    ("芯朋", ["无锡"]), ("芯源微", ["沈阳"]), ("北方华创", ["北京"]), ("中微", ["上海"]),
    ("盛美", ["上海"]), ("拓荆", ["沈阳"]), ("精测", ["武汉"]), ("华工科技", ["武汉"]),
    ("锐科激光", ["武汉"]), ("高德红外", ["武汉"]), ("烽火", ["武汉"]), ("光迅", ["武汉"]),
    ("东风康明斯", ["十堰"]), ("东风汽车集团", ["武汉"]), ("东风商用车", ["十堰"]),
    # 追加：未确定率较高的知名集团/地市企业
    ("中国建筑", ["北京"]), ("中国能源建设", ["北京"]), ("中国广核", ["深圳"]), ("中国广核集团", ["深圳"]),
    ("中国中车", ["北京"]), ("中车", ["北京"]), ("中车资阳", ["资阳"]),
    ("海天塑机", ["宁波"]), ("昌河飞机", ["景德镇"]), ("福建中烟", ["厦门"]), ("中一科技", ["武汉"]),
    ("万向", ["杭州"]), ("安凯汽车", ["合肥"]), ("庆铃", ["重庆"]), ("豪迈", ["潍坊"]),
    ("恒生电子", ["杭州"]), ("全柴动力", ["滁州"]), ("因湃", ["广州"]), ("中国电器科学研究院", ["广州"]),
    ("赛力斯", ["重庆"]), ("常发", ["常州"]), ("兴发化工", ["宜昌"]), ("玉柴", ["玉林"]),
    ("新华光", ["襄阳"]), ("许昌智能", ["许昌"]), ("蓝思", ["长沙"]), ("中国银行软件中心", ["北京"]),
    ("湖北消费金融", ["武汉"]), ("中信重工", ["洛阳"]), ("水电二局", ["广州"]), ("新锦成", ["佛山"]),
    ("扬翔", ["贵港"]), ("江铃", ["南昌"]), ("中国食品", ["北京"]), ("中粮", ["北京"]),
    ("五矿", ["北京"]), ("国机", ["北京"]), ("三峡集团", ["武汉"]), ("湖北能源集团", ["武汉"]),
    ("东方电气", ["成都"]), ("中船", ["上海"]), ("中国船舶集团", ["上海"]), ("航空发动机", ["北京"]),
]


def _first_city(text: str) -> str:
    """在文本中从左找到第一个已知城市名。"""
    for city in CITY_TOKENS:
        if city in text:
            return city
    return ""


def infer_work_cities(name: str, title: str = "", text: str = "") -> list[str]:
    """离线推断工作地：优先企业总部映射，其次从名称/文本提取城市。"""
    sample = name + " " + title
    # 1) 企业总部映射（关键词优先）
    for keyword, cities in CORP_HQ:
        if keyword in name:
            return cities
    # 2) 名称/标题中的城市
    found = []
    for source in (name, title):
        city = _first_city(source)
        if city and city not in found:
            found.append(city)
    if found:
        return found[:3]
    # 3) 招聘文本中提及的城市（岗位 JobList 或备注）
    for source in (text, title):
        for city in CITY_TOKENS:
            if city in source:
                found.append(city)
    # 4) 名称只含省份（如"湖北XX公司"）→ 省会兜底
    if not found:
        for prov, city in PROVINCE_TO_CITY.items():
            if prov in name:
                found.append(city)  # 省份→省会兜底（归并到城市名，便于聚合）
                break
    # 去重
    seen, out = set(), []
    for c in found:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:3]


def load_companies(input_path: Path) -> list[dict]:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    rows = []
    seen = set()
    for item in data.get("宣讲会", []) or data.get("招聘信息", []):
        name = (item.get("com_id_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        text = "\n".join(str(x) for x in [
            item.get("title", ""),
            item.get("purpose", ""),
            item.get("address", ""),
            "、".join(str(j.get("city_id_name", "")) for j in (item.get("JobList") or []) if isinstance(j, dict)),
        ] if x)
        rows.append({"name": name, "title": item.get("title", ""), "text": text})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="", help="宣讲会原始数据 JSON（默认自动找 宣讲会_*_原始数据.json）")
    parser.add_argument("--method", default="offline", choices=["offline", "ai"], help="offline=离线(默认)，ai=硅基流动")
    parser.add_argument("--limit", type=int, default=0, help="只分析前 N 家（0=全部）")
    args = parser.parse_args()

    if args.input:
        input_path = Path(args.input)
    else:
        candidates = sorted(DATA.glob("宣讲会_*_原始数据.json"), key=os.path.getmtime, reverse=True)
        if not candidates:
            print("错误：找不到 宣讲会_*_原始数据.json，请先运行 check_update.py --kind preach 或 crawler.py", file=sys.stderr)
            return 2
        input_path = candidates[0]

    print(f"输入文件：{input_path.name}")
    companies = load_companies(input_path)
    if args.limit:
        companies = companies[: args.limit]
    print(f"待分析企业：{len(companies)} 家（method={'离线' if args.method == 'offline' else 'AI'}）")

    rows = []
    for c in companies:
        if args.method == "ai":
            import analyze
            api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("SILICONFLOW_API_KEY", "")).strip()
            if not api_key:
                print("错误：AI 方式需要环境变量 LLM_API_KEY / SILICONFLOW_API_KEY", file=sys.stderr)
                return 2
            res = analyze.call_api(api_key, analyze.DEFAULT_MODEL, c["name"], c["text"])
            cities = res.get("locations") or []
            evidence = res.get("evidence", "")
        else:
            cities = infer_work_cities(c["name"], c["title"], c["text"])
            evidence = "离线：依据企业名称/总部映射推断"
        rows.append({"单位名称": c["name"], "工作地城市": "、".join(cities) if cities else "未确定",
                     "判断依据": evidence})

    # CSV
    csv_path = DATA / "宣讲会_工作地流动.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # 工作地流向分布
    loc_counts = Counter(loc.strip() for r in rows for loc in r["工作地城市"].split("、") if loc.strip() and loc.strip() != "未确定")
    unknown = sum(1 for r in rows if "未确定" in r["工作地城市"])

    md = ["# 宣讲会企业 · 工作地流动分析报告", "",
          f"- 生成时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
          f"- 数据来源：{input_path.name}（{len(companies)} 家企业，已去重）",
          f"- 分析方法：{'离线（企业名称/总部映射）' if args.method == 'offline' else 'AI（硅基流动）'}",
          f"- 工作地未确定企业：{unknown} 家", "",
          "## 工作地流向分布（前 30）", "", "| 城市 | 企业数 |", "|---|---:|"]
    for loc, cnt in loc_counts.most_common(30):
        md.append(f"| {loc} | {cnt} |")
    md.extend(["", "## 全部企业 · 工作地明细", "", "| 单位名称 | 工作地城市 | 依据 |", "|---|---|---|"])
    for r in rows:
        md.append(f"| {r['单位名称']} | {r['工作地城市'] or '未确定'} | {r['判断依据']} |")
    md_path = DATA / "宣讲会_工作地流动报告.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    print(f"\n=== 工作地流向（前 20 城）===")
    for loc, cnt in loc_counts.most_common(20):
        print(f"  {loc}: {cnt}")
    print(f"未确定: {unknown}")
    print(f"\n输出：{csv_path.name}、{md_path.name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户已中止。", file=sys.stderr)
        raise SystemExit(130)
