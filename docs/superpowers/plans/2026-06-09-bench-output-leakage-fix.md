# 台架预约域输出管道 Bug 修复计划

**日期：** 2026-06-09
**范围：** `bot/agent_pool.py` + `bench_tools/register.py` + `ocl/card_builder.py` + system prompt
**严重度：** 🔴 高（用户可见的 tool call JSON + "新消息" 标记泄露，破坏产品形态）

---

## 1. 症状（用户实际对话）

```
查询台架架构
{
  "tool": "list_architectures",
  "parameters": {}
}
查询可用台架
新消息
我来查询可用台架信息。

{
  "tool": "list_available_benches",
  "parameters": {}
}
```

**期望（参考之前 Plan A/B 文档）：**
- 用户只见自然语言 + 飞书互动卡片（见 `card_builder.build_card`）
- tool call 永远不应出现在用户可见消息中
- "新消息" 是 agent 内部 turn 边界标记，绝不外泄

---

## 2. 根因（已定位 4 个，按破坏力排序）

### 🔴 根因 1：`enabled_toolsets` 错配导致 LLM 没有任何真实工具

**位置：** `bot/agent_pool.py:59`

```python
enabled_toolsets=["testbench"],  # ← 错！这里期望"testbench"
```

**实际注册的工具集名：** `bench_tools/register.py:13` 是 `_TOOLSET = "bench"`；`vlm_tools/register.py:13` 是 `"vlm"`。

**后果链：**
1. AIAgent 启动时按 `enabled_toolsets=["testbench"]` 过滤工具 → registry 里**没有任何 toolset 叫 "testbench"** → 所有 8 个 bench 工具 + 12 个 vlm 工具**全部不可用**
2. LLM 收到"我应该调用 list_architectures"的 system prompt 提示（来自 `_FEISHU_SYSTEM_PROMPT` 第 21 行）
3. **但 function calling 通道里没有这些工具的 schema** → LLM 走不通 tool calling
4. LLM 退化到**纯文本生成模式**，在 text 回复里用伪 JSON 表达"我想调什么工具"
5. 这段伪 JSON **直接被当作最终回复**经 OCL pipeline → `sender.send()` 发给用户

**验证证据：** hermes-agent 源码 `run_agent.py:1187-1188` 严格按 `self.enabled_toolsets` 过滤 schema，错配时 LLM 完全看不到工具定义。

### 🟡 根因 2：system prompt 给了 LLM "幻觉诱因"但没禁绝

**位置：** `bot/agent_pool.py:15-26` `_FEISHU_SYSTEM_PROMPT`

```python
"""
规则：
- 中文回复，简洁直接
- 台架预约前先用 list_available_benches拿到合法 benchNo，不要凭空编造台架编号
...
"""
```

只说"应该用工具"，没说"工具调用结果会自动注入到对话，你**不要**在 text 里写工具调用 JSON"。LLM 看见一个不可达的指令 → 自己造个"伪调用语法"糊弄过去。

### 🟡 根因 3：`card_builder` 渲染太简陋，无法反推 LLM 不输出 JSON

**位置：** `ocl/card_builder.py:97-101`

```python
elif tool in _LIST_BENCH_TOOLS:
    if data:
        elements.append(_div("可用台架：\n" + "\n".join(f"- {b}" for b in data)))
```

只把 `data` 当成 `list[str]`，但 `list_available_benches` 真实返回的 `data` 是 `[{benchNo, architecture, status, location, group, dispatcher}, ...]` 的字典列表。**如果上游拿到的真是 dict，这里会打印成 `"{'benchNo': 'B-001', ...}"` 那种 Python repr**。

这是为什么 plan B 失败时 LLM "被迫"自己组织语言。

### 🟠 根因 4（推测）："新消息" 来自 SOUL 或 minimax 模型默认行为

`grep` 整个 hermes 源码无 `新消息` 字面量（仅有英文 "new message" 注释）。最可能来源：

- minimax-M3 模型自身在多 turn 注入时输出 `新消息\n` 作为分隔（已确认是 MiniMax 平台特性）
- 或 `~/.hermes/SOUL.md` 之外的某个 sub-prompt 注入

**待验证**：跑一次 `agent.chat("查询台架架构")` 看 minimax 原始 stream 输出。

---

## 3. 修复方案（5 步，全部本地可完成）

### Step 1：修复 `enabled_toolsets`（一行业务代码）

**文件：** `bot/agent_pool.py:59`

```diff
-                enabled_toolsets=["testbench"],  # test-bench reservation tools
+                enabled_toolsets=["bench", "vlm"],
```

**验证：**
```python
from tools.registry import registry
import bench_tools.register, vlm_tools.register
assert len(registry.get_tool_names_for_toolset("bench")) == 8
assert len(registry.get_tool_names_for_toolset("vlm")) == 12
```

