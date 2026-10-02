# 数字大脑 · 数据指标接口文档

# 1\.1 票务\-今日客流统计

### 1\.1\.1 API概览

URI:/v1/data/digitalBrain/todayVisitorTrafficSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.1\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|

### 1\.1\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|4||entryParkVisitors|int|10|入园数\(工作台\-\>实时客流\(人\)\)|
|||entryParkVisitorsChainRate|decimal|31\.2|入园数环比\(工作台\-\>实时客流\(人\)昨日环比\)|

### 1\.1\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/todayVisitorTrafficSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [29821585]
}'
```

# 1\.2 票务\-报表比对汇总

### 1\.2\.1 API概览

URI:/v1/data/digitalBrain/compareReportSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.2\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|2|startTime|date|是|2026\-01\-01|开始时间|
|3|endTime|date|是|2026\-01\-01|结束时间|
|4|compareStartTime|date|是|2026\-01\-01|比对开始时间|
|3|compareEndTime|date|是|2026\-01\-01|比对结束时间|

### 1\.2\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|||salesOrderNum|int|100|销售订单数|
|||salesTicketNum|int|100|销售票数<br>\(票务\-\>预定票数\)|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||verifyTicketNum|int|100|核销票数\(数据概览\-\>雷达图\-\>核销票量\)|
|||verifyIncomeAmount|number|1000\.0|核销收入金额|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||cancelTicketNum|int|100|取消票数|
|||cancelIncomeAmount|number|1000\.0|取消支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||cancelExpenseAmount|number|1000\.0|取消收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeTicketNum|int|100|撤销撤改票数|
|||revokeIncomeAmount<br>|number|1000\.0|撤销撤改支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeExpenseAmount|number|1000\.0|撤销撤改收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||netSalesTicketNum|int|100|净销售票数\(票务\-\>出票量\)|
|||netSalesIncomeAmount<br>|number|1000\.0|净销售收入金额<br>\(修改:数据\-\>票务\-\>票房收入\)<br>\(数据概览\-\>雷达图\-\>票房收入\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|
|||afterSaleTicketNum|int|100|售后票数|
|||afterSaleIncomeAmount|number|1000\.0|售后支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||afterSaleExpenseAmount|number|1000\.0|售后收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||pickupTicketNum|int|100|已取票数量|
|||avgOrderAmount<br>|number|1000\.0|客单价<br>\(票务\-\>客单价\)<br>\(数据概览\-\>雷达图\-\>客单价\)|
|||avgOrderAmountChangeRate|number|31\.2|客单价变化率\(票务\-\>客单价环比\)|
|||salesOrderNumChangeRate|number|31\.2|销售订单数变化率|
|||salesTicketNumChangeRate|number|31\.2|预定票数变化率\(票务\-\>预定票数环比\)|
|||salesIncomeAmountChangeRate|number|31\.2|销售收入金额变化率|
|||netSalesTicketNumChangeRate|number|31\.2|净预定票数变化率\(票务\-\>出票量环比\)|
|||netSalesIncomeAmountChangeRate|number|31\.2|净销售收入金额变化率\(数据\-\>票务\-\>票房收入同比/环比\)|
|||refundTicketRate|number|31\.2|退票率, 31\.2代表31\.2%\(票务\-\>退票率\)|
|||refundTicketChangeRate|number|31\.2|退票率变化率\(票务\-\>退票率环比\)|
|||avgTicketPrice   |number|31\.2|单票均价|
|||avgTicketPriceChangeRate|number|31\.2|单票均价变化率|

### 1\.2\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/compareReportSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [29821585],
  "startTime": "2026-07-28 00:00:00",
  "endTime": "2026-07-28 10:00:00",
  "compareStartTime": "2026-07-25 00:00:00",
  "compareEndTime": "2026-07-25 10:00:00"
}'
```

# 1\.3 票务\-今日报表汇总

### 1\.3\.1 API概览

URI:/v1/data/digitalBrain/todayReportSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.3\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|||||||

### 1\.3\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|||salesOrderNum|int|100|销售订单数|
|||salesTicketNum|int|100|销售票数|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||cancelTicketNum|int|100|取消票数|
|||cancelIncomeAmount|number|1000\.0|取消支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||cancelExpenseAmount|number|1000\.0|取消收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||verifyTicketNum|int|100|核销票数\(自定义模块\-\>核销票数\)|
|||verifyIncomeAmount|number|1000\.0|核销收入金额\(自定义模块\-\>核销金额\)|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||revokeTicketNum|int|100|撤销撤改票数|
|||revokeIncomeAmount|number|1000\.0|撤销撤改支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeExpenseAmount|number|1000\.0|撤销撤改收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||netSalesTicketNum|int|100|净销售票数\(自定义模块\-\>出票量\)|
|||netSalesIncomeAmount|number|1000\.0|净销售收入金额<br>\(自定义模块\-\>票房收入\)<br>\(工作台\-\>票房收入\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|
|||netVerifyTicketNum|int|100|净核销票数|
|||netVerifyIncomeAmount|number|1000\.0|净核销收入金额|
|||netVerifyExpenseAmount|number|1000\.0|净核销支出金额|
|||afterSaleTicketNum|int|100|售后票数|
|||afterSaleIncomeAmount|number|1000\.0|售后支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||afterSaleExpenseAmount|number|1000\.0|售后收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||pickupTicketNum|int|100|已取票数量|
|||avgOrderAmount|number|1000\.0|客单价|
|||avgTicketPrice|number|1000\.0|单票均价|
|||refundTicketRate|number|31\.2|退票率, 31\.2代表31\.2%|
|||salesOrderNumChainRate|number|31\.2|销售订单数环比|
|||avgOrderAmountChainRate|number|31\.2|客单价环比|
|||avgTicketPriceChainRate|number|31\.2|单票均价环比|
|||salesTicketNumChainRate|number|31\.2|预定票数环比|
|||salesIncomeAmountChainRate|number|31\.2|销售收入金额环比|
|||netSalesTicketNumChainRate|number|31\.2|净预定票数环比\(自定义模块\-\>出票量环比\)|
|||netSalesIncomeAmountChainRate|number|31\.2|净销售收入金额环比\(工作台\-\>票房收入环比\)|
|||refundTicketChainRate|number|31\.2|退票率环比|
|||verifyIncomeAmountChainRate|number|31\.2|核销收入金额环比\(工作台\-\>核销金额环比\)|
|||verifyTicketNumChainRate|number|31\.2|核销票数环比\(工作台\-\>核销票数金额环比\)|

### 1\.3\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/todayReportSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [29821585]
}'
```

# 1\.4 票务\-分时报表汇总

### 1\.4\.1 API概览

URI:/v1/data/digitalBrain/hourlyReportSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.4\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|2|travelDate|date|是|"2026\-08\-11"|查询日期|

### 1\.4\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|||hour|string|8:00|小时\(票务\-\>票房趋势\)|
|||salesTicketNum|int|100|销售票数|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||cancelTicketNum|int|100|取消票数|
|||cancelIncomeAmount|number|1000\.0|取消支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||cancelExpenseAmount|number|1000\.0|取消收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||verifyTicketNum|int|100|核销票数|
|||verifyIncomeAmount|number|1000\.0|核销收入金额|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||revokeTicketNum|int|100|撤销撤改票数|
|||revokeIncomeAmount|number|1000\.0|撤销撤改支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeExpenseAmount|number|1000\.0|撤销撤改收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||netSalesTicketNum|int|100|净销售票数|
|||netSalesIncomeAmount|number|1000\.0|净销售收入金额\(票务\-\>票房趋势\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|
|||netVerifyTicketNum|int|100|净核销票数|
|||netVerifyIncomeAmount|number|1000\.0|净核销收入金额|
|||netVerifyExpenseAmount|number|1000\.0|净核销支出金额|
|||afterSaleTicketNum|int|100|售后票数|
|||afterSaleIncomeAmount|number|1000\.0|售后支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||afterSaleExpenseAmount|number|1000\.0|售后收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||pickupTicketNum|int|100|已取票数量|

### 1\.4\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/hourlyReportSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [29821585],
  "travelDate": "2026-08-11"
}'
```

