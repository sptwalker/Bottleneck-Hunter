# 美股利润表_OpenAPI接口文档
## 一、接口描述

**功能概览**：本接口用于获取 **Gangtise** 美股利润表在标准科目下的结构化数据。

**典型应用场景**：

* 用于评估企业在一定时期内的盈利能力，经营效率和未来发展趋势的核心报表，广泛应用于企业决策、投资分析和融资信贷等场景。

**使用方法**：

* **科目或报表获取**
  通过 `securityCode`、`period` 和 `reportType` 参数进行检索，未指定科目时返回完整利润表。
* **多维高级筛选**
  * **时间维度**：支持通过 `fiscalYear` 过滤，格式必须为完整年份，有具体日期时会覆盖 `fiscalYear` 的筛选，并通过 `startDate` 或 `endDate` 过滤，需严格遵循 `yyyy-MM-dd` 格式传参。
  * **科目筛选**：支持将指定科目填入 `fieldList` 过滤，未指定时默认返回完整利润表。

<br/>

## 二、数据权限范围

不同类型的账号享有不同的历史数据访问范围，具体规则如下：
* **试用账号**：数据权限为当前时间前溯 **3 年**的历史存量数据。
* **正式账号**：数据权限为当前时间前溯 **5 年**的历史存量数据。

<br/>

## 三、OpenAPI 积分消耗

**无积分消耗**

<br/>

## 四、请求说明
### 请求地址
* `https://openapi.gangtise.com/application/open-fundamental/financial-report/income-statement/us`

### 请求方式
* **POST**

### 请求头

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `Authorization` | **String** | accessToken，从 <a href="#/markdown/access-token">【accessToken】</a> 接口获取  |

<br/>

## 五、请求参数

| 参数名 | 必选 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `securityCode` | 是 | **String** | - | 股票代码（如 `"TSLA.O"`） |
| `startDate` | 否 | **String** | - | 开始日期，格式严格为 `yyyy-MM-dd`（有值时会覆盖 `fiscalYear` 的筛选）；若未指定 `endDate`，默认至今 |
| `endDate` | 否 | **String** | - | 结束日期，格式严格为 `yyyy-MM-dd`（有值时会覆盖 `fiscalYear` 的筛选）；若未指定 `startDate`，默认前推三年 |
| `fiscalYear` | 否 | **List\<String\>** | - | 财报年度，格式必须为完整年份。示例：<br>• `["2025"]`<br>• `["2024", "2025", "2026"]` |
| `period` | 否 | **List\<String\>** | `latest` | 报告期：<br>• `q1` - 一季报<br>• `h1` - 上半年报<br>• `q3` - 三季报<br>• `h2` - 下半年报<br>• `nsd` - 不规则跨度<br>• `annual` - 年报<br>• `latest` - 最新一期|
| `reportType` | 否 | **List\<String\>** | `consolidated` | 报表类型：<br>• `consolidated` - 合并报表<br>• `consolidatedRestated` - 合并报表（调整）<br>• `standalone` - 母公司报表<br>• `standaloneRestated` - 母公司报表（调整） |
| `fieldList` | 否 | **List\<String\>** | `[]` | 指定返回字段：<br>• 空数组 `[]`：返回全部字段<br>• 非空数组：只返回指定字段 |

### 请求示例（JSON）

#### 示例 1：提取财报中部分字段

【案例：提取特斯拉 `TSLA.O` 去年中报的营业总收入、营业成本、净利润和基本每股收益指标；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "securityCode": "TSLA.O",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2025"],
  "period": ["h1"],
  "reportType": ["consolidated"],
  "fieldList": [
    "totalOpRev",
    "opCost",
    "netProfit",
    "basicEPS"
  ]
}
```

#### 示例 2：提取多报告期字段

【案例：提取特斯拉 `TSLA.O` 2023-2025 年报的净利润数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "securityCode": "TSLA.O",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2023", "2024", "2025"],
  "period": ["annual"],
  "reportType": ["consolidated"],
  "fieldList": ["netProfit"]
}
```

