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
- `factory_fee/wsgi.py`

## 本地验证 Docker

在项目根目录执行：

```bash
cp .env.production.example .env
```

然后编辑 `.env`，把 `FACTORY_FEE_SECRET_KEY` 换成一串较长随机字符串。

启动：

```bash
docker compose -f docker-compose.aliyun.yml --env-file .env up --build
```

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

4. 创建生产环境变量：

```bash
cp .env.production.example .env
```

5. 修改 `.env`：

```text
FACTORY_FEE_SECRET_KEY=一串足够长的随机字符串
```

6. 启动服务：

```bash
docker compose -f docker-compose.aliyun.yml --env-file .env up -d --build
```

7. 查看状态：

```bash
docker compose -f docker-compose.aliyun.yml ps
docker compose -f docker-compose.aliyun.yml logs -f
```

## 数据持久化

容器内的数据目录是：

```text
/app/data
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

## 备份建议

最低要求：

```bash
tar -czf factory-fee-data-$(date +%Y%m%d_%H%M%S).tar.gz runtime/data
```

正式使用建议：

- 每日备份 `runtime/data`
- 备份文件上传到 OSS 或公司备份盘
- 发布新版本前先备份
- 重大计算完成后先备份

## 端口和访问

默认服务端口：

```text
5050
```

如果只给公司内网访问，建议：

- ECS 安全组限制来源 IP
- 或挂到公司 VPN 后访问

如果给公网访问，必须增加：

- HTTPS
- 登录权限
- 强密码或企业 SSO
- 操作审计

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
