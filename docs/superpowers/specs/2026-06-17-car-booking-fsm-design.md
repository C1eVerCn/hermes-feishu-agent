# 车辆预约对话流重构设计 v2

> 日期：2026-06-17
> 状态：Draft（待用户 review）
> 上游设计参考：`/Users/chris/Downloads/约车Agent对话流设计.pdf` (v1.3)
> 适用范围：`/Users/chris/IM-Test/hermes-feishu-agent_副本` 项目

## 1. 背景与动机

### 1.1 当前问题

项目在 v1 阶段（`2026-06-16-car-booking-design.md`）针对 4 个用户场景做了逐条实现：
- "我想约车" / "现在大Fcar有什么车可以约" / "约第N个" / "确认"

实现路径产生了**过度碎片化的硬编码路由**：

| 硬编码组件 | 行数 | 用途 |
|----------|------|------|
| `_FAST_PATH_PATTERNS` + 5 个 regex | ~50 | 截获"查可用车辆" / "我的预约" 等 |
| `_TYPE_KEYWORDS` 白名单 | ~10 | 防止 "我想约车" 误匹配 |
| `_try_fast_path` 函数 | ~30 | 路由分发 |
| `_try_text_select_vehicle` | ~60 | "约第N个" / "约XXX" 文本选车 |
| `_TEXT_SELECT_BY_INDEX_RE` / `_TEXT_SELECT_BY_VEHICLE_RE` | ~4 | 文本选车正则 |

**总 ~150 行**的硬编码路由逻辑，但只覆盖了 4 个具体场景。**多轮对话**（如选车 → 选时段 → 选任务 → 选地点 → 确认）完全没有状态机。

### 1.2 核心痛点

1. **LLM 智能被压抑**：每个新场景都得加新 regex/分支，LLM 只能做"路由到 fast-path 或 fall through"两件事。
2. **多轮对话没有状态**：如果用户分多轮发"先选大Fcar / 然后 Orin / 然后 1 小时"，现有代码无法跟踪状态，会丢失上文。
3. **时段冲突后置**：现有 `_dry_run_vehicle_reservation` 在用户填完所有信息后才报错，UX 差。
4. **一次到位解析能力缺失**：用户一句"SNV018，明天下午2-4点，MFF调试，上海"本可 1 轮完成，但现状会分 4-5 轮问。

### 1.3 设计目标

1. 用一个**中心化状态机**驱动完整约车流程。
2. **硬约束硬编码**（3/芯片+10/总、表格只显后六位、8h 时长上限、3 候选上限）。
3. **LLM 只做文本抽取**（识别用户自由文本里的槽位），不参与状态转换。
4. 支持 PDF v1.3 的所有快捷路径（DIRECT_BY_ID / 一步到位 / 模糊匹配）。
5. 复用现有 `car_tools/handlers.py` 的 MCP 工具调用，不重写工具层。

## 2. 上游参考

完整参考 `约车 Agent 对话流设计.pdf` (v1.3)。核心创新：

- **DURATION_CONFIRM ★**：用户先说"需要多久" → 系统做**模糊匹配**找出真实无冲突的可用时段 → 候选预验证 → 用户确认。避免"用户填完才发现时段冲突"的回退痛点。
- **DIRECT_BY_ID 快捷路径**：报编号 → 一次走通。
- **多槽位一步到位**：用户一句话给齐 5 槽位 → 已填继承 → 只追问缺失。

PDF 状态：10+ 个（细粒度），每状态单一职责。

## 3. 我们项目的设计适配

### 3.1 合并策略

**采用 PDF v1.3 作为主流程**（10 状态），**叠加**项目特定的展示硬约束：

| 来源 | 约束 |
|------|------|
| PDF v1.3 | 状态机结构、DURATION_CONFIRM 模糊匹配、DIRECT_BY_ID、一步到位、retry 防循环、8h 上限 |
| 项目现有 | 3/芯片+10/总限制、表格只显序号+后六位、查询条件显示在卡片标题 |

### 3.2 状态机（最终版）

