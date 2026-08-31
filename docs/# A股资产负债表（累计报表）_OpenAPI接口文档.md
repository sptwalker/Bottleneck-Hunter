# A股资产负债表（累计报表）_OpenAPI接口文档
## 一、接口描述
 **功能概览**：本接口用于获取 **Gangtise** A股资产负债表累计报表的结构化数据。

**典型应用场景**：

* 用于展示企业在特定时点的财务状况（资产、负债和所有者权益结构）、评估偿债能力和财务稳定性的核心报表，广泛应用于企业财务健康诊断、投融资决策和风险管控等场景。

**使用方法**：

* **科目或报表获取**
  通过 `securityCode` 、`period`和 `reportType` 参数进行检索，未指定科目时返回完整利润表。
* **多维高级筛选**
  * **时间维度**：支持通过 `fiscalYear` 过滤，格式必须为完整年份，有具体日期时会覆盖`fiscalYear`的筛选，并通过 `startTime` 或 `endTime` 过滤，需严格遵循 `yyyy-MM-dd` 格式传参。
  * **科目筛选**：支持将指定科目填入`fieldList`过滤，未指定时默认返回完整利润表。

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
* `https://openapi.gangtise.com/application/open-fundamental/financial-report/balance-sheet/accumulated`

### 请求方式
* **POST**

### 请求头

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `Authorization` | **String** | accessToken，从 <a href="#/markdown/access-token">【accessToken】</a> 接口获取  |

## 五、请求参数
| 参数名 | 必选 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `securityCode` | 是 | **String** | - | 股票代码（如 `"000001.SZ"`） |
| `startDate` | 否 | **String** | - | 开始日期，格式严格为 `yyyy-MM-dd`（有值时会覆盖 `fiscalYear` 的筛选）；若未指定 `endDate`，默认至今 |
| `endDate` | 否 | **String** | - | 结束日期，格式严格为 `yyyy-MM-dd`（有值时会覆盖 `fiscalYear` 的筛选）；若未指定 `startDate`，默认前推三年 |
| `fiscalYear` | 否 | **List\<String\>** | - | 财报年度，格式必须为完整年份。示例：<br>• `["2025"]`<br>• `["2024", "2025", "2026"]` |
| `period` | 否 | **List\<String\>** | `latest` | 报告期：<br>• `q1` - 一季报<br>• `interim` - 中报<br>• `q3` - 三季报<br>• `annual` - 年报<br>• `latest` - 最新一期 |
| `reportType` | 否 | **List\<String\>** | `consolidated` | 报表类型：<br>• `consolidated` - 合并报表<br>• `consolidatedRestated` - 合并报表（调整）<br>• `standalone` - 母公司报表<br>• `standaloneRestated` - 母公司报表（调整） |
| `fieldList` | 否 | **List\<String\>** | `[]` | 指定返回字段：<br>• 空数组 `[]`：返回全部字段<br>• 非空数组：只返回指定字段 |

### 请求示例

### 示例1：提取财报中部分字段
【案例：提取贵州茅台`600519.SH`去年三季报中流动资产、非流动资产、流动负债和非流动负债的合计值指标】

```json
{
  "securityCode": "600519.SH",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2025"],
  "period": ["q3"],
  "reportType": ["consolidated"],
  "fieldList": [
   "totalCurrAssets",
   "totalNonCurrAssets",
   "totalCurrLiab",
   "totalNonCurrLiab"
  ]
}
```

### 示例2：提取多报告期字段
【案例：提取贵州茅台`600519.SH`2023-2025年报的负债及所有者权益合计数据】

```json
{
  "securityCode": "600519.SH",
  "startDate": null,
  "endDate": null,
  "fiscalYear": ["2023", "2024", "2025"],
  "period": ["annual"],
  "reportType": ["consolidated"],
  "fieldList": ["totalLAndE"]
}
```

### 示例3：提取完整报表
【案例：提取贵州茅台`600519.SH`最新一期的资产负债表数据；（若当前日期为2026.03.24，且2025年年度财报数据暂时未披露）】

```json
{
  "securityCode": "600519.SH",
  "startDate": null,
  "endDate": null,
  "fiscalYear": null,
  "period": ["latest"],
  "reportType": ["consolidated"],
  "fieldList": []
}
```