# 1\.5 票务\-门票报表汇总

### 1\.5\.1 API概览

URI:/v1/data/digitalBrain/ticketReportSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.5\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|3|startTime|date|是|2026\-01\-01|开始时间|
|4|endTime|date|是|2026\-01\-01|结束时间|

### 1\.5\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|5||ticketId|int|100|*门票ID*|
|||ticketName|string|门票|*门票名称*|
|||salesTicketNum|int|100|销售票数\(票务\-\>票种销量\)|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||verifyTicketNum|int|100|核销票数|
|||verifyIncomeAmount|number|1000\.0|核销收入金额|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||cancelTicketNum|int|100|取消票数|
|||cancelIncomeAmount|number|1000\.0|取消支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||cancelExpenseAmount|number|1000\.0|取消收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeTicketNum|int|100|撤销撤改票数|
|||revokeIncomeAmount|number|1000\.0|撤销撤改支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeExpenseAmount|number|1000\.0|撤销撤改收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||netSalesTicketNum|int|100|净销售票数|
|||netSalesIncomeAmount|number|1000\.0|净销售收入金额\(票务\-\>票种票房收入金额\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|
|||netVerifyTicketNum|int|100|净核销票数|
|||netVerifyIncomeAmount|number|1000\.0|净核销收入金额|
|||netVerifyExpenseAmount|number|1000\.0|净核销支出金额|
|||afterSaleTicketNum|int|100|售后票数|
|||afterSaleIncomeAmount|number|1000\.0|售后支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||afterSaleExpenseAmount|number|1000\.0|售后收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||pickupTicketNum|int|100|已取票数量|

### 1\.5\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/ticketReportSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [29821585],
  "startTime": "2026-07-28 00:00:00",
  "endTime": "2026-07-28 10:00:00"
}'
```

# 1\.6 票务\-销售渠道报表汇总

### 1\.6\.1 API概览

URI:/v1/data/digitalBrain/salesChannelReportSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.6\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|3|startTime|date|是|2026\-01\-01|开始时间|
|4|endTime|date|是|2026\-01\-01|结束时间|

### 1\.6\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|5||salesChannelId|int|111|*销售渠道ID*|
|||salesChannelName|string|OTA|*销售渠道名称**\(票务\-\>渠道分布\)*|
|||salesTicketNum|int|100|销售票数|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||verifyTicketNum|int|100|核销票数|
|||verifyIncomeAmount|number|1000\.0|核销收入金额|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||cancelTicketNum|int|100|取消票数|
|||cancelIncomeAmount|number|1000\.0|取消支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||cancelExpenseAmount|number|1000\.0|取消收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeTicketNum|int|100|撤销撤改票数|
|||revokeIncomeAmount|number|1000\.0|撤销撤改支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeExpenseAmount|number|1000\.0|撤销撤改收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||netSalesTicketNum|int|100|净销售票数\(票务\-\>渠道分布\)|
|||netSalesIncomeAmount|number|1000\.0|净销售收入金额\(票务\-\>渠道分布\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|
|||netVerifyTicketNum|int|100|净核销票数|
|||netVerifyIncomeAmount|number|1000\.0|净核销收入金额|
|||netVerifyExpenseAmount|number|1000\.0|净核销支出金额|
|||afterSaleTicketNum|int|100|售后票数|
|||afterSaleIncomeAmount|number|1000\.0|售后支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||afterSaleExpenseAmount|number|1000\.0|售后收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||pickupTicketNum|int|100|已取票数量|

### 1\.6\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/salesChannelReportSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [29821585],
  "startTime": "2026-07-28 00:00:00",
  "endTime": "2026-07-28 10:00:00"
}'
```

# 

# 1\.7 票务\-客流比对汇总

### 1\.7\.1 API概览

URI:/v1/data/digitalBrain/compareVisitorTrafficSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.7\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|2|startTime|date|是|2026\-01\-01|开始时间|
|3|endTime|date|是|2026\-01\-01|结束时间|
|4|compareStartTime|date|是|2026\-01\-01|比对开始时间|
|5|compareEndTime|date|是|2026\-01\-01|比对结束时间|

### 1\.7\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|7||entryParkVisitors|int|10|入园数\(数据概览\-\>雷达图\-\>总客流\)|
|||entryParkVisitorsChangeRate|decimal|31\.2|入园数变化率|

### 1\.7\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/compareVisitorTrafficSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [1757026],
  "startTime": "2026-07-28 00:00:00",
  "endTime": "2026-07-28 10:00:00",
  "compareStartTime": "2026-07-25 00:00:00",
  "compareEndTime": "2026-07-25 10:00:00"
}'
```

# 1\.8 票务\-产品报表汇总

### 1\.8\.1 API概览

URI:/v1/data/digitalBrain/productReportSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.8\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|3|startTime|date|是|2026\-01\-01|开始时间|
|4|endTime|date|是|2026\-01\-01|结束时间|

### 1\.8\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|5||prdId|int|100|*产品ID*|
|||prdName|string|产品|*产品名称*|
|||salesTicketNum|int|100|销售票数|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||verifyTicketNum|int|100|核销票数|
|||verifyIncomeAmount|number|1000\.0|核销收入金额|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||cancelTicketNum|int|100|取消票数|
|||cancelIncomeAmount|number|1000\.0|取消支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||cancelExpenseAmount|number|1000\.0|取消收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeTicketNum|int|100|撤销撤改票数|
|||revokeIncomeAmount|number|1000\.0|撤销撤改支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeExpenseAmount|number|1000\.0|撤销撤改收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||netSalesTicketNum|int|100|净销售票数\(票务\-\>票种销量\)|
|||netSalesIncomeAmount|number|1000\.0|净销售收入金额\(票务\-\>票种票房收入金额\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|
|||netSalesIncomeAmountRatio|number|1000\.0|净销售收入金额占比\(票务\-\>票种销量占比\)|
|||netVerifyTicketNum|int|100|净核销票数|
|||netVerifyIncomeAmount|number|1000\.0|净核销收入金额|
|||netVerifyExpenseAmount|number|1000\.0|净核销支出金额|
|||afterSaleTicketNum|int|100|售后票数|
|||afterSaleIncomeAmount|number|1000\.0|售后支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||afterSaleExpenseAmount|number|1000\.0|售后收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||pickupTicketNum|int|100|已取票数量|

### 1\.8\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/productReportSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "startTime": "2026-07-28 00:00:00",
  "endTime": "2026-07-28 10:00:00",
  "supplierId": [1757026]
}'
```





