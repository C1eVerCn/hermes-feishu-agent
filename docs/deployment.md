# 部署指南

## 前置条件

- Python 3.11+
- `hermes-agent` 已安装（`pip install -e ".[dev]"` 会带上）
- 飞书应用凭证（APP_ID、APP_SECRET），并已开启「获取用户邮箱」权限
- Minimax API Key
- 两个后端服务可达：台架预约 API（9013）、dmz-ess-vlm API（9014）

## 环境变量

完整模板见 [`.env.example`](../.env.example)。要点：

```bash
# 必填
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
MINIMAX_API_KEY=xxx

# 业务后端（两域）
BENCH_API_BASE_URL=http://localhost:9013     # 台架预约
VLM_API_BASE_URL=http://localhost:9014       # VLM 精标

# Bench API 服务账号 JWT（不填用 dev 默认值）
BENCH_JWT_SECRET=
BENCH_JWT_SUB=

# 可选（有默认值）
MINIMAX_BASE_URL=https://api.minimax.chat/v1
MINIMAX_MODEL=MiniMax-Text-01
AGENT_MAX_ITERATIONS=30      # 硬上限，勿降
AGENT_TIMEOUT_SECONDS=120    # 硬上限，勿降
AGENT_POOL_MAX_SIZE=100
HTTP_PORT=8088
LOG_LEVEL=INFO

# OCL
OCL_ADMIN_USER_IDS=ou_admin1,ou_admin2   # 这些 open_id 自动获得 role=3
OCL_MAX_OUTPUT_CHARS=4000
OCL_WARN_OUTPUT_CHARS=2000
OCL_CONTENT_BLOCK_MESSAGE=抱歉，该内容不在我的服务范围内，请换一个问题。

# 自进化记忆落盘根目录（默认 ./data，应挂载持久卷）
# DMZ_MEMORY_HOME=/data
```

## 插件部署（双层防御 Layer 1）

```bash
mkdir -p ~/.hermes/plugins

# 开发：符号链接
ln -sfn /path/to/hermes-feishu-agent/hermes_plugins/feishu_acl ~/.hermes/plugins/feishu_acl
# 生产：拷贝
cp -r /path/to/hermes-feishu-agent/hermes_plugins/feishu_acl ~/.hermes/plugins/feishu_acl
```

在 `~/.hermes/config.yaml` 启用：

```yaml
plugins:
  enabled:
    - feishu_acl
```

> 即使插件未加载，Layer 2 的 `guarded()` 包装器仍会兜底权限校验——但生产务必部署插件以获得双层防御。

## 身份配置

`data/identity_map.json`（gitignored，复制自 `.example`）只存**管理员角色覆盖**；email/name 由飞书 Contact API 自动解析并建档。三种角色来源优先级：

1. `data/identity_map.json` 显式覆盖
2. `OCL_ADMIN_USER_IDS` 环境变量（自动升至 role 3 并写回 store）
3. 飞书能解析到 email → 默认 role 1；否则 role 0（非平台用户）

运行时可在飞书内由管理员调整：`设置角色 <open_id> <1|2|3>`。

## 启动与验证

```bash
python main.py
curl http://localhost:8088/health     # ws_connected=true 即就绪

# 部署前自检（推荐接入 CI）
python scripts/selfcheck.py
```

## 持久化数据

`data/` 下运行时产物（均 gitignored）：

| 文件/目录 | 内容 |
|----------|------|
| `identity_map.json` / `identity_audit.jsonl` | 身份记录 + 审计 |
| `reservation_applicants.json` | 预约人映射（供审批通过后回 DM） |
| `dmz_memory/<hash>/memory.json` | Phase 1 跨会话记忆（30 天 TTL） |
| `feedback/operations/*.jsonl` | Phase 2 操作模式 |
| `curator/suggestions/*.jsonl` | Phase 3 Curator 只读建议 |

容器部署时把 `data/`（或 `DMZ_MEMORY_HOME`）挂载为持久卷，保证跨重启不丢。
