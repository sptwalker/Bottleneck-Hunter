# 美股资产负债表_OpenAPI接口文档
## 一、接口描述

**功能概览**：本接口用于获取 **Gangtise** 美股资产负债表在标准科目下的结构化数据。

**典型应用场景**：

* 用于展示企业在特定时点的财务状况（资产、负债和所有者权益结构）、评估偿债能力和财务稳定性的核心报表，广泛应用于企业财务健康诊断、投融资决策和风险管控等场景。

**使用方法**：

* **科目或报表获取**
  通过 `securityCode`、`period` 和 `reportType` 参数进行检索，未指定科目时返回完整资产负债表。
* **多维高级筛选**
  * **时间维度**：支持通过 `fiscalYear` 过滤，格式必须为完整年份，有具体日期时会覆盖 `fiscalYear` 的筛选，并通过 `startDate` 或 `endDate` 过滤，需严格遵循 `yyyy-MM-dd` 格式传参。
  * **科目筛选**：支持将指定科目填入 `fieldList` 过滤，未指定时默认返回完整资产负债表。

<br/>

## 二、数据权限范围

不同类型的账号享有不同的历史数据访问范围，具体规则如下：
* **试用账号**：数据权限为当前时间前溯 **3 年**的历史存量数据。
* **正式账号**：数据权限为当前时间前溯 **5 年**的历史存量数据。

<br/>

## 三、OpenAPI 积分消耗

调用该接口不消耗积分。

<br/>

## 四、请求说明
### 请求地址
* `https://openapi.gangtise.com/application/open-fundamental/financial-report/balance-sheet/us`

### 请求方式
* **POST**

### 请求头

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `Authorization` | **String** | accessToken，从 <a href="#/markdown/access-token">【accessToken】</a> 接口获取 |

<br/>

## 五、请求参数

| 参数名 | 必选 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `securityCode` | 是 | **String** | - | 股票代码（如 `"TSLA.O"`） |
| `startDate` | 否 | **String** | - | 开始日期，格式严格为 `yyyy-MM-dd`（有值时会覆盖 `fiscalYear` 的筛选）；若未指定 `endDate`，默认至今 |
| `endDate` | 否 | **String** | - | 结束日期，格式严格为 `yyyy-MM-dd`（有值时会覆盖 `fiscalYear` 的筛选）；若未指定 `startDate`，默认前推三年 |
| `fiscalYear` | 否 | **List\<String\>** | - | 财报年度，格式必须为完整年份。示例：<br>• `["2025"]`<br>• `["2024", "2025", "2026"]` |
| `period` | 否 | **List\<String\>** | `latest` | 报告期：<br>• `q1` - 一季报<br>• `h1` - 上半年报<br>• `q3` - 三季报<br>• `nsd` - 不规则跨度<br>• `annual` - 年报<br>• `latest` - 最新一期 |
| `reportType` | 否 | **List\<String\>** | `consolidated` | 报表类型：<br>• `consolidated` - 合并报表<br>• `consolidatedRestated` - 合并报表（调整）<br>• `standalone` - 母公司报表<br>• `standaloneRestated` - 母公司报表（调整） |
| `fieldList` | 否 | **List\<String\>** | `[]` | 指定返回字段：<br>• 空数组 `[]`：返回全部字段<br>• 非空数组：只返回指定字段 |

### 请求示例（JSON）

#### 示例 1：提取财报中部分字段

【案例：提取特斯拉 `TSLA.O` 去年中报的流动资产、非流动资产、流动负债和非流动负债的合计值指标；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "securityCode": "TSLA.O",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2025"],
  "period": ["h1"],
  "reportType": ["consolidated"],
  "fieldList": [
   "totalCurrAssets",
   "totalNonCurrAssets",
   "totalCurrLiab",
   "totalNonCurrLiab"
  ]
}
```

#### 示例 2：提取多报告期字段

【案例：提取特斯拉 `TSLA.O` 2023-2025 年报的所有者权益合计数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "securityCode": "TSLA.O",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2023", "2024", "2025"],
  "period": ["annual"],
  "reportType": ["consolidated"],
  "fieldList": ["totalEquity"]
}
```

#### 示例 3：提取完整报表

