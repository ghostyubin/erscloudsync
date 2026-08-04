# BauduSync - NAS 云同步

仿群晖 CloudSync 的云同步应用，支持百度网盘和 115 网盘，可运行在 Docker 上，同时支持 x86 和 ARM64 (RK3576) 架构的 OpenWrt 系统。

## 功能特性

- **双向同步** - 本地与云端文件双向同步
- **仅上传/仅下载** - 单向同步模式
- **实时同步** - 检测文件变化自动同步
- **定时同步** - 按间隔自动执行同步
- **手动同步** - 按需触发
- **文件过滤** - 按扩展名、大小过滤文件
- **百度网盘** - 支持 OAuth 授权和 Token 直填
- **115 网盘** - 支持扫码登录和 Cookie 直填
- **秒传支持** - 基于文件哈希的快速上传
- **分块上传** - 大文件分块上传，支持断点续传
- **多架构** - 同时支持 linux/amd64 和 linux/arm64

## 快速开始

### Docker Compose 部署（推荐）

```bash
# 1. 克隆或复制项目到 NAS
mkdir -p /vol1/1000/baudusync
cd /vol1/1000/baudusync

# 2. 修改 docker-compose.yml 中的卷挂载路径
# 将 /vol1/1000/sync 改为你的 NAS 存储路径

# 3. 构建并启动
sudo docker compose up -d

# 4. 访问 Web 界面
# http://<NAS-IP>:8099
```

### Docker 命令部署

```bash
docker run -d \
  --name baudusync \
  --restart unless-stopped \
  -p 8099:8099 \
  -v baudusync-data:/app/data \
  -v /path/to/your/nas/folder:/sync \
  -e BAUDUSYNC_SYNC_ROOT=/sync \
  -e TZ=Asia/Shanghai \
  baudusync:latest
```

## 多架构构建

同时构建 x86 和 ARM64 (RK3576) 镜像：

```bash
# 创建 buildx builder
docker buildx create --name baudusync-builder --use

# 构建双架构镜像
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t baudusync:latest \
  --load \
  .

# 或分别构建
docker buildx build --platform linux/amd64 -t baudusync:amd64 --load .
docker buildx build --platform linux/arm64 -t baudusync:arm64 --load .
```

## 配置说明

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| BAUDUSYNC_HOST | 0.0.0.0 | Web 服务监听地址 |
| BAUDUSYNC_PORT | 8099 | Web 服务端口 |
| BAUDUSYNC_SYNC_ROOT | /sync | 本地同步根目录（Docker 卷挂载点）|
| BAUDUSYNC_DATA_DIR | /app/data | 数据目录（数据库存储）|
| BAUDUSYNC_CONCURRENCY | 3 | 并发同步文件数 |
| BAUDUSYNC_CHUNK_SIZE | 8388608 | 传输分块大小（字节）|
| BAIDU_APP_KEY | (空) | 百度网盘 OAuth App Key |
| BAIDU_APP_SECRET | (空) | 百度网盘 OAuth App Secret |
| BAIDU_REDIRECT_URI | http://localhost:8099/.../callback | OAuth 回调地址 |

### 百度网盘连接

**方式一：OAuth 授权（需配置 App Key）**
1. 在 [百度网盘开放平台](https://pan.baidu.com/union/) 注册应用
2. 设置环境变量 `BAIDU_APP_KEY` 和 `BAIDU_APP_SECRET`
3. 在 Web 界面点击「添加连接」→「百度网盘」
4. 点击授权链接，登录并授权
5. 将授权码粘贴回 BauduSync

**方式二：直接输入 Token**
1. 通过其他方式获取百度网盘 access_token
2. 在 Web 界面直接填入 Token

### 115 网盘连接

**方式一：扫码登录（推荐）**
1. 在 Web 界面点击「添加连接」→「115 网盘」
2. 点击「获取二维码」
3. 使用 115 手机 App 扫码
4. 在手机上确认登录

**方式二：Cookie 直填**
1. 从浏览器获取 115 网盘的 Cookie（UID, CID, SEID）
2. 填入对应输入框

## 卷挂载说明

| 容器路径 | 用途 | NAS 路径示例 |
|----------|------|-------------|
| /app/data | 数据库和配置 | /vol1/1000/baudusync/data |
| /sync | 同步文件根目录 | /vol1/1000/sync |

在创建同步任务时，本地目录是相对于 `/sync` 的路径。例如：
- 任务本地目录设为 `/photos`，实际路径为 `/vol1/1000/sync/photos`

## 同步模式说明

| 模式 | 说明 |
|------|------|
| 双向同步 | 本地和云端双向同步，冲突时以较新文件为准 |
| 仅上传 | 只将本地文件上传到云端 |
| 仅下载 | 只将云端文件下载到本地 |

## 在 OpenWrt 上运行

### 前提条件
- OpenWrt 已安装 Docker
- 设备架构为 x86_64 或 ARM64（RK3576）

### 部署步骤
1. 将构建好的镜像传输到 OpenWrt 设备：
   ```bash
   # 在构建机器上
   docker save baudusync:latest | gzip > baudusync.tar.gz
   # 传输到 OpenWrt
   scp baudusync.tar.gz root@openwrt:/tmp/
   # 在 OpenWrt 上加载
   docker load < /tmp/baudusync.tar.gz
   ```

2. 创建 docker-compose.yml 并启动：
   ```bash
   docker compose up -d
   ```

3. 配置防火墙开放 8099 端口（如需要外网访问）

## 项目结构

```
baudusync/
├── app/
│   ├── main.py              # FastAPI 主应用
│   ├── config.py             # 配置管理
│   ├── database.py           # SQLite 数据库
│   ├── api/                  # REST API 端点
│   │   ├── connections.py    # 云连接管理
│   │   ├── tasks.py          # 同步任务管理
│   │   └── system.py         # 系统信息
│   ├── providers/            # 云存储 Provider
│   │   ├── base.py           # Provider 基类
│   │   ├── baidu.py          # 百度网盘
│   │   └── p115.py           # 115 网盘
│   └── services/
│       ├── sync_engine.py    # 同步引擎
│       └── scheduler.py      # 任务调度
├── frontend/                 # Web 前端
├── Dockerfile                # Docker 构建文件
├── docker-compose.yml        # Docker Compose 配置
└── build.sh                  # 多架构构建脚本
```

## 技术栈

- **后端**: Python 3.11 + FastAPI + Uvicorn
- **数据库**: SQLite (aiosqlite)
- **HTTP**: aiohttp (异步 HTTP 客户端)
- **调度**: APScheduler + Watchdog (文件监控)
- **前端**: 原生 HTML/CSS/JS
- **Docker**: python:3.11-slim 基础镜像
