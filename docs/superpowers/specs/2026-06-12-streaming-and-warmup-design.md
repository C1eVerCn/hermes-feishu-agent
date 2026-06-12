# Bot 响应延迟优化 — Streaming + 冷启动预热

**日期:** 2026-06-12
**状态:** 设计已批准,待实施
**作者:** brainstorm 流程 (miniMax + 用户)

---

## 1. 目标

- 用户在飞书 DM 发消息后 **p50 ≤ 5 秒** 内看到第一条反馈气泡
- **含冷启动**:容器重启后用户首条消息也满足 5s 标准
- 维持现有 292 单测全绿
- 不引入新依赖 (LLM 限定为 minimax M2.7-highspeed,不换 provider)
- 不破坏现有 OCL pipeline / card builder / identity_map 行为

---

## 2. 当前状态 (基线)

最新一次 trace (2026-06-12 实测):
- 总消息延迟 p50 = 11.98s
- 内部各阶段:
  - `agent_pool.get_or_create`: 1.07s
  - `executor.submit`: 0.00s
  - `future.result` (agent.chat 整体): 10.91s
    - LLM call #1: 1.9s
    - tool: 0.06s
    - LLM call #2: 8.1s
    - agent 内部状态: 0.85s
- 冷启动首条: 33-69s (agent_pool 首次创建 + hermes-agent 内部 lazy init 累计 46s)

LLM 真实延迟 (5-12s) 不可控,**本方案通过让用户在前 2s 看到 streaming 占位把感知延迟从 30s+ 降到 2s**,并通过后台预热避免冷启动的 46s 启动延迟。

---

## 3. 方案:Streaming 端到端 + 后台预热

### 3.1 核心思路

- 已有基础设施:
  - `AIAgent.chat(message, stream_callback=callable | None) -> str` — hermes-agent 已支持 streaming 回调
  - `feishu/typing_indicator.py` — 已接进 handler,2s timer 发占位
  - 缺: 把两者连起来,让 typing 占位被 streaming token 边收边 update
- 新增: agent_pool 首次创建后 spawn 后台线程调一次 dummy "hello" 触发 hermes-agent lazy init

### 3.2 数据流

```
User Feishu DM
    │  message
    ▼
feishu/ws_client → bot/handler._handle
    │
    ├─► TypingIndicator.start()  (2s timer → 发"⏳ 正在处理"占位气泡)
    │
    ├─► agent_pool.get_or_create(user_id)
    │     ├─ cache hit → 立即返
    │     └─ cache miss (cold start) → 创建 AIAgent + spawn 后台 _warmup 线程
    │                                                       (thread 调一次 dummy chat 走完 lazy init)
    │
    ├─► executor.submit(agent.chat, text, stream_callback=_cb)
    │     │
    │     └─► AIAgent.chat(msg, cb)  ┌─ LLM streaming ─┐
    │         │                       │ 每 token → cb(t) │
    │         │                       └──────────────────┘
    │         │                          │
    │         │                          ▼
    │         │                       handler._on_chunk(t)
    │         │                          │ accumulate + 节流 (0.3s)
    │         │                          ▼
    │         │                       sender.edit_message(typing_id, accumulated)
    │         │
    │         ├─ [optional tool call] (~0.1s, fmp backend)
    │         │
    │         └─ LLM streaming (call #2)
    │
    └─► TypingIndicator.stop()  (typing 占位已被 edit_message 覆盖,不删)
        OCL pipeline → card_builder.build_card
        sender.send_card(chat_id, card)  (完整 bench 列表卡片)
```

**冷启动场景**: `agent_pool.get_or_create` 创建新 agent 后立即 spawn `_warmup_thread` 调 `agent.chat("hello")` 触发 hermes-agent 内部 lazy import (anthropic provider, tool registry, prompt template, etc)。后台异步跑,不阻塞主流程。用户首条消息在 ~5s 内到第一条 token。

### 3.3 组件改动

| 文件 | 改动 | 类型 |
|------|------|------|
| `bot/handler.py` | 改 `agent.chat(msg)` → `agent.chat(msg, stream_callback=_cb)`;管理 typing → streaming → final 状态机 | 修改 |
| `bot/handler.py` | 加 `_on_streaming_chunk(token: str)` 内部函数:累积 + 节流 + edit_message | 新增 |
| `feishu/typing_indicator.py` | `stop()` 不删占位消息(让 edit_message 覆盖);加 `edit_message()` helper | 扩展 |
| `feishu/sender.py` | 加 `edit_message(chat_id, msg_id, content)` 静态方法(封装飞书 PATCH `/im/v1/messages/{msg_id}`) | 新增 |
| `bot/agent_pool.py` | `get_or_create()` 首次创建后 spawn `threading.Thread(target=_warmup_agent)` 调一次 `agent.chat("hello")` | 扩展 |
| `tests/unit/test_handler_streaming.py` | 6 个新单测 | 新增 |
| `tests/unit/test_sender_edit.py` | 2 个新单测 | 新增 |

**无新增依赖**,无 config 字段(节流默认 0.3s hardcode)。

### 3.4 节流策略

- 在 `_on_streaming_chunk` 维护 `pending_tokens: list[str]` + `last_flush: float`
- 每收 token → 追加到 `pending_tokens` + 检查 `now - last_flush > 0.3s`
- 超 0.3s → 调一次 `sender.edit_message(typing_id, accumulated)` (合并发送)
- turn done (agent.chat 返回) → 立即 flush 剩余 + 调 TypingIndicator.stop
- 避免每 token 一次 PATCH 拖慢

