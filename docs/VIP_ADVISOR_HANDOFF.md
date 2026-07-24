# VIP 私人财务顾问 — 交接词 / 当前开发日志

> 用途：切到新会话时，直接把本文件贴给 Claude，并按“下一阶段任务”继续，不需要重新梳理上下文。
> 当前主线：`main` = `1749857`（已推送 origin/main）
> 当前 VIP 基线：`python -m pytest tests/test_vip_*.py -q -p no:cacheprovider -o addopts=""` → **53 passed, 2 skipped**

---

## 一、给新会话的交接词（可直接复制）

我们继续做 **Bottleneck-Hunter 的 VIP 私人财务 AI 顾问线**。

请先读取这几份文档，再继续后续任务：
- `docs/VIP_ADVISOR_PLAN.md`
- `docs/VIP_ADVISOR_TECH_SPEC.md`
- `docs/VIP_ADVISOR_HANDOFF.md`（本文件）

### 当前已完成状态

1. **P0 安全底座**
- `auth.db` 已有 `financial_documents`、`advice_audit_trail`
- `require_vip` 门禁已接入
- `number_guard` / `compliance` 公共件已完成
- 删号级联已接 `auth.db` 部分（watchlist 侧待后续更完整接入）

2. **P1/P2/P5 花旗链路**
- 花旗月结单可上传 / 解析 / 对账 / 规范化 / 物化 / 出报告
- 多币种（HKD→USD）口径已理顺
- ETF ISIN 锚已支持（如 `US4642875235 → SOXX`）
- 7 期真实花旗月结单（2025-12 ~ 2026-06）对账全部 `$0.00` 差，`ok`

3. **P5 AI 顾问叙事**
- `vip_advisor` 角色已接入
- 报告支持 AI 分层叙事（宏观/配置/操作）
- 叙事统一过 `number_guard`

4. **B：VIP web 面板**
- `/api/vip/statements/upload`
- `/api/vip/reports/generate`
- `/api/vip/derivatives/upload`
- `/api/vip/derivatives`
- 前端 `VIP 顾问` 页面已可上传结单、生成报告、上传衍生品条款

5. **C1：现金层**
- `total_equity = 持仓 + 可投资现金`
- 报告已显示“其中可投资现金”

6. **C2：衍生品建模**
- 已支持：
  - Citi MLI / Booster
  - Nomura Daily Accumulator / Decumulator
- 报告可自动附“衍生品 / 结构化产品风险摘要”

7. **A：野村结单完整账户口径**
- 野村账户结单（密码 PDF）可解密 ingest
- 可抽：Cash / Equities / Derivatives / Liabilities / NAV
- 物化时支持 `account_total_usd`，野村用 NAV 覆盖总权益口径

8. **P6：实时咨询窗口**
- `chat_sessions` / `chat_messages` 已建表
- `/api/vip/chat`
- `/api/vip/chat/sessions`
- `/api/vip/chat/sessions/{id}`
- 前端聊天窗口已可用（流式 SSE）
- 回答后统一过 `number_guard`，并挂免责声明

9. **VIP 审查修复** 已完成
- 聊天 `event:error` 正确处理
- 文档列表按 `market` 真过滤
- 月结单上传也支持 `pdf_password`
- `session_id` 跨市场串用已修
- 衍生品去重稳定性已修
- AI 配置页已有 `VIP 顾问` 模块 tab

10. **部署问题已修**
- `python-multipart` 已加依赖
- 当前部署前请先 `git pull origin main && ./deploy.sh`

### 当前最适合继续做的下一阶段任务（推荐顺序）

**优先级 1：把野村结单也完整打通到“真实报告验收”**
- 现在代码已支持解析野村结单与 NAV 锚点，但还没像花旗那样做“真实线上手工验收一轮”
- 建议先在部署后真实上传野村结单（密码 `22704339`）跑一份报告，确认页面与报告口径都正常

**优先级 2：把 VIP 页面做成专业账户工作台（多页签）**
用户已经明确提过要：
- 账户总览
- 最近持仓
- 历史交易
- 自动复盘
- 咨询解读

目前 VIP 页面更像“功能面板”，不是完整工作台。这个是最自然的下一步产品工作。

**优先级 3：建立完整账户流水层**
目前：
- 花旗导出文件已看过结构
- 但还没有把导出文件真正接成：
  - `transactions`
  - 已实现盈亏
  - 余额轨迹
  - 资产变化曲线

用户明确要求建立完整账户记录分析体系，这一步是未来最关键的底层工程。

### 不要再做的事 / 已明确的边界
- **暂时不要再扩其它券商文件**（用户明确说过）
- 先把现有花旗 / 野村链路做扎实，再扩未知券商 LLM fallback

---

## 二、真实样本路径（非常重要）

