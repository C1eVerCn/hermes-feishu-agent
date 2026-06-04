# 飞书 Hermes Agent 框架设计文档

**日期：** 2026-06-02  
**状态：** 待审核  
**阶段：** 飞书链路打通（Phase 1）

---

## 1. 背景与目标

### 1.1 项目目标

构建一个基于 `hermes-agent`（NousResearch）的飞书智能体，核心探索目标：

1. **链路打通**：飞书消息 → WebSocket 接收 → hermes-agent LLM 循环 → 飞书回复
2. **输出控制**：格式、内容边界、权限、工具调用边界、输出长度（后续 Phase 扩展）
3. **可复用性**：换业务域只改配置文件，框架代码不动

### 1.2 Phase 1 交付范围

本文档覆盖 Phase 1：飞书链路打通 + hermes-agent 集成。目标是一条端到端可运行的链路，同时把安全性、可观测性、可靠性的基础设施在这一阶段就埋好，后续扩展不需要补债。

**验收标准：**
- 飞书发一条消息，机器人通过 hermes-agent 调用 Minimax API，返回有意义的回复
- 连接断线后自动重连，不需要人工介入
- 同一条消息不会被处理两次
- 超过 30s 未回复时用户能看到"正在处理"的提示

---

## 2. 架构设计

### 2.1 整体架构

```
飞书用户
  │
  │  WebSocket 长连接（飞书主动推送，无需公网 IP）
  ▼
飞书开放平台
  │
  │  im.message.receive_v1 事件
  ▼
┌─────────────────────────────────────────────────────┐
│                 hermes-feishu-agent                  │
│                                                      │
│  feishu/ws_client.py                                 │
│    ├─ lark-oapi WSClient（WebSocket 接收）            │
│    ├─ 事件去重（内存 LRU，event_id TTL=24h）          │
│    └─ 监管循环（断线指数退避重连）                    │
│                  │                                   │
│                  ▼                                   │
│  feishu/typing_indicator.py                          │
│    └─ 发送"正在处理..."占位消息（>5s 场景）           │
│                  │                                   │
│                  ▼                                   │
│  agent/handler.py（桥接层）                          │
│    ├─ 提取 user_id、chat_id、消息文本                 │
│    ├─ 查找或创建 AIAgent 实例（per user_id）          │
│    └─ 调用 AIAgent.chat()（线程池）                  │
│                  │                                   │
│                  ▼                                   │
│  hermes-agent AIAgent                                │
│    ├─ LLM 调用循环（自动处理工具调用 + 重试）         │
│    ├─ 多轮会话记忆（SQLite state.db）                 │
│    └─ SOUL.md 系统提示                               │
│                  │                                   │
│                  ▼                                   │
│  feishu/sender.py                                    │
│    ├─ 文本消息发送（≤4096 字符）                      │
│    ├─ 超长文本自动分片                               │
│    └─ 限流保护（5 msg/s）+ 指数退避重试              │
│                                                      │
│  infra/health.py                                     │
│    └─ HTTP /health 端点（FastAPI，主线程）            │
│                                                      │
│  infra/metrics.py                                    │
│    └─ 埋点：消息延迟、LLM 耗时、错误率              │
└─────────────────────────────────────────────────────┘
  │
  │  HTTPS，OpenAI-compatible
  ▼
Minimax API（MiniMax-M1 / MiniMax-Text-01）
```

### 2.2 线程模型

```
主线程
  └── FastAPI HTTP 服务（uvicorn，/health 端点）

后台线程 A：WS 监管循环
  └── lark-oapi WSClient（阻塞式，断线自动重启）
       └── 事件回调（快速入队，不阻塞 WS 线程）

后台线程 B：消息消费线程
  └── 从队列取事件 → 去重 → 发 typing → 调 Agent → 发回复

线程池（ThreadPoolExecutor，max_workers=5）
  └── AIAgent.chat() 同步调用（CPU+IO 密集，阻塞）
```

**关键约束：**
- WS 事件回调只做入队，不做任何阻塞操作
- 每个 `user_id` 对应一个 `AIAgent` 实例（hermes-agent 不支持跨实例共享会话）
- `AIAgent` 实例缓存在内存 dict 中（LRU，最多 100 个用户）

