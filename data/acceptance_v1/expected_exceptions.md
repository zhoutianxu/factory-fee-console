# V1 业务验收样例：预期异常说明

| 样例编号 | 预期异常编码 | 预期原因 |
|---|---|---|
| A01 | 无 | 精确匹配成功，无异常 |
| A02 | 无 | ALL 通配匹配成功，无异常 |
| A03 | MATERIAL_RULE_NOT_FOUND | 表2物料主数据无匹配规则 |
| A03 | MODEL_COEF_RULE_NOT_FOUND | 物料未匹配导致机型上下文为空，表3无匹配规则 |
| A03 | UNIT_FEE_RULE_NOT_FOUND | 物料未匹配导致机型上下文为空，表4无匹配规则 |
| A03 | PNL_DELIVERY_RULE_NOT_FOUND | 物料未匹配导致机型类别/工艺/物料组为空，表5无匹配规则 |
| A04 | MODEL_COEF_RULE_NOT_FOUND | 表2已匹配，但表3没有对应机型系数规则 |
| A05 | UNIT_FEE_RULE_NOT_FOUND | 表2、表3已匹配，但表4没有对应单台加工费规则 |
| A06 | PNL_DELIVERY_RULE_NOT_FOUND | 表2、表3、表4已匹配，但表5没有对应损益交货量规则 |
| A07 | MATERIAL_RULE_DUPLICATED | 表2同一优先级命中 2 条规则 |
| A07 | MODEL_COEF_RULE_DUPLICATED | 表3同一优先级命中 2 条规则 |
| A07 | UNIT_FEE_RULE_DUPLICATED | 表4同一优先级命中 2 条规则 |
| A07 | PNL_DELIVERY_RULE_DUPLICATED | 表5同一优先级命中 2 条规则 |
| A08 | 无 | USD 规则有汇率，正常换算 |
| A09 | 无 | 是否计入损益交货量 = 否，正常将损益交货量置 0 |
| A10 | DELIVERY_QTY_INVALID | 交货数量为非数字异常值 |
| A10 | UNIT_FEE_EXCHANGE_RATE_MISSING | 非 CNY 币种缺少汇率 |

## 异常编码覆盖

本样例包覆盖以下异常编码：

- MATERIAL_RULE_NOT_FOUND
- MATERIAL_RULE_DUPLICATED
- MODEL_COEF_RULE_NOT_FOUND
- MODEL_COEF_RULE_DUPLICATED
- UNIT_FEE_RULE_NOT_FOUND
- UNIT_FEE_RULE_DUPLICATED
- UNIT_FEE_EXCHANGE_RATE_MISSING
- PNL_DELIVERY_RULE_NOT_FOUND
- PNL_DELIVERY_RULE_DUPLICATED
- DELIVERY_QTY_INVALID

说明：A08 用于校验非 CNY 且汇率存在时的正常换算；A10 用于校验非 CNY 但汇率缺失时写入 `UNIT_FEE_EXCHANGE_RATE_MISSING`。
