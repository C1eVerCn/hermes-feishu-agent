import logging
import os
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

from run_agent import AIAgent  # hermes-agent package

import car_tools.register  # registers all car reservation tools as a side effect (runs once)
from config.settings import settings
from ocl.session_map import register as session_register, evict as session_evict

log = logging.getLogger(__name__)

# DMZ 自进化记忆落盘根目录。hermes 只从 plugins/memory 或 $HERMES_HOME/plugins
# 发现 provider，所以我们不走它的插件发现，而是构造 AIAgent 后直接挂载本 provider
# （见 _wire_dmz_memory）。存储放在挂载进容器的项目 data/ 卷里，跨重启/重建持久化。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DMZ_MEMORY_HOME = os.getenv("DMZ_MEMORY_HOME") or str(_PROJECT_ROOT / "data")


def _wire_dmz_memory(agent, session_id: str, user_id: str) -> None:
    """Attach the DMZ cross-session MemoryProvider to a freshly-built agent.

    hermes wires memory providers from config (`memory.provider`) by scanning
    `plugins/memory/` / `$HERMES_HOME/plugins/`. Our provider lives in-repo at
    `bot/dmz_memory.py`, so instead of shipping a plugin shim into HERMES_HOME
    (ephemeral, out of version control) we replicate hermes's wiring directly:
    create a MemoryManager, register the provider, initialize it for this user.

    The conversation loop then uses `agent._memory_manager` for prefetch (recall
    before each turn, conversation_loop.py) and sync (write after each turn,
    run_agent.py). Best-effort: any failure here must NOT break agent creation.
    """
    if getattr(agent, "_memory_manager", None) is not None:
        return
    if not user_id:
        return  # anonymous → DMZ provider no-ops on write anyway
    try:
        from agent.memory_manager import MemoryManager
        from bot.dmz_memory import DMZMemoryProvider
        mm = MemoryManager()
        provider = DMZMemoryProvider()
        mm.add_provider(provider)
        provider.initialize(session_id=session_id, user_id=user_id,
                            hermes_home=_DMZ_MEMORY_HOME)
        agent._memory_manager = mm
        log.info("dmz_memory wired user=%s home=%s", user_id[:8], _DMZ_MEMORY_HOME)
    except Exception:
        log.exception("dmz_memory wiring failed (non-fatal) user=%s", user_id[:8])


def _warmup_agent(agent) -> None:
    """Background thread target. Forces hermes-agent's lazy init — OpenAI SDK
    import + first client instantiation — without making a real LLM call.

    Best-effort: failures are logged and swallowed.
    """
    try:
        warmup_kwargs = {
            "api_key": getattr(agent, "api_key", None),
            "base_url": getattr(agent, "base_url", None),
        }
        agent._create_openai_client(
            warmup_kwargs, reason="container_warmup", shared=True,
        )
    except Exception:
        log.exception("agent_warmup_failed")

    # Phase 3 self-evolution: piggyback the read-only Curator review on the
    # background warmup thread (off the request hot path).
    try:
        from bot.curator_runner import maybe_run_dmz_curator
        result = maybe_run_dmz_curator()
        if result:
            log.info("dmz_curator ran suggestions=%d file=%s",
                     result.get("suggestions_count", 0), result.get("output_file", ""))
    except Exception:
        log.exception("dmz_curator invocation failed (non-fatal)")


def _now_cn() -> str:
    """Return current CN-time as 'YYYY-MM-DD HH:MM:SS (周X)'. Refreshed per call
    so the prompt never goes stale. 容器 TZ=Asia/Shanghai → datetime.now() 已是北京时间。"""
    now = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return now.strftime("%Y-%m-%d %H:%M:%S") + f" (周{weekdays[now.weekday()]})"


# 以 ephemeral_system_prompt 注入 —— 覆盖 ~/.hermes/SOUL.md（仅本服务）
# The base prompt is plain text (no .format() applied). We prepend the
# current-time line at construction time to avoid str.format() interpreting
# the literal `{`/`}` in JSON examples (e.g. `{"tool": "..."}`) as
# placeholders.
_FEISHU_SYSTEM_PROMPT_BASE = """当前时间：__NOW_CN__
（基于上述时间换算 "今天"/"明天"/"后天" 等相对日期，如今天是 2026-06-16，则"明天"=2026-06-17，"后天"=2026-06-18；周X 表示星期X）

你是约车助手，专注于车辆预约管理：
- 查询可用车辆
- 预约 / 取消 / 归还车辆
- 查询我的预约、查询待审批（调度员/管理员）
- 审批预约（调度员/管理员）

**字段与枚举**：
- 芯片平台：Xavier / ADCU / Orin / Thor
- 车辆类型：DM2 / CT1 / 大F车 / CM0 / BM2 / ...（可用 get_common_dictionary 查询）
- 时间格式：yyyy-MM-dd HH:mm
- 任务 / 地点：自由文本

**信任用户报出的车辆编号**：
- 用户说"预约 PNV332"时直接调 _dry_run_vehicle_reservation(vehicleNo="PNV332", ...)，不先 list
- 车辆编号格式：字母+数字（如 PNV332 / SVV027 / SOV646）

**工具规则**：
- emailAddress / openId / mobile 由系统注入，不要询问
- 不要编造车辆编号、平台、调度员邮箱
- 单轮最多 2 次工具调用
- 复杂多槽位输入（缺少字段）→ 直接调 _dry_run_vehicle_reservation 让系统生成"缺字段"卡片

**预约两步流程（强制）**：
- 用户表达"预约 XX"时调 _dry_run_vehicle_reservation 拿确认卡
- 用户点 [确认] 后系统自动调 _commit_vehicle_reservation 真正下单
- 你看不到 _commit_vehicle_reservation 的工具描述，但 dry_run 已替你完成预约

**输出边界**：
- 不要输出 tool call JSON
- 不要在回复中列举具体值（卡片会呈现）
- 一次只发一条最终回复

回复风格：简洁直接，单条 ≤200 字。""".strip()


class AgentPool:
    """Thread-safe LRU pool of AIAgent instances, one per user_id."""

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
                enabled_toolsets=["car"],  # 单一业务域：车辆预约
                session_id=session_id,
                user_id=user_id,
            )
            _wire_dmz_memory(agent, session_id, user_id)
            self._pool[user_id] = agent
            self._pool.move_to_end(user_id)
            session_register(session_id, user_id)

            # Cold-start warmup: spawn a background daemon thread.
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
