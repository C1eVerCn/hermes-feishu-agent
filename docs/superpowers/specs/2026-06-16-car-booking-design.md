# 车辆预约域改造 — 设计文档

**日期：** 2026-06-16
**项目：** hermes-feishu-agent
**作者：** Claude + chris

---

## 1. 背景与目标

当前 `hermes-feishu-agent`（DMZ 智能体）整合两个业务域：

- **台架预约**（`bench_tools/`，@9013）— 内部测试台架管理
- **VLM 精标数据**（`vlm_tools/`，@9014）— 自动驾驶视觉数据查询

业务方决策：合并两个域为**单一车辆预约域**（车辆是测试车的升级版，含芯片平台
枚举 Xavier/ADCU/Orin/Thor）。原 `reservation_agent-test-agent_multigraph`（LangGraph 实现）
已验证完整业务流与状态机，本项目保留其**业务流程与设计哲学**，但内核沿用当前
hermes-feishu-agent 的架构（OCL 流水线、双层防御、AIAgent 池、跨会话记忆、Curator 等）。

> 设计目标：在不重写内核的前提下，把 vehicle reservation 业务接进现有的
> hermes-feishu-agent 飞书通道。完成替换后代码应保留 ~90% 现有结构，新增/改造
> 集中在业务层。

## 2. 关键决策（已与用户确认）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 业务域 | **单域（车辆预约），彻底替换** bench + VLM |
| 2 | MCP 拓扑 | **外部 MCP server**（用户提供接口文档），hermes 通过 `~/.hermes/config.yaml::mcp_servers` 连接；bot 端不实现 MCP server |
| 3 | 意图分类 | **保留**现有 Layer 0/0.5/0.6 + Agent 路径，**不引入** intent_router 单次 LLM 分类 |
| 4 | 中断点表达 | **飞书卡片按钮**（select_vehicle / confirm_booking），确定性走 `card_action_handler`，不经过 LLM |
| 5 | 身份字段 | **email + openid +（未来 mobile）** 传给 MCP；服务端从 `contextvars` 注入，**不作为 LLM 工具参数**（与现有 bench 不变量一致） |
| 6 | Normalizer 层 | **全量 Pydantic strict 模式**（与 LangGraph 参考项目一致），MCP 边界 fail-fast |
| 7 | MVP 范围 | **6 个意图全量** — 7 业务工具 + 2 助手（get_user_context / get_common_dictionary） |
| 8 | 身份注入层 | **Python 薄包装层** `car_tools/handlers.py`（与 bench 模式一致），guarded() L2 兜底 |

## 3. 架构

### 3.1 集成拓扑

```
┌─────────────────────────────────────────────────────────────┐
│  飞书 WS  ←  feishu/ws_client + sender + notify  (保留)       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  bot/handler.py  (改造)                                      │
│  ├─ Layer 0   简单意图 (你好/帮助)              (保留)         │
│  ├─ Layer 0.5 快速路径 (查车/查我的预约)        (新增车业务)   │
│  ├─ Layer 0.6 快速路径 (预约 dry_run)           (新增车业务)   │
│  ├─ Layer "car 业务子分支" (状态机)             (新增)        │
│  │    └─ 走 car_state 推进 + 飞书卡片按钮回调                  │
│  └─ Agent 路径 (重写 system prompt)              (改造)        │
└────────────────────┬────────────────────────────────────────┘
       │                       │
       ▼                       ▼
  OCL pipeline           car_tools/handlers.py
  format→content→         (新增：薄 Python 包装)
  intent→length→         ├─ 7 业务工具 + 2 助手
  card_builder           ├─ 每个 handler:
       │                 │  1. contextvars 读 openid/email/mobile
       │                 │  2. guarded() 门控 (L2)
       │                 │  3. 拼 args
       │                 │  4. mcp_client.call(tool, args)
       │                 │  5. normalizers.raw_to_pydantic
       │                 │  6. 返回 stringified JSON
       │                 └─ 双层防御 L1 走 feishu_acl plugin
       ▼                           │
  飞书发送 (sender)                  ▼
                          外部 MCP server (用户提供)
                          stdio subprocess  或  HTTP/SSE
```

### 3.2 文件结构

**新增**（`car_tools/` 9 个 + `bot/car_state.py` + `bot/mcp_client.py` + tests 8 个）：

