# Gangtise 数据接入本系统 — 实施计划

> 日期：2026-08-30 ｜ 范围：A+B（Provider 补基本面 + 催化剂事件源）
> 凭据模型：admin 配置 ak/sk，双开关（接入 / 全局共享），共享开则全体复用 admin key 消耗。

## 1. 核心约束（决定一切的两个事实）

**约束一：Skill 脚本不进生产。** `.claude/` 被本仓库 [.gitignore:16](../.gitignore#L16) 忽略，生产服务器（`/root/walker/Bottleneck-Hunter`）的 git checkout **没有** `.claude/skills/gangtise-*`。故生产级接入**绝不能** import 或 subprocess 调用 skill 脚本——那在生产必然 ImportError / FileNotFound。

**约束二：Skill 脚本的取数逻辑不可直接复用。** `financial_data()` 等函数：
- 用**模块级全局** `GTS_AUTHORIZATION`（import 时从 env/`.authorization` 固化）——无法按调用者切换 token，违背多用户隔离。
- 返回 `format_response()` 的**格式化字符串 + 落盘 CSV**，非结构化 dict——DataHub 要 dict。

**结论：接入劈成两层，互不依赖。**

| 层 | 位置 | 用途 | 谁用 | 进 git |
|---|---|---|---|---|
| **交互层** | `.claude/skills/gangtise-*`（已装） | Claude Code 会话内自然语言投研查询 | admin/开发者手动 | ❌（本地） |
| **生产层** | `bottleneck_hunter/data_provider/gangtise_*`（新写） | 进 DataHub，喂 L1-L4 决策 | 系统自动 | ✅ |

生产层**参考** skill 脚本里的接口 URL / payload / 字段映射（它们是最好的接口文档），但代码独立、git 追踪、token 按调用者解析。

## 2. 凭据模型（对"绝无全局 key"铁律的受控例外）

系统铁律：Key 严格按用户隔离，`resolve_data_source_key` 绝不借他人 key、绝不读 os.environ（[data_source_catalog.py:239](../bottleneck_hunter/data_provider/data_source_catalog.py#L239)）。

用户指定的 Gangtise 模型是**显式授权的受控例外**（非静默全局 key）：
- admin 在后台配置**一对** ak/sk，存 `data_source_keys`（id=`gangtise`，AES 加密，复用 [save_data_source_key](../bottleneck_hunter/auth/store.py#L741)）。
- 两个开关存 `system_config`（复用 [schedule_config.is_global_enabled](../bottleneck_hunter/watchlist/schedule_config.py) 同款读写）：
  - `gangtise_enabled`：总开关。关 = provider 不进候选（等于没装）。
  - `gangtise_global_shared`：关 = 仅 admin 自己的请求能解析到 key（admin 独享）；开 = **所有用户**的请求都回退到 admin 的 key（全体共享消耗）。

**新解析函数** `resolve_gangtise_credentials(user_id) -> (ak, sk) | None`（放 data_source_catalog.py，与 resolve_data_source_key 并列）：
```
if not gangtise_enabled: return None
admin_sub = 找 role=="admin" 用户
if user_id == admin_sub: 用 admin 自己的 gangtise key      # 独享路径
elif gangtise_global_shared: 用 admin 的 gangtise key       # 显式授权共享
else: return None                                           # 普通用户未开共享 → 无 key
```
- **绝不读 env 全局 key**（铁律不破）；key 唯一来源是 admin 在 DB 里的加密配置。
- 共享是 admin 在 UI 上**主动开启**的显式授权，审计可查，非代码默认。用 `ponytail:` 注释标明这是唯一受控例外及其边界。

## 3. 生产层实现

### 3.1 HTTP 客户端 — 新文件 `data_provider/gangtise_client.py`
纯 stdlib+requests 薄客户端（零新依赖，requests 已在用）：
- `_get_token(ak, sk) -> str|None`：POST `openapi.gangtise.com/application/auth/oauth/open/loginV2`（照抄 skill utils `get_authorization`）。token 按 (ak,sk) 缓存 + TTL（避免每次取数都换 token）。
- `fetch_financials(ak, sk, ticker, market, table) -> dict|None`：调三大报表接口，解析 body 成 dict。
- `fetch_earnings_forecast(ak, sk, ticker, market) -> dict|None`：券商一致预期。
- 证券解析：ticker→Gangtise 证券代码。先用系统已有 ticker（A股 6位代码直接可用），避免引 skill 的 security.py。拿不准的留 `ponytail:` 标注上升路径（接 open-reference 搜索接口）。
- 所有请求 `timeout`、失败抛异常（交给 DataHub 熔断），不 print。

### 3.2 Provider — 加进 `data_provider/providers.py`
新 `GangtiseProvider(CapabilityProvider)`，照抄现有 provider 四方法结构（[AkshareEarningsProvider](../bottleneck_hunter/data_provider/providers.py#L573) 为模板）：
- `name="gangtise"`，`priority=0`（抢在 akshare 免费兜底前，因为有券商一致预期）。
- `capabilities() = {CAP_FINANCIALS, CAP_EARNINGS}`。
- `markets() = {"a_stock","hk_stock","us_stock"}`（Gangtise 全覆盖；起步先 a_stock，港美股留标注）。
- `fetch(cap, ticker, market, user_id)`：`creds = resolve_gangtise_credentials(user_id)`；无 creds 返回 None（走下个 provider）；有则调 client。
- 在 [build_providers()](../bottleneck_hunter/data_provider/providers.py#L660) 加 `GangtiseProvider()`。

**填补系统空缺**：`CAP_FINANCIALS` 系统标注"预留，暂无 provider"（[hub.py:28](../bottleneck_hunter/data_provider/hub.py#L28)），Gangtise 是**第一个** financials provider。A股 earnings 此前"免费无机构一致预期"（[providers.py:617](../bottleneck_hunter/data_provider/providers.py#L617)），Gangtise 补齐。

### 3.3 数据源目录 — `data_source_catalog.py`
`DATA_SOURCE_CATALOG` 加一条 `{"id":"gangtise", "name":"Gangtise 投研", "env":"", ...}`，让后台 AI 配置中心自动出现配置项。**但**：Gangtise 用 ak/sk 双字段 + 双开关，非单 key——需 catalog 支持"双字段"或复用 base_url 存 ak、encrypted_key 存 sk（`save_data_source_key` 现成有 base_url+encrypted_key 两字段，正好装 ak+sk）。probe 用 `_get_token` 试连。

### 3.4 AI 能力清单 — `ai_tools.py`
`_CAP_LABELS`（[ai_tools.py:51](../bottleneck_hunter/data_provider/ai_tools.py#L51)）已有 `CAP_EARNINGS`；补 `CAP_FINANCIALS` 标签（"财务报表"："三大报表关键科目"）。Gangtise 数据自动进 AI 分析师协商环（build_manifest 按 available_capabilities 过滤，key 在则自动列出）。

## 4. B — 催化剂事件源

将 Gangtise 公告 / 投研日程接进 [catalyst_monitor.py](../bottleneck_hunter/watchlist/catalyst_monitor.py)，把"LLM 猜催化剂"升级为"真实事件驱动"。
- `gangtise_client.py` 加 `fetch_upcoming_events(ak, sk, ticker) -> list[dict]`（财报日历 / 调研日程 / 重大公告，参考 gangtise-file `investment_calendar.py` / `announcement.py` 接口）。
- catalyst_monitor 在 LLM 识别前，先注入 Gangtise 的**确定日期事件**（财报日、股东大会）作为高置信催化剂；LLM 只补"软催化剂"。
- 归 VIP/决策用户，按 for_user/for_market 隔离。
- **边界**：起步只接"有明确日期"的事件（财报日历最稳）；纪要/研报的叙事催化剂留 `ponytail:` 标注，第二步再做。

## 5. 前端 — 后台 admin 配置
[admin_api.py](../bottleneck_hunter/web/admin_api.py) 加端点（复用现有 data-source-key 存取 + `_require_admin`）：
- `GET/PATCH /gangtise-config`：ak（明文回显 hint）/ sk（password_set，不回显）/ 两开关。
- `POST /gangtise-test`：`asyncio.to_thread(_get_token, ak, sk)` 试连。
前端后台区加：ak/sk 表单 + 两个开关（接入 / 全局共享）+ 测试连接按钮。仅 admin 可见。

## 6. 幂等与隔离
- token 缓存按 (ak,sk) key，不跨 admin 混。
- 全局共享开时，所有用户请求都用 admin key → DataHub 记账落在 admin 名下（消耗可审计）。
- 关总开关 → provider `resolve` 返 None → `_candidates` 自动剔除（[hub.py:127](../bottleneck_hunter/data_provider/hub.py#L127)），等于无缝下线。

## 7. 验证
1. **单元自检** `gangtise_client.py` 的 `demo()`：mock token 换取 + 断言 resolve_gangtise_credentials 三分支（admin独享/共享开全体可用/普通用户未开共享得 None）。
2. **真实连通**：admin 配 ak/sk → 测试连接返回 ok（用你给的 ak/sk 实测）。
3. **端到端**：admin 请求 A股 financials → 命中 Gangtise（DataHub 记账 source=gangtise）；开全局共享 → 普通用户也命中；关总开关 → provider 消失、回落 akshare。
4. **全量 pytest** 每步后跑（零后端回归）。
5. 首页更新历史：commit 含 `📢` 行首白话行。

## 8. 刻意不做（YAGNI）
- 不接 gangtise-agent 叙事文本进交叉验证（C 档，用户未选；叙事时效性弱，价值递减）。
- 不接 gangtise-screener（与系统自有选股重叠）/ gangtise-private（弱相关）。
- 证券代码解析起步只做 A股 6位直通；港美股 + 名称模糊匹配留标注。
- token 缓存用进程内 dict + TTL，不引 Redis（单进程够用）。

## 9. 风险
- ak/sk 是**云端付费额度**，全局共享开 = 全体消耗 admin 额度 → UI 明确提示 + 默认关。
- Gangtise 接口变更 → 熔断兜底自动回落免费源，不阻断决策。
- 生产无 skill 脚本 → 生产层零依赖 skill，已在 §1 规避。
