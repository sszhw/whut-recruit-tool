# WHUT 校招信息采集与分析工具

面向**武汉理工大学校园招聘**（宣讲会 / 双选会 / 招聘信息）的采集、分析与 Web 界面一体化工具。
数据源为武汉理工大学就业信息网（`scc.whut.edu.cn`，基于 `mobile.php` JSON 接口）。

## 功能一览

- **抓取**：按日期范围抓取招聘信息、宣讲会、双选会（后台任务 + 实时日志，按 ID 增量去重）。
- **更新招聘信息（增量）**：一键抓取学校网站**最新**招聘公告与双选会，与本地按 ID 比对后**只把新增记录合并进主库**（不新建单日快照文件，页面/分析/推荐的数据口径不会被打散），与每日 09:00 计划任务同一套逻辑。
- **企业分析**：调用 LLM（可切换提供商）判定企业性质（央企/国企/民企…）+ 工作地点，输出 CSV + 报告。
- **工作地流动分析**：对宣讲会企业推断工作地，生成 `宣讲会_工作地流动.csv` + 报告；方法可选
  `offline`（本地关键词 + 总部映射，免费）或 `ai`（走配置的 LLM）。
- **宣讲会浏览**：搜索 / 筛选（工作地、场馆、线下线上、日期）；支持**一键收藏全部 / 一键取消收藏全部**（按当前筛选范围），并可**导出收藏为 Excel** 或 **iCalendar (.ics)**（日历 App 可直接导入/订阅）。
- **投递推荐**：上传简历（图片/扫描件走视觉 OCR）→ AI 生成本次宣讲会的岗位推荐。
- **统计总览**：数量看板 + 类型 / 地点分布图 + CSV/报告导出。
- **今日行动中心**：首页直接回答「今天新增了哪些招聘 / 三天内有哪些宣讲会 / 值得立刻看的场次和待办」，
  指标卡可点击，跳转即自动套用筛选条件。
- **数据健康**：总览页展示主库记录数、覆盖日期、最近抓取时间、详情缺失、未分析企业、工作地未确定数量。
- **统一任务中心**：顶栏抽屉集中展示运行中/历史任务（进度条、成功失败状态、耗时、错误摘要、日志、停止），
  任务状态与日志落盘，服务重启后仍可查看。
- **统一数据主库口径**：页面列表、企业分析、投递推荐共用同一份「合并去重后的主数据」，不再各读各的文件
  （见下方「架构：统一数据口径」）。
- **主题切换**：顶栏一键切换「浅色 · SaaS Dashboard」/「深色 · Material Dark」。

## 目录结构

```
whut-recruit-tool/
├── README.md                       # 本说明
├── requirements.txt                # Python 依赖
├── .gitignore                      # 排除运行数据 / 配置 / 产物
├── config.example.json             # 配置模板（复制为 config.json 并填入 Key）
├── 启动服务.bat                    # 入口启动脚本（双击即可；自动检查 8765 端口，已在运行则提示，不重复启动）
├── data/                           # 【运行数据】抓取/分析产物、收藏、缓存、日志（.gitignore 排除）
├── app/                            # 【源码 + 前端】
│   ├── server.py                   # Flask Web 服务（http://127.0.0.1:8765）
│   ├── ui.html                     # 前端页面
│   ├── repository.py               # 统一数据访问层：跨原始文件合并 + ID 去重 + 缓存 + 聚合统计
│   ├── crawler.py                  # 采集（招聘 / 宣讲会 / 双选会）
│   ├── analyze.py                  # 企业性质 + 工作地点 AI 分析
│   ├── analyze_preach.py           # 宣讲会工作地流动分析
│   ├── resume.py                   # 简历解析 + 投递推荐
│   ├── check_update.py             # 增量更新统一入口（招聘+双选会 / 宣讲会，--kind 区分）
│   └── run_daily.py                # 每日更新调度
├── scripts/                        # 【可移植批处理】
│   ├── start_server.bat            # 启动 Web 界面
│   └── check_daily.bat             # 每日增量更新（可挂计划任务）
├── docs/                           # 【文档】
│   ├── 数据格式.md                 # 数据文件与字段说明
│   └── 优化实施记录.md             # 对照《架构与网页功能优化建议》的实施情况与后续计划
├── tests/                          # 【测试】pytest：合并去重 / 宣讲筛选 / 推荐过滤 / ICS / 报告解析 / 任务管理
├── pyproject.toml                  # pytest / ruff 配置
└── samples/                        # 【样例数据】可放置脱敏后的示例 JSON（自行准备）
```

## 安装与启动

