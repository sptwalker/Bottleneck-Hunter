# 部署与升级指南

生产为**单容器 Docker** 部署（web + 内置 scheduler，SQLite 持久化）。
本机开发用 `bottleneck-hunter serve` 即可，不需要 Docker。

## 一、目录与端口

- 生产：`bh.youdoogo.com`，容器名 `bottleneck-hunter`，主机端口 **8089** → 容器 8000。
- nginx 在 `/usr/local/nginx`（反代 8089；桌面借道 WS 需在 nginx 加 `Upgrade` 头）。
- 时区：容器内固定 `Asia/Shanghai`（[Dockerfile](../Dockerfile) / [docker-compose.yml](../docker-compose.yml)）。

## 二、首次部署

```bash
git clone https://github.com/sptwalker/Bottleneck-Hunter.git
cd Bottleneck-Hunter

# 准备可写的 .env（应用会写回 AI 配置，必须存在且可写）
cp .env.example .env        # 按需填 API key；也可留空，登录后在「系统配置中心」配

# 首次构建 + 后台启动
HOST_PORT=8089 docker compose up -d --build
```

数据卷（`./data` / `./output` / `./.env`）自动挂载；`./data` 内含 SQLite 库 + 加密密钥 + JWT
secret，**务必持久化并备份**（丢失则已加密的用户 Key 无法解密）。

## 三、升级（拉新代码 + 重建）

**每次上线新版本都用这一套**——依赖变更（如新增数据源包）会自动随镜像重建装上：

```bash
cd /path/to/Bottleneck-Hunter
git pull origin main
HOST_PORT=8089 docker compose up -d --build
```

`--build` 会在 `pyproject.toml` 变化时让 `pip install -e .` 层缓存失效 → 重新装依赖 →
用新镜像重建容器。数据卷不受影响，不丢配置。

> compose 老版本用带横杠的 `docker-compose` 替换 `docker compose`。
> `HOST_PORT` 不设时默认 8000；生产固定用 8089。

## 四、验证

```bash
# 启动日志正常（应见 "Application startup complete" + "Watchlist scheduler configured with N jobs"）
docker compose logs --tail=40 bottleneck-hunter

# 健康检查：根路径 302→登录 即存活
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8089/   # 期望 302

# 确认关键数据源包已进容器（A股兜底源）
docker compose exec bottleneck-hunter python -c "import baostock; print('baostock OK')"
```

## 五、数据源依赖说明

A 股行情有多个数据源，按优先级自动回退（前面挂了自动落下一个）：

| 顺序 | 源 | 服务器 | 是否默认安装 |
|---|---|---|---|
| 0 | efinance | 东方财富 | optional（`datasources` 组） |
| 1 | akshare | 东方财富 | **主依赖，默认装** |
| 2 | pytdx | 券商 TDX | optional |
| 3 | **baostock** | 独立服务器 | **主依赖，默认装** |

- akshare/efinance 共用**东方财富**服务器，其偶发 `RemoteDisconnected`（限流/网络）时会**同时失效**。
- **baostock** 走独立服务器、纯 Python 体积小，是东财挂掉时唯一可靠的 A 股兜底，故已提入主依赖，
  `pip install -e .`（含镜像构建）自动安装，**A 股开箱即用，无需额外操作**。
- 想启用全部增强源（efinance/pytdx/finnhub）：`pip install -e ".[datasources]"`；
  Docker 里则在 [Dockerfile](../Dockerfile) 的 `pip install -e .` 改为 `pip install -e ".[datasources]"` 后重建。

## 六、回滚

```bash
git checkout <上一个正常的 commit>
HOST_PORT=8089 docker compose up -d --build
```

数据卷保留，回滚只换代码/镜像，不动数据。

## 七、常见问题

- **首页「更新历史」/「新手必读」为空**：`UPDATE_HISTORY.json` 与 `docs/` 已在
  [Dockerfile](../Dockerfile) 里 `COPY` 进镜像，重建即带上最新；本地跑则确保在仓库根启动。
- **端口占用**：`HOST_PORT` 改成空闲端口，并同步改 nginx 反代目标。
- **改了依赖但容器没更新**：确认用了 `--build`；仍不行加 `--no-cache`：
  `HOST_PORT=8089 docker compose build --no-cache && HOST_PORT=8089 docker compose up -d`。