```
START
├─ user 给编号 → DIRECT_BY_ID
│   ├─ 全齐 → CONFIRM
│   ├─ 缺时间 → DURATION_CONFIRM
│   ├─ 缺时长 → SELECT_DURATION
│   └─ 只编号 → SELECT_DURATION
└─ user 描述车型 → SELECT_VEHICLE_TYPE
    ├─ 单芯片 → 跳 CONFIRM_CHIP → VEHICLE_ENTRY
    └─ 多芯片 → CONFIRM_CHIP → VEHICLE_ENTRY
        ├─ "已知编号" → 等输入
        └─ "帮我查" → SELECT_FROM_LIST
            → DURATION_CONFIRM ★
            → SELECT_TIME (fallback / 冲突)
            → INPUT_TASK
            → INPUT_LOCATION
            → CONFIRM
            → COMMIT
            → SUCCESS
```

### 3.3 13 个状态详细定义

| 状态 | 进入条件 | 渲染 | 用户输入 → 下一态 |
|------|---------|------|----------------|
| **START** | 默认 | 入口卡：车型按钮 + "直接输入编号"按钮 | 车型 → SELECT_VEHICLE_TYPE；编号 → DIRECT_BY_ID |
| **DIRECT_BY_ID** | 报编号 | 校验格式 + 查库；不在库 → 提示重输 | 全齐 → CONFIRM；缺时间 → DURATION_CONFIRM；缺时长 → SELECT_DURATION；只编号 → SELECT_DURATION |
| **SELECT_VEHICLE_TYPE** | 描述车型 | 车型按钮：DM2 / CT1 / 大F车 / CM0 / BM2 | 选车型 → CONFIRM_CHIP（多芯片）或 VEHICLE_ENTRY（单芯片） |
| **CONFIRM_CHIP** | 多芯片 | 芯片按钮：Xavier / ADCU / Orin / Thor | 选芯片 → VEHICLE_ENTRY |
| **VEHICLE_ENTRY** | 已选车型+芯片 | "已知编号"/"帮我查" 按钮 | 选已知 → 等用户输入；选帮我查 → SELECT_FROM_LIST |
| **SELECT_DURATION** | 需时长 | 时长按钮：30分钟 / 1小时 / 2小时 / 3小时 / 半天 / 1天 / 其它 | 选按钮 → DURATION_CONFIRM；选其它 → 自由文本 |
| **SELECT_FROM_LIST** | 查车结果 | 表格（**序号 + 后六位**，≤10 行）+ 时长按钮 | 选编号+时长 → DURATION_CONFIRM；一句"编号+时长" → 同上 |
| **DURATION_CONFIRM ★** | 编号+时长已定 | 模糊匹配时段候选（最多 3 个，预验证无冲突） | 选时段 → INPUT_TASK；无候选 → DC-5/10；时长超 8h → 重选 |
| **SELECT_TIME** | DC-10 retry 兜底 | 预设时间按钮（1h/2h/4h/8h 滑窗） | 选时段 → INPUT_TASK |
| **INPUT_TASK** | 缺任务 | 输入提示 + 高频任务按钮 | 任务文本 → INPUT_LOCATION；空/超长 → 重出 |
| **INPUT_LOCATION** | 缺地点 | 城市按钮：上海/北京/广州/... | 地点文本 → CONFIRM；全齐（一步到位） → 直接 CONFIRM |
| **CONFIRM** | 全齐 | 二次确认卡（[确认]/[修改]/[取消]） | 确认 → COMMIT；修改 → 回对应槽位；取消 → IDLE |
| **COMMIT** | 确认 | 提交中 spinner | (auto) → SUCCESS |
| **SUCCESS** | 完成 | 成功卡（含调度员列表） | (final) |

### 3.4 LLM 介入点（**仅 5 处**）

LLM 只在以下时机被调用（`agent_pool.get_or_create(user_id).chat()`）：

