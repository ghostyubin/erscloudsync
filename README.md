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
| BAUDUSYNC_CONCURRENCY | 3 | 并发同步文件数（界面上可覆盖，见下）|
| BAUDUSYNC_CHUNK_SIZE | 8388608 | 传输分块大小（字节）|
| BAIDU_APP_KEY | (内置) | 百度网盘 OAuth App Key |
| BAIDU_APP_SECRET | (内置) | 百度网盘 OAuth App Secret |
| BAIDU_REDIRECT_URI | oob | OAuth 回调方式 |

**并发上传数量**除了环境变量，还能在「全局设置 → 同步设置」里直接改（1–16）：
值存在 `settings` 表（键 `sync_concurrency`），**每一轮同步开始时读取**，改完不用重启，
下一轮生效。环境变量只是没设置过时的默认值——库里一旦有值，以库为准。

下面三个只在 `docker-compose.yml` 里做路径插值（宿主机的卷挂载点），**不传入容器**：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| BAUDUSYNC_CONFIG_DIR | ./config | 宿主机侧的 /config 挂载源 |
| BAUDUSYNC_SYNC_DIR | ./sync | 宿主机侧的 /sync 挂载源 |
| BAUDUSYNC_DOWNLOAD_DIR | ./downloads | 宿主机侧的 /downloads 挂载源 |

建议把它们写进 `.env`（已被 git 忽略），而不是改 compose 文件：

```bash
cat > .env <<'EOF'
BAUDUSYNC_CONFIG_DIR=/volume1/docker/baudusync
BAUDUSYNC_SYNC_DIR=/volume1/sync
BAUDUSYNC_DOWNLOAD_DIR=/volume1/downloads
EOF
```