```bash
# 1. 安装依赖（Python 3.9+）
pip install -r requirements.txt

# 2. 配置 LLM（可选，离线分析可跳过）
cp config.example.json config.json   # Windows: copy config.example.json config.json
#   填入 provider / *_api_key / model

# 3. 启动
python app/server.py --port 8765
#   或双击根目录「启动服务.bat」（自动检查 8765 端口，已在运行会提示，不会重复启动）
#   或 scripts/start_server.bat（基础版，直接运行）
```

打开浏览器访问 http://127.0.0.1:8765

> 「启动服务.bat」**不会自动打开浏览器**，启动后请手动访问上方的地址。

## LLM 提供商配置

在「设置」页选择提供商并填写对应 Key / 模型，保存后写入 `config.json`。

| provider | 接口 Base URL | 默认模型 | 用途 |
|----------|--------------|----------|------|
| `siliconflow` | https://api.siliconflow.cn/v1 | Qwen/Qwen2.5-72B-Instruct | 企业分析 / 流动分析(AI) / 投递推荐 |
| `deepseek` | https://api.deepseek.com | deepseek-chat | 同上（OpenAI 兼容） |

> AI 调用失败（余额不足 / 网络错误等）会自动降级为本地离线匹配。

## 数据命名规范

| 类型 | 命名 |
|------|------|
| 宣讲会原始数据 | `宣讲会_<年>_原始数据.json` |
| 招聘原始数据 | `武汉理工大学招聘信息_<起>_至_<止>_原始数据.json` |
| 企业分析 | `企业分析_<主题>.csv` / `企业分析报告.md` |
| 工作地流动 | `宣讲会_工作地流动.csv` / `.md` |
| 分析缓存 | `<类型>_缓存.json` |

数据均在 **`data/` 子目录**读写（各脚本 `DATA = ROOT / "data"`）。见 `docs/数据格式.md`。

## 架构：统一数据口径

原始数据散落在多个 `*_原始数据.json`（按抓取日期命名）中。历史上页面展示会跨文件合并去重，
而企业分析 / 投递推荐只读「最新修改的那个文件」——一旦最新文件只是一次当日小快照，
分析样本就会远小于页面展示范围。

现在统一由 `app/repository.py` 提供唯一入口：

| 能力 | 说明 |
|------|------|
| `raw_items(kind)` | 跨**全部**原始文件合并、按学校网站 ID 去重（新文件优先），`kind ∈ recruit / fair / preach` |
| 缓存 | 按「文件列表 + mtime + 大小」签名缓存，数据变化自动失效；避免每次请求重复解析几十 MB JSON |
| `master_summary()` | 招聘 / 双选会 / 宣讲会的记录数、覆盖日期、最近更新时间、详情缺失 |
| `companies()` | 企业清单（企业分析与投递推荐共用），含公告数与最长正文 |

接入点：`/api/status`、`/api/health`、`/api/actions`、招聘 / 双选会 / 宣讲会列表、`analyze.py --merge`、
`resume.build_companies()`。**新增读取数据的代码请一律走 repository，不要再自己 glob「最新文件」。**

## 后台任务与日志

- 任务状态机：`queued → running → succeeded / failed / cancelled`，含进度（解析子进程 `[3/120]` 输出）、
  成功/失败摘要、开始结束时间与耗时。
- 落盘位置：`data/任务历史.json`（历史记录）+ `data/任务日志/<task_id>.log`（每个任务独立日志，
  服务重启后仍可通过「任务中心 → 查看日志」回看）。
- 任务 ID 为 ASCII（`crawl_…` / `analyze_…` / `flow_…` / `preach-check_…`），同类型任务互斥，
  可在任务中心或原页面「停止」。
- 相关接口：`GET /api/tasks`、`GET /api/task/<id>/log`、`POST /api/task/stop`、
  `GET /api/health`（数据健康）、`GET /api/actions`（今日行动中心）。

## 测试

```bash
pip install pytest            # 或 pip install -e ".[dev]"（含 ruff）
python -m pytest              # 47 项：合并去重 / 缓存 / 宣讲筛选 / 推荐过滤 / ICS / 报告解析 / 任务管理
python -m ruff check app tests
```

测试使用临时目录（`tests/conftest.py` 的 `data_dir` 夹具会把 repository 指向 `tmp_path`），
**不会读写项目真实数据**；任务管理器用例会启动真实子进程并断言日志/进度/持久化。

## 技术栈

- 后端：Python + Flask（`server.py`），后台任务以子进程 + 日志轮询实现
- 前端：原生 HTML/CSS/JS（`ui.html`），多主题
- LLM：OpenAI 兼容接口（硅基流动 / DeepSeek），结构化 JSON 输出 + 失败降级
- 导出：CSV（UTF-8 BOM）与 Excel（openpyxl）
- 采集：`requests` + `mobile.php` JSON 接口，按 `id` 增量去重

---
仅供学习与本地使用。抓取数据请遵守目标网站的使用条款。API Key 请勿提交到仓库。