## 六、返回参数

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `securityCode` | **String** | 股票代码（固定字段） |
| `companyName` | **String** | 股票中文名称 （固定字段）|
| `category` | **String** | 公告类型（招股说明书、年度报告等） （固定字段）|
| `announcementDate` | **String** | 信息发布日期（固定字段） |
| `endDate` | **String** | 财报截止日期（固定字段） |
| `earliestAnncDate` | **String** | 首次公告日期（固定字段） |
| `fiscalYear` | **String** | 财报年（固定字段） |
| `period` | **String** | 报告期 （固定字段）|
| `reportType` | **String** | 报表类型（固定字段） |
| `companyType` | **String** | 企业报表格式：<br>• `1` - 一般企业<br>• `2` - 银行<br>• `3` - 保险公司<br>• `4` - 证券公司<br>• `5` - 信托投资公司<br>• `6` - 其他 （固定字段）|
| `currency` | **String** | 币种（固定字段） |
| `unit` | **String** | 单位（固定字段） |
| `currAssets` | **Double** | **流动资产** |
| ↳ `monetaryAssets` | **Double** | 货币资金/现金及存放中央银行款项 |
| ↳↳ `cash` | **Double** | 其中：货币资金 |
| ↳↳ `clientDeposit` | **Double** | 其中：客户资金存款 |
| ↳↳ `depositCentralBank` | **Double** | 其中：现金及存放中央银行款项 |
| ↳ `settleReserve` | **Double** | 结算备付金 |
| ↳↳ `clientFundReserve` | **Double** | 其中：客户备付金 |
| ↳ `fundsLent` | **Double** | 拆出资金 |
| ↳ `fundsForFinancing` | **Double** | 融出资金 |
| ↳ `depositInterbank` | **Double** | 存放同业款项 |
| ↳ `depositAssoc` | **Double** | 存放联行款项 |
| ↳ `preciousMetals` | **Double** | 贵金属 |
| ↳ `tradingAssets` | **Double** | 交易性金融资产合计 |
| ↳↳ `tradingFinAssets` | **Double** | 其中：交易性金融资产 |
| ↳↳ `finAssetsFVTPL` | **Double** | 其中：以公允价值计量且其变动计入当期损益的金融资产 |
| ↳ `derivAssets` | **Double** | 衍生金融资产 |
| ↳ `marginDeposited` | **Double** | 存出保证金 |
| ↳ `notesAcctsRecv` | **Double** | 应收票据及应收账款 |
| ↳↳ `notesReceivable` | **Double** | 其中：应收票据 |
| ↳↳ `acctsReceivable` | **Double** | 其中：应收账款 |
| ↳ `recvFinancing` | **Double** | 应收款项融资 |
| ↳ `advPay` | **Double** | 预付款项 |
| ↳ `insReceivables` | **Double** | 应收保费 |
| ↳ `recvSubrogation` | **Double** | 应收代位追偿款 |
| ↳ `reinsReceivables` | **Double** | 应收分保账款 |
| ↳ `recvReinsReserves` | **Double** | 应收分保合同准备金 |
| ↳↳ `recvReinsUnearnedRes` | **Double** | 其中：应收分保未到期责任准备金 |
| ↳↳ `recvReinsClaimsRes` | **Double** | 其中：应收分保未决赔款准备金 |
| ↳↳ `recvReinsLifeRes` | **Double** | 其中：应收分保寿险责任准备金 |
| ↳↳ `recvReinsHealthRes` | **Double** | 其中：应收分保长期健康险责任准备金 |
| ↳ `otherRecvIncIntDiv` | **Double** | 其他应收款（含利息和股利） |
| ↳↳ `otherReceivable` | **Double** | 其中：其他应收款 |
| ↳↳ `dividendReceivable` | **Double** | 其中：应收股利 |
| ↳↳ `interestReceivable` | **Double** | 其中：应收利息 |
| ↳ `finLeaseRecv` | **Double** | 应收融资租赁款 |
| ↳ `receivables` | **Double** | 应收款项 |
| ↳ `cashMarginRecv` | **Double** | 应收货币保证金 |
| ↳ `pledgeMarginRecv` | **Double** | 应收质押保证金 |
| ↳ `settleGuaranteeRecv` | **Double** | 应收结算担保金 |
| ↳ `riskLossRecv` | **Double** | 应收风险损失款 |
| ↳ `feesCommRecv` | **Double** | 应收手续费及佣金 |
| ↳ `reverseRepoAssets` | **Double** | 买入返售金融资产 |
| ↳ `inventory` | **Double** | 存货 |
| ↳↳ `consumableBioAssets` | **Double** | 其中：消耗性生物资产 |
| ↳↳ `dataResourceInv` | **Double** | 其中：数据资源（存货） |
| ↳ `contractAssets` | **Double** | 合同资产 |
| ↳ `insContractAssets` | **Double** | 保险合同资产 |
| ↳ `reinsContractAssets` | **Double** | 分出再保险合同资产 |
| ↳ `assetsHeldForSale` | **Double** | 持有待售资产 |
| ↳ `agencyAssets` | **Double** | 代理业务资产 |
| ↳ `prepayDeferredExp` | **Double** | 待摊费用 |
| ↳ `policyholderPledgeLoan` | **Double** | 保户质押贷款 |
| ↳ `nonCurrAssetsDue1Yr` | **Double** | 一年内到期的非流动资产 |
| ↳ `otherCurrAssets` | **Double** | 其他流动资产 |
| ↳ `specItemsCurrAssets` | **Double** | 流动资产特殊项目 |
| ↳ `adjItemsCurrAssets` | **Double** | 流动资产调整项目 |
| ↳ `totalCurrAssets` | **Double** | 流动资产合计 |
| `nonCurrAssets` | **Double** | **非流动资产** |
| ↳ `loansAdvances` | **Double** | 发放贷款和垫款 |
| ↳ `totalDebtInv` | **Double** | 债权投资合计 |
| ↳↳ `debtInvestments` | **Double** | 其中：债权投资 |
| ↳↳ `finAssetsAmortCost` | **Double** | 其中：以摊余成本计量的金融资产 |
| ↳ `totalOtherDebtInv` | **Double** | 其他债权投资合计 |
| ↳↳ `otherDebtInvestments` | **Double** | 其中：其他债权投资 |
| ↳↳ `finInvFVOCI` | **Double** | 其中：以公允价值计量且其变动计入其他综合收益的金融投资 |
| ↳ `invLoansRecv` | **Double** | 投资-贷款及应收款项（应收款项类投资） |
| ↳ `timeDep` | **Double** | 定期存款 |
| ↳ `totalOtherEquityInv` | **Double** | 其他权益工具投资合计 |
| ↳↳ `otherEquityInv` | **Double** | 其中：其他权益工具投资 |
| ↳↳ `nonTradeEquityFVOCI` | **Double** | 其中：以公允价值计量且其变动计入其他综合收益的非交易性权益工具投资 |
| ↳ `finAssetsFVOCI` | **Double** | 以公允价值计量且其变动计入其他综合收益的金融资产 |
| ↳ `htmInvestments` | **Double** | 持有至到期投资 |
| ↳ `afsFinAssets` | **Double** | 可供出售金融资产 |
| ↳ `otherNonCurrFinAssets` | **Double** | 其他非流动金融资产 |
| ↳ `entrustedLoans` | **Double** | 委托贷款 |
| ↳ `ltReceivables` | **Double** | 长期应收款 |
| ↳ `ltEquityInvest` | **Double** | 长期股权投资 |
| ↳ `capitalMarginDep` | **Double** | 存出资本保证金 |
| ↳ `investmentProp` | **Double** | 投资性房地产 |
| ↳ `totalPPE` | **Double** | 固定资产合计 |
| ↳↳ `ppe` | **Double** | 其中：固定资产 |
| ↳↳ `ppeDisposal` | **Double** | 其中：固定资产清理 |
| ↳ `totalCIP` | **Double** | 在建工程合计 |
| ↳↳ `cip` | **Double** | 其中：在建工程 |
| ↳↳ `constrMaterials` | **Double** | 其中：工程物资 |
| ↳ `prodBioAssets` | **Double** | 生产性生物资产 |
| ↳ `pubWelfareBioAssets` | **Double** | 公益性生物资产 |
| ↳ `oilGasAssets` | **Double** | 油气资产 |
| ↳ `rouAssets` | **Double** | 使用权资产 |
| ↳ `intangAssets` | **Double** | 无形资产 |
| ↳↳ `tradingSeatFee` | **Double** | 其中：交易席位费 |
| ↳↳ `dataResourceIntang` | **Double** | 其中：数据资源（无形资产） |
| ↳ `devExp` | **Double** | 开发支出 |
| ↳↳ `dataResourceDevExp` | **Double** | 其中：数据资源（开发支出） |
| ↳ `goodwill` | **Double** | 商誉 |
| ↳ `ltDeferredExp` | **Double** | 长期待摊费用 |
| ↳ `sepAcctAssets` | **Double** | 独立账户资产 |
| ↳ `deferredTaxAssets` | **Double** | 递延所得税资产 |
| ↳ `assetsInLieu` | **Double** | 抵债资产 |
| ↳ `futuresMembershipInv` | **Double** | 期货会员资格投资 |
| ↳ `otherNonCurrAssets` | **Double** | 其他非流动资产 |
| ↳ `specItemsNonCurrAssets` | **Double** | 非流动资产特殊项目 |
| ↳ `adjItemsNonCurrAssets` | **Double** | 非流动资产调整项目 |
| ↳ `totalNonCurrAssets` | **Double** | 非流动资产合计 |
| `otherAssets` | **Double** | **其他资产（其他）** |
| ↳ `finInvestments` | **Double** | 金融投资 |
| ↳ `otherAssetsMisc` | **Double** | 其他资产 |
| ↳ `specItemsAssets` | **Double** | 资产特殊项目 |
| ↳ `adjItemsAssets` | **Double** | 资产调整项目 |
| `totalAssets` | **Double** | **资产总计** |
| `currLiab` | **Double** | **流动负债** |
| ↳ `stBorrowings` | **Double** | 短期借款 |
| ↳↳ `pledgedBorrowings` | **Double** | 其中：质押借款 |
| ↳ `stFinancingPay` | **Double** | 应付短期融资款 |
| ↳ `stBondsPay` | **Double** | 应付短期债券 |
| ↳ `borrowCentralBank` | **Double** | 向中央银行借款 |
| ↳ `fundsBorrowed` | **Double** | 拆入资金 |
| ↳ `totalTradingFinLiab` | **Double** | 交易性金融负债合计 |
| ↳↳ `tradingFinLiab` | **Double** | 其中：交易性金融负债 |
| ↳↳ `finLiabFVTPL` | **Double** | 其中：以公允价值计量且其变动计入当期损益的金融负债 |
| ↳ `derivFinLiab` | **Double** | 衍生金融负债 |
| ↳ `notesAcctsPay` | **Double** | 应付票据及应付账款 |
| ↳↳ `notesPayable` | **Double** | 其中：应付票据 |
| ↳↳ `acctsPayable` | **Double** | 其中：应付账款 |
| ↳ `advFromCust` | **Double** | 预收款项 |
| ↳ `unearnedPremRes` | **Double** | 预收保费 |
| ↳ `contractLiab` | **Double** | 合同负债 |
| ↳ `insContractLiab` | **Double** | 保险合同负债 |
| ↳ `reinsCededLiab` | **Double** | 分出再保险合同负债 |
| ↳ `repoLiab` | **Double** | 卖出回购金融资产款 |
| ↳ `depInterbankDep` | **Double** | 吸收存款及同业存款 |
| ↳↳ `custDeposits` | **Double** | 其中：吸收存款 |
| ↳↳ `interbankDep` | **Double** | 其中：同业及其他金融机构存放款项 |
| ↳ `dueToAffiliates` | **Double** | 联行存放款项 |
| ↳ `clientBrokeragePay` | **Double** | 代理买卖证券款 |
| ↳ `underwritingSecPay` | **Double** | 代理承销证券款 |
| ↳ `empBenefitsPay` | **Double** | 应付职工薪酬 |
| ↳ `taxPayable` | **Double** | 应交税费 |
| ↳ `otherPayIncIntDiv` | **Double** | 其他应付款（含利息和股利） |
| ↳↳ `otherPayable` | **Double** | 其中：其他应付款 |
| ↳↳ `dividendPayable` | **Double** | 其中：应付股利 |
| ↳↳ `interestPayable` | **Double** | 其中：应付利息 |
| ↳ `payables` | **Double** | 应付款项 |
| ↳ `feesCommPay` | **Double** | 应付手续费及佣金 |
| ↳ `cashMarginPay` | **Double** | 应付货币保证金 |
| ↳ `pledgeMarginPay` | **Double** | 应付质押保证金 |
| ↳ `futuresInvestorFundPay` | **Double** | 应付期货投资者保障基金 |
| ↳ `reinsPayable` | **Double** | 应付分保账款 |
| ↳ `agencyLiab` | **Double** | 代理业务负债 |
| ↳ `liabHeldForSale` | **Double** | 持有待售负债 |
| ↳ `claimsPay` | **Double** | 应付赔付款 |
| ↳ `policyholderDivPay` | **Double** | 应付保单红利 |
| ↳ `policyholderDepInvFund` | **Double** | 保户储金及投资款 |
| ↳ `insContractReserves` | **Double** | 保险合同准备金 |
| ↳ `marginReceived` | **Double** | 存入保证金 |
| ↳ `accruedExp` | **Double** | 预提费用 |
| ↳ `deferredIncome` | **Double** | 递延收益 |
| ↳ `guarCompensReserve` | **Double** | 担保赔偿准备金 |
| ↳ `guarBusinessReserve` | **Double** | 担保业务准备金 |
| ↳ `futuresRiskReserve` | **Double** | 期货风险准备金 |
| ↳ `nonCurrLiabDue1Yr` | **Double** | 一年内到期的非流动负债 |
| ↳ `otherCurrLiab` | **Double** | 其他流动负债 |
| ↳ `specItemsCurrLiab` | **Double** | 流动负债特殊项目 |
| ↳ `adjItemsCurrLiab` | **Double** | 流动负债调整项目 |
| ↳ `totalCurrLiab` | **Double** | 流动负债合计 |
| `nonCurrLiab` | **Double** | **非流动负债** |
| ↳ `ltInsContractReserves` | **Double** | 长期保险合同准备金 |
| ↳↳ `unearnedPremResLt` | **Double** | 其中：未到期责任准备金 |
| ↳↳ `outstandingClaimsRes` | **Double** | 其中：未决赔款准备金 |
| ↳↳ `lifeInsLiabRes` | **Double** | 其中：寿险责任准备金 |
| ↳↳ `ltHealthRes` | **Double** | 其中：长期健康险责任准备金 |
| ↳ `ltBorrowings` | **Double** | 长期借款 |
| ↳ `bondsPay` | **Double** | 应付债券 |
| ↳↳ `prefSharesBonds` | **Double** | 其中：优先股（应付债券） |
| ↳↳ `perpetualBonds` | **Double** | 其中：永续债（应付债券） |
| ↳ `leaseLiab` | **Double** | 租赁负债 |
| ↳ `totalLtPayables` | **Double** | 长期应付款合计 |
| ↳↳ `ltPayables` | **Double** | 其中：长期应付款 |
| ↳↳ `specificPayables` | **Double** | 其中：专项应付款 |
| ↳ `finLeasePay` | **Double** | 应付融资租赁款 |
| ↳ `ltEmpBenefitsPay` | **Double** | 长期应付职工薪酬 |
| ↳ `provisions` | **Double** | 预计负债 |
| ↳ `ltDeferredIncome` | **Double** | 长期递延收益 |
| ↳ `sepAcctLiab` | **Double** | 独立账户负债 |
| ↳ `deferredTaxLiab` | **Double** | 递延所得税负债 |
| ↳ `otherNonCurrLiab` | **Double** | 其他非流动负债 |
| ↳ `specItemsNonCurrLiab` | **Double** | 非流动负债特殊项目 |
| ↳ `adjItemsNonCurrLiab` | **Double** | 非流动负债调整项目 |
| ↳ `totalNonCurrLiab` | **Double** | 非流动负债合计 |
| `otherLiab` | **Double** | **其他负债（其他）** |
| ↳ `otherLiabMisc` | **Double** | 其他负债 |
| ↳ `specItemsLiab` | **Double** | 负债特殊项目 |
| ↳ `adjItemsLiab` | **Double** | 负债调整项目 |
| `totalLiab` | **Double** | **负债合计** |
| `equity` | **Double** | **所有者权益（或股东权益）** |
| ↳ `shareCapital` | **Double** | 实收资本（或股本） |
| ↳ `otherEquityInstr` | **Double** | 其他权益工具 |
| ↳↳ `prefSharesEquity` | **Double** | 其中：优先股（其他权益工具） |
| ↳↳ `perpetualBondsEquity` | **Double** | 其中：永续债（其他权益工具） |
| ↳ `capReserve` | **Double** | 资本公积 |
| ↳ `lessTreasuryShares` | **Double** | 减：库存股 |
| ↳ `specReserve` | **Double** | 专项储备 |
| ↳ `oci` | **Double** | 其他综合收益 |
| ↳ `surplusReserve` | **Double** | 盈余公积 |
| ↳ `genRiskProv` | **Double** | 一般风险准备 |
| ↳ `tradingRiskProv` | **Double** | 交易风险准备 |
| ↳ `otherReserves` | **Double** | 其他储备（公允价值变动储备） |
| ↳ `retainedEarn` | **Double** | 未分配利润 |
| ↳ `fxTransDiff` | **Double** | 外币报表折算差额 |
| ↳ `unrecogInvLoss` | **Double** | 未确认投资损失 |
| ↳ `specItemsParentEq` | **Double** | 归属母公司所有者权益特殊项目 |
| ↳ `adjItemsParentEq` | **Double** | 归属母公司所有者权益调整项目 |
| ↳ `totalParentEq` | **Double** | 归属母公司所有者权益合计 |
| ↳↳ `parentOrdinaryEq` | **Double** | 其中：归属于母公司普通股股东权益 |
| ↳ `nonControllingInterests` | **Double** | 少数股东权益 |
| ↳ `specItemsTotalEq` | **Double** | 所有者权益特殊项目 |
| ↳ `adjItemsTotalEq` | **Double** | 所有者权益调整项目 |
| `totalEquity` | **Double** | **所有者权益（或股东权益）合计** |
| `liabAndEquity` | **Double** | **负债和所有者权益** |
| ↳ `specItemsLAndE` | **Double** | 负债和权益特殊项目 |
| ↳ `adjItemsLAndE` | **Double** | 负债和权益调整项目 |
| `totalLAndE` | **Double** | **负债和所有者权益（或股东权益）总计** |