1. **SELECT_VEHICLE_TYPE** 收自由文本（如"我想约个大点的车"）→ 抽 vehicle_type
2. **SELECT_DURATION** 收"其它"自由文本（如"两个半小时"）→ 抽 duration_minutes
3. **DIRECT_BY_ID / SELECT_FROM_LIST** 收一句"编号+时长+时间+任务+地点" → 一次抽 5 槽位
4. **INPUT_TASK** 收任务名 → 抽/校验 taskName
5. **INPUT_LOCATION** 收地点 → 抽 location

其它一切（按钮渲染、时段匹配、查车、提交预约）都是硬编码 MCP 工具调用，**不经过 LLM**。

## 4. 数据结构

### 4.1 `CarBookingState`（扩展自 `car_state.CarPendingState`）

```python
@dataclass
class CarBookingState:
    user_id: str
    state: str = "START"           # FSM 当前状态名

    # 槽位
    vehicle_type: str = ""          # 车型：DM2/CT1/大F车/CM0/BM2
    chip: str = ""                  # 平台：Xavier/ADCU/Orin/Thor
    vehicle_no: str = ""            # 车辆编号：SNV018 / PNV000
    duration_minutes: int = 0       # 时长：60=1h, 90=1.5h, 480=8h(MAX)
    time_range_start: str = ""      # 时段开始：yyyy-MM-dd HH:mm
    time_range_end: str = ""        # 时段结束
    task_name: str = ""
    location: str = ""

    # 内部
    last_vehicles: list = field(default_factory=list)  # 最近查车结果
    last_query: dict = field(default_factory=dict)      # 最近查询条件
    retry_count: int = 0            # DC-10 防循环
    expires_at: float = 0           # TTL（与现有 pending 一致）
```

### 4.2 系统 Prompt（注入 LLM）

```
你是车辆预约助手。系统已为你解析用户当前处于哪一步（FSM state），
并把可用按钮 / 可选项 / 当前槽位都放在 CarBookingState 里。
你**只**做一件事：根据用户的最近一句话，从中抽取以下槽位（如有）：
  - vehicle_type  (DM2 / CT1 / 大F车 / CM0 / BM2)
  - chip          (Xavier / ADCU / Orin / Thor)
  - vehicle_no    (如 SNV018 / PNV000)
  - duration_minutes  (整数，如 60 表示 1 小时，90 表示 1.5 小时)
  - time_range    ("2026-06-17 14:00" 至 "2026-06-17 16:00")
  - task_name     (用户给的原话，**不补全、不修改**)
  - location      (用户给的原话)

严禁：
- 禁止猜测相近编号（如把「CSUV24」自动修正为「CSUV024」）
- 禁止修改用户已填的任务名称
- 禁止展示时段候选（必须由 DURATION_CONFIRM 走模糊匹配）
- 禁止在未确认时长前进入时间选择

只返回 JSON：{"extracted": {<槽位>: <值>}, "ask": "<给用户的简短反问>"}
```

## 5. 关键设计决策

### 5.1 LLM 与状态机边界

- **LLM 状态机独立**：LLM 不"知道"自己在哪个 FSM 状态。FSM 调用 LLM 时把 `state`、`slots` 注入到 prompt；LLM 只返回抽到的槽位。
- **LLM 不决定下一步**：FSM 函数 `advance(state, user_msg, extracted_slots) → (new_state, response)` 是纯 Python，由 FSM 决定。
- **LLM 失败兜底**：如果 LLM 返回空 / 异常，FSM 降级到"按钮引导"（与现状一致）。

### 5.2 时段匹配（DURATION_CONFIRM）

按 PDF 规则：
- 搜索窗口：N = `ceil(总预约需求天数 / 2)`，最少 3 天
- 候选条件：时长 ≥ 用户时长 AND 时段内无有效预约
- 排序：最早可用
- 上限：3 个；超出截断，提示"其它时段请联系管理员"
- 冲突重选（DC-4）：选了候选但被他人抢 → 致歉 + 重列其它
- retry 兜底（DC-10）：3 次解析失败 → 切到 SELECT_TIME 预设按钮

