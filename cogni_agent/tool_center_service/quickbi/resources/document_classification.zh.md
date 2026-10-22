# 文档分类体系定义（V2.0）

> **用途**：作为文档解析器步骤 2 的分类依据，定义 10 大类、37 种子类型的字段结构。
> **版本**：V2.0（2026-04-07）- 新增 H/I/J 类（技术与运维、客服与销售、政务与合规）
> **更新记录**：
> - 2026-04-03: 初始版本（A-G 类，25 个子类）
> - 2026-04-07: V2.0 新增 12 个子类，总计 37 个

## 分类体系概览

| 大类 | 英文标识 | 子类数量 | 核心范围 |
|------|---------|---------|---------|
| A. 财务与税务 | finance-tax | 8 | 发票、账单、银行流水、费用报销、合同金额、收据、财务审计 |
| B. 人力资源 | human-resources | 7 | 劳动合同、简历、薪资、绩效、离职、培训、员工档案 |
| C. 供应链与采购 | supply-chain-procurement | 6 | 采购合同、入库单、质检报告、供应商、物流订单、报价 |
| D. 行政与法务 | admin-legal | 6 | 营业执照、身份证、护照、保密协议、资质证书、公文通知 |
| E. 医疗类 | medical | 4 | 病历、处方单、检验报告、体检报告 |
| F. 保险类 | insurance | 3 | 保单、理赔申请、结案通知 |
| G. 物流类 | logistics | 3 | 运单、提单、报关单 |
| H. 技术与运维类 | tech-ops | 3 | 系统日志、漏洞扫描、服务器监控 |
| I. 客服与销售类 | sales-service | 3 | 客诉工单、销售报价、退换货单 |
| J. 政务与合规类 | gov-compliance | 3 | 招投标文件、政务审批、合规审计 |
| 未识别类 | unrecognized | - | 无法归类或解析失败的文档 |

**总计**：10 大类 × 43 个子类（其中 37 个预定义 + 2 个系统类：未识别、解析失败）

---

## 字段规范

**每个子类的 Sheet 命名和字段定义：**
1. 每个子类一个 Sheet，Sheet 名称为中文（如 `增值税发票`、`银行回单`）
2. 每个子类隐含第一列：`filename`（源文件名）
3. 字段名使用英文 `snake_case`，Excel 表头使用中文（括号内附英文字段名）
4. 每个子类至少 3 个字段，最多不超过 20 个字段

---

## A. 财务与税务类（finance-tax）

