# 添加规则与数据管理：进度与历史

本文档是“添加规则与数据管理”工作的项目内交接记录。后续继续开发时，以本文件、README、验收报告和代码为准，不再依赖外部聊天记录。

## 当前结论

状态：已完成 V1 本地版。

这项工作已经移动并固化在当前项目中，包含：

- 规则管理：表2-表5规则文件上传、预览、下载、旧文件备份。
- SAP 数据管理：上传 SAP 流水，按年月和工厂范围导入，生成 import_batch_id。
- 数据计算：基于指定 SAP 批次和当前规则执行加工费计算。
- 数据存储：SAP 原始数据、规则快照、计算结果、异常、运行日志写入 SQLite。
- 结果查看：查看最近运行记录，下载结果 Excel 和异常 Excel。
- 验收数据：V1 业务口径验收样例、预期结果、实际对照表和验收报告已入库。

## 项目内落点

核心代码：

- `factory_fee/web_app.py`：本地管理台，包含规则管理、SAP 数据导入、计算触发、运行记录和结果下载。
- `factory_fee/sap_import.py`：SAP 文件导入、字段别名归一、批次生成、原始流水入库。
- `factory_fee/pipeline.py`：按 import_batch_id 取数、加载规则、保存规则快照、执行计算、写入结果。
- `factory_fee/db.py`：SQLite 表结构和兼容性补列。
- `factory_fee/calculator.py`：业务计算、规则命中、异常生成。
- `factory_fee/matcher.py`：优先级匹配、ALL 通配、重复命中检测。

运行入口：

- `scripts/run_web.py`：启动本地管理台。
- `scripts/run_pipeline.py`：命令行跑主计算链路。
- `scripts/run_acceptance_v1.py`：跑 V1 业务口径验收。
- `scripts/init_db.py`：初始化数据库。

数据与配置：

- `config/config.yaml`：默认配置。
- `config/config.local.yaml`：本地管理台配置。
- `config/config.acceptance_v1.yaml`：验收配置。
- `data/input/`：默认 SAP 与表2-表5规则样例。
- `data/output/`：本地计算输出。
- `data/factory_fee.db`：默认 SQLite 数据库。
- `data/factory_fee.local.db`：本地管理台 SQLite 数据库。
- `data/acceptance_v1/`：V1 验收输入、预期、输出、验收数据库。

## 已实现能力

### 规则管理

- 支持管理四张规则表：
  - 表2 物料主数据：`table2_material_master`
  - 表3 机型系数：`table3_model_coef`
  - 表4 单台加工费：`table4_unit_fee`
  - 表5 损益交货量：`table5_pnl_delivery_rule`
- 支持上传 `.csv`、`.xlsx`、`.xls`。
- 上传后统一归一化字段并保存为 CSV。
- 覆盖前会备份旧规则文件，降低误覆盖风险。
- 页面支持预览当前规则和下载当前规则。

### SAP 数据管理

- 支持从页面上传 SAP 流水文件。
- 支持按年月 `yyyymm` 和工厂范围筛选导入。
- 支持常见中文字段别名映射到系统字段。
- 每次导入生成独立 `import_batch_id`。
- 原始 SAP 流水写入 `raw_sap_monthly_data`。
- 导入批次记录写入 `sap_import_batch`。

### 计算与存储

- 页面计算必须选择 SAP 数据批次，避免隐式使用错误数据。
- 每次计算生成独立 `run_id`。
- 计算时保存表2-表5规则快照到 `rule_table_snapshot`。
- 计算结果写入 `calc_result`。
- 异常明细写入 `calc_exception`。
- 运行日志写入 `calc_run_log`。
- 同步输出两个 Excel：
  - `calc_result_<run_id>.xlsx`
  - `calc_exception_<run_id>.xlsx`

### 本地管理台页面

- 首页：查看数据量、最近运行、选择 SAP 批次并触发计算。
- SAP 数据页：上传 SAP 流水，查看批次和数据预览。
- 规则页：维护表2-表5规则。
- 运行记录页：查看历史运行并下载输出文件。

