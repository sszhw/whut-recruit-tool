# WHUT 校招信息采集与分析工具

面向**武汉理工大学校园招聘**（宣讲会 / 双选会 / 招聘信息）的采集、分析与 Web 界面一体化工具。
数据源为武汉理工大学就业信息网（`scc.whut.edu.cn`，基于 `mobile.php` JSON 接口）。

## 功能一览

- **抓取**：按日期范围抓取招聘信息、宣讲会、双选会（后台任务 + 实时日志，按 ID 增量去重）。
- **企业分析**：调用 LLM（可切换提供商）判定企业性质（央企/国企/民企…）+ 工作地点，输出 CSV + 报告。
- **工作地流动分析**：对宣讲会企业推断工作地，生成 `宣讲会_工作地流动.csv` + 报告；方法可选
  `offline`（本地关键词 + 总部映射，免费）或 `ai`（走配置的 LLM）。
- **宣讲会浏览**：搜索 / 筛选（工作地、场馆、线下线上、日期）；支持**一键收藏全部 / 一键取消收藏全部**（按当前筛选范围），并可**导出收藏为 Excel** 或 **iCalendar (.ics)**（日历 App 可直接导入/订阅）。
- **投递推荐**：上传简历（图片/扫描件走视觉 OCR）→ AI 生成本次宣讲会的岗位推荐。
- **统计总览**：数量看板 + 类型 / 地点分布图 + CSV/报告导出。
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
│   ├── crawler.py                  # 采集（招聘 / 宣讲会 / 双选会）
│   ├── analyze.py                  # 企业性质 + 工作地点 AI 分析
│   ├── analyze_preach.py           # 宣讲会工作地流动分析
│   ├── resume.py                   # 简历解析 + 投递推荐
│   ├── check_recruit_update.py     # 招聘 + 双选会 增量更新
│   ├── check_preach_update.py      # 宣讲会 增量更新
│   ├── run_daily.py                # 每日更新调度
│   └── _run_preach.py              # 宣讲会抓取辅助脚本
├── scripts/                        # 【可移植批处理】
│   ├── start_server.bat            # 启动 Web 界面
│   └── check_daily.bat             # 每日增量更新（可挂计划任务）
├── docs/                           # 【文档】
│   └── 数据格式.md                 # 数据文件与字段说明
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

## 技术栈

- 后端：Python + Flask（`server.py`），后台任务以子进程 + 日志轮询实现
- 前端：原生 HTML/CSS/JS（`ui.html`），多主题
- LLM：OpenAI 兼容接口（硅基流动 / DeepSeek），结构化 JSON 输出 + 失败降级
- 导出：CSV（UTF-8 BOM）与 Excel（openpyxl）
- 采集：`requests` + `mobile.php` JSON 接口，按 `id` 增量去重

---
仅供学习与本地使用。抓取数据请遵守目标网站的使用条款。API Key 请勿提交到仓库。
