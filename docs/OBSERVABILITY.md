# 运维日志栈（Grafana + Loki + Alloy）

给云端部署一套**实时、可检索、可告警**的运维日志，替代原来只在页面里看的简易内存日志
（`LogBroadcaster`）。核心目标：出问题时能**快速把日志喂给 Claude 定位系统故障**。

- **Alloy**：抓本机 Docker 容器 stdout（含 `bottleneck-hunter`）→ 推给 Loki。**应用零改动**。
- **Loki**：单机 filesystem 存储、14 天保留、低资源。仅内网。
- **Grafana**：日志 UI / 实时 live tail / LogQL 检索 / 告警。经 nginx 反代 `bh.youdoogo.com/grafana/`。
- 只对外暴露 **Grafana 一个面**（有登录墙）；Loki/Alloy 不出内网。

相关文件：[docker-compose.yml](../docker-compose.yml)、[deploy/loki/loki-config.yaml](../deploy/loki/loki-config.yaml)、
[deploy/alloy/config.alloy](../deploy/alloy/config.alloy)、[deploy/grafana/provisioning/datasources/loki.yaml](../deploy/grafana/provisioning/datasources/loki.yaml)、
[deploy/nginx-observability.conf](../deploy/nginx-observability.conf)。

---

## 一、上线（在服务器）

前置：确认服务器有 **~0.5GB 空闲内存**（三服务合计约 400–600MB）。

```bash
cd /path/to/Bottleneck-Hunter
git pull origin main

# .env 里设 Grafana 管理员强密码
echo 'GRAFANA_ADMIN_PASSWORD=<你的强密码>' >> .env   # 或编辑 .env

# 拉起（含原有 bottleneck-hunter + 新增 loki/alloy/grafana）
HOST_PORT=8089 docker compose up -d

# nginx 反代 /grafana/：把片段 include 进 bh.youdoogo.com 的 server{} 块后 reload
#   include /path/to/Bottleneck-Hunter/deploy/nginx-observability.conf;
/usr/local/nginx/sbin/nginx -t && /usr/local/nginx/sbin/nginx -s reload
```

验证四服务：`docker compose ps`（bottleneck-hunter / bh-loki / bh-alloy / bh-grafana 均 Up）。

## 二、用户怎么看日志

浏览器开 `https://bh.youdoogo.com/grafana/` → admin + 上面设的密码登录 → 左侧 **Explore** →
数据源选 **Loki** → 输入 LogQL：

```logql
{container="bottleneck-hunter"}                          # 全部应用日志
{container="bottleneck-hunter"} |~ "(?i)error|traceback" # 只看报错
{container="bottleneck-hunter"} |= "产业链拆解超时"        # 定位某类问题
```

右上角开 **Live** 即实时 live tail。发现问题后，**框选那几行复制**发给 Claude，或用下面的"让 Claude 直接查"。

## 三、让 Claude 直接查日志（免复制粘贴）

一次性准备（在 Grafana）：
1. 左下 **Administration → Users and access → Service accounts → Add** → 角色 **Viewer** → 建 Token → 复制。
2. 在**你的开发机**（跑 Claude Code 的机器）建一个**不入库**的文件（`.claude/` 已被 gitignore）：
   ```bash
   # .claude/loki.env
   GRAFANA_BASE=https://bh.youdoogo.com/grafana
   GRAFANA_SA_TOKEN=glsa_xxx...
   ```

之后你只需告诉 Claude「大概几点 + 关键词」，Claude 用如下方式自查（走 Grafana 数据源代理，
uid 固定 `loki`）：

```bash
source .claude/loki.env
# 查最近 1 小时的报错（start/end 为纳秒时间戳）
END=$(date +%s)000000000; START=$(( $(date +%s) - 3600 ))000000000
curl -sG -H "Authorization: Bearer $GRAFANA_SA_TOKEN" \
  "$GRAFANA_BASE/api/datasources/proxy/uid/loki/loki/api/v1/query_range" \
  --data-urlencode '{container="bottleneck-hunter"} |~ "(?i)error|traceback|超时|失败"' \
  --data-urlencode "start=$START" --data-urlencode "end=$END" \
  --data-urlencode "limit=500" --data-urlencode "direction=backward"
```

> Token 是独立 **Viewer** 权限，可在 Grafana 随时吊销；泄露不影响 admin。切勿把 Token 写进仓库。

## 四、常见故障 LogQL 速查

| 场景 | 查询 |
|---|---|
| 产业链拆解超时/卡死 | `{container="bottleneck-hunter"} |~ "拆解超时|decompose failed|TimeoutError"` |
| A股数据源全失效 | `{container="bottleneck-hunter"} |~ "所有实时数据源均失败|RemoteDisconnected"` |
| 决策/LLM 报错 | `{container="bottleneck-hunter"} |~ "(?i)error" |~ "llm|invoke|chat/completions"` |
| HTTP 5xx | `{container="bottleneck-hunter"} |~ " 5\\d\\d "` |
| 登录/鉴权异常 | `{container="bottleneck-hunter"} |= "auth" |~ "(?i)401|403|失败"` |
| 定时任务 | `{container="bottleneck-hunter"} |= "scheduler"` |

## 五、告警（可选，后续接）

Grafana → Alerting 可对 LogQL 结果计数设阈值告警（如「5 分钟内 ERROR > 10」「出现『所有实时数据源均失败』」），
通知走邮件/webhook。首版先保证采集+检索闭环，告警按需再配。

## 六、安全与运维须知

- 仅 `/grafana/` 公网可达（登录墙）；Loki/Alloy 仅 compose 内网。
- `GRAFANA_ADMIN_PASSWORD`、SA Token 均不进 git（`.env`、`.claude/` 已忽略）。
- Alloy 只读挂载 `docker.sock`；仅本机。
- 保留期 14 天（改 [loki-config.yaml](../deploy/loki/loki-config.yaml) 的 `retention_period`）。
- 日志数据在命名卷 `loki-data`，容器重建不丢；磁盘吃紧时缩短保留期。
- 国内网络：全自托管、日志不出境；镜像 `grafana/*` 从 Docker Hub 拉取（服务器现已能拉取基础镜像）。