# 1\.9 总览\-雷达图指标

### 1\.9\.1 API概览

URI:/v1/data/digitalBrain/api/radarChart

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.9\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSC123456|景区ID|
|2|startTime<br>|<br>string|是<br>|2026\-08\-17 12:00:00|开始时间|
|3|endTime|string|是|2026\-08\-17 12:00:00|结束时间|

### 1\.9\.3 返回参数

|序号|字段|字段|字段|类型|示例|注释|
|---|---|---|---|---|---|---|
|1|status|||bool|true|请求状态|
|2|data|||object|\{\.\.\.\}|数据体|
|||max||object|\{\.\.\.\}|*极值（行业最高值）*|
||||boxOfficeAmount|number|100|*票房收入金额*|
||||avgOrderValue|number|100|*客单价*|
||||totalVisitors|number|100|*总客流*|
||||reputationScore|number|10|*口碑分*|
||||verifiedTicketQty|number|100|*核销票量*|
|||among||object|\{\.\.\.\}|*及格线（行业平均水平）*|
||||boxOfficeAmount|number|50|*票房收入金额*|
||||avgOrderValue|number|50|*客单价*|
||||totalVisitors|number|50|*总客流*|
||||reputationScore|number|5|*口碑分*|
||||verifiedTicketQty|number|50|*核销票量*|
|||own||object|\{\.\.\.\}|*自己（当前景区数据）*|
||||boxOfficeAmount|number|70|*票房收入金额*|
||||avgOrderValue|number|60|*客单价*|
||||totalVisitors|number|65|*总客流*|
||||reputationScore|object|\{\.\.\.\}|*自己（口碑分）*|
||||D|number|7\.7514|近1日口碑分|
||||W|number|7\.7514|近7日口碑分|
||||M|number|7\.7514|近30日口碑分|
||||S|number|7\.7514|近60日口碑分|
||||N|number|7\.7514|近90日口碑分|
||||verifiedTicketQty|number|80|*核销票量*|
|3|msg|||string|success|返回信息|

### 1\.9\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/radarChart' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "startTime": "2026-08-17 17:00:00",
  "endTime": "2026-08-17 17:00:00",
  "scenicId": "PFTSCA01018088"
}'
```

# 1\.10 OTA直销\-OTA营收指标

### 1\.10\.1 API概览

URI:/v1/data/digitalBrain/api/otaIndicator

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.10\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSC123456|景区ID|
|2|startTime|date|是|2026\-01\-01|开始时间|
|3|endTime|date|是|2026\-01\-01|结束时间|
|4|account|string|是|A001|用户ID|

### 1\.10\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||object|\{\.\.\.\}|数据体|
|||totalAmount|number|100|销售额|
|||totalAmountMomRate|decimal|100|销售额环比占比|
|||totalAmountYoyRate|decimal|100|*销售额同比占比*|
|||totalOrderNum|number|10|*订单数*|
|||totalOrderNumMomRate|decimal|100|*订单数环比占比*|
|||dealTicketQty|number|50|*成交票量*|
|||dealTicketQtyMomRate|decimal|50|*成交票量环比占比*|
|||verifyTicketQty|number|50|*核验票量*|
|||verifyTicketQtyMomRate|decimal|5|*核验票量环比占比*|
|||dealResourceQty|number|50|*成交资源数*|
|||dealResourceQtyMomRate|decimal|70|*成交资源数环比占比*|
|||canelOrderNumRate|decimal|60|*退订率*|
|||canelOrderNumMomRate|decimal|65|*退订数环比占比*|
|3|msg||string|success|返回信息|

### 1\.10\.4 示例

```Bash
curl --location --request POST https://data.12301dev.com/v1/data/digitalBrain/api/otaIndicator \
--header xpftkey: pfk_FAKEtestKEYforFixtureONLY0000 \
--header Content-Type: application/json \
--data-raw {
  "startTime": "2026-08-17 17:00:00",
  "endTime": "2026-08-17 17:00:00",
  "scenicId": "PFTSCA01018088",
  "account": "A001"
}
```



# 1\.11 舆情\-维度分数和平台占比

### 1\.11\.1 API概览

URI:/v1/data/digitalBrain/api/opinion/dimPlfm

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.11\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|qTime|string|是|2026\-08\-04|查询时间|
|2|scenicId|string|是|PFTSCA01009835|景区ID|

### 1\.11\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|code||number|0|状态码|
|3|msg||string||返回信息|
|4|data||array|\[\.\.\.\]|数据数组|
|||dimensionScore|array|\[\.\.\.\]|维度分数数组|
|||dimensionScore1d|number|9\.3772|1日维度分数|
|||dimensionScore7d|number|9\.06|7日维度分数|
|||dimensionScore30d|number|7\.6627|30日维度分数|
|||dimensionScore60d|number|7\.5997|60日维度分数|
|||dimensionScore90d|number|7\.6212|90日维度分数|
|||dimensionLevel1Name|string|游玩体验|一级维度名称|
|||commentRate|array|\[\.\.\.\]|平台评论占比数组|
|||platformName|string|携程|平台名称|
|||commentRate1d|decimal|0\.0187|1日评论占比|
|||commentRate7d|decimal|0\.0184|7日评论占比|
|||commentRate30d|decimal|0\.0196|30日评论占比|
|||commentRate60d|decimal|0\.0195|60日评论占比|
|||commentRate90d|decimal|0\.0195|90日评论占比|

### 1\.11\.4 示例

```Bash
curl --location --request POST https://data.12301dev.com/v1/data/digitalBrain/api/opinion/dimPlfm \
--header xpftkey: pfk_FAKEtestKEYforFixtureONLY0000 \
--header Content-Type: application/json \
--data-raw {
  "qTime": "2026-08-04",
  "scenicId": "PFTSCA01009835"
}
```

# 1\.12 全局\-通过景区ID获取供应商ID列表

### 1\.12\.1 API概览

URI:/v1/data/digitalBrain/getSupplierIdByScenicId

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.12\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|2|scenicId|string|是|PFTSCA01018088|景区ID|

### 1\.12\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|code||number|0|状态码|
|3|msg||string||返回信息|
|4|data||array|\[\.\.\.\]|数据数组|
|||scenicId|string|PFTSCA01018088|景区ID|
|||supplierId|array|\[1757026\]|供应商ID列表|

### 1\.12\.4 示例

```Bash
curl --location --request POST https://data.12301dev.com/v1/data/digitalBrain/getSupplierIdByScenicId\
--header xpftkey: pfk_FAKEtestKEYforFixtureONLY0000 \
--header Content-Type: application/json \
--data-raw {
  "scenicId": "PFTSCA01018088"
}
```



# 1\.13 舆情\-舆情综合评价得分

### 1\.13\.1 API概览

URI:/v1/data/digitalBrain/api/opinion/overallScore

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.13\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|qTime|string|是|2026\-08\-05|查询时间|
|2|scenicId|string|是|PFTSCA01009835|景区ID|

### 1\.13\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|code||number|0|状态码|
|3|msg||string||返回信息|
|4|data||array|\[\.\.\.\]|数据数组|
|||scenicName|string|天山天池|景区名字|
|||refreshTime<br>|string|2026\-08\-04T20:13:23|数据刷新时间|
|||emotionalScore|number|7\.6291|综合得分|
|||period|string|1d|时间粒度：1d/7d/30d/60d/90d|
|5|trace\_id||string|3ac993ed2587bbfe51adb211f70b4682|链路追踪ID|

### 1\.13\.4 示例

```Bash
curl --location --request POST https://data.12301dev.com/v1/data/digitalBrain/api/opinion/overallScore \
--header xpftkey: pfk_FAKEtestKEYforFixtureONLY0000 \
--header Content-Type: application/json \
--data-raw {
  "qTime": "2026-08-05",
  "scenicId": "PFTSCA01009835"
}
```

```JSON
{
  "status": true,
  "data": [
    {
      "scenicName": "天山天池",
      "refreshTime": "2026-08-04T20:13:23",
      "emotionalScore": 7.6291,
      "period": "1d"
    },
    {
      "scenicName": "天山天池",
      "refreshTime": "2026-08-04T20:13:23",
      "emotionalScore": 7.8986,
      "period": "7d"
    },
    {
      "scenicName": "天山天池",
      "refreshTime": "2026-08-04T20:13:23",
      "emotionalScore": 8.2255,
      "period": "30d"
    },
    {
      "scenicName": "天山天池",
      "refreshTime": "2026-08-04T20:13:23",
      "emotionalScore": 8.2948,
      "period": "60d"
    },
    {
      "scenicName": "天山天池",
      "refreshTime": "2026-08-04T20:13:23",
      "emotionalScore": 8.3076,
      "period": "90d"
    }
  ],
  "msg": "",
  "code": 0,
  "trace_id": "3ac993ed2587bbfe51adb211f70b4682"
}
```

# 1\.14 票务\-销售报表收入指标

### 1\.14\.1 API概览

URI:/v1/data/digitalBrain/api/touristDailyReportTrend

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.14\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|2|startDate|date|是|2026\-01\-01|开始时间|
|3|endDate|date|是|2026\-01\-01|结束时间|

### 1\.14\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|||travelDate|string|2026\-01\-01|日期\(票务\-\>票房趋势\)|
|||salesTicketNum|int|100|销售票数|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||verifyTicketNum|int|100|核销票数|
|||verifyIncomeAmount|number|1000\.0|核销收入金额|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||netSalesTicketNum|int|100|净销售票数|
|||netSalesIncomeAmount|number|1000\.0|净销售收入金额\(票务\-\>票房趋势\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|

### 1\.14\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/dailyReportTrend' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "supplierId": [29821585],
  "startDate": "2026-07-01",
  "endDate": "2026-07-26"
}'
```