### 花旗月结单
- 路径：`C:\Users\walker\Documents\walker\银行文件\花旗月结单\`
- 已验证：7 期月结单全对账通过

### 花旗导出文件
- 路径：`C:\Users\walker\Documents\walker\银行文件\花旗导出文件\`
- 用途：后续做完整账户流水、余额轨迹、已实现盈亏层

### 花旗日常文件
- 路径：`C:\Users\walker\Documents\walker\银行文件\花旗日常文件\`
- 已分类 / 建模：
  - Citi MLI / Booster
  - 结构化产品条款书

### 野村结单
- 路径：`C:\Users\walker\Documents\walker\银行文件\野村结单\`
- 密码：`22704339`
- 已支持：密码 PDF ingest + NAV 锚点抽取

### 野村日常文件
- 路径：`C:\Users\walker\Documents\walker\银行文件\野村日常文件\`
- 密码：`22704339`
- 已分类：
  - `termsheet-*`：可解密并读条款
  - `irf-*`：Investment Product Rationale Record（审计附件，不定价）
  - OAC / ODC：Accumulator / Decumulator 条款书

---

## 三、最近关键提交（按时间倒序）

- `1749857` fix: VIP 文件上传端点补齐 python-multipart 依赖（修复服务器启动 500）
- `7405ad1` fix(vip): 审查修复——聊天错误事件/会话隔离/文档市场过滤/密码上传/去重稳定性
- `aec8c40` feat(vip): P6—实时咨询窗口（流式聊天 + 会话存储 + facts护栏）
- `31d9ca5` feat(vip): B—日常衍生品文件接入 web 上传链，报告自动纳入风险摘要
- `80a074e` feat(vip): A—野村结单按完整账户口径打通到出报告（含密码 PDF）
- `3fb5db3` feat(vip): 野村结单解析接入多券商 ingest（密码 PDF）+ 日常衍生品/票据分类完善
- `8d8fba8` feat(vip): 野村日常衍生品文件分类与首批解析支持（Accumulator/Decumulator）
- `2f8ce9f` feat(vip): C3—多券商兼容骨架（detector + parser registry + 未知格式安全拒绝）
- `793a68c` feat(vip): C2—结构化产品建模（Accumulator/Decumulator + MLI Booster）
- `e968c08` feat(vip): C1—现金层并入完整账户口径，total_equity = 持仓 + 可投资现金
- `968acf2` feat(vip): B—web 端点 + 前端面板，VIP 用户可网页上传月结单看报告
- `8b69e76` feat(vip): A—报告接入 AI 顾问团队分层叙事（宏观/配置/操作）

---

## 四、关键文件索引

### VIP 后端
- `bottleneck_hunter/vip/ingest.py`
- `bottleneck_hunter/vip/portfolio.py`
- `bottleneck_hunter/vip/derivatives.py`
- `bottleneck_hunter/vip/chat.py`
- `bottleneck_hunter/vip/number_guard.py`
- `bottleneck_hunter/vip/compliance.py`

### Web/API
- `bottleneck_hunter/web/vip_api.py`
- `bottleneck_hunter/web/auth_api.py`
- `bottleneck_hunter/web/static/js/vip.js`
- `bottleneck_hunter/web/static/index.html`
- `bottleneck_hunter/web/static/js/ai-config.js`

### Store / Schema
- `bottleneck_hunter/auth/store.py`
- `bottleneck_hunter/auth/dependencies.py`
- `bottleneck_hunter/watchlist/store_schema.py`
- `bottleneck_hunter/watchlist/store_simtrading.py`

### 文档
- `docs/VIP_ADVISOR_PLAN.md`
- `docs/VIP_ADVISOR_TECH_SPEC.md`
- `docs/VIP_ADVISOR_HANDOFF.md`

### 测试（当前 VIP 基线）
- `tests/test_vip_p0_common.py`
- `tests/test_vip_p0_store.py`
- `tests/test_vip_ingest.py`
- `tests/test_vip_ingest_nomura.py`
- `tests/test_vip_portfolio.py`
- `tests/test_vip_derivatives.py`
- `tests/test_vip_api.py`
- `tests/test_vip_chat.py`

---

## 五、下一会话建议直接开工的具体任务

### 推荐任务 1（最优先）
**把 VIP 面板升级成专业账户工作台**

目标：参考模拟持仓页面布局，做多页签：
- 账户总览
- 最近持仓
- 历史交易
- 自动复盘
- 咨询解读

理由：用户已经明确提出，这是当前最直接的产品缺口；现有数据底座已经够支撑 UI 升级。

### 推荐任务 2
**接入花旗导出文件，建立完整账户流水层**

目标：
- 解析 `花旗导出文件`
- 写入 `transactions`
- 建立：
  - 已实现盈亏
  - 余额轨迹
  - 资产变化曲线

理由：这会真正完成用户最初要求的“完整账户记录分析体系“。

### 推荐任务 3
**部署后做真实验收陪跑**

命令：
```bash
cd ~/walker/Bottleneck-Hunter
git pull origin main
./deploy.sh
```

验收顺序：
1. 上传花旗月结单
2. 生成报告
3. 上传野村结单（密码）
4. 生成完整账户报告（看 NAV）
5. 上传一份衍生品 term sheet
6. 再生成报告（看衍生品风险摘要）
7. 在咨询窗口提问

---

## 六、提醒

- 当前工作树和远端主线应已同步到最新 main；如有不确定，先：
  ```bash
  git status
  git log --oneline -5
  ```
- 所有 VIP 相关改动已在主线上，不需要再切分支。
- 若继续做“账户总览 / 历史交易 / 自动复盘“ UI，请**优先复用模拟交易页的结构与样式**，不要重新发明一套页面系统。
