# 工厂加工费计算轻量化工具 V1

这是一个透明、可查看、可交给 Codex / GitHub / TClaw 继续迭代的 Python 轻量化计算工具。

第一版目标不是做完整企业级系统，而是先跑通一条月度计算流水线：

1. 读取当月 SAP 加工费流水
2. 读取表2-表5规则表
3. 存入本地 SQLite 数据库
4. 执行物料主数据、机型系数、单台加工费、损益交货量规则匹配
5. 计算加工费 CNY、标准工时、综合难度
6. 输出结果 Excel 和异常 Excel

## 目录结构

当前项目根目录：
- config/config.yaml：运行配置
- data/input/：输入模板和样例数据
- data/output/：输出结果
- factory_fee/：核心代码
- scripts/run_pipeline.py：主入口脚本
- scripts/init_db.py：初始化数据库
- tests/：最小测试

## 安装依赖

建议使用 Python 3.10+。

安装：

pip install -r requirements.txt

## 运行样例

在项目根目录执行：

python scripts/run_pipeline.py

运行成功后会生成：

- data/factory_fee.db
- data/output/calc_result_<run_id>.xlsx
- data/output/calc_exception_<run_id>.xlsx

## 本地管理台

如果需要一个不做权限管理的轻量页面，可以启动本地管理台：

python scripts/run_web.py

默认访问：

http://127.0.0.1:5050

管理台提供：

- 规则管理：上传、预览、下载表2-表5规则文件；上传前会自动备份旧文件。
- 数据计算：按年月和工厂范围触发现有计算流水线。
- 数据储存：原始 SAP 数据、规则快照、计算结果、异常和运行日志继续写入 SQLite。
- 结果查看：查看运行记录，并下载结果 Excel 与异常 Excel。

当前版本不包含登录、角色、权限、审批等权限管理能力。

## Windows 本地部署包

给公司 Windows 电脑使用时，优先使用根目录下的：

- `Start_Windows.bat`
- `启动本地管理台.bat`
- `README_WINDOWS.md`

双击 `Start_Windows.bat` 会自动创建 `.venv`、安装依赖并启动管理台。首次启动需要电脑能访问 Python 依赖源。

## 阿里云部署

V1.1 起，项目已补充服务器部署底座，支持以 Docker 方式部署到阿里云 ECS。

相关文件：

- `Dockerfile`
- `docker-compose.aliyun.yml`
- `.env.production.example`
- `deploy/nginx/factory-fee-console.conf`
- `config/config.production.yaml`
- `scripts/aliyun_deploy.sh`
- `scripts/aliyun_backup.sh`
- `scripts/aliyun_restore.sh`
- `scripts/aliyun_status.sh`
- `docs/ALIYUN_DEPLOYMENT.md`

本地 Docker 验证：

```bash
bash scripts/aliyun_deploy.sh
```

详细部署说明见：

- docs/ALIYUN_DEPLOYMENT.md

## 进度与历史

“添加规则与数据管理”工作的进度、历史、已实现能力、验收结论和下一步建议已整理到：

- docs/RULES_AND_DATA_MANAGEMENT_HISTORY.md
- docs/NETLIFY_PRODUCTION_READINESS.md

## 当前输入文件

默认读取：

- data/input/sap_monthly.csv
- data/input/table2_material_master.csv
- data/input/table3_model_coef.csv
- data/input/table4_unit_fee.csv
- data/input/table5_pnl_delivery_rule.csv

后续接飞书时，可以保持计算核心不变，只替换读取模块。

## 重要原则

1. 不写死 run_id。
2. 不写死 import_batch_id。
3. 先本地文件跑通，再接飞书 API。
4. 结果为 0 行时，不做回写。
5. 每条异常必须有原因。
6. 代码必须可见、可版本管理。

## 匹配规则

V1.1 起，规则引擎以“计算流程设置”页面中用户选择的匹配字段为准。

- 每个步骤只按界面勾选的匹配字段执行匹配。
- 配置表中的“优先级”字段只作为普通数据字段保留，不再隐藏参与筛选。
- 如需按优先级拆分规则，应在界面新增多个步骤，或把优先级作为明确的业务字段纳入流程设计。
- 规则表中的 `ALL` 仍表示通配，空值不作为通配。

## 新增计算列

V1.1 起，计算公式不再只写在代码里，而是在“计算流程设置”页面以“新增计算列”的形式维护。

默认计算列包括：

- 损益交货量
- 单台加工费CNY
- 加工费CNY
- 标准工时
- 综合难度

这些列都可以在页面中启用、停用、修改公式、修改列名，也可以继续新增或删除。公式按顺序执行，后一列可以引用前一列。

常用函数：

- `num(value)`：转为数字，空值按 0 处理。
- `ifelse(condition, true_value, false_value)`：条件判断。
- `if_eq(value, expected, true_value, false_value)`：等值判断。
- `if_in(value, "是,Y,YES,1,TRUE", true_value, false_value)`：集合判断。
- `coalesce(a, b, c)`：返回第一个非空值。

## 下一步建议

V1 本地跑通后，再逐步增加：

1. 飞书 sheet 读取模块
2. 飞书结果回写模块
3. 每月 SAP sheet 链接作为参数输入
4. 更完整的字段映射配置
5. 更严格的异常校验