#### 示例 3：提取完整报表
【案例：提取特斯拉 `TSLA.O` 最新一期的利润表数据；（若当前日期为2026.05.08，且2026年一季度数据已披露）】

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

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `securityCode` | **String** | 股票代码（固定字段） |
| `companyName` | **String** | 股票中文名称（固定字段） |
| `category` | **String** | 公告来源（固定字段） |
| `announcementDate` | **String** | 信息发布日期（固定字段） |
| `startDate` | **String** | 财报开始日期（固定字段） |
| `endDate` | **String** | 财报截止日期（固定字段） |
| `fiscalYear` | **String** | 财报年（固定字段） |
| `timeCovered` | **String** | 时间跨度（海外股票涉及不规则时间跨度，如年报跨度15个月）（固定字段） |
| `period` | **String** | 报告期（固定字段） |
| `reportType` | **String** | 报表类型（固定字段） |
| `companyType` | **String** | 企业报表格式：<br>• `1` - 一般企业<br>• `2` - 银行<br>• `3` - 保险公司<br>• `4` - 证券公司<br>• `5` - REIT（固定字段） |
| `currency` | **String** | 币种（固定字段） |
| `unit` | **String** | 单位（固定字段） |
| `totalOpRev` | **Double** | **营业总收入** |
| ↳ `opRev` | **Double** | 营业收入 |
| ↳ `premium` | **Double** | 保费 |
| ↳ `netInvIncome` | **Double** | 净投资收入 |
| ↳ `realizedNetInvGain` | **Double** | 已实现净投资收益  |
| ↳ `policyFeesCharges` | **Double** | 保单费用及手续费  |
| ↳ `invBanking` | **Double** | 投资银行  |
| ↳ `assetMgmtSecBiz` | **Double** | 资产管理和证券业务  |
| ↳ `netIntIncome` | **Double** | 利息收入净额   |
| ↳↳ `totIntIncome` | **Double** | 利息收入总计  |
| ↳↳↳ `loanLeaseInt` | **Double** | 贷款与租赁  |
| ↳↳↳ `securitiesInt` | **Double** | 证券  |
| ↳↳↳ `tradingAssetsInt` | **Double** | 交易性资产  |
| ↳↳↳ `intIncomeSpecItems` | **Double** | 利息收入特殊科目  |
| ↳↳ `totIntExp` | **Double** | 利息支出总计  |
| ↳↳↳ `depositInt` | **Double** | 存款  |
| ↳↳↳ `shortTermBorrowInt` | **Double** | 短期借款  |
| ↳↳↳ `longTermDebtInt` | **Double** | 长期债务  |
| ↳↳↳ `tradingLiabInt` | **Double** | 交易性负债  |
| ↳↳↳ `intExpSpecItems` | **Double** | 利息支出特殊科目  |
| ↳ `totNonIntIncome` | **Double** | 非利息收入总计  |
| ↳↳ `invBrokerageFees` | **Double** | 投资和经纪费用  |
| ↳↳ `creditCardIncome` | **Double** | 信用卡收入  |
| ↳↳ `serviceFees` | **Double** | 服务费  |
| ↳↳ `netGainInvSecurities` | **Double** | 投资证券净收益  |
| ↳↳ `insContractIncome` | **Double** | 保险合同收益  |
| ↳↳ `tradingActivityGain` | **Double** | 交易性活动收益  |
| ↳↳ `nonIntIncomeSpecItems` | **Double** | 非利息收入特殊科目  |
| ↳ `otherIncome` | **Double** | 其他收入  |
| ↳ `revSpecItems` | **Double** | 收入特殊科目  |
| `totOpCost` | **Double** | **营业总成本** |
| ↳ `opCost` | **Double** | 营业成本  |
| ↳ `totOpExp` | **Double** | 营业支出合计 |
| ↳↳ `netIntExp` | **Double** | 净利息支出 |
| ↳↳↳ `intExp` | **Double** | 利息支出（适用企业类型：一般企业、银行、证券公司、REIT）  |
| ↳↳↳ `intIncome` | **Double** | 利息收入 |
| ↳↳ `opExpSpecItems` | **Double** | 营业支出特殊科目 |
| ↳ `totExp` | **Double** | 费用合计 |
| ↳↳ `sgAndAdminExp` | **Double** | 销售及一般管理费用  |
| ↳↳ `rdExp` | **Double** | 研发费用  |
| ↳↳ `opExpense` | **Double** | 营业费用 |
| ↳↳ `intdepreciationAmort` | **Double** | 折旧及摊销 |
| ↳↳ `totExpSpecItems` | **Double** | 费用合计特殊科目 |
| ↳ `totClaimExp` | **Double** | 赔款及费用合计 |
| ↳↳ `lossAdjExp` | **Double** | 损失及损失调整费用 |
| ↳↳ `policyholderBenefits` | **Double** | 保单持有人利益 |
| ↳↳ `intExp` | **Double** | 利息支出（适用企业类型：保险公司）  |
| ↳↳ `claimExpSpecItems` | **Double** |    赔款及费用特殊科目 |
| ↳ `totNonIntExp` | **Double** | 非利息支出总计 |
| ↳↳ `compBenefitsSec` | **Double** | 薪酬和福利-证券 |
| ↳↳ `marketingPromo` | **Double** | 营销及市场推广 |
| ↳↳ `compBenefitsBank` | **Double** | 薪酬和福利-银行 |
| ↳↳ `infoSoftEquip` | **Double** | 信息、软件及设备 |
| ↳↳ `netLeaseExp` | **Double** |    净租赁费用 |
| ↳↳ `nonIntExpSpecItems` | **Double** | 非利息支出特殊科目 |
| ↳ `creditLossProvision` | **Double** | 信贷亏损准备金 |
| `pretaxProfitContOps` | **Double** | **持续经营业务税前利润** |
| ↳ `opProfit` | **Double** | 营业利润 |
| ↳ `assocCoEquityEarnings` | **Double** | 联营公司股权收益 |
| ↳ `pretaxProfSpecItems` | **Double** | 持续经营业务税前利润特殊科目 |
| `incomeTax` | **Double** | **减：所得税** |
| `profitAfterTaxContOps` | **Double** | **持续经营业务税后利润** |
| `discOpsProfit` | **Double** | **非持续性营业利润** |
| `netProfitSpecItems` | **Double** | **净利润特殊科目** |
| `netProfit` | **Double** | **净利润** |
| ↳ `prefShareDiv` | **Double** | 优先股股息 |
| ↳ `netProfitMinority` | **Double** | 归属于少数股东的净利润 |
| ↳ `netProfitParent` | **Double** | 归属于母公司股东的净利润 |
| ↳ `netProfitParentSpecItems` | **Double** | 归属于母公司股东的净利润特殊科目 |
| `otherCompIncome` | **Double** | **其他综合收益的税后净额** |
| `totalCompIncome` | **Double** | **综合收益总额** |
| ↳ `compIncMinority` | **Double** | 归属于少数股东的综合收益 |
| ↳ `compIncParent` | **Double** | 归属于母公司股东的综合收益 |
| `basicEPS` | **Double** | 基本每股收益 |
| ↳ `basicEPSContOps` | **Double** | 持续运营基本每股收益 |
| ↳ `basicEPSDiscOps` | **Double** | 非持续运营基本每股收益 |
| `dilutedEPS` | **Double** | 稀释每股收益 |
| ↳ `dilutedEPSContOps` | **Double** | 持续运营稀释每股收益 |
| ↳ `dilutedEPSDiscOps` | **Double** | 非持续运营稀释每股收益 |
| **补充科目** | - | - |
| `grossProfit` | **Double** | 毛利（公式：毛利=营业总收入-营业成本） |
| `prefProfitOther` | **Double** | 优先股净利润和其他项（公式：优先股净利润和其他项=优先股股息+归属于母公司股东的净利润特殊科目） |