## 数据库表

当前 SQLite 中由 `factory_fee/db.py` 管理以下表：

- `sap_import_batch`：SAP 导入批次。
- `raw_sap_monthly_data`：导入后的 SAP 原始流水。
- `rule_table_snapshot`：每次计算使用的规则快照。
- `calc_run_log`：计算运行日志。
- `calc_result`：计算结果。
- `calc_exception`：异常明细。

## 验收结果

V1 业务口径验收已完成，报告位置：

- `data/acceptance_v1/output/acceptance_report_v1.md`

验收结论：

- 验收样例总行数：10
- 成功计算行数：4
- 异常行数：6
- 异常日志行数：13
- 实际结果与预期结果一致：是
- 存在计算偏差：否
- 存在字段映射问题：否
- ALL 通配生效：是
- 建议进入下一阶段：是

覆盖场景：

- 精确匹配成功。
- ALL 通配匹配成功。
- 表2物料主数据未匹配。
- 表3机型系数未匹配。
- 表4单台加工费未匹配。
- 表5损益交货量规则未匹配。
- 同一优先级重复命中。
- 非 CNY 币种按汇率换算。
- 是否计入损益交货量为“否”时结果为 0。
- 交货数量异常与非 CNY 汇率缺失。

## 历史时间线

以下时间线依据当前项目文件和产物整理。

### 2026-05-13

- 建立 V1 本地计算项目结构。
- 增加配置、文件读取、数据库初始化、主计算流水线。
- 增加表2-表5规则匹配和基础计算逻辑。
- 增加最小测试与开发协作规则。
- 建立本地配置和虚拟环境。

### 2026-05-15

- 增加本地管理台入口和依赖。
- README 增加本地管理台说明。
- 增加 SAP 数据导入模块，支持 import_batch_id。
- 调整表1 SAP 流水导入为宽松入库：导入阶段不再做必填字段校验，也不再按文件内年月/工厂过滤行；缺失字段先补空或默认值，后续计算阶段输出异常原因。
- 增加 V1 配置式跨表合并：表1 SAP 流水按 `config/merge_flows.yaml` 合并表2物料主数据，输出合并结果和未匹配异常，并写入 SQLite 与 Excel。
- 升级规则页信息架构：拆分“规则表管理”和“匹配流程管理”，新增匹配流程抽屉；匹配流程支持选择流程、编辑第一步匹配规则表、匹配字段、输出字段，并可测试前 50 行或正式运行后查看结果。
- 增加数据库表：SAP 导入批次、原始 SAP 数据、运行日志扩展字段。
- 调整 pipeline，使计算可以基于已导入 SAP 批次运行。
- 增加规则快照存储，保证每次计算可追溯。
- 增加 V1 验收配置、验收样例、预期结果和验收脚本。
- 增加交货数量异常、非 CNY 汇率缺失等验收覆盖。
- 生成并保存 V1 验收报告，确认业务口径可进入下一阶段。

## 已知边界

当前版本仍是本地轻量版，不包含：

- 登录、角色、权限和审批。
- 飞书 API 读取或回写。
- 多人同时维护规则的冲突处理。
- 规则版本审批流。
- 更复杂的字段映射配置页面。
- 企业级审计、备份和恢复策略。

## 下一步建议

建议下一阶段按顺序推进：

1. 固化文件版本管理：提交当前项目基线。
2. 增加规则上传后的结构校验和必填列校验。
3. 增加规则版本列表、备份下载和回滚入口。
4. 接入飞书 sheet 读取 SAP 月度数据。
5. 接入飞书结果回写，且保持“结果为 0 行不回写”的原则。
6. 增加字段映射配置，减少代码内字段别名维护。
7. 增加面向实际月结的端到端验收样例。

## 继续开发时的最短路径

启动本地管理台：

```bash
python scripts/run_web.py
```

运行 V1 验收：

```bash
python scripts/run_acceptance_v1.py
```

运行测试：

```bash
PYTHONPATH=. .venv/bin/pytest
```
