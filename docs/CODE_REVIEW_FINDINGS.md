# 代码审查验收报告 — 决策中心近期改动

> **审查范围**: `git diff a3a9ef6..HEAD`（21 文件，+1491/-519）
> **审查方式**: code-review skill，8 角度并行（3 correctness + 3 cleanup + altitude + conventions）
> **状态**: 审查已完成，**问题待修复**（上个会话工具异常中断在验证阶段）
> **日期**: 2026-06-30

## 待修复问题清单（按严重度排序）

### 🔴 P0 — 必修（确定的 bug）

#### 1. committee.py `_review_single` except 块 UnboundLocalError
- **位置**: `bottleneck_hunter/watchlist/committee.py` ~line 187-190
- **问题**: 重构后 `provider`/`model` 只在 try 内由 `_invoke_with_retry` 返回值解包。当 chain 内所有备用模型都失败时，`_invoke_with_retry` 在解包前就抛异常进入 except，except 里 `return {... "provider": provider, "model": model}` 引用未绑定变量 → `UnboundLocalError`，掩盖真实错误，该委员从"优雅降级为 abstain"变成整个协程崩溃被 asyncio.gather 吞掉。
- **修复**: 在 try 前预置 `provider, model = "", ""`，或在 except 里用安全默认值。
- **确认**: 2 个独立 agent（Angle A + conventions）都报告，高置信。

#### 2. 前端投委会面板英文结论未翻译（renderCommittee / _voteLabel）
- **位置**: `bottleneck_hunter/web/static/js/decision.js` ~line 673（`_voteLabel`）
- **问题**: `_voteLabel` 用严格相等 `v === 'approve'`/`'reject'`/`'approve_with_modification'`，但实际 verdict 值是 `'approved'`/`'rejected'`/`'approved_with_modifications'`（带 d/s），都不匹配 → 投委会面板头部显示原始英文，i18n 改动在此处失效。
- **修复**: 改为 `includes` 匹配，或统一用已有的 translateVote 映射。

### 🟠 P1 — 应修（翻译/i18n 一致性 + 数据正确性）

#### 3. 译名/CSS类映射在 4 处重复且不一致（decision.js）
- **位置**: decision.js 多处 — `renderMeetingDetail` 的 voteMap（approve→通过）、`renderMeetings` 的 translateVerdict、`_voteLabel`（approve→赞成）、`renderCommittee` 内联 vCls
- **问题**: 同一个 approve 在不同渲染器显示"通过"vs"赞成"，颜色类（approve/conditional）三处 include 检查不一致 → 同一会议在列表/抽屉/概览面板显示不同文字和颜色。新增 vote 值要改 4 处。
- **修复**: 提取统一的 `VOTE_LABELS` / `ROLE_LABELS` / `voteClass()` 模块级常量+函数，所有渲染器复用。

#### 4. overview 委员投票 conditional 误判为 approve 绿色（decision.js）
- **位置**: decision.js ~line 690 `renderCommittee`
- **问题**: `decClass` 用 `decision.includes('approve')` 匹配到 `approve_with_modification`，把"有条件通过"染成纯绿。抽屉渲染器（~605）已修为 `includes('approve') && !includes('modification')`，但 overview 面板未同步 → 两视图对同一票颜色不一致。
- **修复**: 与抽屉渲染器统一逻辑（排除 modification）。

#### 5. 决策概览委员面板依赖 transcript_json，旧记录显示空
- **位置**: `bottleneck_hunter/web/decision_api.py` ~line 480 `decision_overview`
- **问题**: 委员面板从 transcript 里 round==1 的条目重建投票。transcript_json 特性之前创建的旧 committee 记录 transcript=[] → 面板渲染空"暂无评审记录"，即使 committee_review 行有真实数据。
- **修复**: transcript 为空时回退读 `get_reviews_for_execution` / committee_consensus。

### 🟡 P2 — 建议修（健壮性 + 边界）

