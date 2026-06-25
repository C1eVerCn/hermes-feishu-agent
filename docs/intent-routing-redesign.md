# 意图路由重设计：Router-first Hybrid（2026-06-25）

> 解决"流程仍有问题 / agent 主观性体现在哪 / 想要自由度但不漂移"的架构重构记录。

## 1. 背景：旧设计的两个病

### 1.1 hermes agent 的"主观性"几乎为零

重构前，LLM 在系统里**真正能自主决策**的地方只有两处：

| 位置 | LLM 的自主空间 | 实际被压成 |
|---|---|---|
| `handler._handle` 末尾 | 仅当前面所有正则 miss 后才轮到 LLM | **最后兜底**，不是主角 |
| `agent_pool` system prompt | "单轮最多 2 次工具调用""不要输出 tool call JSON""不要列举具体值" | 一个**受限填槽器** |
| 工具暴露 | booking 只给 `_dry_run_vehicle_reservation` | 真正多步预约被 FSM 硬编码接管 |
| `ocl/intent_filter` | LLM 出完答案后再用正则**事后审查** | 输出还要被二次否决 |

结论：意图路由这个**最该用模型**的环节，反而交给了脆弱正则；LLM 被降级为兜底。

### 1.2 意图识别散落 6 处、互相漂移（"正则跑步机"）

意图逻辑曾分布在：

1. `handler._SIMPLE_REPLIES`（问候）
2. `handler.BOOKING_INTENT` + `_has_intent`/`_has_negation` + `is_vehicle_id` + `_TYPE_KEYWORDS` + 嵌入编号（**约车意图 5 套叠加**）
3. `handler._FAST_PATH_PATTERNS`（查询）
4. `handler._match_query_intent_during_fsm`（FSM 逃逸）
5. `car_booking_fsm.BOOKING_INTENT_PHRASES`（**和 #2 重复一份**）
6. `car_booking_fsm.ESCAPE_PHRASES`（**和 handler._ESCAPE_PHRASES 重复一份**）

合并时发现 3 个真实漂移 BUG：

- **escape 短语两份不一致**（handler 有"换个/不订了/不要了"，fsm 有"退出/不约了/不选了"）
- **booking 短语两份不一致**
- `handler._has_intent` 正则写成 `r"[\\s\\S]{0,12}"`（双反斜杠，只能匹配 `{\,s,S}`，几乎等于"verb 紧贴 vehicle word"），而**测试用单反斜杠重新实现**——线上代码与测试脱节；且 `handler.is_vehicle_id` 缺 fsm 有的"必须含数字"约束。

git log 满是 `fix robust intent regex` / `扩展口语化变体` / `支持嵌入编号` / `修无关触发` —— 每来一个没覆盖的说法就加一条正则，永远追不上自然语言。

## 2. 设计：LLM 当路由器 + FSM 当执行轨道

一句话：**理解"用户想干嘛"交给大模型；安全执行多步流程交给确定性 FSM。**

```
用户消息
  Tier 1（确定性，零歧义，瞬回）
    问候 → FSM 续答 → 精确查询短语 → 明确约车意图 → 身份/管理命令
                              │ 全部 miss
                              ▼
  Tier 2（LLM 结构化分类）intent_router.classify → {intent, slots, confidence}
        book(高置信)   → fsm.start_booking(slots) 播种 + 跳到第一个缺口
        query_*(高置信) → fast_path.run_tool 直接调工具建卡
        cancel/return/approve(高置信) → fast_path.run_mutation 确定性分发（需识别符）
        identity       → replies.identity_reply
        chitchat       → 引导话术
        其余/低置信     → ↓
  Agent 路径（兜底，最大自由度）AIAgent.chat（带全工具，OCL 守卫）
```

### 2.1 四个目标如何同时满足

**① 自由度（不像 LangGraph 限死）**
- 意图识别归 LLM：措辞多样/表达有问题（"帮我整辆车明天用俩小时跑MFF"）由模型理解。
- `book` 时 LLM **抽槽位**，`start_booking` 用槽位**播种 car_state** 并跳到第一个未填步骤——
  说全了的人直接落到"确认卡"，不再被 8 步按钮 march。
- 真正开放式请求落到完整 agentic LLM 自由推理。