---

## 3. 项目结构

```
hermes-feishu-agent/
├── main.py                      # 入口：启动所有线程 + uvicorn
├── .env.example                 # 环境变量模板
├── pyproject.toml               # 依赖声明
│
├── feishu/
│   ├── __init__.py
│   ├── ws_client.py             # WSClient 封装 + 监管重连循环
│   ├── sender.py                # 消息发送：限流 + 分片 + 重试
│   └── typing_indicator.py      # "正在处理..."占位消息
│
├── agent/
│   ├── __init__.py
│   ├── handler.py               # 事件 → Agent → 回复的桥接逻辑
│   └── agent_pool.py            # AIAgent 实例池（per user_id，LRU）
│
├── infra/
│   ├── __init__.py
│   ├── health.py                # FastAPI /health 端点
│   ├── metrics.py               # 埋点计数器（消息量、延迟、错误）
│   └── dedup.py                 # 事件去重（内存 LRU）
│
├── config/
│   ├── __init__.py
│   └── settings.py              # 统一读取 .env
│
└── ~/.hermes/                   # hermes-agent 约定配置目录
    ├── config.yaml              # provider=custom，Minimax endpoint
    └── SOUL.md                  # 系统提示（基础角色定义）
```

---

## 4. 各模块详细设计

### 4.1 feishu/ws_client.py — WebSocket 接收层

**职责：** 维护与飞书的 WebSocket 长连接，处理断线重连，将事件投入消息队列。

**关键设计：**

```python
# 监管循环（外层包装，lark-oapi 内部仅重试 ~7 次）
def supervise_ws():
    delay = 2
    while True:
        try:
            ws = lark.ws.Client(app_id, app_secret, event_handler)
            ws.start()          # 阻塞，内部有限次重试后退出
        except Exception as e:
            log.error(f"WS 断线: {e}，{delay}s 后重连")
            time.sleep(delay)
            delay = min(delay * 2, 60)   # 指数退避，上限 60s
        else:
            delay = 2           # 正常退出时重置延迟

# 事件回调（快速，不阻塞）
def on_message(data: P2ImMessageReceiveV1):
    event_id = data.header.event_id
    if dedup.is_duplicate(event_id):
        return
    event_queue.put(data)       # 非阻塞入队
```

**去重策略：**
- 使用 `functools.lru_cache` 或内存 dict + TTL 模拟（无 Redis 依赖）
- key = `event_id`，TTL = 24 小时（Feishu 最大重传窗口）
- 按 `message_id` 去重（不用 `event_id`，因为图片消息会生成多个 event）

### 4.2 agent/agent_pool.py — Agent 实例池

**职责：** 管理 `AIAgent` 实例的生命周期，保证每个用户有且只有一个 Agent 实例。

```python
class AgentPool:
    def __init__(self, max_size=100):
        self._pool: OrderedDict[str, AIAgent] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get_or_create(self, user_id: str) -> AIAgent:
        with self._lock:
            if user_id in self._pool:
                self._pool.move_to_end(user_id)   # LRU 更新
                return self._pool[user_id]
            agent = AIAgent(
                model=settings.MINIMAX_MODEL,
                provider="custom",
                base_url=settings.MINIMAX_BASE_URL,
                api_key=settings.MINIMAX_API_KEY,
                quiet_mode=True,
                max_iterations=30,                # 防止无限循环
            )
            self._pool[user_id] = agent
            if len(self._pool) > self._max_size:
                self._pool.popitem(last=False)    # 淘汰最旧
            return agent
```

**max_iterations=30：** 限制单次对话最多 30 轮工具调用，防止 Agent 陷入无限循环（Agent 自身缺陷防御）。

### 4.3 agent/handler.py — 桥接层

**职责：** 把飞书事件翻译成 Agent 调用，把 Agent 响应翻译成飞书消息。

