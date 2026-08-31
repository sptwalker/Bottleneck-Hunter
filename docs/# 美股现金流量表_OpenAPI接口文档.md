# 美股现金流量表_OpenAPI接口文档
## 一、接口描述

**功能概览**：本接口用于获取 **Gangtise** 美股现金流量表在标准科目下的结构化数据。

**典型应用场景**：

* 用于评估企业在一定期间内现金流入流出情况、揭示真实偿债能力和运营健康度的核心报表，广泛应用于企业流动性管理、投资评估和风险预警等场景。

**使用方法**：

* **科目或报表获取**
  通过 `securityCode`、`period` 和 `reportType` 参数进行检索，未指定科目时返回完整现金流量表。
* **多维高级筛选**
  * **时间维度**：支持通过 `fiscalYear` 过滤，格式必须为完整年份，有具体日期时会覆盖 `fiscalYear` 的筛选，并通过 `startDate` 或 `endDate` 过滤，需严格遵循 `yyyy-MM-dd` 格式传参。
  * **科目筛选**：支持将指定科目填入 `fieldList` 过滤，未指定时默认返回完整现金流量表。

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
* `https://openapi.gangtise.com/application/open-fundamental/financial-report/cash-flow-statement/us`

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
| `period` | 否 | **List\<String\>** | `latest` | 报告期：<br>• `q1` - 一季报<br>• `h1` - 上半年报<br>• `q3` - 三季报<br>• `h2` - 下半年报<br>• `nsd` - 不规则跨度<br>• `annual` - 年报<br>• `latest` - 最新一期 |
| `reportType` | 否 | **List\<String\>** | `consolidated` | 报表类型：<br>• `consolidated` - 合并报表<br>• `consolidatedRestated` - 合并报表（调整）<br>• `standalone` - 母公司报表<br>• `standaloneRestated` - 母公司报表（调整） |
| `fieldList` | 否 | **List\<String\>** | `[]` | 指定返回字段：<br>• 空数组 `[]`：返回全部字段<br>• 非空数组：只返回指定字段 |

### 请求示例（JSON）

#### 示例 1：提取财报中部分字段

【案例：提取特斯拉 `TSLA.O` 去年中报的经营、投资、筹资活动现金流量净额指标；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "securityCode": "TSLA.O",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2025"],
  "period": ["h1"],
  "reportType": ["consolidated"],
  "fieldList": [
    "netOpCashFlows",
    "netInvCashFlows",
    "netFinCashFlows"
  ]
}
```

#### 示例 2：提取多报告期字段

【案例：提取特斯拉 `TSLA.O` 2023-2025 年报的筹资活动现金流量净额数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

```json
{
  "securityCode": "TSLA.O",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2023", "2024", "2025"],
  "period": ["annual"],
  "reportType": ["consolidated"],
  "fieldList": ["netFinCashFlows"]
}
```

#### 示例 3：提取完整报表

【案例：提取特斯拉 `TSLA.O` 最新一期的现金流量表数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

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
| `startDate` | **String** | 财报开始日期（固定字段） |
| `endDate` | **String** | 财报截止日期（固定字段） |
| `fiscalYear` | **String** | 财报年（固定字段） |
| `timeCovered` | **String** | 时间跨度（海外股票涉及不规则时间跨度，如年报跨度15个月）（固定字段） |
| `period` | **String** | 报告期（固定字段） |
| `reportType` | **String** | 报表类型（固定字段） |
| `companyType` | **String** | 企业报表格式（固定字段）：<br>• `1` - 一般企业<br>• `2` - 银行<br>• `3` - 保险公司<br>• `4` - 证券公司<br>• `5` - REIT<br>• `6` - 其他 |
| `currency` | **String** | 币种（固定字段） |
| `unit` | **String** | 单位（固定字段） |
| `netOpCashFlows` | **Double** | **经营活动产生的现金流量净额** |
| ↳ `deprAmort` | **Double** | 折旧及摊销 |
| ↳ `wcChange` | **Double** | 营运资金变动 |
| `netInvCashFlows` | **Double** | **投资活动产生的现金净额** |
| ↳ `capex` | **Double** | 资本性支出 |
| ↳ `invPurchase` | **Double** | 投资购买 |
| `netFinCashFlows` | **Double** | **筹资活动产生的现金流量净额** |
| ↳ `stockIssuance` | **Double** | 股票发行 |
| ↳ `shareBuyback` | **Double** | 股份回购 |
| ↳ `divPaid` | **Double** | 支付股息 |
| `fxEffectOnCash` | **Double** | **汇率变动对现金及现金等价物的影响** |
| `specItemsEffectNetIncCash` | **Double** | **现金及现金等价物净增加额特殊科目** |
| `cashEquivalentsIncrease` | **Double** | **现金及现金等价物净增加额** |
| `openingCashBalance` | **Double** | **期初现金及现金等价物余额** |
| `cashEndSpecItems` | **Double** | **期末现金及现金等价物余额特殊科目** |
| `closingCashBalance` | **Double** | **期末现金及现金等价物余额** |

### 返回示例（JSON）

#### 示例 1：提取财报中部分字段

【案例：提取特斯拉 `TSLA.O` 去年中报的经营、投资、筹资活动现金流量净额指标；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

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
    "netOpCashFlows",
    "netInvCashFlows",
    "netFinCashFlows"
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
      4696000000.00,
      -4595000000.00,
      -554000000.00
    ]
  ]
}
```

#### 示例 2：提取多报告期字段

【案例：提取特斯拉 `TSLA.O` 2023-2025 年报的筹资活动现金流量净额数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

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
    "netFinCashFlows"
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
      2589000000.00
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
      3853000000.00
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
      1139000000.00
    ]
  ]
}
```

#### 示例 3：提取完整报表

【案例：提取特斯拉 `TSLA.O` 最新一期的现金流量表数据；（若当前日期为 2026.05.08，且 2026 年一季度数据已披露）】

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
    "netOpCashFlows",
    "deprAmort",
    "wcChange",
    "netInvCashFlows",
    "capex",
    "invPurchase",
    "netFinCashFlows",
    "stockIssuance",
    "fxEffectOnCash",
    "cashEquivalentsIncrease",
    "openingCashBalance",
    "closingCashBalance"
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
      3937000000.00,
      1590000000.00,
      375000000.00,
      5023000000.00,
      -2493000000.00,
      10320000000.00,
      1172000000.00,
      361000000.00,
      47000000.00,
      39000000.00,
      17616000000,
      17655000000
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