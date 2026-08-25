# 美股 keyed providers（FMP/Finnhub/Polygon/Tiingo/AlphaVantage）深度用足·研究报告（2026-08）

> 问题：要继续完善美股数据，如何把现有 keyed providers 的深度用足？
> 方法：核实每个 provider 在系统里**已声明能力 vs 实际实现 vs 真正能力**的三层落差，只列有真实收益的缺口，按 YAGNI 排优先级。
> 结论先行：**美股真正的缺口不在"缺源"，而在"能力承诺了却没接 provider"——`institutional`/`insider`/`smartmoney` 三类对模型承诺了，却只有免费 yfinance 一条腿，keyed 源一个都没挂**。补法不是引新源，是把已配 Key 的 FMP/Finnhub 接到这三条已存在的 track 管线上。

---

## 0. 现状三层落差（已核实到代码行）

### DataHub 能力矩阵实况

| 能力 CAP_* | 对 AI 承诺(`_CAP_LABELS`) | 全托管 provider(`build_providers`) | 半托管 track 管线 | 美股实际数据源 |
|---|---|---|---|---|
| `quote`/`daily` | ✅ | FetcherManager | — | yf/akshare_us + keyed |
| `earnings` | ✅ | **FMP/Finnhub/AlphaVantage** | — | ✅ keyed 已用足 |
| `financials` | ✅ | **FMP/Tiingo/AlphaVantage** | — | ✅ keyed 已用足 |
| `news` | ✅ | **FMP/Finnhub/Tiingo/AlphaVantage** | — | ✅ keyed 已用足 |
| `options` | ✅ | **Polygon** + yfinance 兜底 | — | ✅ keyed 已用足 |
| `institutional` | ✅ 承诺"机构持仓13F" | ❌ **无 provider** | [institutional_pipeline.py:130](../bottleneck_hunter/watchlist/institutional_pipeline.py#L130) | ⚠️ **仅 yfinance** |
| `insider` | ✅ 承诺"内部人交易" | ❌ **无 provider** | [sec_pipeline.py:370](../bottleneck_hunter/watchlist/sec_pipeline.py#L370) SEC EDGAR | ⚠️ **仅 EDGAR/yf** |
| `sec` | ✅ 承诺"SEC/公告" | ❌ 无 provider | sec_pipeline SEC EDGAR | ⚠️ 仅 EDGAR |
| `smartmoney` | ✅ 承诺"聪明钱聚合" | ❌ 无 provider | [smart_money.py:284](../bottleneck_hunter/chain/smart_money.py#L284) | ⚠️ **仅 yfinance** |
| `notice` | ✅ 承诺"交易所公告" | ❌ 无 provider | notice_pipeline | A股向 |

### 一句话诊断

**earnings/financials/news/options 四类：keyed 源已经用得很足**——FMP 免费档吃透（income-statement/analyst-estimates/quote），Finnhub 兜 earnings/news，AlphaVantage 兜 OVERVIEW，Polygon 专供期权。这块没有明显浪费。

**institutional/insider/smartmoney 三类：keyed 源完全没接**——全靠 yfinance 一条腿。yfinance 的 `institutional_holders`/`recommendations`/`insider_transactions` 恰恰是它**最不稳、最易被限流、字段最贫**的接口。这才是"深度没用足"的真正所在。

---

## 1. 真实缺口（只列有收益的）

### U1 — 机构持仓深度：FMP 补 yfinance 13F 🔴 最高

**现状**：[institutional_pipeline.py](../bottleneck_hunter/watchlist/institutional_pipeline.py) 美股机构持仓**只调 yfinance** `t.institutional_holders`（13F top holders）+ `recommendations`（分析师评级）。yfinance 这两个接口是重灾区：
- `institutional_holders` 常返空/被 Yahoo 限流（走 `yf_gate.throttle()` 已是补救）。
- 只有"当前 top holders"，**无季度环比**——而 [decision_engine.py `_holder_qoq`](../bottleneck_hunter/watchlist/decision_engine.py) 和 committee 拥挤度分析要的正是 QoQ 增减方向。

**FMP 能补什么**（已配 Key 用户零成本）：
- `/institutional-ownership/symbol-ownership?symbol=X` — 机构持仓总量 + **上季度对比**（investorsHolding / lastInvestorsHolding / 增减持股数），直接喂 `_holder_qoq`。
- `/institutional-ownership/institutional-holders/symbol-summary` — 13F 汇总，比 yfinance 稳。
- `/grades-consensus` / `/price-target-consensus` — 分析师评级**一致预期**（buy/hold/sell 计数 + 目标价），比 yfinance `recommendations`（经常空）质量高一个数量级。

**改法**：仿 P1 A股写法——`institutional_pipeline` 里加 FMP 优先、yfinance fallback（**不删旧路**）。`track("fmp", CAP_INSTITUTIONAL, "us_stock")`。收益：committee 拥挤度/QoQ 对美股从"yfinance 时有时无"变"FMP 稳定有季度对比"。

### U2 — 内部人交易：Finnhub/FMP 补 EDGAR 解析 🟡 中

**现状**：[sec_pipeline.py](../bottleneck_hunter/watchlist/sec_pipeline.py) 从 SEC EDGAR **手解 Form 4 XML**（`track("sec_edgar", CAP_SEC)`）。EDGAR 免费权威，但 Form 4 XML 解析脆、要限速 10req/s、聚合成"净买卖"要自己算。

**FMP/Finnhub 能补什么**：
- Finnhub `/stock/insider-transactions?symbol=X` — **已聚合**的内部人买卖记录（name/share/change/transactionDate），免 XML 解析。
- FMP `/insider-trading?symbol=X` — 同类，含 transactionType/securitiesTransacted。

**判断**：EDGAR 已能出数，这是**质量/稳定性增强而非填空**。按 YAGNI，**次优先**——除非生产日志显示 EDGAR Form 4 解析失败率高，否则先不动。

### U3 — 期权链深度：Polygon 已接，可扩历史/greeks ⚪ 低

**现状**：[PolygonProvider](../bottleneck_hunter/data_provider/providers.py#L482) 已用 `/v3/snapshot/options`（PCR/OI/notable trades），yfinance 兜底。已相当足。Polygon 还有 greeks/IV/历史合约，但**当前决策链没有消费点**——加了没人读 = YAGNI，不做。

### U4 — FMP 财务深度字段（付费档才有）⚪ 低

FMP `_fetch_financials_sync` 已注明 ratios 多为付费档 → 软失败，roe/负债/现金流留空。这**不是接入问题是订阅档位问题**，代码已优雅降级。用户升 FMP 付费档即自动生效，无需改码。

---

## 2. 优先级与工作量

| ID | 缺口 | 优先级 | 改动面 | 一句话价值 | 建议 |
|---|---|---|---|---|---|
| **U1** | FMP 机构持仓 QoQ + 评级一致预期 | 🔴 最高 | institutional_pipeline 加 FMP 优先分支（~仿 P1，1 源 + fallback） | yfinance 最烂的两个接口换成 FMP 稳定源，喂活 committee QoQ | **值得做** |
| U2 | Finnhub/FMP 内部人聚合 | 🟡 中 | sec_pipeline 加可选源 | EDGAR XML 解析的稳定性增强 | 待生产验证失败率 |
| U3 | Polygon 期权 greeks/历史 | ⚪ 低 | provider + 新消费点 | 无下游消费 | YAGNI 不做 |
| U4 | FMP 付费财务字段 | ⚪ 低 | 无（升订阅档即生效） | roe/现金流填空 | 无需改码 |

---

## 3. 核心结论

1. **earnings/financials/news/options：keyed 源已用足**，无浪费，别动。
2. **真正的"没用足"是 institutional/smartmoney 只挂了 yfinance 一条腿**——而这恰是 yfinance 最不可靠的接口。**U1（FMP 机构持仓）是唯一高收益项**：一个已配 Key 用户零成本、仿 P1 现成写法、直接喂活 committee 的 QoQ 分析。
3. 其余（U2/U3/U4）按 YAGNI 暂不做，标注上升路径。

**若批准，只做 U1**：仿 efinance P1 的 inline+track 模式，在 institutional_pipeline 给美股加 FMP 优先/yfinance fallback，不新建 provider、不新增表、不加依赖。

---

生成时间：2026-08-25
