# 部署指南

## 前置条件

- Python 3.11+
- `hermes-agent` 已安装（`pip install hermes-agent`）
- 飞书应用凭证（APP_ID、APP_SECRET）
- Minimax API Key
- Mock API 服务（Phase 2，端口 8090）

## 环境变量

```bash
# 必填
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
MINIMAX_API_KEY=xxx

# 可选（有默认值）
MINIMAX_BASE_URL=https://api.minimax.chat/v1
MINIMAX_MODEL=MiniMax-Text-01
AGENT_MAX_ITERATIONS=30
AGENT_TIMEOUT_SECONDS=120
AGENT_POOL_MAX_SIZE=100
HTTP_PORT=8088
LOG_LEVEL=INFO

# OCL
OCL_ADMIN_USER_IDS=ou_admin1,ou_admin2
OCL_MAX_OUTPUT_CHARS=4000
OCL_WARN_OUTPUT_CHARS=2000
OCL_CONTENT_BLOCK_MESSAGE=抱歉，该内容不在我的服务范围内，请换一个问题。

# Mock API
MOCK_API_BASE_URL=http://localhost:8090
MOCK_API_TOKEN=<从 mock_api 启动日志中获取>
```

## Plugin 部署

### 开发环境（符号链接）

```bash
mkdir -p ~/.hermes/plugins
ln -sfn /path/to/hermes-feishu-agent/hermes_plugins/feishu_acl ~/.hermes/plugins/feishu_acl
```

### 生产环境（拷贝）

```bash
mkdir -p ~/.hermes/plugins
cp -r /path/to/hermes-feishu-agent/hermes_plugins/feishu_acl ~/.hermes/plugins/feishu_acl
```

### 启用 Plugin

在 `~/.hermes/config.yaml` 中添加：

```yaml
plugins:
  enabled:
    - feishu_acl
```

## 启动

```bash
# 启动 Mock API（端口 8090）
python3 -m uvicorn mock_api.main:app --port 8090 --log-level warning &

# 启动主服务
python3 main.py

# 验证健康检查
curl http://localhost:8088/health
```

## 部署验证清单

- [ ] `curl http://localhost:8088/health` 返回 `ws_connected: true`
- [ ] Plugin 发现验证：
  ```bash
  python3 -c "
  from hermes_cli.plugins import discover_plugins
  discover_plugins(force=True)
  plugins = __import__('hermes_cli.plugins').plugins.get_plugin_manager().list_plugins()
  assert any(p.get('name') == 'feishu_acl' for p in plugins), 'Plugin NOT found!'
  "
  ```
- [ ] 飞书发送 "你好" → 收到正常回复
- [ ] 飞书发送 "我的权限" → 返回当前权限组
- [ ] 飞书发送 "申请写入权限" → 管理员收到通知
- [ ] 管理员发送 "批准 ou_xxx write" → 申请人收到通知
- [ ] 全量单元测试：`pytest tests/unit/ -v` 全部通过
- [ ] 集成测试（mock_api 运行中）：`MOCK_API_TOKEN=<token> pytest tests/ -v` 全部通过

## 回滚

```bash
# 移除 plugin 符号链接
rm ~/.hermes/plugins/feishu_acl

# 从 config.yaml 中移除 feishu_acl
# 编辑 ~/.hermes/config.yaml，删除或注释 plugins.enabled 中的 feishu_acl

# 重启服务
```

Plugin 禁用后，Layer 2（guarded() handler 包装器）继续工作，权限检查不受影响。

## 关键日志

| 日志模式 | 含义 |
|---------|------|
| `feishu_acl: BLOCKED tool=X user=Y session=Z` | Layer 1 成功阻断工具调用 |
| `tool_blocked tool=X user_id=Y` | Layer 2 兜底阻断 |
| `content_blocked reason=X len=Y` | 内容过滤触发 |
| `ocl_pipeline_error` | Pipeline 异常，fail-open |
