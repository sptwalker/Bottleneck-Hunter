# 盈利预测\_OpenAPI 接口参数

## 一、接口描述

**功能概览**：本接口用于获取**Gangtise**平台券商盈利预测相关数据信息。

**典型应用场景**：

- 适用于查看当前券商对个股的盈利预测信息，帮助专业投资者深入了解当前市场预期情况。

**使用方法**：

- **多维高级筛选**
  - **时间维度**：支持通过 `startDate` 与 `endDate` 过滤。
  - **精准匹配**：支持按 `证券代码（格式如 `"600211.SH"`）、提取维度、提取指标进行查找。

<br/>

## 二、数据权限范围

不同类型的账号享有不同的历史数据访问范围，具体规则如下：

- **试用账号**：数据权限为当前时间前溯**3个月**的历史存量数据。
* **正式账号**：数据权限长度可依据购买的**服务等级或支付费用**进行灵活调整。

<br/>

## 三、OpenAPI 积分消耗

返回的列表包含全部盈立预测的参数信息。调用该接口成功返回数据后，将按 **0.5 积分 / 条**扣除积分。

<br/>

## 四、请求说明
### 请求地址
* `https://openapi.gangtise.com/application/open-fundamental/earning-forecast`

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
| `securityCode` | 是 | **String** | - | 查询一致预期的证券，代码格式如 `"000001.SZ"` |
| `startDate` | 否 | **String** | 与 endDate 相同 | 开始日期，格式严格为 `"yyyy-MM-dd"` |
| `endDate` | 否 | **String** | 当前日期 | 结束日期，格式严格为 `"yyyy-MM-dd"` |
| `consensusList` | 否 | **List\<String\>** | - | 一致预期的可选择指标：<br>• `netIncome`-归母净利润<br>• `netIncomeYoy`-归母净利润同比增速<br>• `eps`-每股收益<br>• `pe`-市盈率<br>• `bps`-每股净资产<br>• `pb`-市净率<br>• `peg`-PEG<br>• `roe`-净资产收益率<br>• `ps`-市销率 |



### 请求示例

```JSON
{
  "securityCode": "600519.SH",
  "startDate": "2026-03-20",
  "endDate": "2026-03-25",
  "consensusList": [
    "netIncome",
    "eps",
    "pe"
  ]
}
```


## 六、返回参数

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `securityCode` | **String** | 证券代码，如 `"000001.SZ"` |
| `securityName` | **String** | 证券名称，如 `"贵州茅台"` |
| `updateList` | **List\<Object\>** | 更新记录列表 |
| ↳ `date` | **String** | 预测信息的日期，如 `"2026-03-25"` |
| ↳ `fieldList` | **List\<Object\>** | 一致预测信息合集 |
| ↳↳ `forecastYear` | **String** | 盈利预测的预测年份，展示三年（固定字段） |
| ↳↳ `netIncome` | **Double** | 归母净利润（可选字段） |
| ↳↳ `netIncomeYoy` | **Double** | 归母净利润同比增速（%）（可选字段） |
| ↳↳ `eps` | **Double** | 每股收益（可选字段） |
| ↳↳ `pe` | **Double** | 市盈率（可选字段） |
| ↳↳ `bps` | **Double** | 每股净资产（可选字段） |
| ↳↳ `pb` | **Double** | 市净率（可选字段） |
| ↳↳ `peg` | **Double** | PEG（可选字段） |
| ↳↳ `roe` | **Double** | 净资产收益率（可选字段） |
| ↳↳ `ps` | **Double** | 市销率（可选字段） |


### 返回示例

```JSON
{
  "code": "000000",
  "msg": "操作成功",
  "status": true,
  "data": {
    "securityCode": "600519.SH",
    "securityName": "贵州茅台",
    "updateList": [
      {
        "date": "2026-03-25",
        "fieldList": [
          {
            "forecastYear": "2026E",
            "netIncome": 1250.50,
            "eps": 62.50,
            "pe": 28.60
          },
          {
            "forecastYear": "2027E",
            "netIncome": 1450.80,
            "eps": 72.50,
            "pe": 24.80
          },
          {
            "forecastYear": "2028E",
            "netIncome": 1680.20,
            "eps": 84.00,
            "pe": 21.50
          }
        ]
      },
      {
        "date": "2026-03-24",
        "fieldList": [
          {
            "forecastYear": "2026E",
            "netIncome": 1248.30,
            "eps": 62.40,
            "pe": 28.70
          },
          {
            "forecastYear": "2027E",
            "netIncome": 1448.50,
            "eps": 72.40,
            "pe": 24.90
          },
          {
            "forecastYear": "2028E",
            "netIncome": 1675.80,
            "eps": 83.80,
            "pe": 21.80
          }
        ]
      },
      {
        "date": "2026-03-23",
        "fieldList": [
          {
            "forecastYear": "2026E",
            "netIncome": 1245.80,
            "eps": 62.30,
            "pe": 28.80
          },
          {
            "forecastYear": "2027E",
            "netIncome": 1445.20,
            "eps": 72.30,
            "pe": 25.00
          },
          {
            "forecastYear": "2028E",
            "netIncome": 1670.50,
            "eps": 83.50,
            "pe": 22.00
          }
        ]
      },
      {
        "date": "2026-03-22",
        "fieldList": [
          {
            "forecastYear": "2026E",
            "netIncome": 1243.20,
            "eps": 62.20,
            "pe": 28.90
          },
          {
            "forecastYear": "2027E",
            "netIncome": 1442.80,
            "eps": 72.20,
            "pe": 25.10
          },
          {
            "forecastYear": "2028E",
            "netIncome": 1665.30,
            "eps": 83.30,
            "pe": 22.20
          }
        ]
      },
      {
        "date": "2026-03-21",
        "fieldList": [
          {
            "forecastYear": "2026E",
            "netIncome": 1240.50,
            "eps": 62.10,
            "pe": 29.00
          },
          {
            "forecastYear": "2027E",
            "netIncome": 1440.30,
            "eps": 72.10,
            "pe": 25.20
          },
          {
            "forecastYear": "2028E",
            "netIncome": 1660.20,
            "eps": 83.00,
            "pe": 22.40
          }
        ]
      },
      {
        "date": "2026-03-20",
        "fieldList": [
          {
            "forecastYear": "2026E",
            "netIncome": 1238.80,
            "eps": 62.00,
            "pe": 29.10
          },
          {
            "forecastYear": "2027E",
            "netIncome": 1437.50,
            "eps": 71.90,
            "pe": 25.30
          },
          {
            "forecastYear": "2028E",
            "netIncome": 1655.80,
            "eps": 82.80,
            "pe": 22.50
          }
        ]
      }
    ]
  }
}
```