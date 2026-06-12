import logging
import threading
from collections import OrderedDict
from datetime import datetime, timedelta

from run_agent import AIAgent  # hermes-agent package

import bench_tools.register # registers all bench reservation tools as a side effect (runs once)
import vlm_tools.register # registers VLM real-API tools as a side effect (runs once)
from config.settings import settings
from ocl.session_map import register as session_register, evict as session_evict

log = logging.getLogger(__name__)


def _warmup_agent(agent) -> None:
    """Background thread target. Forces hermes-agent's lazy init —
    OpenAI SDK import (240ms) + first client instantiation — without
    making a real LLM call (which would cost $$ and pollute session
    history with a stray "hello" turn).

    `agent._create_openai_client` is the lazy-init chokepoint: the
    OpenAI SDK is only imported on the first client creation, which
    happens inside the first agent.chat() call. Pre-creating the
    client here moves that cost off the critical path of the first
    user message. The MCP client and tool registry are already
    loaded by the AIAgent constructor.

    Failures are logged and swallowed — the next user message will
    fall back to the lazy-init path, just slow.
    """
    try:
        agent._create_openai_client(
            agent._client_kwargs, reason="container_warmup", shared=True,
        )
    except Exception:
        log.exception("agent_warmup_failed")


def _now_cn() -> str:
    """Return current CN-time as 'YYYY-MM-DD HH:MM:SS (周X)'. Refreshed per
    call so prompt never goes stale."""
    now = datetime.now() + timedelta(hours=8)  # UTC+8 China
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return now.strftime("%Y-%m-%d %H:%M:%S") + f" (周{weekdays[now.weekday()]})"


# 以 ephemeral_system_prompt注入 —覆盖 ~/.hermes/SOUL.md（仅本服务）
# The base prompt is plain text (no .format() applied). We prepend the
# current-time line at construction time to avoid str.format() interpreting
# the literal `{`/`}` in JSON examples (e.g. `{"tool": "..."}`) as
# placeholders.
_FEISHU_SYSTEM_PROMPT_BASE = """当前时间：__NOW_CN__
（基于上述时间换算 "今天"/"明天"/"后天" 等相对日期，如今天是 2026-06-10，则"明天"=2026-06-11，"后天"=2026-06-12；周X 表示星期X）

你是 DMZ智能体助手，整合两个业务域：
- 台架预约：查询架构、查询可用台架、预约/取消/归还台架、（调度员）审批预约
- VLM精标数据：查询场景/相机/Bag/帧图片列表与详情、下载元数据、（管理员）触发同步

规则：
- 中文回复，简洁直接
- **台架编号 vs 架构名区分**：
  - 台架编号格式：`CT001`/`TJ001`/`TJ052503`/`TB001`（字母+数字，可能含「0」开头的数字编号）—— 这是台架号
  - 架构名格式：`1.0架构`/`1.5架构`/`L3架构`/`L4架构`（带中文「架构」二字或 L+数字）
  - 绝不要把 `CT001` 当作 `CT001架构`；架构名不含「CT/TJ/TB」开头
- **信任用户报出的台架号**：用户说"预约CT001"时，**直接调 reserve_bench(benchNo="CT001", ...)**，不要先 list_available_benches 验证
- 工具返回的合法台架编号来自 list_available_benches；但**用户原话中的 `CT001`/`TJ001` 等符合台架号格式的就直接信任**
- **dry_run 字段值按用户原话字面填，不做语义判断/重写/互换**：
  - 用户说"任务是测试"→ `taskName="测试"`；说"目的是感知压测"→ `testPurpose="感知压测"`
  - 反过来：说"任务是感知压测"→ `taskName="感知压测"`；说"目的是测试"→ `testPurpose="测试"`
  - **不要**因为"感知压测"听起来更像任务名就把两者互换；按用户说的"X是Y"中的 Y 直接落到对应字段
  - 用户没说"任务是X/目的是X"时，留空走 dry_run 缺字段补全流程，不要替用户填值
- VLM 的 bagId/frameId只能从查询接口拿，不要编造
- 台架预约时间格式必须是 yyyy-MM-dd HH:mm:ss
- 不要询问或编造用户邮箱，台架预约由系统自动注入身份
- VLM工具不要传 emailAddress（VLM API 不鉴权）
- 不确定时说明，不编造信息；不讨论政治敏感、有害内容

输出边界（硬规则，违反会被 OCL 剥离）：
- 禁止在回复正文中输出 tool call JSON（如 {"tool": "..."}、{"name": ..., "arguments": ...} 等任何伪调用格式）
- 工具调用由系统在后台执行，**你看不到工具调用的过程**，只看到结果——拿到结果后用自然语言总结
- **工具返回的具体值（台架编号、架构名、时间、BagId 等）由系统以卡片形式单独呈现**——你**永远不要**在正文里列举这些具体值；正文只写**一句总结**（如"1.0架构共5个可用台架"）和**下一步提示**（如"如需预约直接告知台架编号"）。**违反会让用户看到重复内容**
- **例外（用户明确要列表时）**：如果用户**明确要列表**（如"给我具体台架号""列出所有台架"），**必须在正文里逐行列出**。当前 fmp 的 `list_available_benches` 端点只返台架号字符串（`['CT001','TJ001',...]`），**不返 per-bench 架构**——所以你做不到 `TJ001 - 1.0架构` 的精确配对。可做的：
  - 按 **系列**（CT / TJ / TB）分组列出
  - 同时调 `list_architectures` 拿 5 个架构名（1.0/1.5/3.0/L3/L4）放在回复里
  - 明确告诉用户"每个台架的具体架构需 fmp 端点改动后才能展示"
  - **这一条覆盖前一条规则**
- 禁止输出"新消息""新会话""System:""Human:""Assistant:""---"等内部 turn 标记
- 一次只发一条最终回复；如需多步决策，工具调用在后台完成，不在 text 里模拟
- **不要使用"暂时无法查询，请稍后再试"这类搪塞话术**——如果工具没返回你需要的数据，再调用一次相关工具；用户说"X架构的台架"时，**直接调 list_available_benches(architecture="X")** 即可，不要先 list_architectures 验证存在性
- **单轮对话最多 2 次工具调用**——超时前必须给出最终回复；不要为了"确认数据完整性"做第 3 次、第 4 次调用
- 真实失败时（如工具返回 HTTP 5xx），才说"暂时无法查询，请联系管理员"，否则永远基于工具返回的数据回答

**台架预约两步流程（API 层已强制）**：
- 用户表达"预约/提交 XX 台架"时，**调 `dry_run_reserve_bench` 拿确认卡片**——你**只能**调这个工具；真实预约工具对 LLM 不可见
- 用户点"✅ 确认"后系统自动完成预约；你无需再调任何工具
- dry_run 模式下，**不要**在正文里重新罗列预约信息——确认卡片已经完整呈现

回复风格（不要复述自己的思考过程）：
- 单条回复 ≤ 200 字（除非在列举数据/表格）
- 不要写"但是…说明…这不是X的问题"这种分析性转折
- 不要列"建议：1. … 2. …"这种自检清单；遇到无法处理时一句"请联系管理员"即可
- 失败原因用一句话说明，不要展开技术细节""".strip()


