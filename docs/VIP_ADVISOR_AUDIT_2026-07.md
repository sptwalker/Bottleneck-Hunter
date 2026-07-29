# 私人投资顾问（VIP）代码审计与修复报告 · 2026-07

对私人投资顾问全链路（结单解析 → 规范化 → 物化 → 每日推算 → 校准 → 归因 → 展示）做的一次
产品识别 / 解析 / 指标 / 币种 / 时间 / 计算公式适用性专项审计。分两轮修复：TIER-1（高危，直接
影响金额/币种/持仓可见性）与 TIER-2（口径一致性与静默失真）。

严格边界（全程遵守）：
- PDF 密码为用户凭据，只读、绝不写入源码/日志。财务 PII 未经确认不提交/推送/删除。
- VIP 仍 advice-only：只写推算层/衍生品条款/账户日志，绝不写 `sim_*` 真值（结算单物化除外）。
- 时区：UTC 存储 / 北京展示(fmtBJ) / 调度 Asia-Shanghai。多用户/市场隔离经 `.for_user().for_market()`。

---

## 一、TIER-1（高危，已修）

| 编号 | 文件 | 问题 | 修复 |
|---|---|---|---|
| I1 | ingest.py `_parse_nomura_summary` | 野村 Summary 现金/权益按固定列偏移抽取，对当前版式会把 %-列当美元值 | 新增 `_usd_before_pct`：从 %-列向前回扫首个可解析美元值 |
| I2 | ingest.py `_position_usd_value` | 非美元持仓若本币等于报告币，直接把**本币数值当美元**返回 | 删除该短路分支，非美元一律走 FX 换算；无 FX 返回 0 |
| I3 | ingest.py `_parse_nomura_structured` | FCN 标的符号取值不稳 | 扫描 `(SYM MK)`/`Underlying:`/ISIN 兜底，symbol=符号‖ISIN‖首词 |
| I4 | ingest.py `_parse_citi_derivatives` | 累沽(DECUMULATOR)被一律当累购(accumulator) | 正则捕获 ACCUMULATOR/DECUMULATOR → 正确 `product_family` |
| I5 | ingest.py `_parse_cash` | 现金段遇非 EUR 才 break，逻辑写反 | 改为无币种即 break |
| I6 | ingest.py `_currency_amount` | 人民币符号 `￥`（全角）漏识别 | 补 `¥`/`￥` → CNY |
| D1 | derivatives.py `classify_pdf` | Daily Callable FCN 被误判为 MLI | 命中 FCN 关键词优先返回 `fcn` |
| D4 | derivatives.py `extract_mli_terms` | KI% 缺抽时为 0 | 缺失回落 `ki_price/initial×100` |
| NG1 | number_guard.py `verify_numbers` | 负数校验用无符号子串匹配，`$-656,223` 与 `$656,223` 混淆 | 通道 1 改为符号感知（负数须匹配带 `-` 串） |
| PVS | portfolio.py `value_series` | 单锚点被"抹平"复制成多点，伪造历史净值曲线 | 单锚点只折一个点，不外推 |
| OVW | portfolio.py `build_account_overview` | 权重分母口径不稳，结构性产品并入后可能 >100% | 分母取 `max(权威总权益, Σ持仓)` |
| PFX | projection.py `project_stock_mtm` | 非美元持仓用本币收盘价×1.0 写出约 FX 倍高估 | 币种守卫：非美元跳过逐日重估、沿用结单权威美元市值 |

---

## 二、TIER-2（口径一致性 / 静默失真，已修）

| 编号 | 文件 | 问题 | 根因 | 修复 |
|---|---|---|---|---|
| PROJ2 | store_schema/store/store_vip_projection/derivatives/projection | 同标的多笔衍生品（野村双 ORCL）在每日推算里互相覆盖、读时折叠 | `vip_projections` 去重键无 lot 维度 | 加 `lot_key` 列 + 重建 UNIQUE（幂等迁移）；写/读/loader/accrual 全链打通；`latest_projection_map` 同标的累加市值/浮盈/数量 |
| PROJ4 | importer.py | 花旗账户总权益被高估约一整笔融资（样本 +46%） | `Total Assets` 是**总资产(gross)**，被直接当净权益锚；野村(NAV)/招银(TOTAL VALUE) 本是净值 | 花旗分支 `nav = TotalAssets − loan`（`0<loan<nav` 时），三家口径统一为净值 |
| PROJ6 | projection.py `project_derivative_accrual` | 累购累计交易日高估（不含市场节假日） | `np.busday_count` 只去周末 | 改用行情层**真实交易日**计数（节假日天然缺行、且不分美股/港股日历）；未覆盖窗口末端时回落 `busday_count` |
| PROJ7 | projection.py | 推算业务日用 UTC 日期，北京凌晨算成前一天（且是 UNIQUE 键组件） | `_now_iso()[:10]` | `as_of` 默认改用 `_today()`（北京日期），与全系统时区约定一致 |
| PROJ8 | importer.py | 结单权威总额抽取失败时静默回落"持仓+现金"估算（漏结构性产品/衍生品），顾问无感 | 无提示 | 锚缺失时记 `anomaly/warn` 账户日志，明示该期总权益为估算值 |
| D3 | derivatives.py `extract_accumulator_terms`（花旗路径） | 缺抽 St-DS 时默认 0 → 跌破行权价时把下行累股/亏损算成 **0**，恰在最危险方向静默清零 | 默认值 `0.0` | 缺失回落市场惯例 `2×DS`（偏保守），标为校准旋钮 |

### 复核后判定「非缺陷」

| 编号 | 疑点 | 复核结论 |
|---|---|---|
| PROJ5 | 结单衍生品落库 lot_key 是否真填 | 解析器均已产出 `lot_key`、importer 已透传、`save_derivative_term` 已写列，链路完整，无需改动 |
| ATTR1 | 归因事件 payload 疑似新旧币种混用 | `sim_positions` 无币种列、`market_value` 恒为美元基准，新旧两侧均 USD，**不成立**，未改（不臆造修复） |

---

## 三、验证

- 模块自检：`derivatives` / `attribution` / `ingest` demo 断言全过；`projection`+`importer` 导入无误。
- PROJ2 端到端自检：双 lot 不折叠、`latest_projection_map` 累加正确、同 lot 幂等覆盖。
- `pytest tests/test_vip_portfolio.py tests/test_vip_derivatives.py`：核心用例通过；新增
  FCN 折叠 / 重解析清幽灵行两条用例通过。仓内既有 2 条失败（`test_total_overview_aggregates_accounts`、
  `test_startup_purges_empty_account_ref_residue`）已 `git stash` 对照 clean HEAD 确认为**存量失败**，与本次无关。
- `ruff`：本次引入的唯一超长行已修；其余 E702/E501/SIM 为全仓既有风格。
- 服务器经 `bottleneck-hunter serve --port 8010` 重启，`vip_projections` lot_key 迁移在 live DB 静默跑通。

## 四、后续（未做，非本轮范围）

- 野村 NAV 版式锚点化抽取（拿到可复现样本后替换固定列偏移），消除 PROJ8 的回落场景。
- FX 逐日重估（P2/P3）：非美元持仓当前沿用结单权威美元市值，暂不逐日重估。
- 花旗 `Total Assets` 为 gross 的判断基于会计惯例与同结单融资行；如遇特殊版式，以 dump 只读核对为准。