【案例：提取特斯拉 `TSLA.O` 最新一期的资产负债表数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "securityCode": "TSLA.O",
  "startDate": null,
  "endDate": null,
  "fiscalYear": null,
  "period": ["latest"],
  "reportType": ["consolidated"],
  "fieldList": []
}
```

## 六、返回参数

### 顶层返回结构

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `code` | **String** | 响应码，`000000` 表示成功 |
| `message` | **String** | 响应消息 |
| `status` | **Boolean** | 请求是否成功 |
| `fieldList` | **List\<String\>** | 数据列表字段名，包含固定字段（`securityCode`、`companyName` 等）和请求的财务报表科目字段，按传入的 `fieldList` 顺序排列 |
| `list` | **List\<Object\>** | 财务报表数据，每行按 `fieldList` 顺序排列，每个内层数组对应一条报告期记录 |

### 数据字段明细

> 返回参数中未标注 ↳ 的为一级节点科目；标注一个 ↳ 为二级节点；标注两个 ↳ 为三级节点。

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `securityCode` | **String** | 股票代码（固定字段） |
| `companyName` | **String** | 股票中文名称（固定字段） |
| `category` | **String** | 公告类型（招股说明书、年度报告等）（固定字段） |
| `announcementDate` | **String** | 信息发布日期（固定字段） |
| `endDate` | **String** | 财报截止日期（固定字段） |
| `fiscalYear` | **String** | 财报年（固定字段） |
| `timeCovered` | **String** | 时间跨度（海外股票涉及不规则时间跨度，如年报跨度15个月）（固定字段） |
| `period` | **String** | 报告期（固定字段） |
| `reportType` | **String** | 报表类型（固定字段） |
| `companyType` | **String** | 企业报表格式（固定字段）：<br>• `1` - 一般企业<br>• `2` - 银行<br>• `3` - 保险公司<br>• `4` - 证券公司<br>• `5` - REIT<br>• `6` - 其他 |
| `currency` | **String** | 币种（固定字段） |
| `unit` | **String** | 单位（固定字段） |
| `currAssets` | **Double** | **流动资产** |
| ↳ `totalCash` | **Double** | 总现金 |
| ↳↳ `cashEquivalents` | **Double** | 现金及现金等价物 |
| ↳↳ `shortTermInv` | **Double** | 短期投资 |
| ↳ `cashDepInterbank` | **Double** | 现金及存放同业款项 |
| ↳ `bankDeposits` | **Double** | 银行存款 |
| ↳ `tradingSecurities` | **Double** | 交易性证券 |
| ↳ `tradingAssets` | **Double** | 交易性金融资产 |
| ↳ `fedFundsSoldAndReverseRepo` | **Double** | 联邦基金出售及买入返售 |
| ↳ `securitiesBorrowed` | **Double** | 借入证券 |
| ↳ `accountsReceivable` | **Double** | 应收账款 |
| ↳ `netAccountsReceivable` | **Double** | 应收账款净值 |
| ↳ `recvInvestmentIncome` | **Double** | 应收投资收益 |
| ↳ `reinsuranceAssets` | **Double** | 再保险资产 |
| ↳ `premiumsOtherRecv` | **Double** | 保险费及其他应收账款 |
| ↳ `inventory` | **Double** | 存货 |
| ↳ `advPay` | **Double** | 预付款项 |
| ↳ `deferredTaxAssetsCurr` | **Double** | 延递所得税资产-流动资产 |
| ↳ `specItemsCurrAssets` | **Double** | 流动资产特殊科目 |
| ↳ `totalCurrAssets` | **Double** | 流动资产合计 |
| `nonCurrAssets` | **Double** | **非流动资产** |
| ↳ `netLoans` | **Double** | 贷款净值 |
| ↳↳ `loans` | **Double** | 贷款 |
| ↳↳ `loanLossProvisions` | **Double** | 减：贷款损失准备金 |
| ↳ `debtSecuritiesAssets` | **Double** | 债务证券资产 |
| ↳ `mortgageAssets` | **Double** | 按揭(抵押)资产 |
| ↳ `equityOtherInvestments` | **Double** | 股权和其他投资 |
| ↳ `equitySecurities` | **Double** | 权益证券 |
| ↳ `heldToMaturitySec` | **Double** | 持至到期证券 |
| ↳ `totalInvestments` | **Double** | 总投资 |
| ↳↳ `shortTermInv` | **Double** | 短期投资（该层级适用于金融公司，不区分流动和非流动资产） |
| ↳↳ `otherInvestments` | **Double** | 其他投资 |
| ↳ `netPpe` | **Double** | 物业、厂房和设备净值 |
| ↳↳ `grossPpe` | **Double** | 物业、厂房和设备总值 |
| ↳↳ `accumDepreciation` | **Double** | 减：累计折旧 |
| ↳ `accumAmortization` | **Double** | 累计摊销 |
| ↳ `netIntangibleAssets` | **Double** | 无形资产净值 |
| ↳ `goodwill` | **Double** | 商誉 |
| ↳ `sepAcctAssets` | **Double** | 独立账户资产 |
| ↳ `defPolAcqCost` | **Double** | 递延保单取得成本 |
| ↳ `defTaxRevenue` | **Double** | 递延税收入 |
| ↳ `defTaxAssetsNonCurr` | **Double** | 递延所得税资产-非流动资产 |
| ↳ `specNonCurrAssets` | **Double** | 非流动资产特殊科目 |
| ↳ `totalNonCurrAssets` | **Double** | 非流动资产合计 |
| `otherAssets` | **Double** | **其他资产（其他）** |
| ↳ `specItemsAssets` | **Double** | 资产特殊科目 |
| `totalAssets` | **Double** | **总资产** |
| `currLiab` | **Double** | **流动负债** |
| ↳ `shortTermDebt` | **Double** | 短期债务 |
| ↳ `stBorrowings` | **Double** | 短期借款 |
| ↳ `deposits` | **Double** | 存款 |
| ↳ `accountsPayable` | **Double** | 应付账款 |
| ↳ `payables` | **Double** | 应付款项 |
| ↳ `incomeTaxPayable` | **Double** | 应缴所得税 |
| ↳ `taxesPayable` | **Double** | 应付税款 |
| ↳ `accruedLiabilities` | **Double** | 应计负债 |
| ↳ `tradingLiabilities` | **Double** | 交易性负债 |
| ↳ `fedFundsPurchased` | **Double** | 联邦基金购入 |
| ↳ `reinsuranceLiabilities` | **Double** | 再保险负债 |
| ↳ `deferredRevenueCurr` | **Double** | 递延收入-流动负债 |
| ↳ `specCurrLiab` | **Double** | 流动负债特殊科目 |
| ↳ `totalCurrLiab` | **Double** | 流动负债合计 |
| `nonCurrLiab` | **Double** | **非流动负债** |
| ↳ `longTermDebt` | **Double** | 长期债务 |
| ↳ `futurePolicyBenefits` | **Double** | 未来政策效益 |
| ↳ `policyholderFunds` | **Double** | 投保人基金 |
| ↳ `unearnedPremiums` | **Double** | 未赚保费 |
| ↳ `separateAccountLiab` | **Double** | 独立账户负债 |
| ↳ `deferredTaxLiab` | **Double** | 递延所得税负债 |
| ↳ `deferredRevenueNonCurr` | **Double** | 递延收入-非流动负债 |
| ↳ `specNonCurrLiab` | **Double** | 非流动负债特殊科目 |
| ↳ `totalNonCurrLiab` | **Double** | 非流动负债合计 |
| `otherLiab` | **Double** | **其他负债（其他）** |
| ↳ `specItemsLiab` | **Double** | 总负债特殊科目 |
| `totalLiab` | **Double** | **总负债** |
| `equity` | **Double** | **所有者权益（或股东权益）** |
| ↳↳ `preferredStock` | **Double** | 优先股 |
| ↳↳ `commonStock` | **Double** | 普通股 |
| ↳↳ `additionalPaidInCapital` | **Double** | 额外实收资本 |
| ↳↳ `retainedEarnings` | **Double** | 未分配利润 |
| ↳↳ `treasuryStock` | **Double** | 库存股 |
| ↳↳ `accumulatedOci` | **Double** | 累计其他综合性收益 |
| ↳↳ `specParentEq` | **Double** | 归属母公司股东权益特殊科目 |
| ↳ `totalParentEq` | **Double** | 归属母公司股东权益合计值 |
| ↳ `nonControllingInterests` | **Double** | 少数股东权益 |
| ↳ `specTotalEq` | **Double** | 股东权益合计特殊科目 |
| `totalEquity` | **Double** | **所有者权益（或股东权益）合计** |

### 返回示例（JSON）

#### 示例 1：提取财报中部分字段

【案例：提取特斯拉 `TSLA.O` 去年中报的流动资产、非流动资产、流动负债和非流动负债的合计值指标；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "code": "000000",
  "message": "请求成功",
  "status": true,
  "fieldList": [
    "securityCode",
    "companyName",
    "category",
    "announcementDate",
    "endDate",
    "fiscalYear",
    "timeCovered",
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "totalCurrAssets",
    "totalNonCurrAssets",
    "totalCurrLiab",
    "totalNonCurrLiab"
  ],
  "list": [
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2025/10-Q",
      "2025-07-24",
      "2025-06-30",
      "2025",
      "6个月",
      "h1",
      "consolidated",
      "1",
      "USD",
      "元",
      61133000000.00,
      67434000000.00,
      30008000000.00,
      20487000000.00
    ]
  ]
}
```

