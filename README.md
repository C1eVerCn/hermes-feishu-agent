# hermes-feishu-agent

基于 [hermes-agent](https://github.com/NousResearch/hermes-agent) 的飞书智能体，实现受控 LLM 输出（权限、内容、格式、能力边界）。

**当前进度：** Phase 1+2+3+3.5 完成，110 个测试全部通过。

## 快速开始

```bash
# 安装依赖
pip install -e ".[dev]"

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 FEISHU_APP_ID、FEISHU_APP_SECRET、MINIMAX_API_KEY

# 部署权限 plugin
mkdir -p ~/.hermes/plugins
ln -sfn $(pwd)/hermes_plugins/feishu_acl ~/.hermes/plugins/feishu_acl

# 在 ~/.hermes/config.yaml 中添加：
#   plugins:
#     enabled:
#       - feishu_acl

# 启动 Mock API（另一个终端）
python3 -m uvicorn mock_api.main:app --port 8090 --log-level warning

# 启动主服务
python3 main.py

# 健康检查
curl http://localhost:8088/health
```

## 架构

```
飞书 WS → feishu/ws_client.py → bot/handler.py
                                       │
                              ┌─ 意图检测（bypass agent）
                              └─ agent.chat() → Minimax API
                                       │
                              hermes 工具调度
                                 ├─ L1: pre_tool_call plugin（硬阻断）
                                 └─ L2: guarded() wrapper（兜底）
                                       │
                              ocl/pipeline.apply()
                                 ├─ format_control
                                 ├─ content_filter
                                 └─ length_limiter
                                       │
                              feishu/sender.py → 飞书
```

## 权限模型

三个权限组，JSON 文件持久化，飞书内申请审批：

| 组 | 工具 | 获取 |
|----|------|------|
| read | 查询用户/订单 | 默认 |
| write | 创建订单/用户、支付、发货 | 申请 |
| report | 报表任务 | 申请 |

用户输入"申请写入权限"→ 管理员审批 → 权限生效。全程飞书内完成。

## 目录结构

```
feishu/          — 飞书传输层（WS、发送、限流）
bot/             — Agent 桥接（消费、Agent 池）
ocl/             — 输出控制层（格式、内容、长度、权限）
mock_tools/      — Mock API 工具注册
mock_api/        — Mock 企业 REST API
infra/           — 去重、指标、健康检查
config/          — 统一配置
hermes_plugins/  — hermes 插件（feishu_acl ACL）
tests/           — unit/ + integration/
docs/            — 架构、部署、设计决策文档
```

## 测试

```bash
# 单元测试
FEISHU_APP_ID=x FEISHU_APP_SECRET=x MINIMAX_API_KEY=x pytest tests/unit/ -v

# 全量（需要 mock_api 运行）
MOCK_API_TOKEN=<token> pytest tests/ -v
```

## 文档

- [系统架构](docs/architecture.md)
- [部署指南](docs/deployment.md)
- [设计决策](docs/design-decisions.md)
- [Phase 4 手动测试清单](docs/phase4-test-checklist.md)