### 返回示例（JSON）

#### 示例 1：提取财报中部分字段

【案例：提取特斯拉 `TSLA.O` 去年中报的营业总收入、营业成本、净利润和基本每股收益指标；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

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
    "startDate",
    "endDate",
    "fiscalYear",
    "timeCovered",
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "totalOpRev",
    "opCost",
    "netProfit",
    "basicEPS"
  ],
  "list": [
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2025/10-Q",
      "2025-07-24",
      "2025-01-01",
      "2025-06-30",
      "2025",
      "6个月",
      "h1",
      "consolidated",
      "1",
      "USD",
      "元",
      41831000000.00,
      34800000000.00,
      1610000000.00,
      0.49
    ]
  ]
}
```

#### 示例 2：提取多报告期字段

【案例：提取特斯拉 `TSLA.O` 2023-2025 年报的净利润数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

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
    "startDate",
    "endDate",
    "fiscalYear",
    "timeCovered",
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "netProfit"
  ],
  "list": [
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2023/10-K",
      "2024-01-29",
      "2023-01-01",
      "2023-12-31",
      "2023",
      "12个月",
      "annual",
      "consolidated",
      "1",
      "USD",
      "元",
      14974000000.00
    ],
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2024/10-K",
      "2025-01-30",
      "2024-01-01",
      "2024-12-31",
      "2024",
      "12个月",
      "annual",
      "consolidated",
      "1",
      "USD",
      "元",
      7153000000.00
    ],
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2025/10-K",
      "2026-01-29",
      "2025-01-01",
      "2025-12-31",
      "2025",
      "12个月",
      "annual",
      "consolidated",
      "1",
      "USD",
      "元",
      3855000000.00
    ]
  ]
}
```