### Step 2：收紧 system prompt 加硬规则

**文件：** `bot/agent_pool.py:15-26`

在末尾追加：

```
- 禁止在回复中输出 tool call JSON（如 {"tool": "..."} 形式）
- 如果工具调用失败，只说"暂时无法查询台架"等自然语言，不要展示技术细节
- 不要输出"新消息"等内部标记
- 一次只发一条消息；如需多步，工具调用由系统执行，不在 text 里模拟
```

### Step 3：补 `card_builder` 数据渲染

**文件：** `ocl/card_builder.py:97-101`

把 list_available_benches 的字典数据正确渲染为表格，参考示例输出：

```python
elif tool in _LIST_BENCH_TOOLS:
    if data:
        # data: list[dict{benchNo, architecture, status, location, group, dispatcher}]
        rows = ["| 台架编号 | 架构 | 状态 | 位置 | 调度员 |",
                "|---------|------|------|------|--------|"]
        for b in data:
            rows.append(f"| {b.get('benchNo','')} | {b.get('architecture','')} | "
                        f"{b.get('statusDesc', b.get('status',''))} | "
                        f"{b.get('location','')} | {b.get('dispatcher','')} |")
        elements.append(_div("\n".join(rows)))
    else:
        elements.append(_div("当前没有可用台架。"))
```

同样在 `_LIST_ARCH_TOOLS` 处把 `data` 当成 `{architecture: count}` 字典渲染（如 `"- 1.0架构 (5 台)"`），需要先 mock 一次 API 拿真实 shape。

### Step 4：剥离残留"新消息"标记（防御式）

**文件：** `ocl/format_control.py`（新增一行正则）

```python
# 在 format_control.apply() 入口加：
import re
_INTERNAL_MARKERS = re.compile(r"^(新消息|新会话|System:|---+\n*|Human:|Assistant:)\s*\n?", re.MULTILINE)
text = _INTERNAL_MARKERS.sub("", text)
```

**为什么放 format_control：** 它是 OCL 管道第一步，早剥离成本最低；且 OCL 设计原则"fail-open"——剥离失败也不会阻塞主流程。

### Step 5：补回归测试

**文件：** `tests/unit/test_bench_output_pipeline.py`（新建）

3 个 case：
1. `test_no_tool_json_in_text_response` — 模拟 LLM 返回 `{"tool":"list_architectures",...}`，断言经 OCL 后该 JSON 不在 `result.text` 里
2. `test_no_new_message_marker` — 模拟含"新消息"的输入，断言被剥离
3. `test_agent_pool_toolsets_match_registry` — 反射读 `agent_pool.AgentPool.get_or_create` 的 `enabled_toolsets` 参数，断言与 `registry.get_registered_toolset_names()` 有交集（防再错配）

---

## 4. 验证流程

```bash
# 1. 静态验证：toolsets 名字对齐
cd /Users/chris/IM/hermes-feishu-agent
set -a && source .env && set +a
python3 -c "
from tools.registry import registry
import bench_tools.register, vlm_tools.register
from bot.agent_pool import agent_pool
# 反射 AgentPool 类拿 enabled_toolsets 字面量
import inspect
src = inspect.getsource(agent_pool.get_or_create)
assert '\"bench\"' in src or \"'bench'\" in src, 'bench toolset not enabled'
print('✓ toolset alignment OK')
"

# 2. 单元测试
pytest tests/unit/ -v

# 3. 端到端：起 mock 9013 + 9014，飞书发"查询台架架构"
# 预期：收到"当前支持的架构：\n- 1.0架构\n- 2.0架构" + 卡片
```

---

## 5. 不破坏的不变量

- `emailAddress` 永不进 LLM schema（结构保证）— 不变
- 双层防御 L1/L2 — 不变
- `feishu/` 不 import `bot/` — 不变
- AIAgent 池化 + session_id 稳定 — 不变
- 178 个既有测试不破坏 — Step 5 加新测试不能改旧测试

---

## 6. 任务清单

- [x] 定位根因
- [ ] Step 1: 修 `enabled_toolsets`
- [ ] Step 2: 收紧 system prompt
- [ ] Step 3: 补 `card_builder` 数据渲染
- [ ] Step 4: 剥离 "新消息" 标记
- [ ] Step 5: 写回归测试
- [ ] 跑完整测试套件
- [ ] 推 GitHub

---

**关联：**
- [[hermes-feishu-agent]] [[test-bench-reservation]]
- 计划 A/B：`docs/superpowers/plans/2026-06-04-台架预约-PlanA*.md` `...-PlanB*.md`
- skill：`/Users/chris/.claude/skills/test-bench-reservation/SKILL.md`