# 1\.15 OTA直销\-雷达图分数分布

### 1\.15\.1 API概览

URI:/v1/data/digitalBrain/api/otaRadarChart

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.15\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSC123456|景区ID|
|2|evaluationMonth|string|是|2026\-01|年月|
|3|account|string|是|A001|商户ID|

### 1\.15\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||object|\{\.\.\.\}|数据体|
|||radarMax|object|\{\.\.\.\}|*极值（行业最高值）*|
|||orderConfirmInNRateScore|number|4|N分钟订单确认率得分|
|||performanceDefectRateScore|number|1|履约缺陷率得分|
|||serviceManualInterventionRateScore|number|4|携程客服人工介入率得分|
|||IM120SecondsTimelyReplyRateScore|number|5|IM120秒回复及时率得分|
|||ctripPhoneConnectionRateScore|number|5|携程电话接通率得分|
|||radarAmong|object|\{\.\.\.\}|*及格线（行业平均水平）*|
|||orderConfirmInNRateScore|number|4|N分钟订单确认率得分|
|||performanceDefectRateScore|number|1|履约缺陷率得分|
|||serviceManualInterventionRateScore|number|4|携程客服人工介入率得分|
|||IM120SecondsTimelyReplyRateScore|number|5|IM120秒回复及时率得分|
|||ctripPhoneConnectionRateScore|number|5|携程电话接通率得分|
|||radarOwn|object|\{\.\.\.\}|*自己（当前景区数据）*|
|||orderConfirmInNRateScore|number|4|N分钟订单确认率得分|
|||performanceDefectRateScore|number|1|履约缺陷率得分|
|||serviceManualInterventionRateScore|number|4|携程客服人工介入率得分|
|||IM120SecondsTimelyReplyRateScore|number|5|IM120秒回复及时率得分|
|||ctripPhoneConnectionRateScore|number|5|携程电话接通率得分|
|3|msg||string|success|返回信息|

### 1\.15\.4 示例

```Bash
curl --location --request POST https://data.12301dev.com/v1/data/digitalBrain/api/otaRadarChart \
--header xpftkey: pfk_FAKEtestKEYforFixtureONLY0000 \
--header Content-Type: application/json \
--data-raw {
  "scenicId":"PFTSCA01018088",
  "evaluationMonth":"2026-07",
  "account": "A001"
}
```

# 1\.16 票务\-销售报表趋势指标

### 1\.16\.1 API概览

URI:/v1/data/digitalBrain/longRangeCompareReportSummary

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:

### 1\.16\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|supplierId|array|是|\["29821585"\]|供应商ID|
|2|startTime|date|是|2026\-01\-01|开始时间|
|3|endTime|date|是|2026\-01\-01|结束时间|
|4|compareStartTime|date|是|2026\-01\-01|比对开始时间|
|3|compareEndTime|date|是|2026\-01\-01|比对结束时间|

### 1\.16\.3 返回参数