```
car_tools/
  __init__.py
  register.py          # 7 业务 + 2 助手注册到 hermes registry (仿 bench_tools/register.py)
  schemas.py           # Pydantic: Vehicle, Reservation, Dispatcher, Platform, ...
  normalizers.py       # MCP raw dict → Pydantic (fail-fast)
  handlers.py          # 7 业务 + 2 助手 handler (薄 Python 包装)
  card_builder.py      # 5 套飞书卡片 (list/confirm/success/fail/approval)
  notify_dispatchers.py  # 预约成功 DM 调度员 (in-process async)
  notify_applicant.py    # 审批后 DM 预约人 (in-process async)
  mcp_client.py        # hermes mcp_tool 包装 (统一接口)
  provision.py         # 首次接触自动开账号 (仿 bench_tools/provision.py, 按需)

bot/
  car_state.py         # per-user 状态机 (10min TTL, 仿 dry_run_state.py)

tests/unit/
  test_car_handlers.py
  test_car_normalizers.py
  test_car_schemas.py
  test_car_state.py
  test_car_card_builder.py
  test_notify_dispatchers.py
  test_notify_applicant.py
  test_mcp_client.py
```

**改造**（10 个）：

```
config/settings.py            # 新增 CAR_MCP_SERVER_NAME 等
ocl/identity.py               # 扩展 CallerIdentity (openid+email+mobile?)
ocl/tool_guard.py             # 替换 set_current_user/email → set_current_caller
ocl/permission.py             # TOOL_MIN_ROLE 替换为车业务 8 工具
ocl/intent_filter.py          # _DOMAIN_KEYWORDS 改 (车业务)
ocl/format_control.py         # 微调 (适配车业务 Markdown 习惯)
bot/agent_pool.py             # _FEISHU_SYSTEM_PROMPT_BASE 重写
bot/handler.py                # 新增 car 业务子分支 + 飞书卡片回调
bot/card_action_handler.py    # 新增 action: select_vehicle / confirm_booking
bot/dry_run_state.py          # 字段适配车业务
bot/reservation_store.py      # 字段适配车业务
bot/dmz_memory.py             # 微调 (preference pattern 加车业务关键词)
hermes_plugins/feishu_acl/__init__.py  # 微调 (log 信息加车业务标识)
scripts/selfcheck.py          # 删 check_vlm_no_email, 改 _STALE_TOKENS
```

**删除**：

```
bench_tools/                  # 整个目录
vlm_tools/                    # 整个目录
tests/unit/test_bench_*.py    # 4 文件
tests/unit/test_vlm_*.py      # 1 文件
```

**配置变更**：

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  car_booking:
    command: "python"          # 用户的 MCP server 启动命令
    args: ["/path/to/car_mcp_server.py"]
    timeout: 30
    connect_timeout: 10
    # 或 HTTP 方式:
    # url: "http://car-mcp:8765/mcp"
    # headers: { Authorization: "Bearer ..." }
```

```bash
# .env
CAR_MCP_SERVER_NAME=car_booking   # 对应 ~/.hermes/config.yaml::mcp_servers 中的 key
```

## 4. 数据契约

### 4.1 工具清单（MCP LLM-facing）

| # | 工具名 | 意图 | 必填参数（不含身份） | 可选参数 |
|---|---|---|---|---|
| 1 | `fetch_available_vehicles` | query | vehicleType, platform, startTime, endTime | — |
| 2 | `single_vehicle_reservation` | booking | vehicleNo, startTime, endTime, taskName, location | remark, vin |
| 3 | `cancel_vehicle_reservation` | cancel | vehicleNo | reservationId |
| 4 | `approval_vehicle_reservation` | approve | vehicleNo, approved(bool) | reviewComment, reservationId |
| 5 | `return_vehicle` | return | vehicleNo, returnLocation, keyPosition, changeModule, vehicleStatus(int) | vehicleStatusDescription, vin |
| 6 | `fetch_user_reservation` | records (applicant) | — | startTime, endTime, vehicleNo, taskName, status |
| 7 | `fetch_user_approval` | records (approver) | — | 同上 |
| 8 | `get_user_context` | (any) | emailAddress | — |
| 9 | `get_common_dictionary` | (any) | typeCode | — |

**身份字段**（不暴露给 LLM，每个工具都要）：

- `openId` (string, 必填)
- `emailAddress` (string, 必填)
- `mobile` (string, 可选；当前 stub 为空，2026 Q3 接入)

### 4.2 槽位定义

**Booking 必填槽**（`bot/slot_validator.py` 校验）：

```python
{
    "vehicle_type":   "DM2/CT1/大F车/CM0/BM2/...",  # 来自 get_common_dictionary
    "platform":       "Xavier/ADCU/Orin/Thor",       # 枚举
    "start_time":     "yyyy-MM-dd HH:mm",             # 日期+时间
    "end_time":       "yyyy-MM-dd HH:mm",             # 日期+时间, start<end
    "task_name":      str,
    "location":       str,
}
```

**Query 必填槽**：`{vehicle_type, platform, start_time, end_time}`（与 booking 一致）

**Cancel 必填槽**：`{vehicle_no}`

**Return 必填槽**：`{vehicle_no, return_location, key_position, change_module, vehicle_status}`

**Approve 必填槽**：`{vehicle_no, approved}`

**Records 必填槽**：`{}`（全部可选）

### 4.3 Pydantic schemas

```python
# car_tools/schemas.py
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["Xavier", "ADCU", "Orin", "Thor"]

class Vehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle_no: str
    vin: Optional[str] = None
    license_plate: Optional[str] = None
    vehicle_type: str
    platform: Platform
    project: Optional[str] = None
    remark: Optional[str] = None

class Dispatcher(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    email: str

class Reservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle_no: str
    vehicle_type: Optional[str] = None   # record 查询不返回
    platform: Optional[str] = None
    license_plate: Optional[str] = None
    start_time: str    # yyyy-MM-dd HH:mm
    end_time: str
    task_name: Optional[str] = None
    location: Optional[str] = None
    status: str        # 中文 "待审批/已批准/已驳回/已取消/已归还"
    reviewer: Optional[str] = None
    reviewer_remark: Optional[str] = None
    return_time: Optional[str] = None
    return_location: Optional[str] = None

class ReservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    vehicle_no: str
    license_plate: Optional[str] = None
    vehicle_type: str
    platform: Platform
    start_time: str
    end_time: str
    task_name: str
    location: str
    remark: Optional[str] = None
    dispatchers: list[Dispatcher] = Field(default_factory=list)
    reason: Optional[str] = None
    applicant_name: Optional[str] = None
    applicant_email: Optional[str] = None
    applicant_open_id: Optional[str] = None
    applicant_mobile: Optional[str] = None

class ApprovalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    vehicle_no: str
    start_time: str
    end_time: str
    task_name: str
    reviewer: str
    review_comment: Optional[str] = None
    applicant_name: Optional[str] = None
    applicant_email: Optional[str] = None
    applicant_open_id: Optional[str] = None

class CancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle_no: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    operator: Optional[str] = None
    cancel_time: Optional[str] = None

class ReturnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle_no: str
    return_location: str
    key_position: str
    change_module: str
    vehicle_status: str
    vehicle_status_description: Optional[str] = None
    return_time: Optional[str] = None
```

### 4.4 Normalizer 接口

```python
# car_tools/normalizers.py
class NormalizeError(ValueError):
    """MCP 返回不符合 Pydantic schema 时抛出。"""
    def __init__(self, source: str, reason: str, *, raw: Any = None): ...

def normalize_vehicles(raw: Any) -> list[Vehicle]: ...
def normalize_records(raw: Any, *, source: str = "fetch_user_reservation") -> list[Reservation]: ...
def normalize_reservation_result(raw: Any, *, applicant: CallerIdentity) -> ReservationResult: ...
def normalize_approval_result(raw: Any) -> ApprovalResult: ...
def normalize_cancel_result(raw: Any) -> CancelResult: ...
def normalize_return_result(raw: Any) -> ReturnResult: ...
```

## 5. 状态机：booking 完整 happy path

### 5.1 关键不变量

- WS 回调必须立即返回（< 50ms）；`event_queue` 解耦
- 每次 LLM 工具调用走双层防御（L1 plugin + L2 guarded）
- OCL pipeline < 100ms，fail-open
- identity 注入对 LLM 不可见
- `bot/car_state` 状态 10min TTL（与 `dry_run_state` 对齐）
- `reservation_store` 持久化 reservation_id → applicant 映射

### 5.2 时序

```
T0  飞书 WS 推送「查明天 9 点到 18 点 DM2 Xavier 车」
T1  ws_client._on_message → dedup → event_queue
T2  bot/handler._handle (consumer thread)
    ├─ Layer 0/0.5/0.6: 命中 car_query_fast_path
    │  └─ car_tools.handlers.fetch_available_vehicles
    │     ├─ get_current_caller() → {openid, email, mobile}
    │     ├─ L2 guard: role>=1 ✓
    │     ├─ mcp_client.call(...) → MCP raw
    │     ├─ normalizers.normalize_vehicles → list[Vehicle] (Pydantic)
    │     └─ return json.dumps([Vehicle, ...])
    └─ OCL → card_builder.build_vehicles_card
       ├─ 摘要 "📋 共 5 辆可用"
       └─ actions: [选1][选2]...[选5][取消]
T3  sender.send_card → 飞书
T4  用户点 [选1] → 飞书 p2_card_action_trigger
T5  bot/card_action_handler.handle
    ├─ action='select_vehicle', value={vehicle_no: 'PNV332'}
    ├─ bot/car_state.save(user_id, {intent:'booking', vehicle_no, vehicle_type, platform, ...})
    ├─ 调 car_tools.handlers.dry_run_create_reservation (LLM 后面再调)
    │  └─ 缺 task_name, location → return {missing_fields, summary}
    └─ OCL → build_missing_fields_card → [补全文本 + 提示]
T6  用户回复 "任务是高速测试，地点是测试场A区"
T7  bot/handler._handle
    ├─ car_state 取出 pending → 合并新文本 → 重 dry_run
    └─ OCL → build_confirm_card → [确认][取消]
T8  用户点 [确认] → card_action_handler
    ├─ action='confirm_booking'
    ├─ car_tools.handlers.single_vehicle_reservation
    │  ├─ L2 guard: role>=1 ✓
    │  ├─ mcp_client.call(...)
    │  ├─ normalizers.normalize_reservation_result → ReservationResult
    │  ├─ car_tools.notify_dispatchers.submit → feishu/notify 异步
    │  └─ bot/reservation_store.save(key, applicant_caller, vehicle, ...)
    └─ OCL → build_success_card → 飞书
T9  调度员收到飞书 DM → 审批 (调 approval_vehicle_reservation)
    ├─ car_tools.notify_applicant.submit → 申请人 DM
    └─ OCL → build_approval_card → 飞书
```

### 5.3 State 字段（`bot/car_state.py`）

```python
@dataclass
class CarPendingState:
    user_id: str
    intent: str              # 'booking' / 'cancel' / 'return' / 'approve' / 'records'
    vehicle_no: str | None
    vehicle_type: str | None
    platform: str | None
    start_time: str | None
    end_time: str | None
    task_name: str | None
    location: str | None
    approved: bool | None    # approve intent
    return_location: str | None
    key_position: str | None
    change_module: str | None
    vehicle_status: str | None
    review_comment: str | None
    selected_vehicle: Vehicle | None  # 从查询结果中选出的车
    expires_at: float        # monotonic time

# 内存 dict + lock (仿 dry_run_state.py)
_state: dict[str, CarPendingState] = {}
_TTL_SECONDS = 600
```

### 5.4 状态机状态转换表

| 当前状态 | 用户动作 | 下一状态 | 触发动作 |
|----------|---------|---------|---------|
| IDLE | 「查车」 | QUERY_PENDING | fetch_available_vehicles |
| IDLE | 「预约」 + 车号+时间+任务+地点全 | CONFIRM | dry_run_reservation |
| IDLE | 「预约」+ 部分 | CLARIFY | 反问缺什么 |
| QUERY_PENDING | 点 [选1..N] | BOOKING_DRY_RUN | dry_run_reservation |
| QUERY_PENDING | 点 [取消] / 说"取消" | IDLE | clear state |
| BOOKING_DRY_RUN | 补全缺失字段 | (回到 BOOKING_DRY_RUN) | 重 dry_run |
| BOOKING_DRY_RUN | 点 [确认] | BOOKING_SUBMIT | single_vehicle_reservation |
| BOOKING_DRY_RUN | 点 [取消] | IDLE | clear state |
| BOOKING_SUBMIT | success | DISPATCH_NOTIFIED | notify_dispatchers |
| DISPATCH_NOTIFIED | — | IDLE | 终态 |
| (any) | 说"算了/换个" | IDLE | clear state (escape) |

## 6. 双层防御

### 6.1 L1 (hermes pre_tool_call plugin)

**位置**：`hermes_plugins/feishu_acl/__init__.py`（保留并微调）

**机制**：
```
hermes 调工具前
  └─ pre_tool_call hook
      └─ session_id → session_map.lookup() → user_id
          └─ permission.is_tool_permitted(user_id, tool_name)
              ├─ True → 放行
              └─ False → return {"action": "block", "message": "权限不足"}
```

**fail-open**：任何异常（plugin 未加载 / session_map miss / permission 抛错）→ return None → 放行，依赖 L2 兜底。

### 6.2 L2 (guarded 包装)

**位置**：`car_tools/register.py` 每个工具用 `guarded(name, handler)` 包装

**机制**：
```python
def guarded(tool_name, inner_handler):
    def _wrapper(args, **_):
        caller = get_current_caller()
        if not caller.openid:
            return json.dumps({"error": "匿名调用，无 open_id"})  # 内部/系统调用
        if not permission.is_tool_permitted(caller.openid, tool_name):
            return json.dumps({"error": "权限不足：请联系管理员申请相应权限"})
        return inner_handler(args)
    return _wrapper
```

### 6.3 关系

- L1 强（hermes 内部，session_id 可靠）但依赖 plugin 加载 + session_map 同步
- L2 弱（contextvars 跨线程残留风险）但不依赖任何外部配置
- L1 阻断 → 工具不调；L1 通过但 L2 阻断 → 工具返回 `{"error": "..."}` 作为工具结果
- 任何异常 L1 fail-open → L2 兜底

## 7. identity 注入

### 7.1 CallerIdentity（扩展现有 `ocl/identity.py`）

```python
@dataclass(frozen=True)
class CallerIdentity:
    openid: str
    email: str
    mobile: Optional[str] = None   # 2026 Q3 接入，stub 为 None

    def as_dict(self) -> dict:
        return {
            "openId": self.openid,
            "emailAddress": self.email,
            **({"mobile": self.mobile} if self.mobile else {}),
        }
```

**构造入口**（保留现有 `ocl/identity.py` 的飞书 Contact API + 缓存逻辑）：
```python
def build_caller_identity(openid: str) -> CallerIdentity:
    """从 open_id 查 email + mobile。失败时 email='' 仍要构造 (mobile=None)。"""
    email = email_of(openid) or ""
    mobile = mobile_of(openid)        # 新增, stub 实现返回 None
    return CallerIdentity(openid=openid, email=email, mobile=mobile)
```

### 7.2 tool_guard 改造

**现状**：`set_current_user(openid)` + `set_current_email(email)` (两个 ContextVar)

**改造**：`set_current_caller(caller: CallerIdentity)` (一个 ContextVar, 存对象)

```python
# ocl/tool_guard.py
_caller: ContextVar[CallerIdentity] = ContextVar("ocl_caller", default=None)

def set_current_caller(caller: CallerIdentity) -> None: ...
def get_current_caller() -> CallerIdentity: ...   # 返回 None 当匿名
def get_caller_dict() -> dict: ...                 # 注入 MCP args 用
def guarded(tool_name, inner_handler): ...         # 改造为读 caller
```

### 7.3 handler 注入流程

```python
# car_tools/handlers.py (伪代码)
def fetch_available_vehicles(args: dict) -> str:
    caller = get_current_caller()                              # L2 fallback
    if not caller or not caller.openid:
        return json.dumps({"error": "未识别用户身份"})
    # 拼 args: 身份 + 用户给的参数
    full_args = {
        **caller.as_dict(),
        "vehicleType": args.get("vehicle_type"),
        "platform": args.get("platform"),
        "startTime": args.get("start_time"),
        "endTime": args.get("end_time"),
    }
    try:
        raw = mcp_client.call("fetch_available_vehicles", full_args)
    except McpError as e:
        return json.dumps({"error": f"MCP 调用失败: {e}"})
    try:
        vehicles = normalizers.normalize_vehicles(raw)
    except NormalizeError as e:
        return json.dumps({"error": f"MCP 返回格式异常: {e.reason}"})
    return json.dumps([v.model_dump() for v in vehicles], ensure_ascii=False)
```

## 8. 通知链路（in-process async, 不用 subprocess）

### 8.1 现有 `feishu/notify.py` 复用

- `feishu/notify.submit_dispatchers_by_email_blocking(emails, subject, body)` — 已有
- `feishu/notify.submit_text_to_user(open_id, text)` — 已有
- ThreadPoolExecutor(max_workers=2) — 已隔离 LLM 池

### 8.2 car_tools/notify_dispatchers.py

```python
def submit_reservation_dispatchers(reservation_result: ReservationResult) -> Future:
    """预约成功后异步通知所有 dispatcher。"""
    if not reservation_result.dispatchers:
        return _executor.submit(lambda: 0)
    subject = "📋 新预约待审批"
    body = f"申请人：{reservation_result.applicant_name or reservation_result.applicant_email}\n" \
           f"车辆编号：{reservation_result.vehicle_no}\n" \
           f"开始时间：{reservation_result.start_time}\n" \
           f"任务：{reservation_result.task_name}\n" \
           f"地点：{reservation_result.location}\n" \
           "请尽快审批。"
    emails = [d.email for d in reservation_result.dispatchers]
    return submit_dispatchers_by_email_blocking(emails, subject, body)
```

### 8.3 car_tools/notify_applicant.py

```python
def submit_approval_to_applicant(approval_result: ApprovalResult, applicant_open_id: str) -> Future:
    """审批后异步通知预约人。"""
    title = "✅ 您的车辆预约已通过审批" if approval_result.approved else "❌ 您的车辆预约已被拒绝"
    body = (f"{title}\n"
            f"车辆编号：{approval_result.vehicle_no}\n"
            f"开始时间：{approval_result.start_time}\n"
            f"结束时间：{approval_result.end_time}\n"
            f"任务：{approval_result.task_name}\n"
            f"审批人：{approval_result.reviewer}\n"
            f"审批意见：{approval_result.review_comment or '无'}")
    return submit_text_to_user(applicant_open_id, body)
```

**为什么不用 subprocess**：参考项目用 `subprocess.run(lark_message_tool.py)`，有 30s 阻塞 + 进程管理成本。我们 `feishu/notify.py` 已经在异步线程池里跑过 — 直接复用更优。

## 9. MCP 客户端

### 9.1 选型

hermes-agent 自带 `tools/mcp_tool.py`，通过 `~/.hermes/config.yaml::mcp_servers` 配置后**自动发现** MCP 工具并注册到 `registry`。**不需要**在 `car_tools/` 写 MCP 客户端代码。

但为统一异常处理 + 注入 caller + 测试 mock，**写一个薄包装** `car_tools/mcp_client.py`：

```python
class CarMcpClient:
    """对 hermes 内部 MCP 工具的薄包装。
    
    hermes-agent 已通过 mcp_tool.py 自动发现 MCP 工具,
    此处只做:
    1. 统一异常处理
    2. args 注入 (caller + 用户参数)
    3. 测试时 mock
    """
    def __init__(self, server_name: str = None):
        self.server_name = server_name or settings.CAR_MCP_SERVER_NAME
        # 从 hermes registry 取 toolset 下的工具名集合
        self._tools = registry.get_tool_names_for_toolset(self.server_name)
    
    def call(self, tool_name: str, args: dict, timeout: float = 30) -> Any:
        tool = registry.get_tool(tool_name)
        if tool is None:
            raise McpToolNotFound(f"MCP 工具 {tool_name} 未注册到 toolset {self.server_name}")
        return tool.handler(args)   # hermes 内部已做超时 + 重试
```

### 9.2 hermes 配置

`~/.hermes/config.yaml`:
```yaml
mcp_servers:
  car_booking:
    command: "python"
    args: ["/opt/car-mcp/server.py"]
    timeout: 30
    connect_timeout: 10
    env:
      CAR_BOOKING_API_URL: "https://car-platform.immotors.com/api"
```

**注意**：MCP server 启动方式（stdio / HTTP）由用户接口文档决定，我们 bot 端不实现。

## 10. 卡片设计

### 10.1 5 套卡片

| 卡片 | 触发 | 元素 | 按钮 |
|------|------|------|------|
| **车辆列表** | fetch_available_vehicles 成功 | 摘要 + 车辆表格（lark_md 表格） | [选1][选2]...[选N][取消] |
| **缺失字段** | dry_run_reservation 缺字段 | 列出缺失字段中文标签 + 示例句子 | 无（用户回复文本） |
| **确认预约** | 槽位齐 | 车辆 + 时间 + 任务 + 地点 + 申请人 | [确认][取消] |
| **预约成功** | create_reservation 成功 | 预约详情 + 调度员列表 | 无 |
| **预约失败** | create_reservation 失败 | 失败原因 | 无 |
| **审批结果** | approval 完成 | 审批结论 + 车辆/时间/任务/审批人 | 无 |

### 10.2 按钮 payload schema

```python
# 按钮 action 字段值
ACTION_SELECT_VEHICLE = "select_vehicle"
ACTION_CONFIRM_BOOKING = "confirm_booking"
ACTION_CANCEL_FLOW = "cancel_flow"   # 通用取消

# 按钮 value
{"action": "select_vehicle", "vehicle_no": "PNV332"}
{"action": "confirm_booking", "vehicle_no": "PNV332", "task_name": "...", ...}
{"action": "cancel_flow"}
```

### 10.3 与现有 `ocl/card_builder.py` 关系

`ocl/card_builder.py` 保留 — 它给 Agent 路径的 LLM 输出兜底。
`car_tools/card_builder.py` 是**业务专用**卡片构建器，给 Layer 0.5/0.6/状态机/Agent 的工具返回用。

## 11. OCL pipeline 改造

`ocl/pipeline.py` **保持不变**（5 步流水线通用）。改造点：
- `format_control` 适配车业务 Markdown 习惯（车辆/时间/芯片等术语）
- `content_filter` 加车业务白名单关键词
- `intent_filter._DOMAIN_KEYWORDS` 改为：
  ```python
  _DOMAIN_KEYWORDS = (
      "车辆", "预约", "审批", "调度员", "归还", "取消预约",
      "vehicle", "platform", "reservation",
      "Xavier", "ADCU", "Orin", "Thor",
      "架构", "状态", "不可用", "待审批", "已批准", "已驳回", "已完成", "已取消",
      "任务", "地点",
  )
  ```
- `_CHITCHAT_MARKERS` 删除 VLM/精标 词；保留通用闲聊词

## 12. 系统 prompt 重写

`bot/agent_pool.py::_FEISHU_SYSTEM_PROMPT_BASE`：

```python
_BASE = """当前时间：__NOW_CN__
（... 时间换算说明 ...）

你是 DMZ 智能体助手，专注于车辆预约管理：
- 查询可用车辆
- 预约 / 取消 / 归还车辆
- 查询我的预约、查询待审批（调度员/管理员）
- 审批预约（调度员/管理员）

**字段与枚举**：
- 平台：Xavier / ADCU / Orin / Thor
- 时间格式：yyyy-MM-dd HH:mm
- 任务 / 地点：自由文本

**信任用户报出的车辆编号**：
- 用户说"预约 PNV332"时直接调 single_vehicle_reservation(vehicleNo="PNV332", ...)，不先 list
- 车辆编号格式：字母+数字（如 PNV332 / SVV027 / SOV646）

**工具规则**：
- emailAddress / openid / mobile 由系统注入，不要询问
- 不要编造车辆编号、架构名、调度员邮箱
- 单轮最多 2 次工具调用

**预约两步流程（强制）**：
- 用户表达"预约 XX"时调 dry_run_reservation 拿确认卡
- 用户点 [确认] 后系统自动调 single_vehicle_reservation 真正下单
- 你看不到 single_vehicle_reservation 的工具描述，但 dry_run 已替你完成预约

**输出边界**：
- 不要输出 tool call JSON
- 不要在回复中列举具体值（卡片会呈现）
- 一次只发一条最终回复

回复风格：简洁直接，单条 ≤200 字。"""
```

## 13. 测试

### 13.1 单测（8 个文件）

| 文件 | 覆盖 |
|------|------|
| `test_car_handlers.py` | 7+2 handler：args 注入、L2 门控、normalizer 调用、错误分支 |
| `test_car_normalizers.py` | happy / 缺字段 / 类型错误 / 空数据 / schema 漂移 |
| `test_car_schemas.py` | Pydantic extra=forbid、Platform 枚举 |
| `test_car_state.py` | save / get / clear / TTL 过期 / 状态机推进 |
| `test_car_card_builder.py` | 5+1 套卡片：元素 / 按钮 / 摘要 |
| `test_notify_dispatchers.py` | submit 异步、email 解析、空 dispatcher |
| `test_notify_applicant.py` | submit 异步、reservation_store 查 openid、字段缺失 |
| `test_mcp_client.py` | tool 查找、调用、timeout、异常 |

**Mock 模式**：
- `mcp_client` mock → 不真连 MCP
- `feishu/notify` mock → 不真发飞书
- `bot/reservation_store` mock → 不真落盘

### 13.2 集成测试（手动 / docker-compose）

5 路径：
1. 查车 → 选车 → 确认 → 下单 → 通知调度员
2. 审批 → 通知预约人
3. 取消预约
4. 还车
5. 我的预约

### 13.3 selfcheck.py 改造

- 删 `check_vlm_no_email`（VLM 域不再存在）
- `_STALE_TOKENS` 改：
  ```python
  _STALE_TOKENS = ["mock_api", "mock_tools", "create_order", "list_orders",
                   "pay_order", "ship_order", "create_report_job",
                   "test_bench", "test_vlm", "/fmp/"]
  ```
- 加 `check_car_servers` — 验证 `~/.hermes/config.yaml::mcp_servers` 至少一个 car entry
- `_KEY_MODULES` 替换 `bench_tools.register` → `car_tools.register` 等

## 14. 迁移步骤（6 阶段，14-18 天）

### Phase 1：清理（1 天）

- 删 `bench_tools/`、`vlm_tools/`
- 删 `tests/unit/test_bench_*.py`（4 文件）、`test_vlm_*.py`（1 文件）
- 改 `pyproject.toml` 包列表
- 改 `config/settings.py` 删 BENCH/VLM env，加 CAR_MCP_SERVER_NAME
- 跑 `python scripts/selfcheck.py` 确保 7 项基础检查仍能通过（用 dummy env）

### Phase 2：基础设施（2 天）

- 建 `car_tools/` 框架（`__init__.py` 空、register.py 框架、schemas.py 完整 Pydantic）
- 建 `bot/car_state.py`（仿 dry_run_state）
- 改 `ocl/tool_guard.py` 引入 `CallerIdentity` + `set_current_caller`（保留旧 set_current_user/email 作 alias）
- 改 `ocl/identity.py` 加 `build_caller_identity` + `mobile_of`（stub）
- 改 `ocl/permission.py::TOOL_MIN_ROLE` 替换为车业务 8 工具
- 改 `hermes_plugins/feishu_acl` log 标识
- 跑 import 检查：所有 8 个新模块可导入

### Phase 3：业务核心（3 天）

- 建 `car_tools/handlers.py` 7+2 个 handler（mock mcp_client）
- 建 `car_tools/normalizers.py` 6 个 normalizer + `NormalizeError`
- 建 `car_tools/mcp_client.py` 薄包装
- 建 `car_tools/register.py` 8 工具注册到 hermes registry（`enabled_toolsets=["car"]`）
- 改 `bot/agent_pool.py` 改 `enabled_toolsets=["car"]` + 重写 system prompt
- 跑 import 检查 + 注册 8 工具全部 found

### Phase 4：状态机 + 卡片回调（2 天）

- 建 `car_tools/card_builder.py` 5+1 套卡片
- 改 `bot/handler.py`：
  - 加 car_query_fast_path (Layer 0.5) 关键词：「查车 / 可用车辆 / 列表」
  - 加 car_reservation_fast_path (Layer 0.6) 关键词：「预约 PNV332 ... 任务是 X 地点是 Y」
  - 加 car_state 推进逻辑 (Layer "car 业务子分支")
  - 加 escape 关键词：算了/换个/不订了 → clear state
- 改 `bot/card_action_handler.py`：
  - 加 `select_vehicle` / `confirm_booking` / `cancel_flow` 三个 action
  - 走 dry_run_state + car_state 持久化

### Phase 5：通知 + 自进化（2 天）

- 建 `car_tools/notify_dispatchers.py` 调 `feishu/notify.submit_dispatchers_by_email_blocking`
- 建 `car_tools/notify_applicant.py` 调 `feishu/notify.submit_text_to_user` + `bot/reservation_store.find_by_*`
- 改 `bot/reservation_store.py` 字段适配车业务（vehicle_no 替代 bench_no）
- 改 `bot/dry_run_state.py` 字段适配（保留兼容或重写）
- 改 `bot/dmz_memory.py` 加车业务关键词 pattern
- 改 `ocl/intent_filter.py` 改 domain_keyword

### Phase 6：测试 + 文档（4 天）

- 8 个新单测 + mock fixture
- 改 `scripts/selfcheck.py`（见 §13.3）
- 重写 `README.md` / `CLAUDE.md` / `docs/architecture.md` / `docs/design-decisions.md` / `docs/deployment.md`
- 改 `Dockerfile.bot` / `docker-compose.bot.yml`（如 MCP server 启动方式需调整）
- 端到端跑 5 路径（真连外部 MCP）
- 部署验证

## 15. 风险与限制

### 15.1 已知风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| **MCP server 字段命名漂移** | normalizer fail-fast → 工具返回 `{"error": ...}` → OCL 转用户友好消息 | normalizer 单元测试覆盖 happy/缺字段/类型错误 |
| **MCP server 不可用** | 工具返回连接错误 | OCL 兜底，提示"业务系统暂不可用"；mcp_client timeout=30s |
| **飞书 v3 Contact API 不返回 mobile** | mobile 字段恒为 None | 设计预留字段；Q3 接入新权限时再启用 |
| **身份 contextvars 跨线程残留** | 工具在错线程读不到 caller | 沿用 `contextvars.copy_context()` 模式 (已有) |
| **`~/.hermes/config.yaml` 配错 MCP** | 启动时找不到 tool | 自检 `check_car_servers` 验证 |
| **dry_run 状态 10min 过期** | 用户在选车后 10min 不点确认 → 状态过期 | 卡片文案明示"10 分钟内未回复将作废" |

### 15.2 已知限制

1. **不引入 intent_router 单次 LLM 分类** — 复杂多槽位输入仍走 Agent 路径（多轮 ReAct），token 成本较高
2. **不实现 MCP server** — 假设用户提供；若用户接口文档延迟，需 mock 数据
3. **不引入 Coordinator Agent** — 工具数 8，未达 30+ 阈值（参考 design-decisions.md 决策 6）
4. **不实现多车辆同批预约** — 每次只支持 1 辆（与参考项目一致）
5. **不实现跨日时间解析的复杂逻辑** — 沿用 `bot/handler.py::_parse_chinese_time`

## 16. 未来工作（Phase 2+）

- **mobile 字段正式接入**（2026 Q3 接入飞书 contact v3 mobile 权限后）
- **分类与槽位准确率 CI gate**（参考项目设计，已列入设计文档 §12 验证方案但本期不做）
- **多 Agent 路由**（若工具数 > 30，触发 hermes `delegate_task` 多 agent）
- **Prometheus metrics 接入**（导出 latency / error / 工具调用分布）

## 17. 验收标准

- [ ] 6 个意图全部端到端跑通（查 / 订 / 审 / 取 / 还 / 我的预约）
- [ ] 8 个新单测全绿
- [ ] selfcheck.py 7+ 项全绿
- [ ] OCL pipeline < 100ms 性能不变
- [ ] 双层防御 L1 + L2 兜底验证（手动关掉 plugin 测 L2 仍能拦截）
- [ ] 身份注入 email + openid 出现在 MCP 调用 args，**不**出现在 LLM 可见的 tool schema
- [ ] 飞书卡片按钮回调确定性（不经过 LLM），文本"确认/取消"回退路径仍可用
- [ ] 调度员 DM / 申请人 DM 都通过 `feishu/notify.py` 异步发送
- [ ] 跨会话记忆 dmz_memory 在新业务下能积累偏好（架构名 / 任务名）

---

**End of design**
