# 设计决策与经验记录

## 背景

hermes-feishu-agent 是一个探索性项目，目标是打通飞书机器人 → hermes-agent → 受控 LLM 输出 的完整链路。核心探索方向是：在真实的飞书通道上，研究如何对一个工具调用型 Agent 实施有效的输出控制（权限、内容、格式、能力边界）。

## 设计决策

### 1. 为什么选择 hermes-agent 而非 LangChain

**决策：** 使用 hermes-agent（NousResearch 的 `run_agent.AIAgent`）。

**考虑因素：**
- hermes-agent 内建了完整的对话循环、工具调度、session 管理、历史压缩，不需要从头搭建
- `pre_tool_call` plugin hook 提供了官方支持的硬阻断点——这是 LangChain 的 callback 系统做不到的
- hermes 的 `delegate_task` 支持原生多 agent 协作，为未来扩展留了空间

**代价：** hermes 是一个复杂的黑盒，内部行为需要读源码才能理解（例如线程池大小、callback 的只读性质、plugin 的 fail-open 语义）。调试比 LangChain 困难。

### 2. 为什么双层防御而不是单层

**决策：** Plugin（Layer 1）+ guarded() 包装器（Layer 2），两种机制同时运行。

**问题本质：** hermes 内部有独立的工具线程池（`_MAX_TOOL_WORKERS=8`），工具 handler 可能不在设置 thread-local 的消费线程中执行。`threading.local` 的值不会跨线程传递。

**Layer 1（Plugin）：**
- 优势：hermes 内部执行，`session_id` 通过 hermes 框架可靠传递
- 劣势：依赖 plugin 加载成功、session_map 映射存在、权限检查不抛异常。任何一个环节失败 → fail-open

**Layer 2（guarded）：**
- 优势：在 handler 入口执行，不依赖 hermes 内部机制
- 劣势：依赖 thread-local 能在工具线程中读到（大多数情况可以，但不保证）

**为什么不只用 Plugin？** 如果 plugin 文件损坏、config.yaml 误删、或 session_map 因内存压力被清理，权限检查静默失效。Layer 2 是保险丝。

**为什么不只用 guarded？** 线程归属不确定，存在理论上的绕过路径。

### 3. 为什么 session_id 映射而不是直接传 user_id

**决策：** 通过 `session_map` 维护 `session_id → user_id` 映射，Plugin 用 `session_id` 查出 `user_id`。

**为什么不直接把 user_id 注入 plugin hook？** hermes 的 `pre_tool_call` hook 签名是固定的：`(tool_name, args, task_id, session_id, tool_call_id)`。没有 `user_id` 字段。只能利用现有的 `session_id` 参数。

**为什么用 `feishu_{user_id}` 作为 session_id 格式？**
- 人可读：日志中可以直接看到是哪个飞书用户
- 稳定：同一用户的 Agent 不被淘汰就不会变
- 无冲突：`feishu_` 前缀确保不与 hermes 内部 session_id 冲突

**为什么不生成 UUID 做 session_id？** UUID 需要反向映射表（session_id → user_id），多一次查询。`feishu_{user_id}` 可以直接解析——实际上我们仍然走了 session_map，因为要支持 LRU 淘汰后的清理。

### 4. 为什么身份/管理命令在 handler.py 里用关键词匹配而非 Agent

**决策：** 身份查询（`我的权限`）与管理命令（`设置角色 <open_id> <1-5>`、`查看用户`）用正则硬编码匹配，不走 Agent。

**理由：**
- 可靠性：管理命令是格式化指令，不需要 LLM 的理解
- 延迟：关键词匹配 < 1ms，Agent 调用 2-15s
- 安全性：Agent 调用可能被 prompt injection 影响，关键词匹配不会
- 副作用确定性：写身份表 + 落审计是确定性操作，不需要 LLM

> 注：旧版的「申请写入权限 → 管理员批准」自助流程已移除，改为管理员直接 `设置角色`。

**何时该走 Agent：** 只有当指令需要语义理解时（如"帮我查一下最近哪些台架待审批"），才值得付出延迟走 Agent。

### 5. 为什么内容过滤用正则而不是 ML

**决策：** `ocl/content_filter.py` 使用编译正则，不做 ML 分类。

**理由：**
- 延迟：正则 < 1ms，ML 分类 50-500ms（违反 100ms pipeline 预算）
- 确定性：正则没有假阳性概率分布，行为可预测
- 探索目标：项目的核心探索是权限和输出控制架构，不是内容过滤精度

### 6. 为什么不做多 Agent 路由

**讨论过的方案：** Router Agent → Dispatcher → Specialist Agents。

**不做的理由：**
- 当前两域共约 20 个工具，单 Agent 上下文完全够用
- hermes 的 `delegate_task` 的默认深度限制是 1（parent → child），要做路由需要配置 depth=3，增加 3× API 调用成本
- 权限控制放在 Router Agent 里是软约束（LLM prompt），不如 plugin 的硬约束可靠
- 真正需要多 Agent 的标志是工具 > 30 个，不是"担心将来会多"

## 已知局限

1. **Plugin 依赖 `~/.hermes/config.yaml` 配置：** 如果配置文件被误改或删除，Layer 1 失效（Layer 2 仍兜底）
2. **session_map 无 TTL 自动清理：** 如果 Agent 被异常销毁（不经过 LRU 淘汰），映射残留。影响：map 最多 100 条（受 pool max_size 限制），不会无限增长
3. **内容过滤规则有限：** 当前只有政治敏感和密钥泄漏两类正则，覆盖面窄。适合 MVP，不适合生产审查
4. **身份解析依赖飞书 Contact API：** 若用户隐私设置或 app 权限导致拿不到 email，则落为 role=0，需管理员手动 `设置角色`
5. **thread-local 仍有残留风险：** Layer 2 的 thread-local 是概率性防护；身份跨线程靠 `contextvars.copy_context()` 显式传递
