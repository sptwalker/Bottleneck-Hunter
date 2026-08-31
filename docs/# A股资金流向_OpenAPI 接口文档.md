# A股资金流向_OpenAPI 接口文档

## 一、接口描述

**功能概览**：本接口用于获取 **Gangtise** 金融市场个股的日资金流向数据，支持 **A 股**，数据范围覆盖上交所（.SH）、深交所（.SZ）及北交所（.BJ），包含小单、中单、大单、特大单的流入与流出金额及其在总流入/总流出中的占比，以及主力净流入等字段。**本接口仅提供历史资金流向数据，不提供实时数据。**

**典型应用场景**：

* 适用于监控主力资金动向、判断市场情绪、辅助短线交易决策、识别资金吸筹或出货行为，帮助专业投资者进行资金面分析。

**使用方法**：

* **按证券筛选**
  通过 `securityList` 参数指定证券代码列表（A 股格式如 `"000001.SZ"`、`"872931.BJ"`）。
* **按市场查询**
  传入 `["aShares"]` 返回全部 A 股。
* **按日期查询**
  通过 `startDate` 与 `endDate` 参数过滤日期区间，格式严格为 `yyyy-MM-dd`。
* **指定返回字段**
  通过 `fieldList` 参数指定需要返回的字段（`securityCode` 和 `tradeDate` 默认返回，无需指定），不指定则返回全部字段。

<br/>

## 二、数据权限范围

* **试用账号**与**正式账号**均支持查询全市场个股资金流向数据，数据范围覆盖上交所（.SH）、深交所（.SZ）、北交所（.BJ）。
* **数据权限长度**：
  * **试用账号**：当前时间前溯 **3 年**的历史存量数据。
  * **正式账号**：当前时间前溯 **5 年**的历史存量数据。
* **数据入库时间**：交易日当日数据一般在 **16:30 左右**完成入库，入库后方可查询。

<br/>

## 三、OpenAPI 积分消耗

**无积分消耗**

<br/>

## 四、请求说明

### 请求地址

* `https://openapi.gangtise.com/application/open-quote/fund-flow/daily`

### 请求方式

* **POST**

### 请求头
| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `Authorization` | **String** | accessToken，从<a href="#/markdown/access-token">【accessToken】</a> 接口获取  |

<br/>

## 五、请求参数

| 参数名 | 必选 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `securityList` | 是 | **List\<String\>** | - | • 证券代码列表或市场标识。<br>• 批量查询：传入具体证券代码，格式如 `["000001.SZ", "872931.BJ"]`。<br>• 传入 `["aShares"]` 返回全部 A 股 |
| `startDate` | 否 | **String** | endDate 往前一年 | 开始日期，格式严格为 `yyyy-MM-dd` |
| `endDate` | 否 | **String** | 最新已入库交易日 | 结束日期，格式严格为 `yyyy-MM-dd` |
| `limit` | 否 | **Integer** | 6000 | 单次请求最大返回行数（系统最高上限为 10000 行，超过该限制请缩短日期区间分批拉取） |
| `fieldList` | 否 | **List\<String\>** | - | 从返回参数中指定返回的字段，不指定返回全部。<br>• `securityCode` 和 `tradeDate` **默认返回**，无需在 `fieldList` 中指定，响应中始终位于最前。<br>• **响应中的 `fieldList`** 会在请求 `fieldList` 前自动补上 `securityCode`、`tradeDate`，`list` 中每个数据组按此顺序排列 |

### fieldList 可选字段一览

| 分类 | 可选字段 |
| :--- | :--- |
| **基础标识（默认返回）** | `securityCode`、`tradeDate` |
| **小单** | `smallInflow`、`smallOutflow`、`smallNetInflow`、`smallInflowRatio`、`smallOutflowRatio` |
| **中单** | `mediumInflow`、`mediumOutflow`、`mediumNetInflow`、`mediumInflowRatio`、`mediumOutflowRatio` |
| **大单** | `largeInflow`、`largeOutflow`、`largeNetInflow`、`largeInflowRatio`、`largeOutflowRatio` |
| **特大单** | `xlargeInflow`、`xlargeOutflow`、`xlargeNetInflow`、`xlargeInflowRatio`、`xlargeOutflowRatio` |
| **汇总与主力** | `totalInflow`、`totalOutflow`、`totalNetInflow`、`mainInflow`、`mainOutflow`、`mainNetInflow`、`mainInflowRatio`、`mainOutflowRatio` |

### 请求示例（JSON）

**批量查询**：
```json
{
  "securityList": [
    "600519.SH",
    "000001.SZ"
  ],
  "startDate": "2024-05-01",
  "endDate": "2024-05-20",
  "limit": 5000,
  "fieldList": [
    "mainNetInflow",
    "smallInflow",
    "smallOutflow",
    "largeInflow",
    "largeOutflow",
    "xlargeInflow",
    "xlargeOutflow"
  ]
}
```

**全市场查询**：
```json
{
  "securityList": [
    "aShares"
  ],
  "startDate": "2024-05-20",
  "endDate": "2024-05-20",
  "limit": 5000,
  "fieldList": [
    "mainNetInflow"
  ]
}
```

<br/>

## 六、返回参数

### 顶层返回结构

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `code` | **String** | 响应码，`000000` 表示成功 |
| `msg` | **String** | 响应消息 |
| `status` | **Boolean** | 请求是否成功 |
| `data` | **Object** | 数据体，详见下文 |

