# Windows 本地部署包使用说明

这个部署包用于在公司 Windows 电脑上运行完整的本地版“工厂加工费管理平台”。

## 适用范围

- 单机运行
- 会议现场演示
- 规则验证
- 小范围内部试用

数据会保存在当前电脑的项目目录中，不会自动上传到云端。

## 准备工作

Windows 电脑需要安装 Python 3.10 或以上版本。

下载地址：

https://www.python.org/downloads/windows/

安装 Python 时建议勾选：

- Add python.exe to PATH

如果公司电脑不能安装软件，请联系 IT，或使用公司允许的 Python 安装方式。

## 启动方式

1. 解压部署包。
2. 进入解压后的文件夹。
3. 双击：

```text
Start_Windows.bat
```

如果你的 Windows 解压工具能正常显示中文文件名，也可以双击 `启动本地管理台.bat`，两者作用相同。

首次启动会自动：

- 创建 `.venv` 本地 Python 环境
- 安装依赖
- 启动管理台
- 打开浏览器

如果部署包里带有 `wheelhouse` 文件夹，会优先使用离线依赖安装，不需要访问外网。

浏览器访问地址：

```text
http://127.0.0.1:5050
```

## 关闭方式

关闭启动窗口即可停止管理台。

## 数据保存位置

- 数据库：`data/factory_fee.db`
- 上传文件与配置：`data/input/`、`data/config_tables/`、`config/`
- 输出结果：`data/output/`

如果要备份整套本地数据，可以复制整个项目文件夹。

## 常见问题

### 1. 双击后提示找不到 Python

说明电脑还没有安装 Python，或 Python 没有加入 PATH。

先安装 Python 3.10+，重新打开一个窗口再启动。

### 2. 依赖安装失败

常见原因是公司网络限制了 pip 下载。

优先使用带 `wheelhouse` 的离线部署包。也可以让 IT 配置 Python pip 镜像源，或在可联网环境完成首次依赖安装。

### 3. 浏览器打不开 127.0.0.1:5050

先确认启动窗口没有报错，并且窗口还开着。

如果 5050 端口被占用，可以打开 PowerShell 手动运行：

```powershell
.\.venv\Scripts\python.exe scripts\run_web.py --port 5051
```

然后访问：

```text
http://127.0.0.1:5051
```

### 4. 能不能多人同时用？

这个部署包是单机版。多人同时在线使用需要云端化改造，包括云数据库、对象存储、登录权限和操作审计。
