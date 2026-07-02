# BottleneckHunter — 单容器部署（web + 内置 scheduler）
# SQLite 持久化，数据/报告/.env 通过挂卷保留。
FROM python:3.11-slim

# 时区数据（scheduler 使用 Asia/Shanghai）+ 构建期编译器（akshare/lxml 等源码依赖）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只拷贝依赖清单，利用镜像层缓存（依赖不变则跳过重装）
COPY pyproject.toml README.md ./
COPY bottleneck_hunter/ ./bottleneck_hunter/
RUN pip install -e .

# 构建期编译器不进运行时镜像的必要性不高（单阶段简单优先）；
# 如需精简，改多阶段构建。ponytail: 单阶段够用，体积敏感时再拆。

EXPOSE 8000

# 运行时数据目录（挂卷点）——即使未挂卷也能启动
RUN mkdir -p /app/data /app/output

# 容器内 healthcheck：根路径 302→登录 即视为存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:8000/ || exit 1

CMD ["bottleneck-hunter", "serve", "--host", "0.0.0.0", "--port", "8000"]
