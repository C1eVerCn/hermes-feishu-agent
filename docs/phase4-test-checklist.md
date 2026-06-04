# Phase 4 手动测试清单

**状态：** 待执行（需要飞书实例运行 hermes-feishu-agent）  
**前置条件：** hermes-feishu-agent 在主线程运行，Plugin 已部署并启用，Mock API 在端口 8090 运行

---

## M1：read 工具放行

| 项目 | 内容 |
|------|------|
| **用户** | 默认用户（只有 read 权限，未申请任何扩展权限）|
| **输入** | "列出所有用户" |
| **预期** | Agent 调用 `list_users`；L1 plugin 返回 None（放行）；mock_api 返回用户列表；LLM 正常回复 |
| **验证** | 飞书收到用户列表回复；日志无 `feishu_acl: BLOCKED` 关键词 |

## M2：write 工具被 block

| 项目 | 内容 |
|------|------|
| **用户** | 默认用户（只有 read 权限）|
| **输入** | "帮我创建一个订单，商品是测试商品，数量 1" |
| **预期** | Agent 尝试调用 `create_order`；L1 plugin 返回 `{"action":"block"}`；hermes 返回 block message 作为工具结果；LLM 看到错误后回复用户"权限不足" |
| **验证** | 飞书收到权限不足的回复；日志含 `feishu_acl: BLOCKED tool=create_order` |

## M3：申请 → 审批 → 放行

| 项目 | 内容 |
|------|------|
| **用户** | 默认用户（只有 read 权限）|
| **步骤 1** | 用户发送 "申请写入权限" |
| **预期 1** | handler 正则匹配 → submit_request → 通知管理员；用户收到 "已提交申请 (ID: xxxx)" |
| **管理员** | 管理员收到飞书通知 |
| **步骤 2** | 管理员发送 "批准 `<user_open_id>` write" |
| **预期 2** | handler 正则匹配 → resolve_request → grant → 通知申请人 |
| **步骤 3** | 用户再次发送 "创建一个订单，商品是测试商品 2，数量 1" |
| **预期 3** | Agent 调用 `create_order`；L1 plugin 返回 None（放行）；订单创建成功 |
| **验证** | 步骤 1 管理员收到通知；步骤 2 用户收到 "权限申请已通过"；步骤 3 订单成功创建 |

## M4：跨用户隔离

| 项目 | 内容 |
|------|------|
| **用户 A** | 默认用户（只有 read 权限）|
| **用户 B** | 已批准 write 权限 |
| **操作** | A 和 B 同时（或接近同时）发送 "创建一个订单" |
| **预期** | A 的 `create_order` 被 block；B 的 `create_order` 放行 |
| **验证** | 日志显示 A 的 session_id 对应 user_A 被 block；B 的 session_id 对应 user_B 被放行；A 收到权限不足，B 收到订单创建成功 |

## M5：Pool 淘汰 → session_map 同步

| 项目 | 内容 |
|------|------|
| **设置** | `AGENT_POOL_MAX_SIZE=2`（重启服务）|
| **步骤 1** | 用户 A 发送 "列出用户" |
| **步骤 2** | 用户 B 发送 "列出用户" |
| **步骤 3** | 用户 C 发送 "列出用户"（A 被 LRU 淘汰）|
| **步骤 4** | A 再次发送 "创建一个订单" |
| **预期** | A 的 Agent 被重新创建，session_id 重新注册，权限检查正常工作（若 A 只有 read 则 block）|
| **验证** | 日志显示 A 的 Agent 被 evicted 后重新 get_or_create；A 重新注册了新 session_id；权限检查正常 |