实现位置：`bot/car_booking_fsm.py::match_duration_slots()`

### 5.3 表格展示硬约束（项目特定）

- **3/芯片 + 10/总 限制**：`car_tools/card_builder.py::build_vehicles_card` 内部 limit，**不暴露给 LLM**
- **表格只显序号 + 后六位**：limit 函数已经实现，需要在 card_builder 中保留
- **查询条件标题**：card 渲染时从 car_state.last_query 拼成"大Fcar · Xavier 芯片"展示

### 5.4 8h 时长上限

- 入口：SELECT_DURATION 收按钮 + 自由文本
- 校验：`duration_minutes > 480` → 提示"单次上限 8 小时" → 保持 SELECT_DURATION
- 后续：DURATION_CONFIRM 收到超 8h 输入也直接拒绝

## 6. 现有代码迁移

### 6.1 删除（彻底移除）

| 位置 | 代码 | 原因 |
|------|------|------|
| `bot/handler.py` | `_FAST_PATH_PATTERNS` 5 个 regex | FSM 替代 |
| `bot/handler.py` | `_try_fast_path` | FSM 替代 |
| `bot/handler.py` | `_try_text_select_vehicle` | FSM 替代（SELECT_FROM_LIST） |
| `bot/handler.py` | `_TEXT_SELECT_BY_INDEX_RE` / `_TEXT_SELECT_BY_VEHICLE_RE` | FSM 替代 |
| `bot/handler.py` | `_TYPE_KEYWORDS` / `_args_with_type` / `_args_with_platform` / `_empty_args` | FSM 替代 |

预计减少 `bot/handler.py` ~150 行。

### 6.2 保留

- `bot/handler.py` 的 Layer 0（闲聊/帮助）、identity query、admin command
- `bot/handler.py` 的 LLM agent path（调用 `agent_pool.get_or_create().chat()` 的兜底）
- `bot/card_action_handler.py`（卡片回调路径）
- `bot/car_state.py`（扩展而非替换）
- `car_tools/handlers.py` 全部（MCP 工具调用）
- `car_tools/booking_mcp_server.py` 全部
- `car_tools/normalizers.py` 全部
- `car_tools/schemas.py` 全部
- `car_tools/card_builder.py` 中的 `build_vehicles_card` 3/10 限制 + 后六位表格

### 6.3 新增

| 文件 | 职责 | 行数预估 |
|------|------|---------|
| `bot/car_booking_fsm.py` | 13 状态机 + transfer 函数 + 卡片渲染调用 | ~450 |
| `tests/unit/test_car_booking_fsm.py` | 状态机单元测试（每个状态一个 case） | ~400 |
| `docs/superpowers/specs/2026-06-17-car-booking-fsm-design.md` | 本文档 | — |

## 7. 数据流（典型路径）

### 7.1 一步一步走（5 轮对话）

```
U: "我想约车"
B: [SELECT_VEHICLE_TYPE] 车型按钮 + "直接输入编号"按钮

U: "大F车"
B: [CONFIRM_CHIP] 芯片按钮（多芯片：大F车在 Xavier/Orin/Thor 都有）

U: "Xavier"
B: [VEHICLE_ENTRY] "已知编号" / "帮我查"

U: "帮我查"
B: [SELECT_FROM_LIST] 查车 → 表格（≤10 行）+ 时长按钮

U: "选 3，2 小时"
B: [DURATION_CONFIRM] 模糊匹配：3 个候选时段

U: "选 1"
B: [INPUT_TASK] 任务输入提示

U: "MFF 调试"
B: [INPUT_LOCATION] 城市按钮

U: "上海"
B: [CONFIRM] 二次确认卡

U: [点 确认]
B: [COMMIT → SUCCESS] 成功卡
```

### 7.2 一步到位（1 轮对话）

```
U: "SNV018，明天下午 2-4 点，MFF 调试，上海"
B: [CONFIRM] 直接二次确认卡（全齐）

U: [点 确认]
B: [COMMIT → SUCCESS] 成功卡
```