### 返回示例

### 示例1：提取财报中部分字段
【案例：提取贵州茅台`600519.SH`去年三季报中流动资产、非流动资产、流动负债和非流动负债的合计值指标】

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
      "600519.SH",
      "贵州茅台",
      "三季报",
      "2025-10-30",
      "2025-09-30",
      "2025",
      "q3",
      "consolidated",
      "1",
      "CNY",
      "元",
      256587161700.86,
      48151023229.00,
      38763379268.53,
      269780788.48
    ]
  ]
}
```

### 示例2：提取多报告期字段
【案例：提取贵州茅台`600519.SH`2023-2025年报的负债及所有者权益合计数据】

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
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "totalLAndE"
  ],
  "list": [
    [
      "600519.SH",
      "贵州茅台",
      "年度报告",
      "2024-04-03",
      "2023-12-31",
      "2023",
      "q4",
      "consolidated",
      "1",
      "CNY",
      "元",
      272699660092.25
    ],
    [
      "600519.SH",
      "贵州茅台",
      "年度报告",
      "2025-04-03",
      "2024-12-31",
      "2024",
      "q4",
      "consolidated",
      "1",
      "CNY",
      "元",
      298944579918.70
    ],
    [
      "600519.SH",
      "贵州茅台",
      null,
      null,
      null,
      "2025",
      "q4",
      "consolidated",
      "1",
      null,
      null,
      null
    ]
  ]
}
```

