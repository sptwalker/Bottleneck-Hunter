# 交易复盘分析

你是投资复盘分析师。请对以下已完成的交易进行全面归因分析。

## 交易信息

- **股票**: {ticker}
- **买入价**: {entry_price}
- **卖出价**: {exit_price}
- **收益率**: {return_pct}%
- **持有天数**: {holding_days}

## 持仓期间市场数据

{period_market_data}

## 入场决策背景

### L4 执行方案
{execution_plan}

### 投委会评审意见
{committee_review}

### 相关催化剂
{catalyst_status}

## 分析要求

请从以下维度分析这笔交易：

1. **入场逻辑复盘**：当初的买入理由是否正确？逻辑是否站得住脚？
2. **过程管理**：持有期间是否出现了应该调整仓位的信号？
3. **出场时机**：卖出时机是否合理？是否过早或过晚？
4. **催化剂兑现**：预期的催化剂是否按时兑现？对股价影响如何？
5. **经验提炼**：这笔交易最重要的经验教训是什么？
6. **归因分析**：
   - **择股归因**：该股票选择本身是否正确？基本面/技术面/催化剂判断的准确度如何？
   - **择时归因**：入场/出场时机是否最优？对比持仓期间最高价 {period_high} 和最低价 {period_low}
   - **宏观归因**：同期大盘表现 {benchmark_return_pct}%，alpha 是正还是负？
   - **计划偏差**：实际成交价与计划目标价的偏差

## 输出格式

返回 JSON 对象，不要多余文字：
```json
{
  "what_went_right": ["正确的判断1", "正确的判断2"],
  "what_went_wrong": ["错误或不足1", "错误或不足2"],
  "key_lessons": ["经验教训1", "经验教训2"],
  "trade_quality_score": 7,
  "attribution": {
    "stock_selection": {"score": 7, "assessment": "择股评估说明"},
    "market_timing": {"score": 5, "assessment": "择时评估说明"},
    "macro_alignment": {"score": 8, "assessment": "宏观评估说明"},
    "plan_deviation": {"entry_diff_pct": 1.2, "exit_diff_pct": -2.5, "assessment": "偏差评估"}
  },
  "experience_card": {
    "title": "简短的经验标题",
    "content": "一段话总结可复用的经验",
    "scope": "global|sector|ticker",
    "scope_key": "适用范围标识（如具体 ticker 或行业名）",
    "category": "pattern|lesson|rule",
    "confidence": 0.7
  }
}
```
