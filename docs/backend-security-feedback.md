# 给后端（dmz-fmp）的安全 / 接口契约反馈

> 来源：约车飞书机器人（hermes-feishu-agent）联调
> 后端版本：`dmz-fmp` 分支 `dev-260617`，运行容器 `fmp-app (dmz-fmp:latest)` / `dmz-fmp-mcp`
> 整理日期：2026-06-26
> 范围：仅针对 MCP 开放接口 `/fmp/openApiMcp/*`（机器人/Agent 实际调用的那一层）

机器人侧不需要后端为此立即改动；以下是联调中发现、建议后端评估的 3 点，按优先级排列。

---

## 🔴 P0｜`getUserContext` 把 `password`（BCrypt 哈希）回传给了 MCP/Agent

**现象**：调 `POST /fmp/openApiMcp/getUserContext`（在岗用户）返回的 `data` 里含密码哈希：

```json
{
  "code": 200, "message": "success",
  "data": {
    "id": "...", "employeeName": "澄海", "mobile": "158****0001",
    "emailAddress": "", "accountStatus": 1,
    "password": "$2a$10$imOMd3S5E2Cu.28ADTLVJOdt7sN...",   // ← 不应出现
    "dmzUserId": "", "feishuId": "", "createUser": "...", "updateUser": "...",
    "shift": "早班(8-16)", "functionModule": "NP交替测试"
  }
}
```

**根因**：
- `FmpOpenForMcpController.getUserContext()` 直接 `Result.success(employee)`，返回的是 **整张 `Employee` 实体**；
- `EmployeeServiceImpl.queryEmployeeByEmailOrMobile()` 返回裸 `Employee`，`Employee` bean 含 `password` 字段，Jackson 默认序列化它。

**风险**：密码哈希是敏感凭据，离线暴力/字典攻击的输入。它经 MCP 流到 Agent 进程、可能进日志/对话上下文/记忆层——本不该离开 fmp-app 边界。同时 `dmzUserId / feishuId / createUser / updateUser` 等内部字段对机器人也无用，属过度暴露。

**建议**：`getUserContext` 改为返回一个**精简 VO**（只含机器人需要的字段：`id / employeeName / mobile / emailAddress / accountStatus`，外加下方 P1 需要的 `roleIds`），而不是直接返回 `Employee` 实体。最低限度也应给 `Employee.password` 加 `@JsonIgnore`。

> 机器人侧已在记忆/日志层做了敏感字段剥离（`_strip_sensitive`），但这是纵深防御，不能替代接口本身不外泄。

---

## 🟠 P1｜接口契约变更：`getUserContext` 不再返回 `role`（0617 RBAC 重构的副作用）

**现象**：`dev-260617` 把角色从 `employee.role` 单字段重构为标准多角色 RBAC
（`sys_role / sys_user_role / sys_role_menu / sys_menu` + `RoleHelper`，注释明确「不再直接读取 employee.role 字段」）。
`Employee` bean 随之删掉了 `role` 字段，于是 `getUserContext` 的返回里**不再有任何角色信息**。

**影响**：机器人原本依赖 `getUserContext.data.role`（1 工程师 / 2 调度员 / 3 管理员）做粗粒度工具门控。
该字段消失后，机器人这条「从后端同步角色」的链路静默失效（不报错、退回本地 identity_map）。
机器人侧已按"只改我方、不动 MCP"的约定，移除了这条依赖、回归 identity_map 作为角色源——**所以这条不是 bug 单，是契约对齐说明**。

**如果后端希望机器人继续反映后端角色**，二选一即可（任一都能让我方恢复自动同步）：
1. （推荐，改动最小）在 `getUserContext` 的返回 VO 里补 `roleIds: number[]`（来自 `sys_user_role`），
   或直接给一个主角色 `primaryRole`（`RoleHelper.getPrimaryRoleId` 已有此优先级逻辑：3>5>2>1>4）。
2. 新增一个开放接口 `/fmp/openApiMcp/getUserRoles`（入参同 getUserContext），返回该用户的 `roleIds`。

**附：角色档位也从 3 档扩到 5 档**，机器人侧目前是 1/2/3 三档模型，若要对齐需要映射规则：

| roleId | 名称 | 机器人现有模型 |
|--------|------|----------------|
| 1 | 工程师 | role 1 普通用户 |
| 2 | 调度员 | role 2 调度员 |
| 3 | 管理员 | role 3 管理员 |
| 4 | 司机 | （新增，待定：建议归 role 1） |
| 5 | 组管理员 | （新增，待定：介于 2~3，建议归 role 2 或 3） |

需要后端确认 4/5 两个新角色在「约车」场景下的能力边界，我方再定映射。

---

## 🟡 P2｜车组（VehicleGroup）只在管理端，未在 MCP 开放接口暴露（信息同步，非缺陷）

后端 `VehicleGroupController`（`/fmp/vehicleGroup/*`）有完整车组管理能力（增删改、查列表、查组内车/人、人车绑定），
运行库里也有真实数据（`vehicle_group`：车组1/塔山路、爵胜/创新港 等；`employee_vehicle_group_link` 12 条归属）。
但 `FmpOpenForMcpController`（`/fmp/openApiMcp/*`）只开放了 10 个预约/查询接口，**不含任何车组接口**。

机器人当前通过 `fetchAvailableVehicles`（已按调用者车组归属过滤）间接享受车组关系，够用，**暂不需要后端改动**。
仅作记录：若未来机器人要直接展示/查询车组名单，需后端在开放接口补一个 `queryVehicleGroupList` 之类的只读端点。

---

## 附：当前 MCP 开放接口清单（`/fmp/openApiMcp/*`，10 个）

`getUserContext` · `getCommonDictionary` · `fetchAvailableVehicles` · `singleVehicleReservation` ·
`cancelVehicleReservation` · `approvalVehicleReservation` · `returnVehicle` · `fetchUserReservation` ·
`fetchUserApproval` · `fetchTaskBoard`

（其中 `getCommonDictionary`、`fetchTaskBoard` 相对早期版本为新增。）