```python
def process_event(data: P2ImMessageReceiveV1):
    msg    = data.event.message
    sender = data.event.sender
    user_id = sender.sender_id.open_id
    chat_id = msg.chat_id
    text    = extract_text(msg)     # 支持 text 类型，其他类型返回提示

    if not text.strip():
        sender.send(chat_id, "您好，请输入文字消息。")
        return

    metrics.inc("messages_received")

    # 超过 5s 未响应则发 typing 提示
    typing = TypingIndicator(chat_id, threshold_seconds=5)
    typing.start()

    start = time.monotonic()
    try:
        agent = agent_pool.get_or_create(user_id)
        response = executor.submit(agent.chat, text).result(timeout=120)
        latency = time.monotonic() - start
        metrics.record("llm_latency_seconds", latency)
    except TimeoutError:
        response = "抱歉，响应超时（>120s），请稍后重试。"
        metrics.inc("errors_timeout")
    except Exception as e:
        log.exception(f"Agent 调用失败: {e}")
        response = "抱歉，处理您的消息时出现了错误，请稍后再试。"
        metrics.inc("errors_agent")
    finally:
        typing.stop()

    sender.send(chat_id, response)
```

**超时设计：** `executor.submit(...).result(timeout=120)` 限制单次 LLM 调用最长 120s，防止队列堆积。

### 4.4 feishu/sender.py — 消息发送层

**职责：** 封装飞书发消息 API，处理限流、分片、重试。

**关键设计：**
- **限流令牌桶：** 5 msg/s，使用 `threading.Semaphore` 简单实现
- **分片：** 单条消息 > 4096 字符时自动分片，片头加 `[1/3]` 标记
- **重试：** 最多 3 次，429 指数退避，其他错误立即失败

```python
def send(chat_id: str, text: str):
    chunks = split_text(text, chunk_size=3900)    # 留余量
    for i, chunk in enumerate(chunks):
        prefix = f"[{i+1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        _send_with_retry(chat_id, prefix + chunk)
        if len(chunks) > 1:
            time.sleep(0.2)     # 多片时主动降速

def _send_with_retry(chat_id: str, text: str, max_retries=3):
    rate_limiter.acquire()      # 令牌桶限流
    for attempt in range(max_retries):
        resp = client.im.v1.message.create(...)
        if resp.success():
            return
        if resp.code == 429:
            time.sleep(2 ** attempt)
            continue
        raise LarkSendError(resp.code, resp.msg)
```

### 4.5 feishu/typing_indicator.py — 处理中提示

**职责：** LLM 响应时间 > 5s 时，主动发一条"⏳ 正在处理，请稍候..."提示，LLM 响应后更新该消息。

**实现：** 用 `threading.Timer` 延迟发送占位消息，响应到来后通过 `patch` API 更新消息内容（避免发两条）。

### 4.6 infra/metrics.py — 可观测性

**指标（内存计数器，暴露在 /health 端点）：**

| 指标名 | 类型 | 说明 |
|---|---|---|
| `messages_received_total` | Counter | 收到的消息总数 |
| `messages_processed_total` | Counter | 成功处理的消息总数 |
| `errors_total` | Counter | 错误总数（含子类型标签） |
| `llm_latency_seconds` | Histogram | LLM 响应延迟分布 |
| `agent_pool_size` | Gauge | 当前 Agent 实例数 |
| `ws_reconnects_total` | Counter | WebSocket 重连次数 |

**健康端点（GET /health）响应：**
```json
{
  "status": "ok",
  "ws_connected": true,
  "agent_pool_size": 3,
  "metrics": {
    "messages_received_total": 142,
    "errors_total": 2,
    "llm_latency_p50_seconds": 4.2
  }
}
```

---

## 5. hermes-agent 配置

### 5.1 config.yaml

```yaml
# ~/.hermes/config.yaml
model:
  provider: custom
  default: "MiniMax-Text-01"
  base_url: "https://api.minimax.chat/v1"
  api_key_env: "MINIMAX_API_KEY"
  api_mode: chat_completions

memory:
  memory_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
```

### 5.2 SOUL.md（初始系统提示）

```markdown
# Identity
你是一个飞书智能助手，负责帮助用户解答问题和完成任务。
使用中文回答，语气专业友好。

# Style
- 回答简洁明确，避免废话
- 代码示例使用 Markdown 格式
- 不确定时主动说明，不编造信息

# Avoid
- 不讨论政治敏感话题
- 不提供有害内容
- 不假装自己是人类
```

---

## 6. 环境变量

