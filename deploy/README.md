# 服务器部署说明

> **本仓库是公开仓库，这份文档已脱敏**：服务器 IP / 域名、Basic Auth 用户名与口令、云厂商实例名
> 等真实值一律替换成占位符，**只保留在服务器本机**。照着做的时候把占位符换成你自己的值。

这份文件记录「东西在哪、怎么重启、怎么改」，照着做就行。

## 拓扑

```
浏览器 ──80──▶ 宝塔 nginx（HTTP Basic 认证）──127.0.0.1:8765──▶ Docker 容器 whut-recruit-tool
                                                                      │
                                                        /app/data ──▶ 宿主机 /opt/whut-recruit-tool/data
                                                        /app/config.json ──▶ 宿主机 ./config.json
```

- 容器**不对外**暴露端口，只监听 `127.0.0.1:8765`；公网入口只有 nginx 那一层，先过口令再转发。
- 应用本身没有账号体系，Basic Auth 是唯一的门，别把口令发给别人。

## 连接信息

填之前先在服务器本机确认这些值，**不要把它们写进仓库**（口令一旦进 Git 历史就撤不回来）：

| 项 | 值 |
|---|---|
| 访问地址 | `http://<你的服务器IP或域名>` |
| 用户名 | 自己定（下面的命令里用 `whut` 举例） |
| 口令 | **不写在任何文件里**，只在服务器上用 htpasswd 生成（命令见下） |
| 服务器 | 云主机（Ubuntu），内存 ≥2G |
| 代码目录 | `/opt/whut-recruit-tool` |
| 数据目录 | `/opt/whut-recruit-tool/data`（宿主机持久化，容器重建不丢） |

## 常用命令（SSH 登录后）

```bash
cd /opt/whut-recruit-tool

docker compose logs -f            # 看服务日志
docker compose restart            # 重启
docker compose up -d --build      # 改完代码重新构建并启动
docker compose down               # 停掉
docker exec whut-recruit-tool python -u app/run_daily.py   # 手动跑一次每日更新
```

## 生成 / 改 Basic Auth 口令

口令只落在服务器的 htpasswd 文件里，不要写进仓库、也不要写进 `docker-compose.yml`：

```bash
# 第一次生成（用户名用 whut，也可以换成别的）
printf 'whut:%s\n' "$(openssl passwd -apr1 '你的口令')" > /www/server/nginx/conf/htpasswd.whut
/www/server/nginx/sbin/nginx -s reload

# 之后改口令：同一条命令覆盖即可（把『你的口令』换成新的）
```

## 每日 7:00 自动更新

宿主机 crontab（文件：`deploy/cron.whut`）：

```
0 7 * * * /usr/bin/docker exec whut-recruit-tool python -u app/run_daily.py >> /opt/whut-recruit-tool/data/cron_update.log 2>&1
```

- 时区 `Asia/Shanghai`，容器和系统都是，日期不会差 8 小时。
- 依次跑：招聘信息 + 双选会增量 → 宣讲会增量（顺带刷新未分析企业的工作地流动）。
- 日志两份：宿主机 `data/cron_update.log`（标准输出）和 `data/preach_update_log.txt`（应用自己写的，只留最近 20 次）；应用内「任务中心」也能看。
- 改时间：直接 `crontab -e` 改那一行的 `0 7`。

## nginx 那层

- 配置：`/www/server/panel/vhost/nginx/whut.conf`（由 `deploy/nginx-whut.conf` 复制而来）
- 口令文件：`/www/server/nginx/conf/htpasswd.whut`
- 改完记得 `/www/server/nginx/sbin/nginx -t && /www/server/nginx/sbin/nginx -s reload`
- 这个 server 块不在宝塔面板的站点列表里，面板建/删站点不会覆盖它。

## 注意事项

1. **构建镜像时 apt 源已改成腾讯云镜像**：`deb.debian.org` 在这台机器上会卡死，改回官方源会导致构建挂住。
2. **数据是从本机整包迁过来的**（131M：2382 条招聘 / 886 场宣讲会 / 43 场双选会 / 590 家已分析企业）。
   **2026-10-07 已按用户要求删除服务器上的个人文件**：`data/简历照片.png`、
   `data/投递档案.json`、`data/投递档案.json.bak-*`。
   服务器上现在只剩抓取与分析产物（SQLite 主库只有 meta/records/sources 三张表，无个人档案）。
   ⚠️ 不要再在服务器页面的「我的 → 简历档案 / 投递看板」里填真实信息，那个页面现在等于空的。
3. 云防火墙只需 80 端口（已放通）；不要额外放通 8765。
4. 2C2G 内存吃紧，容器常驻约 200MB，跑 AI 分析时留意下。
