# 全栈部署到本地服务器（192.168.8.101）

把**约车 bot + fmp 后端 + fmp-mcp** 三套全部部署到一台服务器。

> ⚠️ 执行环境：Claude 的沙箱**连不到** `192.168.8.101`，下列命令请在**你自己的终端**执行
> （能 SSH 到服务器、且能访问本机源码目录的那个终端）。卡住把输出贴回对话我帮你看。

---

## 0. 架构总览（为什么有顺序依赖）

```
                         docker_fmp-net（bridge 网络，由 fmp 那套 compose 建出）
   ┌──────────────────────────────────────────────────────────────┐
   │  fmp-mysql   fmp-redis   fmp-nacos   fmp-app(:9013)            │
   │                                  ▲                            │
   │           dmz-fmp-mcp(:9015) ────┘ 调 http://fmp-app:9013     │
   │                  ▲                                            │
   │   dmz-CarBooking(:8088) 调 http://dmz-fmp-mcp:9015/fmpMCP/sse │
   └──────────────────────────────────────────────────────────────┘
```

- 三者**共用 `docker_fmp-net`**，靠**容器服务名**互相寻址（`fmp-app` / `dmz-fmp-mcp`）。
- 网络由 **fmp 那套 compose 建出**（compose 项目目录名 `docker` → 网络名 `docker_fmp-net`）。
- **必须按 fmp → mcp → bot 顺序起**：后两者 `external: docker_fmp-net`，网络不存在会起不来。
- 对外出网需求：bot→飞书(open.feishu.cn WS) + Minimax(api.minimaxi.com)；build 时→maven/pip/dockerhub。

**端口**：fmp-app 9013、mcp 9015、bot 8088（/health）、mysql 13307、redis 6380、nacos 8849/9849。

---

## 1. 服务器前置（SSH 进去做一次）

```bash
ssh kellyzhou@192.168.8.101          # passwd: 123456

uname -m                              # 记下架构：x86_64 还是 aarch64（决定下面 mcp 的 PLATFORM）
docker version && docker compose version   # 没装就先装 Docker + compose 插件
#   Ubuntu/Debian: curl -fsSL https://get.docker.com | sh ; sudo usermod -aG docker $USER（重登生效）
curl -s https://api.minimaxi.com >/dev/null && echo "出网OK" || echo "检查出网/代理"

mkdir -p /opt/dmz                     # 统一部署根目录
```

> 若 `uname -m` 是 **aarch64**：改 `dmz-fmp-mcp/deploy.sh` 里 `PLATFORM="linux/amd64"` → `linux/arm64`，
> 否则会 QEMU 模拟跑得很慢甚至失败。x86_64 则不用改。

---

## 2. 把源码 + 密钥传到服务器（在**本机 Mac 终端**执行）

三套源码都在本机。`rsync` 整目录过去（含 bot 的 `.env`、`identity_map.json` 等 gitignore 文件——
部署必需，但排除 `.venv`/缓存/运行时记忆数据）：

```bash
# ① fmp 后端（含 mysql 种子 02_fmp.sql[已含谌一航]、nacos 配置、fmp-app 源码与 Dockerfile）
rsync -avz --exclude '.git' \
  /Users/chris/IM/dmz-fmp/ \
  kellyzhou@192.168.8.101:/opt/dmz/dmz-fmp/

# ② fmp-mcp（含 deploy.sh、deploy.env、Dockerfile、src）
rsync -avz --exclude '.git' --exclude 'target' \
  /Users/chris/Downloads/dmz-fmp-mcp-dev-260409/ \
  kellyzhou@192.168.8.101:/opt/dmz/dmz-fmp-mcp/

# ③ bot（源码 + .env + data/identity_map.json；排除虚拟环境/缓存/运行时记忆）
rsync -avz \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'data/dmz_memory' --exclude 'data/curator' --exclude 'data/feedback' \
  /Users/chris/IM-Test/hermes-feishu-agent_副本/ \
  kellyzhou@192.168.8.101:/opt/dmz/carbooking/
```

> bot 也可以在服务器上 `git clone` GitLab 的 `dev-0626` 分支，但那样**不含** `.env` 和
> `data/identity_map.json`（gitignore），仍需单独 scp 这两个文件。用上面的 rsync 一步到位更省事。

传完在服务器上核一眼密钥/配置都在：