### data 结构

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `total` | **Integer** | 返回数据总行数 |
| `fieldList` | **List\<String\>** | **列名表头**。始终以 `securityCode`、`tradeDate` 开头，其后按请求的 `fieldList` 顺序排列，定义了下方 `list` 中每个数据组的含义 |
| `list` | **List\<List\>** | 数据列表，每个元素为一个数组，数组内各值按 `fieldList` 顺序排列 |

### list 元素字段说明

`list` 中每条记录为一个数组，数组内各位置的取值对应 `fieldList` 中同位置的字段名。以下为所有可选字段的完整说明：

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `securityCode` | **String** | 证券代码（如 `"600519.SH"`、`"872931.BJ"`） |
| `tradeDate` | **String** | 交易日期，格式为 `yyyy-MM-dd` |
| `smallInflow` | **Double** | 小单流入金额（单位：元） |
| `smallOutflow` | **Double** | 小单流出金额（单位：元） |
| `smallNetInflow` | **Double** | 小单净流入金额（单位：元），`smallInflow - smallOutflow` |
| `smallInflowRatio` | **Double** | 小单流入占总流入比例（%），`(smallInflow / totalInflow) × 100` |
| `smallOutflowRatio` | **Double** | 小单流出占总流出比例（%），`(smallOutflow / totalOutflow) × 100` |
| `mediumInflow` | **Double** | 中单流入金额（单位：元） |
| `mediumOutflow` | **Double** | 中单流出金额（单位：元） |
| `mediumNetInflow` | **Double** | 中单净流入金额（单位：元），`mediumInflow - mediumOutflow` |
| `mediumInflowRatio` | **Double** | 中单流入占总流入比例（%），`(mediumInflow / totalInflow) × 100` |
| `mediumOutflowRatio` | **Double** | 中单流出占总流出比例（%），`(mediumOutflow / totalOutflow) × 100` |
| `largeInflow` | **Double** | 大单流入金额（单位：元） |
| `largeOutflow` | **Double** | 大单流出金额（单位：元） |
| `largeNetInflow` | **Double** | 大单净流入金额（单位：元），`largeInflow - largeOutflow` |
| `largeInflowRatio` | **Double** | 大单流入占总流入比例（%），`(largeInflow / totalInflow) × 100` |
| `largeOutflowRatio` | **Double** | 大单流出占总流出比例（%），`(largeOutflow / totalOutflow) × 100` |
| `xlargeInflow` | **Double** | 特大单流入金额（单位：元） |
| `xlargeOutflow` | **Double** | 特大单流出金额（单位：元） |
| `xlargeNetInflow` | **Double** | 特大单净流入金额（单位：元），`xlargeInflow - xlargeOutflow` |
| `xlargeInflowRatio` | **Double** | 特大单流入占总流入比例（%），`(xlargeInflow / totalInflow) × 100` |
| `xlargeOutflowRatio` | **Double** | 特大单流出占总流出比例（%），`(xlargeOutflow / totalOutflow) × 100` |
| `totalInflow` | **Double** | 总流入金额（单位：元），`smallInflow + mediumInflow + largeInflow + xlargeInflow` |
| `totalOutflow` | **Double** | 总流出金额（单位：元），`smallOutflow + mediumOutflow + largeOutflow + xlargeOutflow` |
| `totalNetInflow` | **Double** | 总净流入金额（单位：元），`totalInflow - totalOutflow` |
| `mainInflow` | **Double** | 主力流入金额（单位：元），`largeInflow + xlargeInflow` |
| `mainOutflow` | **Double** | 主力流出金额（单位：元），`largeOutflow + xlargeOutflow` |
| `mainNetInflow` | **Double** | **主力净流入**（单位：元），`mainInflow - mainOutflow`。正值表示主力吸筹，负值表示主力出货 |
| `mainInflowRatio` | **Double** | 主力流入占总流入比例（%），`(mainInflow / totalInflow) × 100` |
| `mainOutflowRatio` | **Double** | 主力流出占总流出比例（%），`(mainOutflow / totalOutflow) × 100` |

> **主力资金定义**：主力资金由大单和特大单合并计算，即 `mainNetInflow = (largeInflow + xlargeInflow) - (largeOutflow + xlargeOutflow)`。
>
> **占比恒等式**：各分类流入占比之和 = 100%（`smallInflowRatio + mediumInflowRatio + largeInflowRatio + xlargeInflowRatio = 100`），流出同理。

### 返回示例（JSON）

> 响应中 `fieldList` 自动补齐了 `securityCode`、`tradeDate`，与请求 `fieldList` 不完全一致是正常行为。

```json
{
  "code": "000000",
  "msg": "操作成功",
  "status": true,
  "data": {
    "total": 2,
    "fieldList": [
      "securityCode",
      "tradeDate",
      "mainNetInflow",
      "smallInflow",
      "smallOutflow",
      "largeInflow",
      "largeOutflow",
      "xlargeInflow",
      "xlargeOutflow"
    ],
    "list": [
      [
        "600519.SH",
        "2024-05-20",
        36900000.00,
        85200000.00,
        91300000.00,
        158200000.00,
        125600000.00,
        42500000.00,
        38200000.00
      ],
      [
        "000001.SZ",
        "2024-05-20",
        63200000.00,
        125600000.00,
        118900000.00,
        268700000.00,
        210500000.00,
        54500000.00,
        49500000.00
      ]
    ]
  }
}
```