#### 6. `_recent_executed_by_ticker` 用 `t['ticker']` 硬下标
- **位置**: `bottleneck_hunter/watchlist/decision_engine.py` ~line 660
- **问题**: sim_trades 行若 ticker 为 NULL → KeyError，且调用处（run_execution_plans / run_tactical_plans 的 prompt 构建）未包 try/except → 一条坏数据中断 L3/L4 生成。
- **修复**: 改 `t.get('ticker')` 并跳过空值。

#### 7. `_is_recent_duplicate` 未知 action 绕过去重
- **位置**: `bottleneck_hunter/watchlist/decision_engine.py` ~line 681
- **问题**: `_BUY_FAMILY={buy,add}` / `_SELL_FAMILY={sell,reduce}` 只覆盖4个动作。若 action 是 trim/accumulate/open/close → fam=空集 → 返回 False → 5天冷却失效，可重复下单。
- **修复**: 扩充动作族，或对未知 action 记日志告警。

#### 8. cooldown 时间戳格式比较脆弱（微秒 vs 秒）
- **位置**: `bottleneck_hunter/watchlist/decision_engine.py` ~line 666
- **问题**: cutoff 用 `datetime.isoformat()`（含微秒），sim_trades.created_at 用 `_now_iso()` timespec='seconds'（无微秒）。字符串字典序比较在恰好同秒边界 `'+'(0x2B) < '.'(0x2E)` → 边界交易被误排除。若 created_at 存 'Z' 后缀或不同时区偏移则更严重。
- **修复**: 统一用 datetime 对象比较，或两边统一 timespec。

#### 9. sentiment_data 过滤丢弃合法 0 值（committee.py）
- **位置**: `bottleneck_hunter/watchlist/committee.py` ~line 276 `build_ticker_background`
- **问题**: `{k:v for ... if v not in (None,[],0)}` 把中性 avg_sentiment=0、news_count=0、put_call_ratio=0 当缺失剔除 → "零正面新闻"与"未采集"无法区分。
- **修复**: 只过滤 None，保留 0。

### 🟢 P3 — 低优先（效率/重复，非 bug）

#### 10. `_recent_executed_by_ticker` 同流程调用2次
- **位置**: decision_engine.py L4 — prompt 构建处 + dedup 循环处各调一次 `store.get_sim_trades(limit=200)`
- **修复**: 提到循环外算一次复用。

#### 11. recent_trades_text 格式化块 L3/L4 复制粘贴
- **位置**: decision_engine.py ~line 595（L3）和 ~808（L4）相同7行循环
- **修复**: 提取 helper。

#### 12. approval_rate 量纲假设（小修）
- **位置**: decision_api.py ~line 503 + decision.js renderCommittee
- **问题**: 前端 `Math.round(meta.approval_rate)%`，若后端存的是 0.75 分数而非 75 百分数 → 显示 1%。需确认 consensus 实际存的量纲。

#### 13. round-2 终票被标记为 round:1（transcript）
- **位置**: committee.py ~line 727
- **问题**: `reviews = reviews2` 后 transcript 把终票标 round:1，前端按 round===1 当"独立首轮"展示 → 改票委员的首轮立场被误显示为终票。
- **修复**: transcript 分别记录 reviews1（首轮）和 reviews2（终票）。

#### 14. get_evidence_log 去掉 market 过滤（已知，store.py:3502）
- 上个会话已确认是有意修复（thesis_id FK 已隐含市场隔离），**无需处理**。

## 已确认无问题的部分
- ab_compare.py 删除干净，无残留引用
- `/meetings/{id}` 前端 `resp.meeting || resp` 已修复并兼容
- role_registry.py 单 provider 场景安全（不崩溃，不产生 None model）
- index.html 元素 ID（dc-meeting-date / dc-light-*）与 JS 匹配
- simtrading ensureSimTradingLoaded 修复正确

## 修复建议顺序
1. 先修 P0（#1 #2）— 确定的运行时崩溃和功能失效
2. 再修 P1（#3 #4 #5）— i18n 一致性和数据展示
3. P2（#6-9）— 健壮性
4. P3 视时间处理

## 验证方式
- 启动: `bottleneck-hunter serve --port 8899`（见记忆 project_server_start_command）
- 修完后跑: `python -m pytest tests/ -x` 确认无回归
- 前端改动硬刷新 Ctrl+Shift+R 验证