LLM 在此路径的 1 次调用：从一句中抽出 5 槽位。

## 8. 测试策略

### 8.1 单元测试

每个状态一个测试 case（13 个），覆盖：
- 进入条件
- 正常转移
- 异常转移（escape / 取消 / 解析失败）
- LLM 失败兜底

外加：
- 13 状态的状态机图遍历测试
- 模糊匹配算法单元测试（DC-1 ~ DC-10）
- retry_count 兜底测试

### 8.2 集成测试

- 完整 5 轮对话（每一步 mock LLM 返回特定槽位）
- 1 轮"一步到位"对话
- 端到端：用户在飞书发消息 → bot 渲染状态机卡片

### 8.3 端到端测试（self-test）

复用现有 `selftest*.py` 模式，在容器内启动 fake backend + mock agent pool，验证：
- 4 个原始场景（"查车" / "我的预约" 等）仍工作
- 5 轮"一步一步走"对话走通
- 1 轮"一步到位"对话走通

## 9. Out of Scope（明确不动）

- ❌ `car_tools/mcp_client.py`（MCP client 架构）
- ❌ `car_tools/booking_mcp_server.py`（FastMCP server）
- ❌ `car_tools/normalizers.py`（snake_case 转换）
- ❌ `car_tools/schemas.py`（Pydantic 模型）
- ❌ `ocl/` 任何文件
- ❌ `feishu/` 任何文件
- ❌ `bot/agent_pool.py`（LLM 池）
- ❌ `bot/curator_runner.py` / `bot/dmz_memory.py`
- ❌ `bot/handler.py` 的 Layer 0 / identity / admin command
- ❌ `bot/card_action_handler.py`
- ❌ Approval 流程（scheduler 审批 DM 用户）
- ❌ 持久化（reservation_store / identity_admin）

## 10. 实施计划

详细 plan 通过 `writing-plans` skill 写。会拆成 5-6 个 PR：

1. **PR1**: CarBookingState 扩展 + 状态机骨架
2. **PR2**: START / SELECT_VEHICLE_TYPE / CONFIRM_CHIP / VEHICLE_ENTRY 实现
3. **PR3**: SELECT_DURATION / SELECT_FROM_LIST / DURATION_CONFIRM（含模糊匹配）
4. **PR4**: SELECT_TIME / INPUT_TASK / INPUT_LOCATION / CONFIRM / COMMIT / SUCCESS
5. **PR5**: DIRECT_BY_ID + 一步到位 + LLM 抽取
6. **PR6**: handler.py 删旧代码 + 接入 FSM + 端到端 self-test

每个 PR 完成后跑 `pytest tests/unit/` 验证 392 → 后续累计通过。

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| LLM 抽取不准确（多槽位一句） | FSM 验证：抽到的槽位若缺关键字段（vehicle_no / duration），fall back 到分步走 |
| 状态机状态丢失（容器重启） | car_state 内存 dict，10 分钟 TTL；重启清空是已有行为 |
| 模糊匹配算法错误 | DC-1~DC-10 每个分支单测；端到端测覆盖 |
| 现有 fast-path 漏迁移 | 删除前在 handler.py 入口加 metric 统计使用次数，确认 0 后再删 |
| 卡片渲染时序问题 | 状态机 advance() 纯函数，无副作用；卡片渲染在 caller 端单步完成 |

## 12. 验收标准

- [ ] 13 状态全部有单元测试
- [ ] 5 轮"一步一步走"对话 self-test 通过
- [ ] 1 轮"一步到位"对话 self-test 通过
- [ ] DIRECT_BY_ID 3 种分支（D-1/D-2/D-3）self-test 通过
- [ ] 4 个原 fast-path 场景仍工作（"查可用车辆" / "我的预约" / 等）
- [ ] `bot/handler.py` 行数从 770 降到 ~600
- [ ] `bot/car_booking_fsm.py` 单文件 ~450 行
- [ ] 总代码量净增 < 200 行（净简化）
- [ ] 容器内 self-test 一次性通过