#### 示例 3：提取完整报表

【案例：提取特斯拉 `TSLA.O` 最新一期的利润表数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

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
    "startDate",
    "endDate",
    "fiscalYear",
    "timeCovered",
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "totalOpRev",
    "opRev",
    "opCost",
    "totOpExp",
    "netIntExp",
    "intExp",
    "intIncome",
    "sgAndAdminExp",
    "rdExp",
    "pretaxProfitContOps",
    "opProfit",
    "pretaxProfSpecItems",
    "incomeTax",
    "profitAfterTaxContOps",
    "netProfit",
    "netProfitMinority",
    "netProfitParent",
    "otherCompIncome",
    "totalCompIncome",
    "compIncMinority",
    "compIncParent",
    "basicEPS",
    "basicEPSContOps",
    "dilutedEPS",
    "dilutedEPSContOps",
    "grossProfit"
  ],
  "list": [
    [
      "TSLA.O",
      "特斯拉",
      "TSLA 2026/10-Q",
      "2026-04-23",
      "2026-01-01",
      "2026-03-31",
      "2026",
      "3个月",
      "q1",
      "consolidated",
      "1",
      "USD",
      "元",
      22387000000.00,
      22387000000.00,
      17667000000.00,
      3437000000.00,
      -342000000.00,
      92000000.00,
      434000000.00,
      1833000000.00,
      1946000000.00,
      748000000.00,
      1283000000.00,
      -535000000.00,
      257000000.00,
      491000000.00,
      491000000.00,
      14000000.00,
      477000000.00,
      -27000000.00,
      464000000.00,
      14000000.00,
      450000000.00,
      0.15,
      0.15,
      0.13,
      0.13,
      4720000000.00
    ]
  ]
}
```

## 七、特殊说明：

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
