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
- **客户端加密** - 按任务开关，上传前本地加密，网盘上只有密文（文件名路径不变），下载自动解密
- **离线解密** - 单文件工具，凭**密码**或任务**私钥文件**就能还原，不需要本程序（对标 Synology Cloud Sync Decryption Tool）
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

# 3. 用仓库里的 docker-compose.yml 启动（只改 volumes 的挂载路径即可）
#    远程机器和本地构建共用这一个文件，区别只是加不加 --build
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
  --memory 2g --memory-swap 3g \
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

# 或用 docker-compose（仓库里只有一个 compose 文件）
docker compose pull && docker compose up -d
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
| BAUDUSYNC_NOTIFY_URL | (空) | 通知网关地址，如 `https://notify.example.com`（界面上可覆盖）|
| BAUDUSYNC_NOTIFY_KEY | (空) | 通知网关的分组 token（界面上可覆盖）|
| BAUDUSYNC_NOTIFY_QUIET | 60 | 文件夹静默多少秒后推送（界面上可覆盖）|
| BAIDU_APP_KEY | (内置) | 百度网盘 OAuth App Key |
| BAIDU_APP_SECRET | (内置) | 百度网盘 OAuth App Secret |
| BAIDU_REDIRECT_URI | oob | OAuth 回调方式 |

**并发上传数量**除了环境变量，还能在「全局设置 → 同步设置」里直接改（1–16）：
值存在 `settings` 表（键 `sync_concurrency`），**每一轮同步开始时读取**，改完不用重启，
下一轮生效。环境变量只是没设置过时的默认值——库里一旦有值，以库为准。

### 同步完成通知

在「全局设置 → 同步完成通知」里填网关地址、分组 token 和**设备名称**，点「保存并发送测试」即可验证。
多台机器推同一个网关时，第一行就是设备名，能分清是谁发的。

按**文件夹**推送，不是按任务：同步根目录下的每个一级子文件夹单独统计，
这个文件夹这一波文件传完、并且静默 N 秒（默认 60）没有新文件后，推送一条汇总：

```
同步设备：飞牛NAS
✅ 传输完成
目录 /sync/1
共 50 个文件 · 6 个文件夹
大小 2.13 GB
用时 1分39秒 · 2026-10-29 21:16
```

只有一层分组：`/sync/1/第一天` 和 `/sync/1/第二天` 都归到 `/sync/1` 这一条。
只有一个文件时多一行文件名（`文件：IMG_0042.ARW` 比「共 1 个文件」有用）。
有失败时第二行变 `⚠️ N 个文件失败`，正文点名出错的文件，并在末尾提示去「同步记录」点重试。

设计上的几个取舍：

- **只在真的传了文件时推送**。全是「无变化」的文件夹不推，否则实时同步任务
  每轮扫描都会冒一条「同步完成 0 个文件」。
- **静默期合并**。拷贝大目录时实时任务会连续触发几十轮扫描，静默期把这一波合并成
  一条，而不是几十条。
- **整轮报错立即推**，不参与合并，第二行变成「同步失败」并带上错误信息。

### 定时汇总（每隔 N 小时的小记录）

勾选「定时汇总」并设好间隔（1–72 小时，默认 6）后，每隔 N 小时再推一条这段时间的
传输小结，用来回答「我不在的时候传了多少」：

```
同步设备：飞牛NAS
📊 近 6 小时
共 64 个文件 · 2 个文件夹
大小 2.56 GB
上传 62 个 · 下载 1 个
目录 /sync/1 41 个 · /sync/2 23 个
失败 1 个：bad.mp4
2026-11-01 02:37 → 09:37
```

- 统计口径是 `sync_records`，**不用内存计数器**：重建容器、重启进程都不会让数字归零。
- 这段时间**没有任何传输就不发**（准点推「0 个文件」只会让人把通知静音）；
  但计时照常推进，不会攒到下个周期变成双倍时长。
- 上次推送的时间点存在 `settings` 表（键 `digest_last_at`），所以重启不会重复推；
  停机超过 4 个周期时只统计最近一个周期，不会冒出一条「近 300 小时」。