#### 示例 2：提取多报告期字段

【案例：提取特斯拉 `TSLA.O` 2023-2025 年报的所有者权益合计数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "code": "000000",
  "message": "请求成功",
  "status": true,
  "fieldList": [
    "securityCode",
    "companyName",
    "category",
    "announcementDate",
    "endDate",
    "fiscalYear",
    "timeCovered",
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "totalEquity"
  ],
  "list": [
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2023/10-K",
      "2024-01-29",
      "2023-12-31",
      "2023",
      "12个月",
      "annual",
      "consolidated",
      "1",
      "USD",
      "元",
      63609000000.00
    ],
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2024/10-K",
      "2025-01-30",
      "2024-12-31",
      "2024",
      "12个月",
      "annual",
      "consolidated",
      "1",
      "USD",
      "元",
      73680000000.00
    ],
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2025/10-K",
      "2026-01-29",
      "2025-12-31",
      "2025",
      "12个月",
      "annual",
      "consolidated",
      "1",
      "USD",
      "元",
      82865000000.00
    ]
  ]
}
```

#### 示例 3：提取完整报表

【案例：提取特斯拉 `TSLA.O` 最新一期的资产负债表数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "code": "000000",
  "message": "请求成功",
  "status": true,
  "fieldList": [
    "securityCode",
    "companyName",
    "category",
    "announcementDate",
    "endDate",
    "fiscalYear",
    "timeCovered",
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "totalCash",
    "cashEquivalents",
    "shortTermInv",
    "accountsReceivable",
    "inventory",
    "advPay",
    "totalCurrAssets",
    "netPpe",
    "grossPpe",
    "accumDepreciation",
    "netIntangibleAssets",
    "defTaxAssetsNonCurr",
    "specNonCurrAssets",
    "totalNonCurrAssets",
    "totalAssets",
    "shortTermDebt",
    "accountsPayable",
    "accruedLiabilities",
    "deferredRevenueCurr",
    "totalCurrLiab",
    "longTermDebt",
    "deferredRevenueNonCurr",
    "specNonCurrLiab",
    "totalNonCurrLiab",
    "totalLiab",
    "commonStock",
    "additionalPaidInCapital",
    "retainedEarnings",
    "accumulatedOci",
    "totalParentEq",
    "nonControllingInterests",
    "specTotalEq"
  ],
  "list": [
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2026/10-Q",
      "2026-03-25",
      "2026-03-31",
      "2026",
      "3个月",
      "q1",
      "consolidated",
      "1",
      "USD",
      "元",
      44743000000.00,
      16603000000.00,
      28140000000.00,
      3959000000.00,
      14434000000.00,
      6612000000.00,
      69748000000.00,
      43213000000.00,
      64564000000.00,
      21351000000.00,
      786000000.00,
      7060000000.00,
      22917000000.00,
      73976000000.00,
      143724000000.00,
      1447000000.00,
      14696000000.00,
      14554000000.00,
      3441000000.00,
      34138000000.00,
      7782000000.00,
      3847000000.00,
      13155000000.00,
      24784000000.00,
      58922000000.00,
      3000000.00,
      44299000000.00,
      39480000000.00,
      334000000.00,
      84116000000.00,
      686000000.00,
      84802000000.00
    ]
  ]
}
```

## 七、特殊说明

1. **报表类型**：请求参数默认填入 `consolidated` - 合并报表，若需要合并报表（调整）的数值，请在入参时填入。具体说明如下：
   * `consolidated` - 合并报表：上市公司第一次发布的原始报表
   * `consolidatedRestated` - 合并报表（调整）：上市公司在最新公布的财报中，针对上年同期的合并报表数据进行调整，以反映修订后的最新数据
   * `standalone` - 母公司报表：上市公司所属集团母公司单独编制的财务报表数据
   * `standaloneRestated` - 母公司报表（调整）：上市公司所属集团母公司在最新公布的报表中，对上年度母公司财务报表数据进行调整后的修订数据

2. **报表数值说明**：所有数值保留两位小数。

3. **报表科目说明**：
   * 返回参数中未标注 ↳ 的是指一级节点科目，标注一个 ↳ 指二级节点，标注两个 ↳ 指三级节点；
   * 取完整报表时：自动过滤空值科目，最终返回有数值的科目；若多个报告期中只有一期有数值，其他报告期无数值，则都不过滤。
   * 取指定科目时：返回全部指定科目。