```bash
# .env.example

# 飞书应用凭证
FEISHU_APP_ID=cli_xxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxx
FEISHU_ENCRYPT_KEY=           # 可选，消息加密时填写
FEISHU_VERIFY_TOKEN=          # 可选

# Minimax API
MINIMAX_API_KEY=xxxxxxxxxx
MINIMAX_BASE_URL=https://api.minimax.chat/v1
MINIMAX_MODEL=MiniMax-Text-01

# Agent 配置
AGENT_MAX_ITERATIONS=30       # 单次对话最大工具调用轮数
AGENT_TIMEOUT_SECONDS=120     # LLM 调用超时
AGENT_POOL_MAX_SIZE=100       # 最大并发用户数

# 服务配置
HTTP_PORT=8080                # 健康检查端点端口
LOG_LEVEL=INFO
```

---

## 7. 安全设计

### 7.1 凭证安全
- 所有密钥通过环境变量注入，不硬编码、不提交到 Git
- `.env` 文件加入 `.gitignore`
- hermes-agent 的 `api_key_env` 配置项只存变量名，不存值

### 7.2 输入验证
- 消息长度限制：单条输入 > 8000 字符时拒绝处理，返回提示（防止 prompt injection 攻击扩大面）
- 消息类型白名单：只处理 `text` 类型，其他类型（文件、语音等）返回"暂不支持"
- 空消息检测：空白字符串直接返回引导提示，不进入 LLM

### 7.3 飞书事件合法性
- lark-oapi SDK 内置飞书签名验证（`encrypt_key` + `verification_token`），框架层无需额外处理
- 事件去重防止重放攻击

### 7.4 LLM 输出安全（Phase 1 基础层）
- hermes-agent 的 SOUL.md 声明内容边界（作为预防层）
- 响应发送前做基础长度检查（> 4096 字符走分片，不截断）
- Phase 2 再加完整的 OCL 输出控制层

### 7.5 资源保护
- `max_iterations=30`：防止 Agent 工具调用无限循环
- `timeout=120s`：防止单次 LLM 调用无限阻塞
- `agent_pool_max_size=100`：防止内存无限增长
- 消息队列 `maxsize=1000`：防止事件堆积撑爆内存
- 线程池 `max_workers=5`：限制并发 LLM 调用数，避免超 Minimax rate limit

---

## 8. 可靠性设计

### 8.1 WebSocket 重连
- 外层监管循环：lark-oapi 内部重试耗尽后，外层以指数退避（2s → 4s → ... → 60s）重启
- 重连次数计入 `ws_reconnects_total` 指标
- 重连期间队列中的消息等待，不丢失（队列容量 1000）

### 8.2 消息处理可靠性
- 事件去重：`message_id` + TTL=24h，防止重复处理
- 异常隔离：单条消息处理失败不影响其他消息
- 超时兜底：120s 超时后返回友好提示，不阻塞队列

### 8.3 Minimax API 可靠性
- 429 Rate Limit：指数退避重试（最多 3 次）
- 网络错误：同样重试，最终失败返回用户友好提示
- hermes-agent 本身有内部重试机制，外层不再双重重试（避免放大请求量）

### 8.4 Agent 自身缺陷防御
| 缺陷类型 | 防御措施 |
|---|---|
| 无限工具调用循环 | `max_iterations=30` 硬上限 |
| LLM 幻觉工具名 | ToolRegistry 收到未知工具名时返回错误 message，让 LLM 自我纠正 |
| 响应超时 | `executor.result(timeout=120)` 强制中断 |
| 上下文窗口溢出 | hermes-agent 内置历史压缩（MEMORY.md 机制） |
| 会话状态污染 | 每用户独立 AIAgent 实例，互不干扰 |
| 空响应 | 检测空字符串，返回"抱歉，未能生成有效回复"兜底 |

---

## 9. 性能与延迟设计

### 9.1 延迟预算

```
飞书事件推送：          ~50ms（WebSocket，飞书侧延迟）
事件入队 + 去重：       ~1ms
typing indicator 触发： 5s（阈值，防止短问题也发提示）
LLM 首次响应：          2s - 15s（Minimax，视 prompt 长度）
消息发送：              ~100ms
──────────────────────
P50 端到端延迟：        ~5s（无工具调用场景）
P95 端到端延迟：        ~30s（有工具调用或长 prompt）
```