class AgentPool:
    """
    Thread-safe LRU pool of AIAgent instances, one per user_id.
    Evicts oldest entry when pool exceeds max_size.
    """

    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._pool: OrderedDict[str, AIAgent] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_create(self, user_id: str) -> AIAgent:
        with self._lock:
            if user_id in self._pool:
                self._pool.move_to_end(user_id)
                return self._pool[user_id]

            # Stable session_id — same across turns until evicted, enables
            # the feishu_acl plugin (Layer 1) to resolve user identity via
            # the pre_tool_call hook and session_map.
            session_id = f"feishu_{user_id}"

            agent = AIAgent(
                model=settings.MINIMAX_MODEL,
                provider="minimax",
                base_url=settings.MINIMAX_BASE_URL,
                api_key=settings.MINIMAX_API_KEY,
                quiet_mode=True,
                max_iterations=settings.AGENT_MAX_ITERATIONS,
                ephemeral_system_prompt=_FEISHU_SYSTEM_PROMPT_BASE.replace("__NOW_CN__", _now_cn()),
                enabled_toolsets=["bench", "vlm"],  # matches _TOOLSET in register.py
                session_id=session_id,           # Phase 3.5: plugin ACL
            )
            self._pool[user_id] = agent
            self._pool.move_to_end(user_id)
            session_register(session_id, user_id)

            # Cold-start warmup: spawn a background daemon thread to force
            # hermes-agent's lazy import (provider loading, tool registry,
            # prompt template). First user message after container start
            # otherwise pays 46s. Subsequent calls hit the cache and skip.
            threading.Thread(
                target=_warmup_agent,
                args=(agent,),
                daemon=True,
                name="agent-warmup",
            ).start()

            if len(self._pool) > self._max_size:
                evicted_id, evicted_agent = self._pool.popitem(last=False)
                evicted_sid = getattr(evicted_agent, "session_id", None)
                if evicted_sid:
                    session_evict(evicted_sid)
                log.debug("Evicted agent for user_id=%s session_id=%s",
                          evicted_id, evicted_sid)

            return agent

    def size(self) -> int:
        with self._lock:
            return len(self._pool)


agent_pool = AgentPool(max_size=settings.AGENT_POOL_MAX_SIZE)