- 「预览汇总」按钮会立刻按最近 N 小时真实统计并发一条，**不动**下次推送的时间点。
- 汇总和「文件夹传完」是两条独立的线，互不影响。
- **通知失败不影响同步**。推送在后台任务里做，网关连不上只会重试 3 次然后放弃，
  同步状态照常更新。

对接的是 [Heimdallr](https://github.com/LeslieLeung/heimdallr) 的 `POST /push`
（`{"key","title","body","msg_type":"text"}`）。地址只填到域名，`/push` 会自动补。
消息一律用 `msg_type=text`：在微信里看企业微信应用消息时，markdown 会显示
「不支持的消息类型」，只有纯文本正常。

自签 HTTPS 证书连不上时，勾选「跳过证书校验」。


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

### 客户端加密

每个任务可以单独勾选「加密上传」：文件在本地加密后才传上去，网盘上只有密文。
**文件名和目录结构保持原样**（`/照片/2024/a.jpg` 仍叫这个名字），只是内容变成乱码，
所以在网盘客户端里还能认出是哪个文件，但打不开、也扫不出内容。

首次使用要在「全局设置 → 客户端加密」设置一次密码（至少 8 位）。

**开启加密的任务在创建时会自动下载一份「私钥文件」**（`baudusync-task-N-key.json`），
相当于 Synology Cloud Sync 那套里的 `private.pem`：有了它，不需要密码、不需要数据库，
也能解开这个任务上传过的所有云端文件。丢了密码时它是唯一的出路，请离线保存。
创建时没存下来，可以打开任务编辑页点「重新导出本任务私钥」（要输一次密码）。

之后：

- **只影响之后新传的文件**。云端已有的明文文件不会重传，也不会被改写；
- **下载时自动判断**。文件名没变，所以靠文件头（魔数 `BSENC` + 版本号 + 分块大小
  三者都要合法）区分，密文就解密、明文就原样落地，两种可以混在同一个目录里；
  只认魔数不够——一个碰巧以 `BSENC` 开头的普通文件会被误判成密文而下载失败；
- **改密码不用重传任何文件**。密码只用来包裹每个任务的密钥，
  换密码只是重新包裹一次；
- 加密任务**不会**再命中网盘的秒传：哈希是对密文算的，同一个文件在不同任务里
  也不能互相秒传。代价是上传会慢一点，好处是网盘侧完全无法做内容比对。

**密码忘了就永远解不开，没有找回途径。** 为了让 NAS 重启后能无人值守地继续同步，
KEK 会用 `/config/enc.key`（权限 0600）里的密钥再封一份存进数据库，启动时自动解开。
**备份数据库时请把这个文件一起备份**，否则换机器时只有 `.db` 是解不开的。

反过来，如果**不**希望它自动解锁（机器要送修、怕磁盘被直接读），把 `/config/enc.key`
移走就行：库里那份封存副本立刻变成一堆解不开的密文，重启后界面显示「待解锁」，
加密任务停下，必须手动输密码才恢复（输密码时会重新生成一个新的 `enc.key`）。
这也是为什么**只备份数据库没有意义**——光有封存副本、没有 enc.key 什么也解不开。

加密格式（想自己写脚本解密时用得上）：

```
0   5B  "BSENC"      魔数
5   1B  version = 2
6   1B  flags        bit1 = 头后面跟着「钥匙牌」
7   1B  key_id
8   4B  chunk_size   默认 4 MiB，与百度分片对齐
12  8B  nonce_prefix 每文件随机
20  8B  plain_size   解密后校验
28 20B  保留
```

`flags & 2` 时，48 字节头后面紧跟一段 84 字节的「钥匙牌」：

```
48  2B  块长度（含后面的 4+16+2+wrapped）
50  4B  任务号
54 16B  salt（scrypt 用）
70  2B  包裹后的密钥长度（固定 60）
72 60B  被 KEK 包裹的任务密钥（12B nonce + 32B DEK + 16B tag）
```

有这段，**光有密码**就能解密：`密码 + salt → scrypt → KEK → 解开包裹 → 任务密钥`。
不需要数据库、不需要私钥文件，和 Synology 的 Cloud Sync Decryption Tool 一致。
不想要这段（云端连可离线验证密码的样本都不给），把「全局设置 → 客户端加密」里的
「文件头里附带密钥」关掉即可，之后新加密的文件就只能靠私钥文件 / 密钥包解。

每块用 AES-256-GCM，IV = `nonce_prefix + 块序号(4 字节大端)`，块尾附 16 字节认证标签，
**头和钥匙牌一起作为 AAD 认证**。所有块都校验通过才会生成目标文件，中途任何一块被改过
都会整体失败；解密完还会拒绝「头里写的长度之外」的多余字节，所以截断、追加、块重排
都能查出来。空文件也带一个 0 长度明文的标签（密文 148 字节），否则它的头没人保护。

代价说明：加密同步每个文件多一遍磁盘读写（先加密成 `*.bsenc.part`，再上传），
这是百度要求「每 4MB 分片的 md5」、115 要求「整文件 SHA1」导致的——两者的哈希都只能
对完整的密文文件计算，没法边加密边流式上传。**中转文件写在源文件旁边**，所以同步源
目录必须可写（只读挂载的目录没法做加密上传），并且要留出至少「单个最大文件」大小的
剩余空间，否则加密会因磁盘满失败（原文件不受影响）。

### 离线解密（不用跑 BauduSync 也能还原）

云端存的只是密文，但这个格式**不依赖本程序**。仓库里的 `tools/bsenc-decrypt.py`
是个单文件脚本，只需要 Python 3.9+ 和 `cryptography`，拷到任何机器都能用。
它和 Synology 的 Cloud Sync Decryption Tool 一样，**密码和私钥文件都能单独解密**：

```bash
pip install cryptography        # 或 apt install python3-cryptography

# ① 只凭密码：不需要数据库、不需要私钥文件
python3 tools/bsenc-decrypt.py decrypt --password '你的密码' -o ./plain ~/cloud/照片

# ② 只凭私钥文件：连密码都不用（密码忘了就走这条）
python3 tools/bsenc-decrypt.py decrypt --key baudusync-task-1-key.json -o ./plain ~/cloud

# 其它常用动作
python3 tools/bsenc-decrypt.py info --password '你的密码' ~/cloud   # 看概况，不写文件
python3 tools/bsenc-decrypt.py decrypt --password '…' --verify-only ~/cloud   # 只校验
python3 tools/bsenc-decrypt.py decrypt --password '…' --in-place ~/cloud/照片   # 原地覆盖
python3 tools/bsenc-decrypt.py decrypt --password '…' --copy-plain -o ./plain ~/cloud
```

容器里也带着这个脚本，可以不给 NAS 装任何东西直接跑：

```bash
docker exec baudusync python /app/tools/bsenc-decrypt.py \
    decrypt --password '你的密码' -o /downloads/plain /sync/照片
```

三种凭据的取舍：

| 凭据 | 命令参数 | 单独能解吗 | 什么时候用 |
|---|---|---|---|
| **密码** | `--password` | ✅ 能 | 文件头里带着钥匙牌（默认开），最省事 |
| **任务私钥** | `--key task-N-key.json` | ✅ 能 | 密码忘了；或者关掉了钥匙牌 |
| BauduSync 数据库 | `--db baudusync.db` | 需配合密码或 `enc.key` | NAS 还在，想用**当前**密码解所有文件 |
| 界面导出的密钥包 | `--keys keys.json` | 需配合密码 | 只有文件备份、想把解密能力单独保管 |

**为什么密码能单独用**：每个加密文件头里都附了一段「钥匙牌」——salt 加上被密码
派生的 KEK 包裹的任务密钥。工具拿密码现推 KEK，解开这段就得到任务密钥。代价写在
下面的威胁模型里。

**改过密码要注意**：钥匙牌是**加密那一刻**的密码封的。改密码之后，老文件用老密码、
新文件用新密码；想用当前密码一次解全部，就用 `--db` 或 `--keys`（库里存的永远是最新
的包裹密钥），或者干脆用私钥文件（它不随密码变化）。

「全局设置 → 客户端加密 → 导出解密密钥」会下载一个 `baudusync-keys-*.json`
（**要重新输一次密码确认**），相当于把所有任务的解密权打包。建议和私钥文件、`enc.key`、
数据库备份分开放，别和密码放同一处。

脚本的行为和本体保持一致：靠文件头判断是不是密文，不是就跳过（`--copy-plain`
可以把明文也一并复制，用于还原整个混合目录）；每块都验 GCM 标签，改过一位就报
「认证失败」且不落地任何半成品；一个文件失败不影响同批其它文件，退出码非 0。

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

### 客户端加密的威胁模型

这部分不是"开了加密就等于安全"，边界要说清楚：

**能防住**

- 网盘方、任何拿到云盘账号的人看不到文件内容（上传的是 AES-256-GCM 密文）；
- 云端数据泄漏、共享链接被转发——拿到密文也解不开（前提是密码没被试出来，
  见下面「钥匙牌」那条）；
- 改动、截断、拼接云端文件会被检测出来（每块都有认证标签，头和块序号都参与认证）。

**防不住**

- **拿到 NAS 磁盘或 `/config` 备份的人**。为了重启后能无人值守跑，KEK 用
  `/config/enc.key` 封了一份在库里，这个文件和解密程序在同一台机器上。密码本身
  的强度对"能读磁盘的人"没有作用——需要的是磁盘加密（LUKS 之类）。
- **密码忘了没有找回途径**。没有后门、没有恢复码，忘了就基本等于丢失——除非你留着
  创建任务时导出的私钥文件（那东西不依赖密码）。
- **文件头里的「钥匙牌」给了云端一份可以离线验证密码猜想的样本**。为了让「只凭密码」
  能解密，每个加密文件都带着 salt 和被 KEK 包裹的任务密钥；拿到密文的人可以拿它
  逐条试密码（每条都要跑一遍 scrypt，32MB / 0.1 秒级）。强密码下这不构成实际威胁，
  但确实比「云端什么都不给」弱一档。介意就把「文件头里附带密钥」关掉，
  代价是密码不再够用，得靠私钥文件或密钥包。
- **导出的密钥包 `baudusync-keys-*.json` 和任务私钥文件**泄漏 + 密码泄漏 = 全部文件。
  它们是把解密权打包带走，所以导出要求重新输一次密码，也请别和密码放在同一处、
  别扔进同一个网盘。
- **密码在 HTTP 请求体里明文传输**。BauduSync 只监听 HTTP，在内网/USB 网段之外
  暴露端口时，请在前面挂一层 TLS 反代。

另外注意：**不要让两个任务指向同一个云端目录并混用加密开关**。加密任务上传密文、
明文任务上传明文，后同步的会覆盖先同步的，看起来"加密失效"或"文件打不开"。
多个任务共用云端目录时，请让它们的加密开关保持一致。

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
│   │   ├── crypto.py         # 客户端加密 (AES-256-GCM + 每任务密钥)
│   │   ├── notify.py         # 同步完成通知 / 定时汇总
│   │   └── scheduler.py      # 任务调度 (定时/实时监控)
│   └── utils/
│       └── helpers.py        # 工具函数
├── frontend/                 # Web 前端 (原生 HTML/CSS/JS)
│   ├── index.html
│   └── static/
│       ├── css/style.css
│       └── js/app.js
├── tools/
│   └── bsenc-decrypt.py      # 离线解密工具（独立运行，不依赖本程序）
├── .github/workflows/
│   └── docker-publish.yml    # CI/CD 自动构建
├── Dockerfile                # 多架构 Docker 构建
├── docker-compose.yml        # 唯一部署文件（GHCR 镜像，含 2G 内存上限）
├── build.sh                  # 本地多架构构建脚本
└── requirements.txt          # Python 依赖
```

## 技术栈

- **后端**: Python 3.11 + FastAPI + Uvicorn
- **数据库**: SQLite (aiosqlite)
- **HTTP**: aiohttp (异步 HTTP 客户端)
- **调度**: APScheduler + Watchdog (文件监控)
- **加密**: cryptography (AES-256-GCM + scrypt 派生)
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
