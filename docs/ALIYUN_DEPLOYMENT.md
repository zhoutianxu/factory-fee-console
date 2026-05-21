# 阿里云部署说明

本文档面向“先把当前系统部署到阿里云供团队使用”的阶段。

## 推荐 V1.1 部署形态

第一阶段建议使用：

- 阿里云 ECS
- Docker / Docker Compose
- SQLite 文件数据库
- ECS 本地数据盘持久化
- 后续再升级到 RDS、OSS、企业登录与权限

这样改造最小，可以尽快把当前本地系统变成服务器系统。

## 服务器建议

演示和小团队试用：

- 2 vCPU / 4 GB 内存
- 40 GB 以上系统盘
- Ubuntu 22.04 或 Alibaba Cloud Linux
- 安全组只开放必要端口

正式使用前建议：

- 加 HTTPS
- 配 Nginx 反向代理
- 配服务器快照和数据库备份
- 增加登录、角色、操作日志
- 评估是否迁移到 RDS

## 部署文件

本次新增的部署相关文件：

- `Dockerfile`
- `docker-compose.aliyun.yml`
- `.dockerignore`
- `.env.production.example`
- `config/config.production.yaml`
- `deploy/docker-entrypoint.sh`
- `deploy/nginx/factory-fee-console.conf`
- `deploy/systemd/factory-fee-console.service`
- `deploy/cron/factory-fee-backup.cron`
- `factory_fee/wsgi.py`
- `scripts/aliyun_deploy.sh`
- `scripts/aliyun_backup.sh`
- `scripts/aliyun_restore.sh`
- `scripts/aliyun_status.sh`

## 本地验证 Docker

在项目根目录执行：

```bash
bash scripts/aliyun_deploy.sh
```

脚本会自动创建 `.env` 并生成 `FACTORY_FEE_SECRET_KEY`。如需修改端口或绑定地址，再编辑 `.env`。

访问：

```text
http://127.0.0.1:5050
```

健康检查：

```text
http://127.0.0.1:5050/healthz
```

## ECS 部署步骤

1. 在 ECS 上安装 Docker 和 Docker Compose。
2. 把项目代码上传到服务器，例如：

```bash
/opt/factory-fee-console
```

3. 进入项目目录：

```bash
cd /opt/factory-fee-console
```

4. 启动服务：

```bash
bash scripts/aliyun_deploy.sh
```

脚本会自动创建 `.env`、生成密钥、创建 `runtime` 目录并启动容器。

5. 查看状态：

```bash
bash scripts/aliyun_status.sh
```

## 推荐目录结构

建议把项目放在：

```text
/opt/factory-fee-console
```

运行后会生成：

```text
/opt/factory-fee-console/runtime
```

`runtime` 是服务器上的核心业务数据目录，包含运行配置、数据库、上传文件、配置表版本、导出结果和备份文件。

## 生产运行结构

容器内核心目录：

```text
/app/runtime/config
/app/runtime/data
```

服务器持久化目录：

```text
runtime/config
runtime/data
runtime/backups
```

说明：

- `runtime/config`：运行时配置。流程配置、配置表类型、导出历史等会持久化在这里。
- `runtime/data`：业务数据。SQLite 数据库、上传数据、配置表版本、计算输出都在这里。
- `runtime/backups`：备份压缩包。

第一次启动时，容器会把项目内置的 `config/` 自动复制到 `runtime/config/`。后续重启或升级不会覆盖已有运行配置。

## 数据持久化

容器内的数据目录是：

```text
/app/runtime/data
```

服务器上的持久化目录是：

```text
runtime/data
```

其中包括：

- SQLite 数据库
- 上传的 SAP 数据
- 配置表版本
- 计算输出
- 导出结果

这个目录必须备份，不能随意删除。

## 备份与恢复

手动备份：

```bash
bash scripts/aliyun_backup.sh
```

备份文件默认保存在：

```text
runtime/backups
```

恢复备份：

```bash
CONFIRM_RESTORE=YES bash scripts/aliyun_restore.sh runtime/backups/某个备份文件.tar.gz
```

正式使用建议：

- 每日备份 `runtime/data`
- 备份文件上传到 OSS 或公司备份盘
- 发布新版本前先备份
- 重大计算完成后先备份

安装每日定时备份示例：

```bash
crontab deploy/cron/factory-fee-backup.cron
```

如果项目路径不是 `/opt/factory-fee-console`，先修改 `deploy/cron/factory-fee-backup.cron` 里的路径。

## 端口和访问

默认服务端口：

```text
5050
```

默认 Compose 只绑定本机：

```text
127.0.0.1:5050
```

这意味着推荐由 Nginx 对外提供访问，应用本身不直接暴露到公网。

如果只给公司内网访问，建议：

- ECS 安全组限制来源 IP
- 或挂到公司 VPN 后访问

如果给公网访问，必须增加：

- HTTPS
- 登录权限
- 强密码或企业 SSO
- 操作审计

## Nginx 反向代理

示例配置：

```text
deploy/nginx/factory-fee-console.conf
```

安装示例：

```bash
sudo cp deploy/nginx/factory-fee-console.conf /etc/nginx/conf.d/factory-fee-console.conf
sudo nginx -t
sudo systemctl reload nginx
```

如果使用域名，把配置里的 `server_name _;` 改成你的域名。

阿里云安全组建议：

- 开放 80 / 443 给公司出口 IP 或办公网段。
- 不直接开放 5050，除非只是临时验证。

## systemd 自启动

Docker Compose 已经配置 `restart: unless-stopped`，一般重启 Docker 后会自动恢复。

如果还希望服务器开机时由 systemd 拉起 Compose，可以使用示例：

```bash
sudo cp deploy/systemd/factory-fee-console.service /etc/systemd/system/factory-fee-console.service
sudo systemctl daemon-reload
sudo systemctl enable factory-fee-console
sudo systemctl start factory-fee-console
```

如果项目路径不是 `/opt/factory-fee-console`，先修改 service 文件里的 `WorkingDirectory`。

## 日常运维命令

启动或更新：

```bash
bash scripts/aliyun_deploy.sh
```

查看状态：

```bash
bash scripts/aliyun_status.sh
```

查看日志：

```bash
docker compose -f docker-compose.aliyun.yml --env-file .env logs -f factory-fee
```

停止：

```bash
docker compose -f docker-compose.aliyun.yml --env-file .env down
```

备份：

```bash
bash scripts/aliyun_backup.sh
```

## 发布新版本流程

1. 先备份：

```bash
bash scripts/aliyun_backup.sh
```

2. 更新代码。

3. 重新部署：

```bash
bash scripts/aliyun_deploy.sh
```

4. 打开 `/healthz` 和首页检查。

5. 抽查一次上传、流程配置、执行计算、查看结果。

## 后续企业化升级路径

第二阶段建议：

1. 权限管控
   - 用户登录
   - 角色：管理员、规则维护、计算人员、只读查看
   - 操作日志

2. 数据库升级
   - SQLite 迁移到 PostgreSQL 或 MySQL
   - 阿里云 RDS 承载结构化数据

3. 文件存储升级
   - 上传文件和导出文件迁移到 OSS
   - 数据库存元数据，文件存在对象存储

4. 部署架构升级
   - Nginx 反向代理
   - HTTPS 证书
   - 自动备份
   - 灰度发布和回滚

5. 模块扩展
   - 收入模块
   - 费用模块
   - 利润表模块
   - 权限和审批
