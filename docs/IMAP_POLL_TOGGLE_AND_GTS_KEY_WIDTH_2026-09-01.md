# 银行邮件轮询开关 + Gangtise Key 控件加宽（2026-09-01）

两个独立的管理员配置中心小改，最小 diff。

## 1　IMAP 自动轮询开关

**需求**：银行邮件自动解读（IMAP）此前只要配了服务器就恒定时轮询，无法在不清空配置的前提下临时停掉自动拉取。加一个开关，关闭后停止定时轮询。

**改法**（沿用既有 `imap_use_ssl` 布尔配置的同构模式，零新表零新依赖）：

- `auth/email_sender.py::resolve_imap_config` 返回值增 `poll_enabled`，DB 与 env 两分支都读 `imap_poll_enabled`，**默认 `"true"`** → 旧库/未配置一律保持原有「配了就轮询」行为，向后兼容。
- `web/admin_api.py`：`UpdateImapConfigRequest` 增 `poll_enabled: bool | None`；PATCH 落 `imap_poll_enabled` 配置；GET `/imap-config` 回显。
- `watchlist/scheduler.py::job_poll_imap`：`imap_configured` 闸之后加一道 `if not cfg.get("poll_enabled", True): return`。**只拦自动定时 job**——管理员手动 `/imap-poll-now`（「立即轮询」按钮）不经此路径，仍可随时手动触发。
- 前端 `index.html` IMAP 面板加「自动轮询」toggle（`admin-imap-poll-enabled`，默认勾选）；`admin.js` load 回显 `c.poll_enabled !== false`、save 带上 `poll_enabled`。

**验收**（真实 `AuthStore`，临时库）：
```
no-cfg   source=env poll_enabled=True          # env 兜底默认开
db-default source=db poll_enabled=True configured=True   # 配 host 未设 poll → 默认开（旧库兼容）
db-off   poll_enabled=False                     # set imap_poll_enabled=false → 关
db-on    poll_enabled=True                      # 再置 true → 开
```
`python -m bottleneck_hunter.vip.mail_ingest` demo OK；全量 **1503 passed, 4 skipped**。

## 2　Gangtise Key 控件加宽

**现象**：Gangtise 投研数据源的 AccessKey/SecretKey 输入框太短。

**根因**：`.admin-config-input` 默认 `width:70px`（为端口这类窄字段设计）。SMTP/IMAP 的文本字段有一条专门加宽到 `320px` 的 id 选择器列表，但 `#admin-gts-ak`/`#admin-gts-sk` 没被纳入 → 落到 70px 默认宽。

**改法**：`css/admin.css` 把这两个 id 加进既有的 320px 加宽选择器列表。一处 CSS，无 JS/结构改动。
