# BottleneckHunter — 单容器部署（web + 内置 scheduler）
# SQLite 持久化，数据/报告/.env 通过挂卷保留。
# 基础镜像固定 bookworm：python:3.11-slim 会浮动到新 Debian(trixie)，而华为云镜像对
# 刚发布的 trixie-updates 尚未同步好 → apt-get update exit 100。bookworm 成熟且已完整同步。
FROM python:3.11-slim-bookworm

# 国内构建加速：默认用华为云 PyPI 镜像（可 --build-arg PIP_INDEX_URL=... 覆盖为官方源）
ARG PIP_INDEX_URL=https://mirrors.huaweicloud.com/repository/pypi/simple

# 时区数据（scheduler 使用 Asia/Shanghai）+ 构建期编译器（akshare/lxml 等源码依赖）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

# 华为云 Debian apt 镜像加速（bookworm 已完整同步；兼容 deb822 与旧 sources.list）
RUN set -eux; \
    for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        if [ -f "$f" ]; then \
            sed -i 's|deb.debian.org|mirrors.huaweicloud.com|g; s|security.debian.org|mirrors.huaweicloud.com|g' "$f"; \
        fi; \
    done

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── 依赖层：只随 pyproject.toml 变化而失效 ──────────────────────────────
# 关键：只用 pyproject + 一个最小包壳先装依赖 → 【改业务代码不再重装依赖】。
# 旧写法把源码 COPY 放在 pip install 之前，每次改码都让该层缓存失效 → 全量重装
# akshare/pandas/langchain 等重依赖 → 构建严重超时。这里把重依赖安装与源码解耦。
COPY pyproject.toml README.md ./
RUN mkdir -p bottleneck_hunter && touch bottleneck_hunter/__init__.py \
    && pip install -e . \
    && rm -rf bottleneck_hunter bottleneck_hunter.egg-info

# ── 源码/非包文件层：改动只影响这之后，依赖层命中缓存（秒级重建）──────────
COPY bottleneck_hunter/ ./bottleneck_hunter/
# 运行时按仓库根定位的非包文件：更新历史 + 新手必读指南（docs/ 整目录随镜像发布）
COPY UPDATE_HISTORY.json ./
COPY docs/ ./docs/
# 源码就位后仅重链接包（--no-deps 不重装依赖）：注册 console_scripts、editable 指向真实源码
RUN pip install -e . --no-deps

EXPOSE 8000

# 运行时数据目录（挂卷点）——即使未挂卷也能启动
RUN mkdir -p /app/data /app/output

# 容器内 healthcheck：根路径 302→登录 即视为存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:8000/ || exit 1

CMD ["bottleneck-hunter", "serve", "--host", "0.0.0.0", "--port", "8000"]
