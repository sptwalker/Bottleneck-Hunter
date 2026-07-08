# 外部数据源接入 — 阶段验收报告与系统级改进方案

> 生成于 DataHub 多源调度接入完成后。审计方法：8 维度并行深审（数据链路/Provider正确性/假数据/降级容错/多用户隔离/调度引擎/健壮性/前端）+ 对抗性验证（21 confirmed / 8 partial / 2 refuted / 1 非问题）+ 关键项人工复核代码。

## 总评

**架构正确、落地有洞。** DataHub 的「质量梯队+档内均衡+额度阀+故障转移」核心逻辑经验证正确，`resolve_data_source_key` 的多用户 key 修复正确，earnings/financials/news 多源已用真实数据跑通互备。但存在：① 一个消费方层面的跨用户 key 泄漏回退；② 一批接线缺口；③ 前端几乎没表达这套能力。

---

## 分项结论

### 数据链路 / 断点 / 假数据
- **A股 earnings 链路空转**：CAP_EARNINGS 对 a_stock 只有付费 Tushare、无免费兜底 → 无 token 用户的 `earnings_reports` 对所有 A股永久为空。（providers.py:232 / earnings_pipeline.py:31）
- **CAP_INSIDER 死能力**：FMP/Finnhub 实现并注册内部人能力，但无任何消费方，返回结构与 `save_insider_trades` 不兼容 —— over-build。（providers.py:199）
- **earnings 营收未转「亿」**：FMP 落裸 USD、Tushare 落裸元，与全系统 `revenue_yi`(亿) 口径冲突，原样喂 LLM 与前端。（providers.py:216）
- **残留假数据（与已修 bug 同源）**：AlphaVantage/Tiingo 财务 Provider 把 trailing PE/EPS 标成 consensus_pe/consensus_eps，换源时覆写 yfinance 真 forward 一致预期。（providers.py:392/478）
- **pre-existing**：uzi_runner 杀猪盘检测仅凭 ticker 让 LLM 纯推理却输出确定性「🟢安全」；fact_check 硬编码绝对 PE 阈值不分行业市场。

### 多用户隔离（最严重）
- **D-1 修复被新消费方架空**：`resolve_data_source_key` 层正确，但 `financial_data.fetch_batch`(未透传 user_id)、`news_pipeline:297`、`options_pipeline:100` 都传 `user_id=""` → hub 走 `any_data_source_key` 借用他人 key + 盗用付费额度。唯 earnings_pipeline 正确透传。
- **额度阀/熔断进程级全局按源不按用户**：按 user 隔离 key 后，A 打满 fmp 全局计数会误掐 B 的独立额度。（scheduler.py:24）

### 降级 / 容错
- **熔断按源名跨能力共享**：track("yfinance") 与期权 provider 撞同一 `_ProviderState` → 复位/误开互相污染，掐断期权唯一免费兜底。（hub.py:178）
- **无 key 源被记假成功**：hub 对未配置源仍 note_call + 记 ok=True → 污染额度阀/均衡/健康统计。（hub.py:127）
- **429 限流未纳入软失败** → 误当故障触发熔断，与 402/403 不一致。（providers.py）
- 骨架完整：hub.fetch 不上抛、financials/news 有免费基线、options 有 yfinance 兜底、402/403 软失败正确。

### 健壮性 / 可维护性
- **fetch_batch 名批量实串行**，`_SEMAPHORE(4)` 形同虚设；叠加 hub 覆盖使每票延迟翻倍。（financial_data.py:527）
- **5 个非 FMP provider 解析/字段映射零单测**。
- **调度全程无日志**：换源/额度跳过/熔断静默发生，无法诊断。（scheduler.py:131）
- hub/manager 两套选源+熔断+记账重复；陈旧测试飘红；rows 记账=dict 键数。

### 调度引擎（核心正确，记账语义失真）
- ✅ order() 排序正确达成「高质量优先+同档轮换」，免费源恒不超额，互备/换源达成。
- ⚠️ 「精确分摊额度」只做到一半：一次 fetch=多次上游请求却按 1 计、per-day 60s 缓存滞后、per-min 内存滑窗重启清零/多 worker N 倍超发。

