# Git 管理说明

本项目从本地 1.0 版本开始纳入 Git 管理。

## 纳入 Git 的内容

- 系统代码：`factory_fee/`
- 启动与打包脚本：`scripts/`、`Start_Windows.bat`、`启动本地管理台.command`
- 基础配置模板：`config/`
- 文档：`README.md`、`README_WINDOWS.md`、`docs/`
- 测试：`tests/`
- Netlify 演示页：`netlify_publish/`、`netlify.toml`

## 不纳入 Git 的内容

- 本地数据库：`*.db`、`*.db-shm`、`*.db-wal`
- 用户上传的业务数据：`data/input/`
- 配置表运行版本库：`data/config_tables/`
- 计算输出与导出文件：`data/output/`、`outputs/`
- 本地虚拟环境和打包产物：`.venv/`、`dist/`

## 后续分支建议

- `main`：稳定可演示版本
- `codex/expense-module`：费用模块
- `codex/profit-statement`：利润表模块
- `codex/access-control`：权限管控
- `codex/server-deployment`：公司服务器部署

## 基线说明

当前首个提交作为“本地收入模块 1.0 基线版”。后续所有大模块都应在此基础上开分支开发，完成验证后再合并回稳定版本。