|序号|字段||类型|示例|注释|
|---|---|---|---|---|---|
|1|status||bool|true|请求状态|
|2|data||array|\[\.\.\.\]|数据体|
|3|msg||string|认证无效|错误信息|
|||salesOrderNum|int|100|销售订单数|
|||salesTicketNum|int|100|销售票数<br>\(票务\-\>预定票数\)|
|||salesIncomeAmount|number|1000\.0|销售收入金额|
|||salesExpenseAmount|number|1000\.0|销售支出金额|
|||verifyTicketNum|int|100|核销票数\(数据概览\-\>雷达图\-\>核销票量\)|
|||verifyIncomeAmount|number|1000\.0|核销收入金额|
|||verifyExpenseAmount|number|1000\.0|核销支出金额|
|||cancelTicketNum|int|100|取消票数|
|||cancelIncomeAmount|number|1000\.0|取消支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||cancelExpenseAmount|number|1000\.0|取消收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeTicketNum|int|100|撤销撤改票数|
|||revokeIncomeAmount|number|1000\.0|撤销撤改支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||revokeExpenseAmount|number|1000\.0|撤销撤改收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||netSalesTicketNum|int|100|净销售票数\(票务\-\>出票量\)|
|||netSalesIncomeAmount|number|1000\.0|净销售收入金额<br>\(修改:数据\-\>票务\-\>票房收入\)<br>\(数据概览\-\>雷达图\-\>票房收入\)|
|||netSalesExpenseAmount|number|1000\.0|净销售支出金额|
|||afterSaleTicketNum|int|100|售后票数|
|||afterSaleIncomeAmount|number|1000\.0|售后支出金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||afterSaleExpenseAmount|number|1000\.0|售后收入金额\(和新报表保持一致,字段翻译和业务含义相反\)|
|||pickupTicketNum|int|100|已取票数量|
|||avgOrderAmount|number|1000\.0|客单价<br>\(票务\-\>客单价\)<br>\(数据概览\-\>雷达图\-\>客单价\)|
|||avgOrderAmountChangeRate|number|31\.2|客单价变化率\(票务\-\>客单价环比\)|
|||salesOrderNumChangeRate|number|31\.2|销售订单数变化率|
|||salesTicketNumChangeRate|number|31\.2|预定票数变化率\(票务\-\>预定票数环比\)|
|||salesIncomeAmountChangeRate|number|31\.2|销售收入金额变化率|
|||netSalesTicketNumChangeRate|number|31\.2|净预定票数变化率\(票务\-\>出票量环比\)|
|||netSalesIncomeAmountChangeRate|number|31\.2|净销售收入金额变化率\(数据\-\>票务\-\>票房收入同比/环比\)|
|||refundTicketRate|number|31\.2|退票率, 31\.2代表31\.2%\(票务\-\>退票率\)|
|||refundTicketChangeRate|number|31\.2|退票率变化率\(票务\-\>退票率环比\)|
|||avgTicketPrice|number|1000\.0|单票均价|
|||avgTicketPriceChangeRate|number|1000\.0|单票均价变化率|

### 1\.16\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/longRangeCompareReportSummary' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000'
--data-raw '{
  "startTime": "2026-01-01 00:00:00",
  "supplierId": [29821585],
  "endTime": "2026-08-01 23:59:59",
  "compareStartTime": "2025-01-01 00:00:00",
  "compareEndTime": "2025-08-01 23:59:59"
}'
```



# 

# 1\.18 客群\-职住地

### 1\.18\.1 API概览

URI:/v1/data/digitalBrain/api/workHomeLocation

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.18\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.18\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|工作地|指标类别\(工作地,/居住地\)|
||data\[\]\.poiName|string|电子科技大学\(清水河校区\)|指标名称|
||data\[\]\.popularity|string|0\.3200|占比|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.ranking|int|1|排序|
||data\[\]\.etlTime|string|2026\-07\-29T10:49:13|数据加工时间|
|11|msg|string|from cache|错误信息|
|12|code|int|0|状态码|
|13|trace\_id|string|\.\.\.|追踪ID|

### 1\.18\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/workHomeLocation' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "工作地",
            "poiName": "电子科技大学(清水河校区)",
            "popularity": "0.3200",
            "periodGranularity": "2026H1",
            "ranking": 1,
            "etlTime": "2026-07-29T10:49:13"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "429804a230d827ee5968234eda287042"
}
```



# 1\.19 客群\-到访偏好

### 1\.19\.1 API概览

URI:/v1/data/digitalBrain/api/visitPref

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.19\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.19\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|景点偏好|指标类别\(景点偏好/美食偏好/购物偏好/酒店偏好\)|
||data\[\]\.poiName|string|国家级景点|指标名称|
||data\[\]\.popularity|string|84\.5500|偏好占比\(%\)|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.tgi|int|898|TGI值|
||data\[\]\.nationalBaseline|string|9\.4100|全国对照值\(%\)|
||data\[\]\.yoyGrowthRate|string|172\.390464|同比增长率\(%\)|
||data\[\]\.etlTime|string|2026\-07\-29T10:48:57|数据加工时间|
|13|msg|string|from cache|错误信息|
|14|code|int|0|状态码|
|15|trace\_id|string|\.\.\.|追踪ID|

### 1\.19\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/visitPref' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "景点偏好",
            "poiName": "国家级景点",
            "popularity": "84.5500",
            "periodGranularity": "2026H1",
            "tgi": 898,
            "nationalBaseline": "9.4100",
            "yoyGrowthRate": "172.390464",
            "etlTime": "2026-07-29T10:48:57"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "7dafed6ee672675360ddaede9e8a6fd8"
}
```



# 1\.20 客群\-核心KPI指标

### 1\.20\.1 API概览

URI:/v1/data/digitalBrain/api/coreMetric

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.20\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.20\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|核心|指标级别:核心/次级|
||data\[\]\.metricCategory|string|客源构成|指标分类:客源构成/人群属性/学历构成/消费水平/景区粘性/年龄构成/性别构成/出行方式/餐饮消费/酒店消费/设备消费/出游季节/购物偏好/景点粘性/人生阶段|
||data\[\]\.metricName|string|本地客群占比|指标名称|
||data\[\]\.metricValue|string|12\.5600|指标数值|
||data\[\]\.metricUnit|string|%|单位|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.insightInterpretation|string|成都市常驻人口|指标解读|
||data\[\]\.momGrowthRate|string|0\.020000|环比增长率\(%\)|
||data\[\]\.etlTime|string|2026\-07\-31T10:00:40|数据加工时间|
|14|msg|string|from cache|错误信息|
|15|code|int|0|状态码|
|16|trace\_id|string|\.\.\.|追踪ID|

### 1\.20\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/coreMetric' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "核心",
            "metricCategory": "客源构成",
            "metricName": "本地客群占比",
            "metricValue": "12.5600",
            "metricUnit": "%",
            "periodGranularity": "2026H1",
            "insightInterpretation": "成都市常驻人口",
            "momGrowthRate": "0.020000",
            "etlTime": "2026-07-31T10:00:40"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "bfea330cf255367bc1b3d85e81640e3b"
}
```



# 1\.21 客群\-出游画像\(旅行行为\)

### 1\.21\.1 API概览

URI:/v1/data/digitalBrain/api/tripProfile

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.21\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.21\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|出游月份|旅行维度:旅游距离/出游月份|
||data\[\]\.tagValue|string|01月|标签值|
||data\[\]\.popularity|string|38\.3700|占比\(%\)|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.tgi|int|150|TGI值|
||data\[\]\.nationalBaseline|string|25\.5500|全国对照值\(%\)|
||data\[\]\.etlTime|string|2026\-07\-29T10:48:42|数据加工时间|
|12|msg|string|from cache|错误信息|
|13|code|int|0|状态码|
|14|trace\_id|string|\.\.\.|追踪ID|