**② 流程正确（不破坏不变量）**
- 执行仍走 FSM / `guarded` handler：必填校验、L1+L2 权限、`dry_run→commit` 两步、
  `emailAddress` 服务端注入——**全部保留**。
- LLM 只**提议** intent+slots，**处置权在确定性层**：无法跳过审批、无法伪造 commit、
  无法绕过字段校验。给自由但不失控。

**③ 不漂移**
- 路由器用**结构化输出 + 枚举归一化**（`intent_router._normalize`）：模型只能返回
  枚举内 intent，槽位只保留白名单键、platform 必须是合法芯片——物理上无法漂成自由闲聊，
  也无法塞 `emailAddress` 之类敏感字段。
- 正则退化为"又笨又精确"：只保留零歧义/高频精确短语，**不再追逐措辞多样性**（那是 LLM 的活）。
- 6 处意图逻辑合并为单一事实源 `bot/intent.py`。

**④ 烂输入也能准识别**
- 这正是 LLM 分类的用武之地；低置信 → 落 agent 兜底，而不是误触发约车。

### 2.2 两档路由（延迟取舍）

给每条消息都加一次 LLM 分类会给查询/问候也增加 ~1~2s。故采用**两档**：

- **Tier 1（正则，瞬回）**：精确问候、FSM 续答、精确查询短语、明确约车意图、纯车辆编号、
  管理命令——这些零歧义、高频，正则足够且不会漂。
- **Tier 2（LLM）**：仅 Tier 1 未命中的"长尾"消息才付 LLM 延迟。

与旧设计的关键差别：旧设计 Tier-1 miss = 行为错误（或落到 agent 误调工具）；新设计
Tier-1 miss = 被 Tier-2 LLM 兜住。所以可以**放心收缩 Tier-1 正则、停止扩张**——正则跑步机停转。

## 3. 代码改动

| 模块 | 变化 |
|---|---|
| `bot/intent.py` | **新增**。意图识别单一事实源（escape/confirm/booking/vehicle-id/type-keyword/query），修了 3 个漂移 BUG。 |
| `bot/intent_router.py` | **新增**。Tier-2 LLM 分类器；结构化输出 + 归一化防漂移；fail-open；`_complete` 是唯一网络出口（可 mock）。 |
| `bot/replies.py` | **新增**（从 handler 拆出）。问候/身份/管理命令文案 + agent 身份前导词。 |
| `bot/fast_path.py` | **新增**（从 handler 拆出）。`try_fast_path`（精确短语）+ `run_tool`（Tier-1/Tier-2 共用）。 |
| `bot/handler.py` | **瘦身为编排骨架**：822 行 → ~330 行；两档路由；删死代码 `_commit_confirmed_booking` / `_CONFIRM_PHRASES`。 |
| `bot/car_booking_fsm.py` | 意图/编号识别复用 `bot.intent`；**新增** `start_booking(user_id, slots)` 槽位播种入口。 |
| `ocl/card_builder.py` | **清死代码**：删旧 bench/architecture 域的数据块渲染（车域从不注册那些工具名）；`build_card` 只渲染文本。 |
| `feishu/notify.py` | 注释里残留的旧域名词清理。 |

测试：`tests/unit/` 482 passed（基线 454；净 +28：新增 intent/router/seeding/handler-router 用例，移除约 15 个死代码用例 + 1 个读源码重实现正则的脆弱用例）。`scripts/selfcheck.py` 8/8 绿。
修复了一个**早已无法收集**的陈旧集成测试（`tests/integration/test_car_booking_e2e.py` 引用了被删的
`STATE_VEHICLE_ENTRY`），并改用真实卡片回调路径驱动按钮步骤。

## 4. 已知 v1 限制 / 后续

- **部分槽位的"跳步"**：`start_booking` 在入口处按"第一个缺口"跳步。若用户给了任务/地点但缺时段，
  进入 DURATION_CONFIRM 选完时段后仍会再问一次任务（罕见组合）。后续可让 DURATION_CONFIRM 之后
  按已填槽位继续跳。
- **带中文前缀的整段编号**（如 `约苏EAM0769`）：`intent.extract_embedded_vehicle_id` 会剥中文前缀，
  对 `苏EAM0769` 这类首字符即中文的车牌可能丢前缀。FSM 的 START 状态保留了自己的精确处理；
  Tier-2 播种依赖 LLM 直接给出完整 `vehicle_no`。
