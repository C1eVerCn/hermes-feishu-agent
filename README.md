# hermes-feishu-agent（约车助手）

基于 [hermes-agent](https://github.com/NousResearch/hermes-agent) + Minimax 的飞书机器人，通过 WebSocket 接收消息，在真实通道上探索**受控 LLM 输出**（权限、内容、格式、能力边界）。

整合两个业务域：
- **台架预约** — 台架预约 REST API（端口 9013）
- **VLM 精标数据** — dmz-ess-vlm REST API（端口 9014）

> 📖 **完整设计说明见 [`docs/项目说明.html`](docs/项目说明.html)**（整体架构 / 工作流程 / 权限控制 / 输出边界 / 自进化约束 / 后续方向）。

## 快速开始

```bash
# 1. 安装依赖
pip install -e ".[dev]"

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填入 FEISHU_APP_ID、FEISHU_APP_SECRET、MINIMAX_API_KEY
# 以及两个后端地址 BENCH_API_BASE_URL(9013)、VLM_API_BASE_URL(9014)

# 3. 配置身份覆盖（可选；email/name 由飞书 Contact API 自动解析）
cp data/identity_map.json.example data/identity_map.json

# 4. 部署权限插件（双层防御的 Layer 1）
mkdir -p ~/.hermes/plugins
ln -sfn $(pwd)/hermes_plugins/feishu_acl ~/.hermes/plugins/feishu_acl
# 在 ~/.hermes/config.yaml 的 plugins.enabled 加入 feishu_acl

# 5. 启动
python main.py

# 6. 健康检查
curl http://localhost:8088/health   # 关注 ws_connected、metrics
```

## 架构一览

```
飞书 WS → feishu/ws_client → bot/handler
                                 │
                ┌─ Layer 0    简单意图（你好/帮助）→ 即时回复
                ├─ Layer 0.5  查询快速路径 → 直接调工具 <1s
                ├─ Layer 0.6  预约快速路径 → dry_run 确认卡片
                ├─ 身份/管理命令（我的权限/设置角色/查看用户）
                └─ Agent.chat（注入已核验身份前缀）→ Minimax
                       │  L1 pre_tool_call 插件（硬拦截）
                       └  L2 guarded() 包装器（兜底）
                       │  工具：bench@9013 / vlm@9014
                       ▼
                   ocl/pipeline.apply  format→content→length→卡片
                       ▼
                   feishu/sender → 飞书（互动卡片 / 文本兜底）
```

## 目录结构

```
feishu/          飞书传输层（WS、发送、限流、通知）
bot/             Agent 桥接、意图分层、卡片回调、身份管理、自进化
ocl/             输出控制层（格式/内容/长度、角色 ACL、卡片构建）
bench_tools/     台架预约工具（emailAddress 服务端注入 + JWT）
vlm_tools/       VLM 精标工具（无身份注入）
hermes_plugins/  hermes 插件（feishu_acl：pre/post_tool_call）
infra/           去重、指标、健康检查、原子 JSON 存储
config/          统一配置（读 .env）
data/            运行时数据（identity_map、记忆、反馈等；gitignored）
scripts/         自动化自检与回环修复脚本
tests/           unit/（无网络）+ integration/
docs/            项目说明.html + superpowers/ 设计文档
```

## 权限模型

三档角色（`role` 来自 `data/identity_map.json` 覆盖、`OCL_ADMIN_USER_IDS` 环境变量、或飞书 email 自动建档）：

| 角色 | 能力 |
|----|------|
| 1 普通用户 | 查询/预约/取消/归还台架、查询自己的预约、查 VLM 公开数据 |
| 2 调度员 | + 审批本组预约、查本组待审批、下载 VLM 元数据 |
| 3 管理员 | + 跨组审批、触发 VLM 同步等系统级操作 |

管理员在飞书内指派角色：`设置角色 <open_id> <1|2|3>`；查看用户：`查看用户`。

## 测试与自检

```bash
# 单元测试（无网络，零环境变量要求）
FEISHU_APP_ID=x FEISHU_APP_SECRET=x MINIMAX_API_KEY=x pytest tests/unit/ -q

# 一键自动化自检（测试 + 编译 + 配置漂移 + 文档/接线检查）
python scripts/selfcheck.py

# 自检 + 回环自动修复（调用 Claude Code headless 反复修到全绿）
scripts/autofix.sh
```

## 文档

- [项目说明（HTML，推荐）](docs/项目说明.html)
- [架构](docs/architecture.md) · [部署](docs/deployment.md) · [设计决策](docs/design-decisions.md)
- [设计文档（历史记录）](docs/superpowers/)