### 1\.21\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/tripProfile' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "出游月份",
            "tagValue": "01月",
            "popularity": "38.3700",
            "periodGranularity": "2026H1",
            "tgi": 150,
            "nationalBaseline": "25.5500",
            "etlTime": "2026-07-29T10:48:42"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "e4c8d2425bd2c74efd2023b10a0b95ef"
}
```



# 1\.22 客群\-客源分布

### 1\.22\.1 API概览

URI:/v1/data/digitalBrain/api/sourceRegion

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.22\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.22\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|常驻城市|来源类型:常驻城市/活跃商圈|
||data\[\]\.poiName|string|四川省\-成都市|城市/商圈名称|
||data\[\]\.popularity|string|12\.5600|占比\(%\)|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.ranking|int|1|排名\(类型内\)|
||data\[\]\.etlTime|string|2026\-07\-29T10:48:30|数据加工时间|
|11|msg|string|from cache|错误信息|
|12|code|int|0|状态码|
|13|trace\_id|string|\.\.\.|追踪ID|

### 1\.22\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/sourceRegion' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "常驻城市",
            "poiName": "四川省-成都市",
            "popularity": "12.5600",
            "periodGranularity": "2026H1",
            "ranking": 1,
            "etlTime": "2026-07-29T10:48:30"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "23c6f03907e31a7277cb734c7116ef48"
}
```



# 1\.23 客群\-热门到访地

### 1\.23\.1 API概览

URI:/v1/data/digitalBrain/api/popularDestination

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.23\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.23\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|基础设施|POI类别:基础设施/娱乐休闲/教育学校/文化场馆/旅游景点/生活服务/生活服务:旅行社/美食/购物/酒店宾馆|
||data\[\]\.poiName|string|成都东站|POI名称|
||data\[\]\.popularity|int|5693|热度值|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.etlTime|string|2026\-07\-29T10:48:18|数据加工时间|
|10|msg|string|from cache|错误信息|
|11|code|int|0|状态码|
|12|trace\_id|string|\.\.\.|追踪ID|

### 1\.23\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/popularDestination' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "基础设施",
            "poiName": "成都东站",
            "popularity": 5693,
            "periodGranularity": "2026H1",
            "etlTime": "2026-07-29T10:48:18"
        }
    ],
    "msg": "",
    "code": 0,
    "trace_id": "ddde81f6c08dd6f23696bd06eaeb97f0"
}
```



# 1\.24 客群\-财富消费画像

### 1\.24\.1 API概览

URI:/v1/data/digitalBrain/api/spendingProfile

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.24\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.24\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|全国消费水平|消费维度:全国消费水平/手机价格/收入水平/是否有车/通勤方式/酒店消费水平/餐饮消费水平|
||data\[\]\.tagValue|string|低|标签值|
||data\[\]\.popularity|string|3\.7300|占比\(%\)|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.tgi|int|37|TGI值|
||data\[\]\.nationalBaseline|string|9\.9900|全国对照值\(%\)|
||data\[\]\.etlTime|string|2026\-07\-29T10:49:07|数据加工时间|
|12|msg|string|from cache|错误信息|
|13|code|int|0|状态码|
|14|trace\_id|string|\.\.\.|追踪ID|

### 1\.24\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/spendingProfile' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "全国消费水平",
            "tagValue": "低",
            "popularity": "3.7300",
            "periodGranularity": "2026H1",
            "tgi": 37,
            "nationalBaseline": "9.9900",
            "etlTime": "2026-07-29T10:49:07"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "026d1406ffbb943d5866f3ba012e6197"
}
```



# 1\.25 客群\-客源省市切换

### 1\.25\.1 API概览

URI:/v1/data/digitalBrain/api/regionSwitch

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.25\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.25\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.metricLevel|string|客源省份|统计类型:客源省份/客源城市/省内客源市|
||data\[\]\.poiName|string|四川省|省/市名称|
||data\[\]\.popularity|string|21\.7600|占游客总数比例\(%\)|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.etlTime|string|2026\-07\-31T10:00:57|数据加工时间|
|10|msg|string|from cache|错误信息|
|11|code|int|0|状态码|
|12|trace\_id|string|\.\.\.|追踪ID|

### 1\.25\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/regionSwitch' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "metricLevel": "客源省份",
            "poiName": "四川省",
            "popularity": "21.7600",
            "periodGranularity": "2026H1",
            "etlTime": "2026-07-31T10:00:57"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "860bcc5989a29afb01befea8b7124e5a"
}
```



# 1\.26 客群\-基础人群画像

### 1\.26\.1 API概览

URI:/v1/data/digitalBrain/api/basicProfile

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.26\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01002434|景区ID|
|2|timeGran|string|是|2026H1|周期粒度 2026H1/2025H1|

### 1\.26\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01002434|景区ID|
||data\[\]\.scenicName|string|九寨沟景区|景区名称|
||data\[\]\.periodGranularity|string|2026H1|周期粒度|
||data\[\]\.metricLevel|string|人生阶段|维度类型:人生阶段/学历/年龄/性别/有孩一族|
||data\[\]\.metricName|string|中学生|标签值|
||data\[\]\.proportion|string|4\.1300|占比\(%\)|
||data\[\]\.tgi|int|45|TGI值|
||data\[\]\.nationalBaseline|string|8\.9700|全国对照值\(%\)|
||data\[\]\.etlTime|string|2026\-07\-29T10:48:04|数据加工时间|
|12|msg|string|from cache|错误信息|
|13|code|int|0|状态码|
|14|trace\_id|string|\.\.\.|追踪ID|

### 1\.26\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/basicProfile' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01002434",
  "timeGran": "2026H1"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01002434",
            "scenicName": "九寨沟景区",
            "periodGranularity": "2026H1",
            "metricLevel": "人生阶段",
            "metricName": "中学生",
            "proportion": "4.1300",
            "tgi": 45,
            "nationalBaseline": "8.9700",
            "etlTime": "2026-07-29T10:48:04"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "ee533d6e4f2a086a6a53d29f42938303"
}
```



# 1\.27 舆情\-评论数/好中差占比/游客总体评分

### 1\.27\.1 API概览

URI:/v1/data/digitalBrain/api/opinion/baseCore

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.27\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01018088|景区ID|