### 9.2 并发能力
- 线程池 `max_workers=5`：同时处理 5 个用户的 LLM 请求
- Minimax free tier：~20 RPM，5 并发足够（4 req/worker/min）
- 超出线程池容量的请求在队列中等待，不丢弃

### 9.3 冷启动优化
- Agent 实例池懒加载：首次消息时创建实例，之后复用
- hermes-agent config.yaml 提前加载到内存（服务启动时）

---

## 10. 可观测性设计

### 10.1 日志规范

```
[2026-06-02 10:23:45] INFO  [handler] user=ou_abc123 chat=oc_xyz msg_id=om_001 action=received
[2026-06-02 10:23:45] INFO  [handler] user=ou_abc123 action=agent_start
[2026-06-02 10:23:49] INFO  [handler] user=ou_abc123 action=agent_done latency=4.2s
[2026-06-02 10:23:49] INFO  [sender]  chat=oc_xyz chunks=1 action=sent
[2026-06-02 10:23:50] ERROR [handler] user=ou_abc123 action=agent_error error="timeout after 120s"
```

**日志字段：** `user_id`（脱敏用 open_id，不记录真实姓名/邮箱）、`chat_id`、`message_id`、`action`、`latency`、`error`

### 10.2 关键告警条件（供后续接入监控时参考）
- `ws_reconnects_total` 在 5 分钟内 > 3：WebSocket 不稳定
- `errors_total` 占 `messages_received_total` > 10%：错误率过高
- `llm_latency_p95` > 60s：LLM 服务异常
- `agent_pool_size` > 80：接近上限，需关注内存

---

## 11. 依赖清单

```toml
# pyproject.toml
[project]
name = "hermes-feishu-agent"
requires-python = ">=3.11"

dependencies = [
    "hermes-agent",          # NousResearch Agent 框架
    "lark-oapi>=1.0",        # 飞书官方 SDK
    "fastapi>=0.110",        # 健康检查 HTTP 服务
    "uvicorn>=0.29",
    "python-dotenv>=1.0",    # .env 加载
    "pydantic>=2.0",
    "httpx>=0.27",
]
```

---

## 12. 实现顺序

1. **项目脚手架**：目录结构、pyproject.toml、.env.example、.gitignore
2. **config/settings.py**：读取所有环境变量，启动时验证必填项
3. **feishu/sender.py**：消息发送（限流 + 分片 + 重试），可独立测试
4. **infra/dedup.py**：去重逻辑，可独立单测
5. **infra/metrics.py** + **infra/health.py**：指标收集 + HTTP 端点
6. **~/.hermes/config.yaml** + **SOUL.md**：hermes-agent 配置
7. **agent/agent_pool.py**：AIAgent 实例池
8. **agent/handler.py**：桥接逻辑（先 mock sender，测试 Agent 调用）
9. **feishu/typing_indicator.py**：处理中提示
10. **feishu/ws_client.py**：WebSocket 接收 + 监管重连
11. **main.py**：把所有线程串起来启动
12. **端到端验证**：飞书发消息，验收全链路

---

## 13. 验证方案

### 13.1 单元测试
```bash
pytest tests/ -v
# 覆盖：去重逻辑、消息分片、限流令牌桶、Agent 实例池 LRU
```

### 13.2 端到端验证步骤
1. 启动服务：`python main.py`
2. 确认健康检查：`curl http://localhost:8080/health`，`ws_connected=true`
3. 飞书发"你好"→ 收到 Minimax 响应（验证基础链路）
4. 飞书发超过 4096 字符的长文本 → 收到分片回复（验证分片）
5. 关闭网络 30s 再恢复 → 服务自动重连，不需要重启（验证重连）
6. 快速连发 10 条消息 → 不崩溃，均有响应（验证限流和队列）
7. 检查 `/health` 指标，延迟和错误率符合预期

### 13.3 后续 Phase 验收点（不在本文档范围，记录备用）
- Phase 2：Mock RESTful API + Tool Calling 链路
- Phase 3：OCL 输出控制层（格式、内容边界、权限、工具边界）
- Phase 4：边界情况验证矩阵（20 个失败场景）
- Phase 5：技术文档输出
