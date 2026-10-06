# WHUT 校招工具 —— 服务端容器镜像
#
# 本地开发仍然照常用 `python app/server.py`；这个镜像只用于服务器上长期跑着的那个实例。
# 数据全部落在宿主机挂载的 ./data，镜像里不打包任何抓取产物或个人数据。

FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    PIP_TRUSTED_HOST=mirrors.cloud.tencent.com

# tzdata：让容器里的「今天 / 近三天」跟北京时间一致，否则宣讲会日期会整体差 8 小时
# 源换成腾讯云镜像：deb.debian.org 在国内机器上经常卡在下载阶段
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g; s|security.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata; \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime; \
    echo "Asia/Shanghai" > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：只改代码时不会重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 挂载点：宿主机 ./data 挂进来，容器重建数据不丢
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=8)"

# 无鉴权，只监听容器内部地址；对外一律走宿主机 nginx 反代（见 deploy/nginx-whut.conf）
CMD ["python", "-u", "app/server.py", "--host", "0.0.0.0", "--port", "8765"]
