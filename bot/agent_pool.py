import logging
import threading
from collections import OrderedDict

from run_agent import AIAgent  # hermes-agent package

import mock_tools.register  # registers all mock_api tools as a side effect (runs once)
from config.settings import settings
from ocl.session_map import register as session_register, evict as session_evict

log = logging.getLogger(__name__)

# Injected as ephemeral_system_prompt — overrides ~/.hermes/SOUL.md for this service only
_FEISHU_SYSTEM_PROMPT = """你是一个台架预约助手，帮助用户查询架构、查询可用台架、预约/取消/归还台架，
以及（调度员）审批预约。

规则：
- 中文回复，简洁直接
- 预约前先用 list_available_benches 拿到合法 benchNo，不要凭空编造台架编号
- 时间格式必须是 yyyy-MM-dd HH:mm:ss
- 不要询问或编造用户邮箱，系统会自动识别当前用户身份
- 不确定时说明，不编造信息；不讨论政治敏感、有害内容""".strip()


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
                ephemeral_system_prompt=_FEISHU_SYSTEM_PROMPT,
                enabled_toolsets=["testbench"],  # test-bench reservation tools
                session_id=session_id,           # Phase 3.5: plugin ACL
            )
            self._pool[user_id] = agent
            self._pool.move_to_end(user_id)
            session_register(session_id, user_id)

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