### 3.5 错误处理

| 失败 | 触发 | 恢复 |
|------|------|------|
| A. Typing 占位发送失败 | 飞书 typing 消息发不出 (群禁言/用户关机器人) | log warning,继续走 stream + card,用户最终仍看到 card |
| B. Stream edit 失败 | PATCH `/messages/{id}` 报 429/网络错 | 累积未发送的 token,下次 push 一起发;连续 3 次失败 → fallback 到原"全量 send_card" 路径 |
| C. Tool call 失败 | fmp backend 不可用 | LLM 收到工具错误按现有逻辑处理 ("服务暂时无法连接"),继续 stream |
| D. 后台预热线程失败 | 首次预热抛异常 (hermes-agent 内部错) | 静默 log,不影响主流程;下次消息时主流程会再走懒加载 |

### 3.6 已知取舍:Typing 占位气泡留在聊天里

飞书 IM API 无 delete 消息能力,只能 PATCH 隐藏。占位气泡(几个字"⏳ 正在处理")留在聊天最终被 edit_message 覆盖成真实回复,不影响阅读。接受这个 UX。

---

## 4. 数据流时序

**稳态场景: 用户发 "查询可用台架"**

```
T=0.00s  User 发消息
T=0.05s  feishu ws 事件入队
T=0.07s  agent_pool.get_or_create 立即返 (cache hit)
T=0.07s  TypingIndicator.start() 启动 2s timer
T=0.10s  future = executor.submit(agent.chat, "查询可用台架", _cb)
         │
         └─ worker thread:
            T=0.5s   LLM streaming "当前共有"        → cb
            T=1.2s   LLM streaming " 9 个可用台架"   → cb
            T=1.5s   LLM "，包含 CT 系"               → cb
            T=1.8s   LLM "列 1 个、TJ 系列 8 个"    → cb
            T=1.9s   LLM #1 done → tool list_available_benches
            T=2.0s   tool returns 9 bench IDs
            T=2.1s   LLM streaming "可用台架:" + 9 IDs → cb
            T=4.5s   LLM #2 done, total 4.5s
T=4.5s   future.result → full text
T=4.5s   TypingIndicator.stop()  (typing 占位已被 edit 覆盖)
T=4.5s   OCL pipeline → card_builder.build_card (含 bench 列表)
T=4.5s   sender.send_card(chat_id, card)
```

**冷启动场景: 容器重启后首条消息**

```
T=0     容器启动
T=+1s   agent_pool 创建,后台 spawn _warmup 线程调 agent.chat("hello")
T=+47s  warmup 完成,hermes-agent 内部 state 准备好
T=+60s  User 发消息 → agent_pool cache hit → 同稳态路径 → T=2s 内看到首 token
```

### 4.1 用户感知时间线

| T | 用户看到什么 |
|---|-------------|
| 0.0s | 消息发出 |
| 2.0s | "⏳ 正在处理..." 占位气泡出现 |
| 2.0-4.5s | 占位气泡被逐字更新 ("当前共有... 9 个..." 滚动显示) |
| 4.5s | 占位变完整回复,下面是 bench 列表卡片 |

**感知延迟: 2s (到第一条反馈),实际总延迟: 4.5s**

---

## 5. 测试

### 5.1 单测 (pytest tests/unit/,无网络)

| 测试 | 验证 |
|------|------|
| `test_handler_streaming.py::test_typing_then_streaming_then_card` | happy path: typing.start → cb 收 3 token → ≥0.3s flush → edit_message → future.result → typing.stop → send_card |
| `test_handler_streaming.py::test_typing_placeholder_failure_continues` | 失败模式 A: typing 发送失败不阻断 stream/card |
| `test_handler_streaming.py::test_stream_edit_failure_3_times_fallback` | 失败模式 B: 连续 3 次 edit 错 → 走原 send_card 路径 |
| `test_handler_streaming.py::test_warmup_thread_fired_on_first_create` | 冷启动: get_or_create 首次创建 → spawn 预热线程 |
| `test_handler_streaming.py::test_no_warmup_on_cache_hit` | 稳态: cache hit → 不 spawn 预热 |
| `test_handler_streaming.py::test_warmup_failure_does_not_block_main` | 失败模式 D: warmup 抛错不阻断主流程 |
| `test_sender_edit.py::test_edit_message_uses_patch_url` | 飞书 PATCH `/im/v1/messages/{msg_id}` URL + payload 正确 |
| `test_sender_edit.py::test_edit_message_retries_on_429` | 429 触发重试 |

### 5.2 手动 e2e (用户跑)

1. 容器重启: `docker compose -f docker-compose.bot.yml up -d --force-recreate`
2. 飞书发 "查询可用台架"
3. **观察现象**:
   - T=2s 内看到 "⏳ 正在处理..." 占位气泡
   - T=3-4s 占位气泡被逐字更新
   - T=5s 内看到完整 bench 列表卡片
4. 飞书发 "查询 TJ 系列"
5. 同样 5s 内反馈 (稳态更快)

### 5.3 通过条件

- 单测: 现有 292 + 新增 8 = **300 全绿**
- e2e: p50 < 5s **含冷启**;用户感知"机器人立刻有反应"
- 现有 typing_indicator 集成不被破坏 (bot 仍能优雅 fallback)

---

## 6. 实施计划

待 writing-plans skill 拆分。本 spec 已批准后即开始。