- **Tier-2 无法离线集成测试**：`_complete` 走 Minimax，单测只能 mock。上线后需观测 `intent_router`
  日志（只记 intent/conf/slot_keys，不记原文）校准 prompt 与置信阈值（当前 `is_confident` 阈值 0.6）。
- **Tier-2 在串行 consumer 线程同步执行**：分类调用硬超时 6s（Minimax 健康时 ~1-2s）。consumer 本就
  串行（agent 路径也 block），故未引入新的并发违例；但慢分类会延迟下一条消息。`unknown`/低置信会先付
  一次分类、再走完整 agent（两次 LLM）——这是"宁可多分类一次也要确定性分发"的取舍。

### cancel/return/approve 确定性分发（2026-06-25 新增）

Tier-2 现支持 cancel/return/approve：`intent_router` 额外抽 `vehicle_no`/`approved`/
`review_comment`，按危险程度分流：

- **cancel（取消）→ 二次确认**：先发确认卡（[确认取消]/[放弃]），用户点确认后由
  `card_action_handler._handle_confirm_mutation` 执行。识别符为 vehicle_no（新版上游去掉
  reservationId，cancel 以 vehicleNo/vin 为键）。
- **return（归还）→ 还车表单 FSM + 二次确认**：`bot/return_fsm.py` 一步步收集 5 个必填字段
  （车辆编号→还车地点→钥匙位置→变更模块→车辆状态→状态描述）→ 确认卡 [确认归还]/[取消] →
  执行 `return_vehicle`。入口：Tier-1 `intent.is_return_intent`（"还车/归还"，Minimax 不可用也能进）
  或 Tier-2 `return`（带 vehicle_no 播种）。按钮用 `ret_*` action，由 card_action_handler 分发。
- **approve（审批）→ 直接执行**：需 role≥2 + vehicle_no + approved 明确；后端再按归属细粒度鉴权。
  （用户只要求 cancel/return 二次确认；approve 如需也可加确认卡。）
- 必须有 vehicle_no 才走 Tier-2，否则落 agent 追问；后端按 emailAddress/mobile + 归属双重把关。
- 防误判：Tier-1 `is_booking_intent` 加动作守卫——句中含 取消/归还/审批 等词时不判为 booking。

### 手机号识别符 + 上游 MCP 对齐（2026-06-25 更新）

新版上游 `dmz-fmp-mcp-260409` 每个 `@Tool` 都接受 `emailAddress + mobile`，且**邮箱/手机号
至少一个**即可鉴权。配合飞书已开通 `contact:user.phone:readonly`：

- **mobile 现在真正端到端**：`identity.mobile_of` 优先级 = identity_map 手动覆盖 > 飞书 Contact API
  （随 email 一并解析、归一化去 +86、缓存）> ""；`CallerIdentity` 携带；`_inject_caller` /
  `mcp_client.call` 注入上游。`查看用户 <手机号>` 查人、`设置手机/绑定手机` 命令。
- **上游签名对齐**：`booking_mcp_server.py` 的 9 个工具按新版 Java `@ToolParam` 顺序重排位置参数，
  每个加 `mobile`；`fetch_available_vehicles` 去掉 startTime/endTime；cancel/approval 去掉
  reservationId；`get_common_dictionary` 上游已注释（降级到内置字典）。
- **容错**：`mcp_client.call` 按目标函数签名过滤未知 kwarg（上游参数增删时不再 `TypeError`，
  如旧 reservationId / startTime 被自动丢弃）。
- 安全：mobile 与 emailAddress/openId 同列 DMZ 记忆 `_SENSITIVE_KEYS`，永不落盘；
  飞书解析日志只记 `has_email/has_mobile` 布尔、不记原文。

## 5. 上线后怎么调

1. 看 `intent_router` 日志统计各 intent 占比与置信分布，校准 0.6 阈值。
2. 若某类高频措辞被 Tier-2 反复正确分类，可考虑把它**固化进 Tier-1**（省一次 LLM）。
3. 若 Tier-2 误分类某类，优先改 `_system_prompt` 的描述/示例，而不是回到加正则的老路。