### A1. 增值税发票（vat-invoice）
*(Sheet: `增值税发票`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| invoice_type | 发票类型 | 专用/普通/电子/其他 |
| invoice_code | 发票代码 | |
| invoice_number | 发票号码 | |
| invoice_date | 开票日期 | YYYY-MM-DD |
| validation_code | 校验码 | 后6位 |
| buyer_name | 购买方名称 | |
| buyer_tax_id | 购买方税号 | 统一信用代码 |
| seller_name | 销售方名称 | |
| seller_tax_id | 销售方税号 | 统一信用代码 |
| amount | 金额 | 不含税金额 |
| tax_amount | 税额 | |
| total_amount | 价税合计 | |
| items | 货物或应税劳务名称 | 多项用分号分隔 |

### A2. 银行回单（bank-receipt）
*(Sheet: `银行回单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| receipt_number | 回单编号 | |
| transaction_date | 交易日期 | YYYY-MM-DD |
| transaction_time | 交易时间 | HH:MM:SS |
| account_name | 户名 | 付款或收款方 |
| account_number | 账号 | |
| bank_name | 开户行 | |
| transaction_amount | 交易金额 | |
| transaction_type | 交易类型 | 转账/汇款/收款 |
| counterparty | 对方户名 | |
| purpose | 用途/摘要 | |
| serial_number | 交易流水号 | |

### A3. 银行对账单（bank-statement）
*(Sheet: `银行对账单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| statement_period | 账单期间 | YYYY-MM-DD to YYYY-MM-DD |
| account_number | 账号 | |
| account_name | 户名 | |
| opening_balance | 期初余额 | |
| closing_balance | 期末余额 | |
| transaction_count | 交易笔数 | 期间内总笔数 |
| total_income | 总收入 | |
| total_expense | 总支出 | |
| bank_signature | 银行签章 | 有/无 |

### A4. 费用报销单（expense-claim）
*(Sheet: `费用报销单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| claim_no | 报销单号 | |
| employee_name | 报销人 | |
| department | 部门 | |
| claim_date | 报销日期 | YYYY-MM-DD |
| expense_category | 费用类别 | 差旅/餐饮/办公/交通等 |
| expense_date | 发生日期 | YYYY-MM-DD |
| amount | 报销金额 | |
| currency | 币种 | 默认人民币 |
| payment_method | 支付方式 | 现金/银行卡/支付宝/微信等 |
| attachments | 附件数 | 发票/收据数量 |
| approver | 审批人 | |
| status | 审批状态 | 待审批/已通过/已驳回 |

### A5. 合同/协议（contract-agreement）
*(Sheet: `合同协议`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| contract_no | 合同编号 | |
| contract_type | 合同类型 | 采购/销售/劳动/租赁/合作等 |
| party_a | 甲方 | |
| party_b | 乙方 | |
| sign_date | 签署日期 | YYYY-MM-DD |
| effective_date | 生效日期 | YYYY-MM-DD |
| expiry_date | 到期日期 | YYYY-MM-DD |
| contract_amount | 合同金额 | 如可识别 |
| payment_terms | 付款方式 | 摘要，≤100字 |
| key_terms | 关键条款 | 违约责任/保密条款/争议解决等摘要 |

### A6. 收据（receipt）
*(Sheet: `收据`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| receipt_number | 收据编号 | |
| receipt_date | 收据日期 | YYYY-MM-DD |
| payer | 付款方 | |
| payee | 收款方 | |
| amount | 金额 | |
| payment_method | 收款方式 | 现金/转账/其他 |
| purpose | 事由 | |
| signature | 收款人签章 | 姓名/盖章 |

### A7. 财务报表/审计报告（financial-report）
*(Sheet: `财务报表审计报告`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| report_type | 报表类型 | 资产负债表/利润表/现金流量表/审计报告 |
| report_period | 报表期间 | |
| entity_name | 编制单位 | |
| currency | 币种 | |
| total_assets | 资产总额 | 如可识别 |
| total_liabilities | 负债总额 | 如可识别 |
| net_profit | 净利润 | 如可识别 |
| audit_opinion | 审计意见 | 无保留/保留/否定/无法表示意见 |
| auditor | 审计机构 | |

### A8. 其他金额类文档（other-amount）
*(Sheet: `其他金额类文档`)*

适用于收据/白条/简单金额记录等未包含税号的场景。

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| doc_type | 文档类型 | 收据/白条/其他 |
| amount | 金额 | |
| date | 日期 | YYYY-MM-DD |
| payer | 付款方 | |
| payee | 收款方 | |
| purpose | 事由 | |

---

## B. 人力资源类（human-resources）

### B1. 劳动合同（labor-contract）
*(Sheet: `劳动合同`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| contract_no | 合同编号 | |
| employee_name | 员工姓名 | |
| employee_id | 员工工号 | |
| id_number | 身份证号 | |
| employer | 用人单位 | |
| contract_type | 合同类型 | 固定期限/无固定期限/以完成一定工作任务为期限 |
| start_date | 合同起始日期 | YYYY-MM-DD |
| end_date | 合同到期日期 | YYYY-MM-DD |
| probation_period | 试用期 | e.g. 3个月 |
| position | 岗位/职务 | |
| salary | 薪酬 | 如可识别 |
| signing_date | 签订日期 | YYYY-MM-DD |

### B2. 简历（resume）
*(Sheet: `简历`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| name | 姓名 | |
| gender | 性别 | |
| phone | 联系电话 | |
| email | 邮箱 | |
| education | 最高学历 | 博士/硕士/本科/大专/其他 |
| major | 专业 | |
| graduation_year | 毕业年份 | YYYY |
| years_of_experience | 工作年限 | 整数 |
| current_company | 当前/最近公司 | |
| current_position | 当前/最近职位 | |
| skills | 核心技能 | 分号分隔 |
| expected_salary | 期望薪资 | 如可识别 |

### B3. 绩效评估（performance-review）
*(Sheet: `绩效评估`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| employee_name | 员工姓名 | |
| employee_id | 员工工号 | |
| department | 部门 | |
| review_period | 评估周期 | e.g. 2025年Q4 |
| reviewer | 评估人 | |
| kpi_score | KPI得分 | 数值 |
| competency_score | 能力评估得分 | 数值 |
| overall_rating | 综合评级 | S/A/B/C/D |
| improvement_plan | 改进计划 | 摘要 |

### B4. 薪资单（payslip）
*(Sheet: `薪资单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| employee_name | 员工姓名 | |
| employee_id | 员工工号 | |
| pay_month | 薪资月份 | YYYY-MM |
| base_salary | 基本工资 | |
| performance_bonus | 绩效奖金 | |
| allowance | 补贴 | 分号分隔各项 |
| overtime_pay | 加班费 | |
| deductions | 扣款 | 分号分隔各项 |
| social_security | 社保扣款 | |
| housing_fund | 公积金扣款 | |
| tax | 个税 | |
| net_pay | 实发工资 | |

### B5. 离职证明（termination-certificate）
*(Sheet: `离职证明`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| employee_name | 员工姓名 | |
| employee_id | 员工工号 | |
| department | 部门 | |
| position | 职位 | |
| hire_date | 入职日期 | YYYY-MM-DD |
| termination_date | 离职日期 | YYYY-MM-DD |
| termination_reason | 离职原因 | 个人/公司/协商等 |
| handover_status | 交接状态 | 已完成/未完成 |
| company_seal | 公司盖章 | 有/无 |

### B6. 培训记录（training-record）
*(Sheet: `培训记录`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| training_name | 培训名称 | |
| training_date | 培训日期 | YYYY-MM-DD |
| trainer | 培训讲师 | |
| attendees | 参训人员 | 分号分隔 |
| duration | 培训时长 | 小时 |
| assessment_result | 考核结果 | 通过/未通过/分数 |
| certificate_issued | 是否颁发证书 | 是/否 |

### B7. 员工档案（employee-profile）
*(Sheet: `员工档案`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| employee_id | 员工工号 | |
| name | 姓名 | |
| gender | 性别 | |
| birth_date | 出生日期 | YYYY-MM-DD |
| id_number | 身份证号 | |
| phone | 联系电话 | |
| email | 邮箱 | |
| department | 部门 | |
| position | 职位 | |
| hire_date | 入职日期 | YYYY-MM-DD |
| status | 在职状态 | 在职/离职/试用期 |

---

## C. 供应链与采购类（supply-chain-procurement）

### C1. 采购合同（purchase-contract）
*(Sheet: `采购合同`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| contract_no | 合同编号 | |
| supplier | 供应商 | |
| buyer | 采购方 | |
| sign_date | 签署日期 | YYYY-MM-DD |
| items | 采购物品 | 分号分隔 |
| quantity | 数量 | 对应物品 |
| unit_price | 单价 | 对应物品 |
| total_amount | 合同总额 | |
| delivery_date | 交货日期 | YYYY-MM-DD |
| payment_terms | 付款条件 | 摘要 |

### C2. 入库单/出库单（inventory-receipt）
*(Sheet: `入库出库单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| receipt_no | 单据编号 | |
| document_type | 单据类型 | 入库/出库 |
| warehouse | 仓库 | |
| date | 日期 | YYYY-MM-DD |
| operator | 经办人 | |
| items | 物料名称 | 分号分隔 |
| quantity | 数量 | 对应物料 |
| unit | 单位 | 对应物料 |
| batch_no | 批次号 | 如有 |
| supplier_customer | 供应商/客户 | |

### C3. 质检报告（quality-inspection）
*(Sheet: `质检报告`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| report_no | 报告编号 | |
| product_name | 产品名称 | |
| batch_no | 批次号 | |
| inspection_date | 检验日期 | YYYY-MM-DD |
| inspector | 检验员 | |
| test_items | 检测项目 | 分号分隔 |
| results | 检测结果 | 对应项目，合格/不合格 |
| conclusion | 检验结论 | 合格/不合格/返工 |
| certificate | 合格证号 | 如有 |

### C4. 供应商资质（supplier-qualification）
*(Sheet: `供应商资质`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| supplier_name | 供应商名称 | |
| credit_code | 统一信用代码 | |
| legal_representative | 法定代表人 | |
| business_scope | 经营范围 | 摘要 |
| qualification_type | 资质类型 | 生产/代理/分销等 |
| valid_period | 有效期 | YYYY-MM-DD to YYYY-MM-DD |
| certification_body | 认证机构 | |

### C5. 物流订单（logistics-order）
*(Sheet: `物流订单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| order_no | 订单号 | |
| shipper | 发货方 | |
| consignee | 收货方 | |
| shipper_address | 发货地址 | |
| consignee_address | 收货地址 | |
| goods_description | 货物描述 | |
| weight | 重量(kg) | |
| volume | 体积(m³) | |
| freight | 运费 | |
| expected_delivery | 预计送达日期 | YYYY-MM-DD |
| carrier | 承运商 | |

### C6. 报价单/询价单（quotation-inquiry）
*(Sheet: `报价询价单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| quote_no | 报价单号 | |
| quote_date | 报价日期 | YYYY-MM-DD |
| supplier | 供应商 | |
| buyer | 采购方 | |
| items | 产品/服务 | 分号分隔 |
| unit_price | 单价 | 对应物品 |
| moq | 最小起订量 | 对应物品 |
| lead_time | 交货周期 | e.g. 15天 |
| valid_until | 报价有效期 | YYYY-MM-DD |
| terms | 商务条款 | 摘要 |

---

## D. 行政与法务类（admin-legal）

### D1. 营业执照（business-license）
*(Sheet: `营业执照`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| company_name | 企业名称 | |
| unified_credit_code | 统一社会信用代码 | 18位 |
| legal_representative | 法定代表人 | |
| company_type | 企业类型 | 有限责任公司/股份有限公司等 |
| registered_capital | 注册资本 | |
| establishment_date | 成立日期 | YYYY-MM-DD |
| business_scope | 经营范围 | 摘要，200字以内 |
| address | 住所 | |
| valid_period | 营业期限 | |

### D2. 身份证（id-card）
*(Sheet: `身份证`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| name | 姓名 | |
| gender | 性别 | 男/女 |
| ethnicity | 民族 | |
| birth_date | 出生日期 | YYYY-MM-DD |
| address | 住址 | |
| id_number | 身份证号码 | 18位 |
| issuing_authority | 签发机关 | |
| valid_period | 有效期限 | 起止日期 |

### D3. 护照（passport）
*(Sheet: `护照`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| name_cn | 中文姓名 | |
| name_en | 英文姓名 | |
| nationality | 国籍 | |
| gender | 性别 | |
| birth_date | 出生日期 | YYYY-MM-DD |
| birth_place | 出生地 | |
| passport_number | 护照号码 | |
| issue_date | 签发日期 | YYYY-MM-DD |
| expiry_date | 有效期至 | YYYY-MM-DD |
| issuing_authority | 签发机关 | |

### D4. 保密协议（nda）
*(Sheet: `保密协议`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| party_a | 甲方 | |
| party_b | 乙方 | |
| sign_date | 签署日期 | YYYY-MM-DD |
| confidential_period | 保密期限 | e.g. 3年/永久 |
| scope_summary | 保密范围摘要 | ≤150字 |
| penalty_clause | 违约赔偿条款 | 摘要 |
| governing_law | 管辖法律 | |

### D5. 资质证书（qualification-license）
*(Sheet: `资质证书`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| cert_number | 证书编号 | |
| holder_name | 持证主体 | |
| issuing_authority | 发证机关 | |
| issue_date | 发证日期 | YYYY-MM-DD |
| expiry_date | 有效期至 | YYYY-MM-DD |
| license_scope | 许可范围 | 摘要 |
| status | 状态 | 有效/吊销/过期 |

### D6. 公文/通知（official-notice）
*(Sheet: `公文通知`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| doc_number | 文号 | |
| issuing_dept | 发文机关 | |
| title | 标题 | |
| publish_date | 发布日期 | YYYY-MM-DD |
| target_audience | 主送/抄送单位 | |
| content_summary | 正文摘要 | ≤200字 |
| urgency | 紧急程度 | 特急/加急/平件 |
| confidential_level | 密级 | 绝密/机密/秘密/公开 |

---

## E. 医疗类（medical）

### E1. 病历（medical-record）
*(Sheet: `病历`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| patient_name | 患者姓名 | |
| gender | 性别 | |
| age | 年龄 | |
| visit_date | 就诊日期 | YYYY-MM-DD |
| hospital | 医疗机构 | |
| department | 科室 | |
| doctor | 主治医生 | |
| chief_complaint | 主诉 | 简要描述，100字以内 |
| diagnosis | 诊断 | |
| treatment_plan | 治疗方案 | 摘要 |
| medications | 用药 | 多项用分号分隔 |

### E2. 处方单（prescription）
*(Sheet: `处方单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| patient_name | 患者姓名 | |
| gender | 性别 | |
| age | 年龄 | |
| prescription_date | 开具日期 | YYYY-MM-DD |
| hospital | 医疗机构 | |
| department | 科室 | |
| doctor | 处方医生 | |
| diagnosis | 临床诊断 | |
| medications | 药品名称 | 多项用分号分隔 |
| dosage | 剂量 | 与药品对应 |
| usage | 用法 | e.g. 口服 tid |
| duration | 疗程 | e.g. 7天 |

### E3. 检验/检查报告（lab-report）
*(Sheet: `检验检查报告`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| report_no | 报告编号 | |
| patient_id | 患者ID | |
| test_date | 检查日期 | YYYY-MM-DD |
| test_items | 检测项目 | 分号分隔 |
| results | 结果值 | 分号分隔，与项目对应 |
| reference_ranges | 参考范围 | 分号分隔 |
| abnormal_flags | 异常标识 | ↑/↓/正常 |
| critical_value | 危急值标记 | 是/否 |
| reviewer | 审核医师 | |

### E4. 体检报告（health-checkup）
*(Sheet: `体检报告`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| checkup_no | 体检编号 | |
| check_date | 体检日期 | YYYY-MM-DD |
| dept_items | 科室/项目 | 分号分隔 |
| indicators | 指标值 | 分号分隔 |
| normal_abnormal | 正常/异常 | 分号分隔 |
| health_advice | 健康建议 | 摘要 |
| overall_risk | 总体风险等级 | 高/中/低 |
| doctor | 总检医师 | |

---

## F. 保险类（insurance）

### F1. 保单（insurance-policy）
*(Sheet: `保单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| policy_number | 保单号 | |
| insurance_company | 保险公司 | |
| insurance_type | 险种 | 寿险/车险/财产险等 |
| insured_name | 被保险人 | |
| insured_id | 被保险人证件号 | |
| policyholder | 投保人 | |
| coverage_amount | 保额 | 数值 |
| premium | 保费 | 数值 |
| payment_frequency | 缴费方式 | 年缴/月缴/趸缴 |
| effective_date | 生效日期 | YYYY-MM-DD |
| expiry_date | 到期日期 | YYYY-MM-DD |
| beneficiary | 受益人 | |

### F2. 理赔申请（insurance-claim）
*(Sheet: `理赔申请`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| claim_number | 理赔编号 | |
| policy_number | 关联保单号 | |
| claimant | 申请人 | |
| incident_date | 出险日期 | YYYY-MM-DD |
| incident_type | 出险类型 | 意外/疾病/车祸等 |
| incident_description | 事故描述 | 摘要，150字以内 |
| claim_amount | 申请金额 | 数值 |
| hospital | 就诊医院 | 如适用 |
| supporting_docs | 附件材料 | 提交的证明材料清单 |
| claim_date | 申请日期 | YYYY-MM-DD |

### F3. 理赔结案通知（claim-settlement）
*(Sheet: `理赔结案通知`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| claim_no | 理赔编号 | |
| policy_no | 关联保单号 | |
| settlement_date | 结案日期 | YYYY-MM-DD |
| approved_amount | 核定赔付金额 | 数值 |
| deductible | 免赔额 | 数值 |
| payment_method | 支付方式 | 银行转账/支票等 |
| closure_reason | 结案原因 | 赔付/拒赔/撤销 |
| insurer_signature | 保险公司签章 | 文本或"有/无" |

---

## G. 物流类（logistics）

### G1. 运单（waybill）
*(Sheet: `运单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| waybill_number | 运单号 | |
| carrier | 承运方 | 物流公司名称 |
| shipper | 发货人 | |
| shipper_address | 发货地址 | |
| consignee | 收货人 | |
| consignee_address | 收货地址 | |
| goods_description | 货物描述 | |
| quantity | 件数 | 整数 |
| weight | 重量(kg) | 数值 |
| freight | 运费 | 数值 |
| shipment_date | 发货日期 | YYYY-MM-DD |
| delivery_date | 预计到达日期 | YYYY-MM-DD |

### G2. 提单（bill-of-lading）
*(Sheet: `提单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| bl_number | 提单号 | |
| bl_type | 提单类型 | 正本/副本/电放 |
| shipper | 托运人 | |
| consignee | 收货人 | |
| notify_party | 通知方 | |
| vessel_name | 船名 | |
| voyage | 航次 | |
| port_of_loading | 装货港 | |
| port_of_discharge | 卸货港 | |
| container_number | 集装箱号 | 多个用分号分隔 |
| seal_number | 铅封号 | |
| goods_description | 货物描述 | |
| gross_weight | 毛重(kg) | 数值 |
| measurement | 体积(CBM) | 数值 |
| issue_date | 签发日期 | YYYY-MM-DD |
| number_of_originals | 正本份数 | 通常3份 |

### G3. 报关单（customs-declaration）
*(Sheet: `报关单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| declaration_no | 报关单号 | |
| customs_code | 海关编码 | |
| importer | 进口商 | |
| exporter | 出口商 | |
| trade_mode | 贸易方式 | 一般贸易/加工贸易等 |
| goods_name | 货物名称 | |
| hs_code | HS编码 | |
| declared_value | 申报价值 | 数值 |
| currency | 币种 | USD/CNY等 |
| port | 口岸 | |
| declaration_date | 申报日期 | YYYY-MM-DD |

---

## H. 技术与运维类（tech-ops）🆕

### H1. 系统日志（system-log）
*(Sheet: `系统日志`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| timestamp | 时间戳 | YYYY-MM-DD HH:MM:SS |
| log_level | 日志级别 | INFO/WARN/ERROR/FATAL |
| service_name | 服务/模块名 | |
| trace_id | 追踪ID | 分布式追踪使用 |
| error_code | 错误码 | 如有 |
| ip_address | IP地址 | 来源或目标 |
| message_summary | 消息摘要 | ≤200字 |
| stack_trace | 堆栈关键行 | 提取关键错误行 |

### H2. 漏洞扫描报告（vulnerability-report）
*(Sheet: `漏洞扫描报告`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| report_id | 报告编号 | |
| scan_date | 扫描时间 | YYYY-MM-DD |
| target_system | 目标系统/IP | |
| vuln_name | 漏洞名称 | |
| cve_id | CVE编号 | 如有 |
| risk_level | 风险等级 | 高/中/低/信息 |
| affected_component | 影响组件 | |
| remediation | 修复建议 | 摘要 |
| status | 修复状态 | 未修复/已修复/已忽略 |

### H3. 服务器监控报表（server-monitoring）
*(Sheet: `服务器监控报表`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| host_name | 主机名/IP | |
| monitor_time | 监控时间 | YYYY-MM-DD HH:MM |
| cpu_usage | CPU使用率(%) | 数值 |
| mem_usage | 内存使用率(%) | 数值 |
| disk_io | 磁盘IO(MB/s) | 数值 |
| network_traffic | 网络流量(Mbps) | 数值 |
| alert_events | 告警事件数 | 整数 |
| sla_availability | 可用性SLA(%) | 数值 |

---

## I. 客服与销售类（sales-service）🆕

### I1. 客诉/工单记录（customer-ticket）
*(Sheet: `客诉工单记录`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| ticket_id | 工单号 | |
| customer_id | 客户ID | |
| create_time | 创建时间 | YYYY-MM-DD HH:MM |
| category | 问题分类 | 产品/物流/售后/计费等 |
| priority | 紧急程度 | P1/P2/P3/P4 |
| handler | 处理人 | |
| sla_hours | 解决时长(小时) | 数值 |
| csat_score | 满意度评分 | 1-5 |
| status | 工单状态 | 待处理/处理中/已关闭 |

### I2. 销售报价单（sales-quotation）
*(Sheet: `销售报价单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| quote_no | 报价单号 | |
| quote_date | 报价日期 | YYYY-MM-DD |
| customer_name | 客户名称 | |
| sales_rep | 销售代表 | |
| items | 产品/服务 | 分号分隔 |
| unit_price | 单价 | 与物品对应 |
| quantity | 数量 | 与物品对应 |
| total_amount | 报价总额 | 数值 |
| valid_until | 报价有效期 | YYYY-MM-DD |
| terms | 商务条款 | 摘要 |

### I3. 售后退换货单（return-exchange）
*(Sheet: `售后退换货单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| return_no | 退换单号 | |
| original_order | 原订单号 | |
| apply_date | 申请日期 | YYYY-MM-DD |
| reason | 退换原因 | 质量/发错货/7天无理由等 |
| items | 退换商品 | 分号分隔 |
| refund_amount | 退款金额 | 数值 |
| logistics_no | 退回物流单号 | |
| approval_status | 审批状态 | 待审批/已通过/已驳回 |

---

## J. 政务与合规类（gov-compliance）🆕

### J1. 招投标文件（bidding-doc）
*(Sheet: `招投标文件`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| project_no | 项目编号 | |
| tenderer | 招标方 | |
| bidder | 投标方 | |
| bid_date | 开标日期 | YYYY-MM-DD |
| bid_price | 投标报价 | 数值 |
| tech_summary | 技术方案摘要 | ≤200字 |
| qualification | 资质证明 | 列出核心资质 |
| evaluation_score | 评标得分 | 数值 |
| result | 中标状态 | 中标/未中标/废标 |

### J2. 政务审批单（gov-approval）
*(Sheet: `政务审批单`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| approval_no | 审批单号 | |
| applicant | 申请人/企业 | |
| matter | 申请事项 | |
| submit_date | 提交日期 | YYYY-MM-DD |
| process_nodes | 审批环节 | 分号分隔 |
| result | 批复结果 | 通过/驳回/需补正 |
| valid_period | 有效期 | YYYY-MM-DD to YYYY-MM-DD |
| attachments | 附件清单 | 分号分隔 |

### J3. 合规审计报告（compliance-audit）
*(Sheet: `合规审计报告`)*

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| audit_target | 审计对象 | |
| audit_period | 审计期间 | |
| auditor | 审计机构 | |
| high_issues | 高风险问题数 | 整数 |
| medium_issues | 中风险问题数 | 整数 |
| low_issues | 低风险问题数 | 整数 |
| rectification_status | 整改状态 | 未整改/整改中/已完成 |
| conclusion | 审计结论 | 摘要 |
| rating | 合规评级 | A/B/C/D |

---

## 未识别类（unrecognized）

Sheet 名称：`未识别`

无法匹配到任何预定义子类的文档归入此类。

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| raw_text_preview | 内容预览 | parsedText 的前 300 字符 |
| possible_type | 疑似类型 | Agent 的最佳猜测（如有） |
| confidence | 置信度 | 高/中/低 |
| reason | 未识别原因 | 内容模糊/类型未覆盖/OCR 质量差 |

## 解析失败类（parse-failed）

Sheet 名称：`解析失败`

文档解析 API 返回 status=failed 的文件。

| 字段名 | 中文表头 | 说明 |
|--------|---------|------|
| error_message | 错误信息 | API 返回的 errorMessage |
| file_type | 文件类型 | 文件扩展名 |
| file_size | 文件大小 | KB/MB |

---

## 📐 分类判定指引（V2.0 更新）

### 核心优先级规则
1. **标题/表头优先**：发票/收据/对账单/费用报销 → A 组；合同/协议 → 按内容判定（劳动 → B2，采购 → C1，其他 → A5/D4）
2. **金额类文档路由**：含税额/税号 → A1；含流水号/账号 → A2/A3；含费用明细 → A4；仅金额无税号 → A8
3. **证件/资质类**：营业执照/身份证/护照/许可证 → D 组，不混入财务
4. **医疗 vs 保险**：病历/处方/检验/体检 → E 组；保单/理赔/结案 → F 组
5. **技术/运维类**：日志/漏洞/监控 → H 组；客服/销售/退换货 → I 组；招投标/审批/审计 → J 组

### 边界情况处理
6. **收据 vs 发票**：无税号/无税额 → A8；完整税务信息 → A1
7. **红字/负数凭证**：仍按原类型分类，金额填负数，类型字段备注"红字/冲销"
8. **多页复合文档**：按首页/核心业务归类，在 `remarks` 或 `key_terms` 中备注"含多类型附件"
9. **OCR 质量低/置信度不足**：必须归为 `unrecognized`，禁止强行提取
10. **空白/无文本/纯图片**：归为 `parse-failed` 或 `unrecognized`（视解析 API 返回状态而定）

### 多语言映射（新增）
| 英文/外文 | 对应分类 | 说明 |
|-----------|----------|------|
| Commercial Invoice / Tax Invoice | A1 | 国际发票结构相似 |
| Bank Statement / Advice | A2/A3 | 按明细或汇总区分 |
| Payslip / Salary Advice | B4 | |
| Purchase Order (PO) / Sales Order (SO) | C1 / I2 | SO 映射到报价单或订单 |
| Delivery Note / Packing List | C2 | |
| Certificate of Analysis (COA) | C4 | |
| Bill of Lading (B/L) / AWB | G2 / G1 | |
| System Log / Error Log | H1 | |
| Vulnerability Scan Report | H2 | |
| Customer Ticket / Case | I1 | |
| Bidding Document / Tender | J1 | |
| Audit Report / Compliance Review | J3 | |

### 动态扩展机制
如某文档明确不属于现有 37 种类型，但具有可识别特征：
1. 新建子类，Sheet 名不超过 31 个字符
2. 定义至少 5 个核心字段（含 1 个日期/金额/状态类分析字段）
3. 提交字段定义经业务确认后再加入路由表
