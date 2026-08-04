# BauduSync - NAS 云同步

仿群晖 CloudSync 的云同步应用，支持百度网盘和 115 网盘，通过 Docker 部署在 NAS 上，同时支持 x86 (amd64) 和 ARM64 架构。

## 功能特性

### 同步能力
- **双向同步** - 本地与云端文件双向同步，冲突时以较新文件为准
- **仅上传 / 仅下载** - 单向同步模式
- **实时同步** - 检测文件变化自动触发同步（watchdog 文件监控）
- **定时同步** - 按间隔自动执行同步（APScheduler 调度）
- **手动同步** - 按需一键触发
- **文件过滤** - 按扩展名、大小过滤文件

### 任务控制
- **暂停 / 恢复** - 每个任务可随时暂停和恢复
- **取消单个文件** - 正在传输的文件可单独取消（跳过该文件）
- **状态标签** - 实时显示「同步中 / 暂停中 / 等待中 / 空闲」状态

### 云盘支持
- **百度网盘** - 支持 OAuth 授权码登录和 Token 直填
- **115 网盘** - 支持扫码登录和 Cookie 直填
- **秒传支持** - 基于文件哈希的快速上传
- **分块上传** - 大文件分块上传，支持断点续传

### 其他
- **日志系统** - 聚合展示所有任务同步日志，支持自动刷新、清除
- **下载管理** - 从网盘下载文件到本地下载目录
- **数据持久化** - 数据库、日志、配置全部持久化到 `/config` 目录

## 快速开始

### 方式一：拉取预构建镜像（推荐）

镜像通过 GitHub Actions 自动构建，支持 amd64 和 arm64 双架构。

```bash
# 1. 登录 GHCR（用你的 GitHub Token）
echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u ghostyubin --password-stdin

# 2. 拉取镜像
docker pull ghcr.io/ghostyubin/erscloudsync:latest

# 3. 创建 docker-compose.yml 并启动
#    参考 docker-compose.test.yml
docker compose up -d

# 4. 访问 Web 界面
#    http://<IP>:5566
```

### 方式二：本地构建

```bash
# 克隆仓库
git clone https://github.com/ghostyubin/erscloudsync.git
cd erscloudsync

# 构建并启动
docker compose up -d --build

# 访问 Web 界面
# http://<IP>:5566
```

### Docker 命令部署

```bash
docker run -d \
  --name baudusync \
  --restart unless-stopped \
  -p 5566:5566 \
  -v /path/to/config:/config \
  -v /path/to/sync:/sync \
  -v /path/to/downloads:/downloads \
  -e TZ=Asia/Shanghai \
  ghcr.io/ghostyubin/erscloudsync:latest
```

## CI/CD 自动构建

项目配置了 GitHub Actions（`.github/workflows/docker-publish.yml`），每次 push 代码到 `main` 分支会自动：

1. 构建多架构 Docker 镜像（`linux/amd64` + `linux/arm64`）
2. 推送到 GitHub Container Registry
3. 打标签：`latest`、分支名、commit SHA 短哈希

**更新镜像：**
```bash
# 其他机器拉取最新镜像
docker pull ghcr.io/ghostyubin/erscloudsync:latest

# 或用 docker-compose
docker compose -f docker-compose.test.yml pull && docker compose -f docker-compose.test.yml up -d
```

## 配置说明

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| BAUDUSYNC_HOST | 0.0.0.0 | Web 服务监听地址 |
| BAUDUSYNC_PORT | 5566 | Web 服务端口 |
| BAUDUSYNC_DATA_DIR | /config | 数据目录（数据库、日志）|
| BAUDUSYNC_SYNC_ROOT | /sync | 本地同步根目录 |
| BAUDUSYNC_DOWNLOAD_DIR | /downloads | 下载文件存放目录 |
| BAUDUSYNC_CONCURRENCY | 3 | 并发同步文件数 |
| BAUDUSYNC_CHUNK_SIZE | 8388608 | 传输分块大小（字节）|
| BAIDU_APP_KEY | (内置) | 百度网盘 OAuth App Key |
| BAIDU_APP_SECRET | (内置) | 百度网盘 OAuth App Secret |
| BAIDU_REDIRECT_URI | oob | OAuth 回调方式 |

### 卷挂载说明

| 容器路径 | 用途 | 持久化 |
|----------|------|--------|
| /config | 数据库、日志、配置 | 必须 |
| /sync | 同步文件根目录 | 必须 |
| /downloads | 从网盘下载的文件 | 可选 |

在创建同步任务时，本地目录是相对于 `/sync` 的路径。例如：
- 任务本地目录设为 `/photos`，实际路径为 `/sync/photos`

### 百度网盘连接

**方式一：OAuth 授权码（推荐）**
1. 在 Web 界面点击「添加连接」→「百度网盘」
2. 点击授权链接，登录百度网盘
3. 复制页面显示的授权码
4. 将授权码粘贴回 BauduSync

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

## 同步模式说明

| 模式 | 说明 |
|------|------|
| 双向同步 | 本地和云端双向同步，冲突时以较新文件为准 |
| 仅上传 | 只将本地文件上传到云端 |
| 仅下载 | 只将云端文件下载到本地 |

## 项目结构

```
erscloudsync/
├── app/
│   ├── main.py              # FastAPI 主应用 + 日志配置
│   ├── config.py             # 配置管理
│   ├── database.py           # SQLite 数据库 CRUD
│   ├── api/                  # REST API 端点
│   │   ├── connections.py    # 云连接管理
│   │   ├── tasks.py          # 同步任务 + 暂停/恢复/取消
│   │   ├── downloads.py      # 下载管理
│   │   ├── logs.py           # 日志查询/清除
│   │   └── system.py         # 系统信息/健康检查
│   ├── providers/            # 云存储 Provider
│   │   ├── base.py           # Provider 基类
│   │   ├── baidu.py          # 百度网盘 (OAuth + PCS API)
│   │   └── p115.py           # 115 网盘 (扫码 + Cookie)
│   ├── services/
│   │   ├── sync_engine.py    # 同步引擎 (暂停/恢复/取消)
│   │   └── scheduler.py      # 任务调度 (定时/实时监控)
│   └── utils/
│       └── helpers.py        # 工具函数
├── frontend/                 # Web 前端 (原生 HTML/CSS/JS)
│   ├── index.html
│   └── static/
│       ├── css/style.css
│       └── js/app.js
├── .github/workflows/
│   └── docker-publish.yml    # CI/CD 自动构建
├── Dockerfile                # 多架构 Docker 构建
├── docker-compose.yml        # NAS 本地部署
├── docker-compose.test.yml   # 远程机器部署 (GHCR 镜像)
├── build.sh                  # 本地多架构构建脚本
└── requirements.txt          # Python 依赖
```

## 技术栈

- **后端**: Python 3.11 + FastAPI + Uvicorn
- **数据库**: SQLite (aiosqlite)
- **HTTP**: aiohttp (异步 HTTP 客户端)
- **调度**: APScheduler + Watchdog (文件监控)
- **日志**: RotatingFileHandler (5MB, 3 backups)
- **前端**: 原生 HTML/CSS/JS
- **Docker**: python:3.11-slim 基础镜像
- **CI/CD**: GitHub Actions → GHCR

## 开发

```bash
# 克隆仓库
git clone https://github.com/ghostyubin/erscloudsync.git
cd erscloudsync

# 本地运行（开发模式）
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 5566 --reload

# 修改代码后推送，自动触发 CI 构建
git add -A && git commit -m "你的修改" && git push
```