### 1\.27\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
|3|data\[\]\.scenicId|string|PFTSCA01018088|景区ID|
|4|data\[\]\.scenicName|string|泰宁九龙潭景区|景区名称|
|5|data\[\]\.publishTime|int|20260913|发布时间|
|6|data\[\]\.positiveRate1d|number|11\.8959|好评占比\(近1日\)|
|7|data\[\]\.positiveRate7d|number|11\.86|好评占比\(近7日\)|
|8|data\[\]\.positiveRate30d|number|13\.03|好评占比\(近30日\)|
|9|data\[\]\.positiveRate60d|number|33\.36|好评占比\(近60日\)|
|10|data\[\]\.positiveRate90d|number|38\.8|好评占比\(近90日\)|
|11|data\[\]\.neutralRate1d|number|40\.1487|中评占比\(近1日\)|
|12|data\[\]\.neutralRate7d|number|41\.56|中评占比\(近7日\)|
|13|data\[\]\.neutralRate30d|number|40\.78|中评占比\(近30日\)|
|14|data\[\]\.neutralRate60d|number|33\.88|中评占比\(近60日\)|
|15|data\[\]\.neutralRate90d|number|35\.18|中评占比\(近90日\)|
|16|data\[\]\.negativeRate1d|number|47\.9554|差评占比\(近1日\)|
|17|data\[\]\.negativeRate7d|number|46\.58|差评占比\(近7日\)|
|18|data\[\]\.negativeRate30d|number|46\.2|差评占比\(近30日\)|
|19|data\[\]\.negativeRate60d|number|32\.76|差评占比\(近60日\)|
|20|data\[\]\.negativeRate90d|number|26\.02|差评占比\(近90日\)|
|21|data\[\]\.emotionalScore1d|string|3\.6004|游客总体评分\(近1日\)|
|22|data\[\]\.emotionalScore7d|string|3\.6277|游客总体评分\(近7日\)|
|23|data\[\]\.emotionalScore30d|string|3\.6412|游客总体评分\(近30日\)|
|24|data\[\]\.emotionalScore60d|string|4\.0116|游客总体评分\(近60日\)|
|25|data\[\]\.emotionalScore90d|string|4\.1735|游客总体评分\(近90日\)|
|26|data\[\]\.commentCount|int|269|总评数\(近1日\)|
|27|data\[\]\.commentCount7d|string|1374|总评数\(近7日\)|
|28|data\[\]\.commentCount30d|int|7031|总评数\(近30日\)|
|29|data\[\]\.commentCount60d|int|15231|总评数\(近60日\)|
|30|data\[\]\.commentCount90d|int|24208|总评数\(近90日\)|
|31|data\[\]\.commentCount1dDod|decimal|\-39\.41|周期评论环比\(近1日\)|
|32|data\[\]\.commentCount7dDod|decimal|\-2\.26|周期评论环比\(近7日\)|
|33|data\[\]\.commentCount30dDod|decimal|\-16\.89|周期评论环比\(近30日\)|
|34|data\[\]\.commentCount60dDod|decimal|\-16\.36|周期评论环比\(近60日\)|
|35|data\[\]\.commentCount90dDod|decimal|0\.0|周期评论环比\(近90日\)|
|36|data\[\]\.etlTime|string|2026\-09\-13T17:40:04|数据加工时间|
|37|msg|string|from cache|错误信息|
|38|code|int|0|状态码|
|39|trace\_id|string|\.\.\.|追踪ID|

### 1\.27\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/opinion/baseCore' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01018088"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01018088",
            "scenicName": "泰宁九龙潭景区",
            "publishTime": 20260914,
            "positiveRate1d": 13.4969,
            "positiveRate7d": 12.16,
            "positiveRate30d": 12.84,
            "positiveRate60d": 32.46,
            "positiveRate90d": 38.86,
            "neutralRate1d": 41.7178,
            "neutralRate7d": 41.1,
            "neutralRate30d": 40.93,
            "neutralRate60d": 34.24,
            "neutralRate90d": 34.87,
            "negativeRate1d": 44.7853,
            "negativeRate7d": 46.74,
            "negativeRate30d": 46.23,
            "negativeRate60d": 33.29,
            "negativeRate90d": 26.28,
            "emotionalScore1d": 3.6718,
            "emotionalScore7d": 3.6259,
            "emotionalScore30d": 3.6397,
            "emotionalScore60d": 3.9965,
            "emotionalScore90d": 4.1687,
            "commentCount": 163,
            "commentCount7d": 1382,
            "commentCount30d": 6838,
            "commentCount60d": 15066,
            "commentCount90d": 24084,
            "commentCount1dDod": -39.41,
            "commentCount7dDod": -2.26,
            "commentCount30dDod": -16.89,
            "commentCount60dDod": -16.36,
            "commentCount90dDod": 0.0,
            "etlTime": "2026-09-14T19:59:56"
        }
    ],
    "msg": "",
    "code": 0,
    "trace_id": "9e6a3529f2c1e63a63207edb5aad4ac0"
}
```



# 1\.28 舆情\-评论数/好中差评论周期分布

### 1\.28\.1 API概览

URI:/v1/data/digitalBrain/api/opinion/commentPeriodDistribution

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.28\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01018088|景区ID|

### 1\.28\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01018088|景区ID|
||data\[\]\.scenicName|string|泰宁九龙潭景区|景区名称|
||data\[\]\.publishTime|int|20260615|发布时间|
||data\[\]\.commentCount|int|331|总评数|
||data\[\]\.positiveCount|int|64|好评数|
||data\[\]\.neutralCount|int|217|中评数|
||data\[\]\.negativeCount|int|50|差评数|
||data\[\]\.etlTime|string|2026\-09\-04T15:44:06|数据加工时间|
|11|msg|string|from cache|错误信息|
|12|code|int|0|状态码|
|13|trace\_id|string|\.\.\.|追踪ID|

### 1\.28\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/opinion/commentPeriodDistribution' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01018088"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01018088",
            "scenicName": "泰宁九龙潭景区",
            "publishTime": 20260615,
            "commentCount": 331,
            "positiveCount": 64,
            "neutralCount": 217,
            "negativeCount": 50,
            "etlTime": "2026-09-04T15:44:06"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "038039e45dfbfabbfddb4e05a969eb3b"
}
```



# 1\.29 舆情\-维度标签分数

### 1\.29\.1 API概览

URI:/v1/data/digitalBrain/api/opinion/dimTagScore

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.29\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01018088|景区ID|

### 1\.29\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01018088|景区ID|
||data\[\]\.scenicName|string|泰宁九龙潭景区|景区名称|
||data\[\]\.publishTime|int|20260913|发布时间|
||data\[\]\.dimensionLevel1|string|游玩体验|维度|
||data\[\]\.dimensionL1Score|string|3\.7093|维度分数\(近1日\)|
||data\[\]\.dimensionL1Score7d|string|3\.7385|维度分数\(近7日\)|
||data\[\]\.dimensionL1Score30d|string|3\.6931|维度分数\(近30日\)|
||data\[\]\.dimensionL1Score60d|string|4\.0298|维度分数\(近60日\)|
||data\[\]\.dimensionL1Score90d|string|4\.1689|维度分数\(近90日\)|
||data\[\]\.etlTime|string|2026\-09\-13T17:40:07|数据加工时间|
|13|msg|string|from cache|错误信息|
|14|code|int|0|状态码|
|15|trace\_id|string|\.\.\.|追踪ID|