> 关于内置百度凭据：`BAIDU_APP_KEY/SECRET` 默认值是 ES 文件管理器的公开凭据
> （来源见 [OpenListTeam 讨论](https://github.com/orgs/OpenListTeam/discussions/19)），
> 属于"公开的第三方客户端凭据"而非本项目的私有密钥，因此随代码分发。
> 如要换成自己的应用，用环境变量或界面里的设置覆盖即可，无需改代码。

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

## 跳过与重试

传输列表里的「跳过」是**持久决定**，不是「中断这一次」：

- 点击后该文件写入 `sync_ignored` 表，后续扫描一律跳过，重启也在
- 同时会在**传输记录**里留一条 `已跳过` 的行（状态 `skipped`，灰色标记），
  状态下拉可筛「仅已跳过」，任务编辑弹窗里也可以逐个恢复

失败和跳过的记录都带「重试」按钮，点它会：

1. 清掉该文件的忽略标记和失败退避计时
2. 立即起一轮同步（若正在同步则并入当前这一轮）
3. 成功之后，这条失败/跳过行会被自动清掉——成功行就是最新状态，
   留着旧行会让人误以为还没修好

工具栏还有「重试全部」（在「清空」左边）：一次重试所有失败/跳过的记录，
按任务分组各自起一轮同步；没有失败/跳过时置灰。

每条记录右侧有删除按钮（垃圾桶图标），只删这条历史，不动本地或云端文件；
某文件反复失败时它下一轮还会写出新的失败行，删除不等于忽略。

行左侧有状态色条：**绿=成功、红=失败、灰=已跳过**，扫一眼就能分出来。

> 传输记录里的失败行是按「同任务 + 同动作 + 同目标」合并的：
> 同一个文件反复失败只占一行，`retry_count` 递增（显示为 `失败 ×N`），
> 不会像早期版本那样每轮刷出一条新记录。

## 排错

### 容器内所有域名解析失败（`Temporary failure in name resolution`）

表现：添加百度授权时报
`❌ Code exchange failed: Cannot connect to host openapi.baidu.com:443 ssl:default [Temporary failure in name resolution]`，
但宿主机上网、容器内直接 `ping 223.5.5.5` 都正常。

原因不在网络，而在 Docker 的内嵌 DNS。用户自定义网络（compose 默认建的
`<项目名>_default`）里，每个容器的 `127.0.0.11` 由 dockerd 在容器 netns 内插一条
DNAT 规则来支撑：

```
-t nat -I DOCKER_OUTPUT -d 127.0.0.11 -p udp --dport 53 -j DNAT --to-destination 127.0.0.11:<port>
```

如果宿主机 dockerd 是以 `iptables='0'` 运行的（OpenWrt 的 UCI 配置
`dockerd.globals.iptables='0'`，或 `daemon.json` 里关了 iptables），这条规则插不进去，
dockerd 日志会出现：

```
level=error msg="Resolver Start failed for container ..., \"setting up IP table rules failed: ...\""
```

内嵌解析器根本没起来，容器里任何域名查询都返回 `[Errno -3]`。此时
**只有默认 `bridge` 网络能用**，自定义网络全军覆没。

注意 `docker run --dns` / compose 的 `services.*.dns:` **解决不了**——它只是给那个
（已经死掉的）内嵌解析器配置上游地址。

可行的修法（选一）：

1. **挂载自定义 resolv.conf（改动最小，推荐）**。文件被挂载后 Docker 不再自动生成它，
   查询直接发给列出的 DNS，绕过坏掉的内嵌解析器：

   ```yaml
   volumes:
     # 必须写宿主机绝对路径，原因见下面的「相对路径陷阱」
     - /path/to/stack/resolv.conf:/etc/resolv.conf:ro
   ```

   `resolv.conf` 内容：

   ```
   nameserver 223.5.5.5
   nameserver 119.29.29.29
   options timeout:2 attempts:2
   ```

#### 相对路径陷阱：`./resolv.conf` 在 Dockge / Portainer 下会炸

如果你是用 **Dockge / Portainer 这类容器化的面板**管理 compose，绑定挂载**必须写宿主机
绝对路径**，不能写 `./resolv.conf`。

原因是面板自己在容器里跑 `docker compose`：Dockge 把宿主机的 `.../dockge/stacks` 挂成
容器内的 `/opt/stacks`，执行时工作目录是 `/opt/stacks/<栈名>`，于是 `./resolv.conf` 被解析成
`/opt/stacks/<栈名>/resolv.conf`。这个路径在**宿主机上不存在**，Docker 会贴心地**自动建成
一个同名空目录**，然后挂载时才发现类型不对：

```
error mounting "/opt/stacks/<栈名>/resolv.conf" to rootfs at "/etc/resolv.conf":
  flags: 0x5000: not a directory: ... Are you trying to mount a directory onto a file?
```

坑在于：**从宿主机手动 `cd` 到栈目录跑 `compose up -d` 是好的**（相对路径解析正确），
只有在面板里点「更新/重启」才会炸，所以很容易误判成"面板有 bug"。

修复两步：

```bash
# 1. compose 里改成绝对路径
sed -i 's|\./resolv\.conf:|/mnt/.../stacks/<栈名>/resolv.conf:|' compose.yaml

# 2. 删掉 Docker 误建的空目录（否则它会一直挡着）
rmdir /opt/stacks/<栈名>/resolv.conf /opt/stacks/<栈名> /opt/stacks
```

验证方式（在面板容器内部解析，这才是面板的真实视角）：

```bash
docker exec dockge-dockge-1 sh -c 'cd /opt/stacks/<栈名> && docker compose config' | grep -A1 resolv
# source 应该是宿主机绝对路径
```

2. 让 dockerd 重新接管 iptables（清掉 `iptables='0'`），然后重启 dockerd。
   副作用是 OpenWrt 上的防火墙规则会多出 Docker 的链，视路由器策略而定。

验证方式：

```bash
docker exec baudusync python -c "import socket;print(socket.gethostbyname('openapi.baidu.com'))"
docker exec baudusync python -c "import urllib.request;print(urllib.request.urlopen('https://openapi.baidu.com/oauth/2.0/token',timeout=10).status)"
```

### compose 里写了空的 `BAIDU_APP_KEY=` 导致授权失败

compose 的 `environment` 若写成 `- BAIDU_APP_KEY=`（值为空），变量会被设成空字符串，
从而**覆盖**内置的 ES 文件管理器凭据，授权请求会带着空 `client_id` 发出去。
代码里已按「空值视为未配置」处理（`os.environ.get(k) or DEFAULT`），
但更稳妥的做法是**不要写这一行**。需要自定义凭据时才填非空值。

## 安全与脱敏

仓库和镜像里**不含**任何用户凭据：

- 凭据（百度 access/refresh token、115 的 UID/CID/SEID Cookie）只存在
  `/config/baudusync.db`，这个目录是宿主机挂载进来的，既不会进镜像也不会进仓库
- Dockerfile 只 `COPY app/ frontend/` 两个目录，`data/`、`config/`、`.env`
  在 `.dockerignore` 里也被排除
- `.gitignore` 覆盖 `data/ config/ sync/ downloads/ *.db *.log .env`
- `GET /api/connections` 返回的 `credentials` 全部替换成 `***`
  （`access_token` / `refresh_token` / 115 的 Cookie 都在内），
  只额外给出 `token_expired`、`token_expires_at` 这类元信息
- 应用密钥接口对 `app_secret` 永远返回 `***`；`app_key` 仅在用内置凭据时明文回显
  （它本来就会出现在授权页 URL 里），自定义的 key 只显示前 4 位
- 错误信息拼接的是 `errno` 和 `errmsg`，不拼 URL，因此 `access_token`
  不会随异常写进日志、任务历史或下载记录

唯一硬编码的是**内置的百度公开凭据**（见上面的说明），可用环境变量覆盖。

### 空文件（0 字节）上传报 `Precreate failed: {'errno': 2}`

百度 `precreate` 对 0 字节文件要求 `block_list` **至少有一项**，传空数组 `[]` 会返回
`errno 2`——这个错误码看着像"文件或目录不存在"，很容易误判成远端子目录没建好（其实目录好好的）。

正确做法是把空内容的 MD5 当唯一分片传：

```
block_list = ["d41d8cd98f00b204e9800998ecf8427e"]
```

`precreate` 会返回 uploadid 且 `block_list: []`（没有分片要传），跳过超级上传直接
`create` 即可落地一个 0 字节文件。代码里由 `EMPTY_FILE_MD5` 常量处理。

### 某个文件一直失败、同步记录刷屏

历史版本对失败的文件没有任何记忆：`upload_only` 的判定是
`if not remote_file or local_changed → 上传`，远端没有这个文件就**每个周期都重试一次**，
realtime 任务于是每 30 秒写一条失败记录。

现在失败会记入 `sync_failures` 表并退避 **1 → 5 → 15 → 60 分钟**（封顶 1 小时）；
本地文件被修改后会立即重置，不等退避结束。同一文件的相同失败在同步记录里只占一行，
重复次数记在 `retry_count`。

要立刻重试，把任务暂停再启动（或改一下文件 mtime）即可。

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
