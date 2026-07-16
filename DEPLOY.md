# Docker 部署

单容器运行整个系统（FastAPI Web UI + 内置 APScheduler 定时任务）。数据用 SQLite，通过挂卷持久化。

## 前置

- 安装 Docker 与 Docker Compose 插件
- 项目根目录下准备 `.env`：

```bash
cp .env.example .env
# 编辑 .env 填入需要的 API Key（可留空，之后在 Web「AI 配置」里填）
```

> `.env` 会被挂载进容器且应用会回写它（Web 端保存 AI 配置）。必须先创建该文件，
> 否则 Docker 会把不存在的 `.env` 当成目录挂载。

## 启动

```bash
docker compose up -d --build
```

- 访问 http://localhost:8000 （首次进入需注册/登录）
- 换端口：`HOST_PORT=9000 docker compose up -d`（容器内固定 8000）

## 常用命令

```bash
docker compose logs -f          # 看日志
docker compose ps               # 状态（含 healthcheck）
docker compose restart          # 重启
docker compose down             # 停止并移除容器（数据保留在 ./data）
docker compose up -d --build    # 改代码后重建
```

## 持久化的目录（挂卷）

| 宿主机 | 容器内 | 内容 |
|--------|--------|------|
| `./data` | `/app/data` | 所有 SQLite 库 + `.encryption_key` + `.jwt_secret` |
| `./output` | `/app/output` | 生成的 Markdown 报告 |
| `./.env` | `/app/.env` | 全局配置 / API Key（应用可读写） |

⚠️ **`./data/.encryption_key` 与 `.jwt_secret` 请勿删除**：前者丢失后已加密的用户 API Key 无法解密，后者丢失会使所有登录会话失效。

## 备份与恢复（务必配置）

`./data` 是唯一真源且默认无副本——卷损坏 = 所有用户/密钥/历史永久灭失。用内置脚本做
WAL 安全的一致快照（裸 `cp` 会丢最近事务）：

```bash
# 库快照到异卷 + 密钥单独去另一处（一损不俱损），保留最近 14 份
python scripts/backup.py --out /mnt/off/bh --keys-dir /mnt/vault --retain 14
# 宿主 crontab（每天 03:17）
17 3 * * * cd /path/to/Bottleneck-Hunter && python scripts/backup.py --out /mnt/off/bh --keys-dir /mnt/vault --retain 14
```

恢复：停容器 → 把某个快照目录里的 `*.db` 拷回 `./data/`，密钥文件去掉时间戳后缀拷回 → 起容器。

## 非 root 运行：挂载目录属主

容器以 uid `10001`（appuser）运行，宿主挂载的 `./data`、`./output`、`./.env` 须对该 uid 可写：

```bash
sudo chown -R 10001:10001 ./data ./output && sudo chown 10001:10001 ./.env
```

## Grafana 密码（日志栈）

`GRAFANA_ADMIN_PASSWORD` 无默认值——未在 `.env` 设置则日志栈容器启动即失败（fail-closed，
避免弱口令 admin 暴露公网 `/grafana/`）。启用日志栈前务必在 `.env` 设强密码。

## 说明

- 时区固定 `Asia/Shanghai`（A 股定时任务依赖）。
- 镜像为单阶段构建，含编译工具链；如需精简体积可改多阶段。
- 未引入 Postgres/Redis：系统本身用 SQLite + 进程内调度，单容器即完整功能。

## 邮箱验证（可选）

新用户注册与修改邮箱需邮箱验证码。**SMTP 未配置时默认不再把验证码写日志**（防日志泄露接管账号）；
仅本地调试可设 `BH_DEV_LOG_CODES=1` 打印验证码。生产请配 SMTP 真发邮件。

正式启用邮件发送，在 `.env` 配置：

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587            # 465 走 SSL，其余端口走 STARTTLS
SMTP_USER=your_account@example.com
SMTP_PASSWORD=your_password_or_app_token
SMTP_FROM=BottleneckHunter <no-reply@example.com>
SMTP_USE_TLS=true
```