### 前端（后端做实、前端隐形）
- 配置页绿灯=只存了 key，被误读为「健康/在用」。（ai-config.js:986）
- **Polygon probe 假信心**：测免费 `/reference/tickers`，但期权需付费订阅 → 测通却 403。（data_source_catalog.py:92）
- 数据报告丢弃 `hub.get_status()` 的 `capabilities`，覆盖矩阵硬编码且漏 financials。（data-report.js:86 / data_report_api.py:42）
- 营收单位跨面板不统一；stale 无逐行标记；provenance 展示不统一。

---

## 系统级改进方案（P0 → P1 → P2）

### P0 — 立即修（安全/正确性回退）
1. 堵回跨用户 key 泄漏：`fetch_batch`/news/options 透传 user_id 到 hub.fetch，对齐 earnings_pipeline。
2. Polygon probe 对齐真实能力（改测期权端点或明示付费订阅）。

### P1 — 尽快修（数据可信/性能/可见性）
3. AV/Tiingo 不再用 trailing 冒充 consensus（consensus_pe 只取 ForwardPE、consensus_eps 只在有前瞻字段时填）。
4. A股 earnings 加 akshare 业绩快报免费兜底 provider。
5. 熔断按 (源名,能力) 隔离，或 track 不复用 _ProviderState。
6. fetch_batch 改 asyncio.gather 真并发。
7. 前端表达多源调度：数据报告渲染 capabilities + source×capability 用量；覆盖矩阵动态推导补 financials；配置页绿灯三态化 + 显示服务能力 + 近7日调用数。
8. earnings 营收转「亿」+ 前端 fmtMoney 统一单位。

### P2 — 优化（健壮性/可维护性/技术债）
- 无 key 源在 _candidates 阶段过滤（不 note_call/不记账）；429 纳入软失败；调度加 debug 日志。
- 额度阀：financials 按请求权重累加、fmp 补 per_min、per-day 叠加内存增量；额度/熔断/记账按 (source,user_id) 隔离。
- 5 个 provider 补离线解析单测；修陈旧飘红测试。
- 删 CAP_INSIDER 死能力（或补消费方）；stale 逐行标记 + provenance 芯片统一；hub/manager 状态类合并。
- pre-existing：fact_check PE 按分位/PEG；trap-detector 无检索返「无法判定」；performance 无风险利率按市场；L2 缺概率不落 EV。

---

## 执行进度

- [x] P0-1 跨用户 key 泄漏 — fetch_batch/news/options 透传 user_id，验证无 key 用户不借他人 key
- [x] P0-2 Polygon probe — 改探期权端点，诚实报告"需付费Options订阅"
- [x] P1-3 AV/Tiingo consensus — 不再用 trailing 冒充，只取 ForwardPE
- [x] P1-4 A股 earnings 兜底 — 新增 AkshareEarningsProvider（stock_yjbb_em），实测茅台 eps=21.76/营收547亿
- [x] P1-5 熔断按能力隔离 — track 不再复用 provider 熔断计数
- [x] P1-6 fetch_batch 并发 — 改 asyncio.gather，信号量生效
- [x] P1-7 前端多源可见性 — 数据报告加能力列 + 覆盖矩阵动态含 financials + 配置页能力标签/诚实文案
- [x] P1-8 营收单位统一 — FMP/Tushare earnings revenue ×1e-8 转亿
- [x] P2 批（本轮完成）：
  - [x] 无 key 源在 _candidates 阶段过滤（不 note_call/不记账，消除幻影流量）
  - [x] 429 纳入软失败（finnhub/AV/FMP 限流→换源而非熔断）
  - [x] 调度器超额跳过加 debug 日志
  - [x] 删 CAP_INSIDER 死能力（FMP/Finnhub）
  - [x] 修陈旧飘红测试（morningstar×2 + health 401 依赖注入）
  - [x] 新增 test_providers_parse.py（6 用例锁字段映射/单位/一致预期语义）

## 剩余（未做，较大或 pre-existing，建议后续单独排期）
- 额度阀按 (source,user_id) 隔离 + financials 请求权重 + fmp per_min 兜底 + per-day 内存增量（跨切面改动）
- hub/manager 两套状态类合并（中型重构）
- 前端：观察池 stale 逐行标记、provenance 数据来源芯片统一、配置页 3-state 健康灯 + 近7日调用数（需后端补 health/usage 下发）
- pre-existing 假数据/硬编码：fact_check PE 分位/PEG、trap-detector 无检索返"无法判定"、performance 无风险利率按市场、L2 缺概率不落 EV、committee 行业趋势输入
- test_scheduler 14-jobs / shutdown（scheduler 内部，与数据源无关）
