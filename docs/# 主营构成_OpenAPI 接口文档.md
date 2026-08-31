# 主营构成_OpenAPI 接口文档

## 一、接口描述

**功能概览**：本接口用于获取**Gangtise**上市公司主营业务构成的结构化数据。

**典型应用场景**：

* 适用于查看公司主营构成的各维度数据，帮助专业投资者深入了解公司业务情况。

**使用方法**：

* **多维高级筛选**
  * **时间维度**：支持通过 `startDate` 与 `endDate` 过滤，支持中报和年报两个报告期。
  * **精准匹配**：支持按 `证券代码（格式如 `"600211.SH"`）、提取维度、提取指标进行查找。

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
* `https://openapi.gangtise.com/application/open-fundamental/main-business`

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
| `securityCode` | 是 | **String** | - | 股票代码 |
| `startDate` | 否 | **String** | endDate往前三年 | 开始⽇期，格式严格为 `yyyy-MM-dd` |
| `endDate` | 否 | **String** | 当前日期 | 结束⽇期，格式严格为 `yyyy-MM-dd` |
| `period` | 否 | **List\<String\>** | - | 报告期：<br>• `interim` - 中报<br>• `annual` - 年报 |
| `fieldList` | 否 | **List\<String\>** | - | 可提取的指标：<br>• `opRevenue` - 营业收⼊<br>• `opRevenueYoy` - 营业收⼊同⽐增速<br>• `opRevenueRatio` - 营业收⼊占⽐<br>• `opCost` - 营业成本<br>• `opCostYoy` - 营业成本同⽐增速<br>• `opCostRatio` - 营业成本占⽐<br>• `grossProfit` - ⽑利<br>• `grossProfitYoy` - ⽑利同⽐增速<br>• `grossProfitRatio` - ⽑利占⽐<br>• `grossMargin` - ⽑利率<br>• `grossMarginYoy` - ⽑利率同⽐增速<br>• `grossMarginRatio` - ⽑利率占⽐ |
| `breakdown` | 是 | **String** | `product` | 提取维度：<br>• `product` - 按产品拆分<br>• `industry` - 按⾏业拆分<br>• `region` - 按地区拆分 |


### 请求示例（JSON）
```json
{
    "securityCode": "000651.SZ",
    "startDate": "2022-09-01",
    "endDate": "2025-07-30",
    "period": [
        "annual"
    ],
    "fieldList": [
        "opRevenue",
        "opRevenueYoy",
        "opRevenueRatio",
        "opCost",
        "opCostYoy",
        "opCostRatio",
        "grossMargin",
        "grossMarginYoy",
        "grossMarginRatio"
    ],
    "breakdown": "product"
}
```

<br/>

## 六、返回参数
| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `securityCode` | **String** | 证券代码 |
| `securityName` | **String** | 证券名称 |
| `breakdown` | **String** | 提取维度：如 `product` |
| `categoryDetail` | **List\<String\>** | 所选提取维度下细分类型名称，如：<br>• `product`：茅台酒 、系列酒、其他业务、其他系列酒 <br>• `industry`：酒类、其他业务 <br>• `region`： 国内、国外、其他业务 |
| `fieldList` | **List\<String\>** | 数据列字段列表，定义下方 `list` 数组中每个数据组的含义 |
| ↳ `periodName` | **String** | 报告期中文名，如 `2025年中报`（固定字段） |
| ↳ `periodEndDate` | **String** | 该报告期的截止日期，如 `2025-06-30`（固定字段） |
| ↳ `categoryName` | **String** | 包含所选提取维度下细分类型名称，如：<br>• `茅台酒`、`国内` <br>• `合计`：表示公司整体（固定字段） |
| ↳ `opRevenue` | **Double** | 营业收入（可选字段） |
| ↳ `opRevenueYoy` | **Double** | 营业收入同比增速（%）（可选字段） |
| ↳ `opRevenueRatio` | **Double** | 营业收入占比（%）（可选字段） |
| ↳ `opCost` | **Double** | 营业成本（可选字段） |
| ↳ `opCostYoy` | **Double** | 营业成本同比增速（%） |
| ↳ `opCostRatio` | **Double** | 营业成本占比（%）（可选字段） |
| ↳ `grossProfit` | **Double** | 毛利（可选字段） |
| ↳ `grossProfitYoy` | **Double** | 毛利同比增速（%）（可选字段） |
| ↳ `grossProfitRatio` | **Double** | 毛利占比（%）（可选字段） |
| ↳ `grossMargin` | **Double** | 毛利率（%）（可选字段） |
| ↳ `grossMarginYoy` | **Double** | 毛利率同比增速（%）（可选字段） |
| ↳ `grossMarginRatio` | **Double** | 毛利率占比（%）（可选字段） |

### 返回示例（JSON）
``` json
{
    "code": "000000",
    "msg": "操作成功",
    "status": true,
    "data": {
        "securityCode": "000651.SZ",
        "securityName": "格力电器",
        "breakdown": "product",
        "categoryDetail": [
            "空调",
            "其他业务",
            "工业制品",
            "绿色能源",
            "生活电器",
            "其他主营",
            "智能装备"
        ],
        "fieldList": [
            "periodName",
            "periodEndDate",
            "categoryName",
            "opRevenue",
            "opRevenueYoy",
            "opRevenueRatio",
            "opCost",
            "opCostYoy",
            "opCostRatio",
            "grossMargin",
            "grossMarginYoy",
            "grossMarginRatio"
        ],
        "list": [
            [
                "2022年报",
                "2022-12-31",
                "总计",
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null
            ],
            [
                "2022年报",
                "2022-12-31",
                "空调",
                134859394542.06,
                2.39,
                null,
                91116284416.91,
                0.6,
                null,
                32.44,
                3.87,
                null
            ],
            [
                "2022年报",
                "2022-12-31",
                "其他业务",
                35822543140.58,
                -16.75,
                null,
                34210528020.51,
                -17.03,
                null,
                4.5,
                7.66,
                null
            ],
            [
                "2022年报",
                "2022-12-31",
                "工业制品",
                7599259996.39,
                137.88,
                null,
                6057662877.96,
                132.58,
                null,
                20.29,
                9.85,
                null
            ],
            [
                "2022年报",
                "2022-12-31",
                "绿色能源",
                4701188530.73,
                61.69,
                null,
                4077474678.11,
                49.37,
                null,
                13.27,
                117.18,
                null
            ],
            [
                "2022年报",
                "2022-12-31",
                "生活电器",
                4567901238.21,
                -6.43,
                null,
                3051711250.15,
                -6.47,
                null,
                33.19,
                0.09,
                null
            ],
            [
                "2022年报",
                "2022-12-31",
                "其他主营",
                1006009387.35,
                -21.8,
                null,
                967478786.51,
                -22.09,
                null,
                3.83,
                10.37,
                null
            ],
            [
                "2022年报",
                "2022-12-31",
                "智能装备",
                432085871.36,
                -49.63,
                null,
                303247852.63,
                -49.97,
                null,
                29.82,
                1.64,
                null
            ]
        ]
    }
}
```