### 1\.29\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/opinion/dimTagScore' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01018088"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01018088",
            "scenicName": "泰宁九龙潭景区",
            "publishTime": 20260913,
            "dimensionLevel1": "游玩体验",
            "dimensionL1Score": "3.7093",
            "dimensionL1Score7d": "3.7385",
            "dimensionL1Score30d": "3.6931",
            "dimensionL1Score60d": "4.0298",
            "dimensionL1Score90d": "4.1689",
            "etlTime": "2026-09-13T17:40:07"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "3334d2db0d840a12cc488323fe57c462"
}
```



# 1\.30 舆情\-平台评论分布

### 1\.30\.1 API概览

URI:/v1/data/digitalBrain/api/opinion/channelCommentDistribution

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.30\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01018088|景区ID|

### 1\.30\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01018088|景区ID|
||data\[\]\.scenicName|string|泰宁九龙潭景区|景区名称|
||data\[\]\.publishTime|int|20260913|发布时间|
||data\[\]\.commentRate|string|0\.0260|平台评论占比|
||data\[\]\.channelName|string|携程|渠道:抖音/快手/小红书/微博/携程|
||data\[\]\.channelId|string|ctrip|渠道ID|
||data\[\]\.positiveRate1d|string|0\.1429|好评占比\(近1日\)|
||data\[\]\.positiveRate7d|string|0\.0667|好评占比\(近7日\)|
||data\[\]\.positiveRate30d|string|0\.0691|好评占比\(近30日\)|
||data\[\]\.positiveRate60d|string|0\.2980|好评占比\(近60日\)|
||data\[\]\.positiveRate90d|string|0\.3535|好评占比\(近90日\)|
||data\[\]\.commentCnt|int|7|总评数\(近1日\)|
||data\[\]\.commentCnt7d|int|45|总评数\(近7日\)|
||data\[\]\.commentCnt30d|int|217|总评数\(近30日\)|
||data\[\]\.commentCnt60d|int|490|总评数\(近60日\)|
||data\[\]\.commentCnt90d|int|795|总评数\(近90日\)|
||data\[\]\.positiveCount|int|1|好评数\(近1日\)|
||data\[\]\.positive7dCount|int|3|好评数\(近7日\)|
||data\[\]\.positive30dCount|int|15|好评数\(近30日\)|
||data\[\]\.positive60dCount|int|146|好评数\(近60日\)|
||data\[\]\.positive90dCount|int|281|好评数\(近90日\)|
||data\[\]\.positiveCommentMom1d|string|0\.0000|好评环比\(近1日\)|
||data\[\]\.positiveCommentMom7d|string|2\.0000|好评环比\(近7日\)|
||data\[\]\.positiveCommentMom30d|string|\-0\.8855|好评环比\(近30日\)|
||data\[\]\.positiveCommentMom60d|string|\-0\.1978|好评环比\(近60日\)|
||data\[\]\.positiveCommentMom90d|string|2\.3855|好评环比\(近90日\)|
||data\[\]\.commentCntRate|string|2\.6022|各渠道分布\(近1日\)|
||data\[\]\.commentCnt7dRate|string|3\.2751|各渠道分布\(近7日\)|
||data\[\]\.commentCnt30dRate|string|3\.0863|各渠道分布\(近30日\)|
||data\[\]\.commentCnt60dRate|string|3\.2171|各渠道分布\(近60日\)|
||data\[\]\.commentCnt90dRate|string|3\.2840|各渠道分布\(近90日\)|
||data\[\]\.etlTime|string|2026\-09\-13T17:40:05|数据加工时间|
|35|msg|string|from cache|错误信息|
|36|code|int|0|状态码|
|37|trace\_id|string|\.\.\.|追踪ID|

### 1\.30\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/opinion/channelCommentDistribution' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01018088"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01018088",
            "scenicName": "泰宁九龙潭景区",
            "publishTime": 20260913,
            "commentRate": "0.0260",
            "channelName": "携程",
            "channelId": "ctrip",
            "positiveRate1d": "0.1429",
            "positiveRate7d": "0.0667",
            "positiveRate30d": "0.0691",
            "positiveRate60d": "0.2980",
            "positiveRate90d": "0.3535",
            "commentCnt": 7,
            "commentCnt7d": 45,
            "commentCnt30d": 217,
            "commentCnt60d": 490,
            "commentCnt90d": 795,
            "positiveCount": 1,
            "positive7dCount": 3,
            "positive30dCount": 15,
            "positive60dCount": 146,
            "positive90dCount": 281,
            "positiveCommentMom1d": "0.0000",
            "positiveCommentMom7d": "2.0000",
            "positiveCommentMom30d": "-0.8855",
            "positiveCommentMom60d": "-0.1978",
            "positiveCommentMom90d": "2.3855",
            "commentCntRate": "2.6022",
            "commentCnt7dRate": "3.2751",
            "commentCnt30dRate": "3.0863",
            "commentCnt60dRate": "3.2171",
            "commentCnt90dRate": "3.2840",
            "etlTime": "2026-09-13T17:40:05"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "e78b694d1932cfc20b13c29d15c403e7"
}
```



# 1\.31 舆情\-情绪词云

### 1\.31\.1 API概览

URI:/v1/data/digitalBrain/api/opinion/emotionWordCloud

请求方式:POST

认证方式:api key 【需联系数据平台开发获取】

参数类型:application/json

### 1\.31\.2 请求参数

|序号|字段|类型|必传|示例|注释|
|---|---|---|---|---|---|
|1|scenicId|string|是|PFTSCA01018088|景区ID|

### 1\.31\.3 返回参数

|序号|字段|类型|示例|注释|
|---|---|---|---|---|
|1|status|bool|true|请求状态|
|2|data|array|\[\.\.\.\]|数据体|
||data\[\]\.scenicId|string|PFTSCA01018088|景区ID|
||data\[\]\.scenicName|string|泰宁九龙潭景区|景区名称|
||data\[\]\.publishTime|int|20260913|发布时间|
||data\[\]\.emotionType|string|negative|情感反方向:positive/neutral/negative|
||data\[\]\.emotionWord|string|人太多|关键字|
||data\[\]\.emotionRate1d|number|55\.02|1日热度占比|
||data\[\]\.emotionRate7d|number|71\.32|7日热度占比|
||data\[\]\.emotionRate30d|number|29\.48|30日热度占比|
||data\[\]\.emotionRate60d|number|15\.21|60日热度占比|
||data\[\]\.emotionRate90d|number|10\.24|90日热度占比|
||data\[\]\.etlTime|string|2026\-09\-13T17:40:09|数据加工时间|
|14|msg|string|from cache|错误信息|
|15|code|int|0|状态码|
|16|trace\_id|string|\.\.\.|追踪ID|

### 1\.31\.4 示例

```Bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/opinion/emotionWordCloud' \
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \
--header 'Content-Type: application/json' \
--data-raw '{
  "scenicId": "PFTSCA01018088"
}'
```

返回示例：

```JSON
{
    "status": true,
    "data": [
        {
            "scenicId": "PFTSCA01018088",
            "scenicName": "泰宁九龙潭景区",
            "publishTime": 20260913,
            "emotionType": "negative",
            "emotionWord": "人太多",
            "emotionRate1d": 55.02,
            "emotionRate7d": 71.32,
            "emotionRate30d": 29.48,
            "emotionRate60d": 15.21,
            "emotionRate90d": 10.24,
            "etlTime": "2026-09-13T17:40:09"
        }
    ],
    "msg": "from cache",
    "code": 0,
    "trace_id": "0991de269f118963cb65d56473f28e9c"
}
```



> （注：部分内容可能由 AI 生成）