```bash
ssh kellyzhou@192.168.8.101 'ls -l /opt/dmz/carbooking/.env /opt/dmz/carbooking/data/identity_map.json /opt/dmz/dmz-fmp-mcp/deploy.env /opt/dmz/dmz-fmp/docker/mysql/init/02_fmp.sql'
```

---

## 3. 部署（SSH 进服务器，**严格按顺序**）

### ① fmp 后端栈（建出 docker_fmp-net；首次会 maven build fmp-app，较慢）

```bash
cd /opt/dmz/dmz-fmp/docker          # ★ 必须在 docker/ 子目录起，网络才叫 docker_fmp-net
docker compose up -d --build
# 等 fmp-app 健康（mysql 初始化 + nacos 配置导入 + Java 启动，约 1–3 分钟）
watch -n5 'docker compose ps'       # 看到 fmp-app (healthy) 再继续；Ctrl-C 退出
docker compose logs -f dmz-fmp      # 需要时看启动日志
```

校验：`curl -s http://localhost:9013/fmp/openApiMcp/getUserContext -X POST -H 'Content-Type: application/json' -d '{"mobile":"19943221833"}'` 应返回**谌一航**（种子已含）。

### ② fmp-mcp（接入 docker_fmp-net，暴露 9015）

```bash
cd /opt/dmz/dmz-fmp-mcp
./deploy.sh --no-pull               # --no-pull：用刚传过来的源码 build，不去 git pull
docker logs --tail 20 dmz-fmp-mcp   # 看是否正常连上 fmp-app/nacos
```

### ③ bot（接入 docker_fmp-net，暴露 8088）

```bash
cd /opt/dmz/carbooking
docker compose -f docker-compose.car.yml up -d --build
docker logs -f dmz-CarBooking       # 看到 "Starting WebSocket client" 即起来了
```

---

## 4. 验证（服务器上）

```bash
# bot 健康 + WS 连上飞书
curl -s http://localhost:8088/health        # 期望 {"status":"ok","ws_connected":true,...}

# 端到端：用谌一航 email 查可约车（应返回车列表）
docker exec fmp-app sh -c 'curl -s -X POST http://localhost:9013/fmp/openApiMcp/fetchAvailableVehicles -H "Content-Type: application/json" -d "{\"emailAddress\":\"chenyihang@immotors.com\",\"platform\":\"Orin\"}"'
```

最后**在飞书里给机器人发「我想约车」**——能返回可约车辆即全链路通。

---

## 5. 常见坑

| 症状 | 原因 / 处置 |
|------|------|
| bot/mcp 起不来，报 network `docker_fmp-net` not found | fmp 栈没先起，或 fmp compose 没在 `docker/` 子目录起（网络名不对）。先把 fmp 起好。 |
| mcp build 极慢/失败 | `deploy.sh` 的 `PLATFORM` 与服务器架构不符。aarch64 服务器改成 `linux/arm64`。 |
| bot 报「非平台用户」 | 后端没该用户。本部署的 mysql 种子已含谌一航；其他人需在 fmp 后台或库里加（见之前对话）。 |
| 飞书收不到/发不出 | 服务器出网到 `open.feishu.cn` 不通；或飞书后台「事件订阅」仍指向旧机器。注意一个 app 的 WS 只能一处连，别和旧机器抢。 |
| 约车时段差 8 小时 | 容器 TZ 没生效；compose 都设了 `TZ: Asia/Shanghai`，确认宿主 docker 正常。 |
| getUserContext 返回 data:null | 该用户 `account_status≠1` 或不在库；查 `employee` 表。 |

## 6. 日常运维

```bash
# 改完 bot 代码（源码挂载）→ 只需重启进程
docker restart dmz-CarBooking
# 拉新版 bot 代码
cd /opt/dmz/carbooking && git pull && docker restart dmz-CarBooking   # 若用 git clone 方式
# 全停 / 全起
cd /opt/dmz/dmz-fmp/docker && docker compose down      # 注意：会删 docker_fmp-net，需连带停 mcp/bot
```

> **重要**：飞书机器人的 WebSocket 长连接**同一时刻只能有一处**。切到新服务器后，
> 务必把旧机器（本 Mac）上的 `dmz-CarBooking` 停掉（`docker stop dmz-CarBooking`），否则两边抢连、消息乱跳。