### 示例3：提取完整报表
【案例：提取贵州茅台`600519.SH`最新一期的资产负债表数据；（若当前日期为2026.03.24，且2025年年度财报数据暂时未披露）】

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
    "period",
    "reportType",
    "companyType",
    "currency",
    "unit",
    "monetaryAssets",
    "cash",
    "fundsLent",
    "notesAcctsRecv",
    "notesReceivable",
    "acctsReceivable",
    "advPay",
    "otherRecvIncIntDiv",
    "reverseRepoAssets",
    "inventory",
    "nonCurrAssetsDue1Yr",
    "otherCurrAssets",
    "totalCurrAssets",
    "loansAdvances",
    "totalDebtInv",
    "debtInvestments",
    "totalOtherDebtInv",
    "otherDebtInvestments",
    "otherNonCurrFinAssets",
    "investmentProp",
    "totalPPE",
    "totalCIP",
    "rouAssets",
    "intangAssets",
    "devExp",
    "ltDeferredExp",
    "deferredTaxAssets",
    "otherNonCurrAssets",
    "totalNonCurrAssets",
    "totalAssets",
    "notesAcctsPay",
    "acctsPayable",
    "contractLiab",
    "depInterbankDep",
    "empBenefitsPay",
    "taxPayable",
    "otherPayIncIntDiv",
    "nonCurrLiabDue1Yr",
    "otherCurrLiab",
    "totalCurrLiab",
    "leaseLiab",
    "deferredTaxLiab",
    "totalNonCurrLiab",
    "totalLiab",
    "shareCapital",
    "capReserve",
    "lessTreasuryShares",
    "oci",
    "surplusReserve",
    "genRiskProv",
    "retainedEarn",
    "totalParentEq",
    "nonControllingInterests",
    "totalEquity",
    "totalLAndE"
  ],
  "list": [
    [
      "600519.SH",
      "贵州茅台",
      "三季报",
      "2025-10-30",
      "2025-09-30",
      "2025",
      "q3",
      "consolidated",
      "1",
      "CNY",
      "元",
      51753057846.45,
      51753057846.45,
      135402538025.64,
      5235061677.50,
      5209529939.88,
      25531737.62,
      21229757.91,
      71073860.68,
      3505663836.03,
      55858862716.48,
      4686422347.31,
      53251632.86,
      256587161700.86,
      2724893752.70,
      1010008247.97,
      1010008247.97,
      496798634.84,
      496798634.84,
      4035935007.45,
      3818474.90,
      21170758112.81,
      3534669471.18,
      228658932.68,
      8649225375.62,
      165998778.47,
      137287075.50,
      5811949314.43,
      181022050.45,
      48151023229.00,
      304738184929.86,
      2822271882.72,
      2822271882.72,
      7749027043.43,
      14473441763.35,
      471949375.75,
      6840000739.89,
      5374646780.18,
      57447774.54,
      974593908.67,
      38763379268.53,
      205116273.86,
      64664514.62,
      269780788.48,
      39033160057.01,
      1256197800.00,
      1374964415.72,
      6000465970.56,
      -1055242.36,
      48503784606.05,
      1061529724.00,
      210875009053.38,
      257069964386.23,
      8635060486.62,
      265705024872.85,
      304738184929.86
    ]
  ]
}
```

## 七、特殊说明：

1.  **报表类型**：请求参数默认填入`consolidated`-合并报表，若需要合并报表（调整）的数值，请在入参时填入。具体说明如下：
    *   `consolidated`-合并报表：上市公司第一次发布的原始报表
    *   `consolidatedRestated`-合并报表（调整）：上市公司在最新公布的财报中，针对上年同期的合并报表数据进行调整，以反映修订后的最新数据
    *   `standalone`-母公司报表：上市公司所属集团母公司单独编制的财务报表数据
    *   `standaloneRestated`-母公司报表（调整）：上市公司所属集团母公司在最新公布的报表中，对上年度母公司财务报表数据进行调整后的修订数据
2.  **报表数值说明**：所有数值保留两位小数。
3.  **报表科目说明**：
    *   返回参数中未标注 ↳ 的是指一级节点科目，标注一个 ↳ 指二级节点，标注两个 ↳ 指三级节点；
    *   取完整报表时：自动过滤空值科目，最终返回有数值的科目；若多个报告期中只有一期有数值，其他报告期无数值，则都不过滤。
    *   取指定科目时：返回全部指定科目。
4.  **单季度报表数据**请使用【A股资产负债表（单季度报表）_OpenAPI接口参数】。
