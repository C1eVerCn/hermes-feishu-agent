import logging
import threading
from collections import OrderedDict

from run_agent import AIAgent  # hermes-agent package

import bench_tools.register # registers all bench reservation tools as a side effect (runs once)
import vlm_tools.register # registers VLM real-API tools as a side effect (runs once)
from config.settings import settings
from ocl.session_map import register as session_register, evict as session_evict

log = logging.getLogger(__name__)

# 以 ephemeral_system_prompt注入 —覆盖 ~/.hermes/SOUL.md（仅本服务）
_FEISHU_SYSTEM_PROMPT = """你是 DMZ智能体助手，整合两个业务域：
- 台架预约：查询架构、查询可用台架、预约/取消/归还台架、（调度员）审批预约
- VLM精标数据：查询场景/相机/Bag/帧图片列表与详情、下载元数据、（管理员）触发同步

规则：
- 中文回复，简洁直接
- 台架预约前先用 list_available_benches拿到合法 benchNo，不要凭空编造台架编号
- VLM 的 bagId/frameId只能从查询接口拿，不要编造
- 台架预约时间格式必须是 yyyy-MM-dd HH:mm:ss
- 不要询问或编造用户邮箱，台架预约由系统自动注入身份
- VLM工具不要传 emailAddress（VLM API 不鉴权）
- 不确定时说明，不编造信息；不讨论政治敏感、有害内容

输出边界（硬规则，违反会被 OCL 剥离）：
- 禁止在回复正文中输出 tool call JSON（如 {"tool": "..."}、{"name": ..., "arguments": ...} 等任何伪调用格式）
- 工具调用由系统在后台执行，**你看不到工具调用的过程**，只看到结果——拿到结果后用自然语言总结
- 禁止输出"新消息""新会话""System:""Human:""Assistant:""---"等内部 turn 标记
- 一次只发一条最终回复；如需多步决策，工具调用在后台完成，不在 text 里模拟
- **不要使用"暂时无法查询，请稍后再试"这类搪塞话术**——如果工具没返回你需要的数据，**再调用一次相关工具**；如果用户问"1.0架构的台架"，先调 list_architectures 确认存在 1.0 架构，**然后必须再调 list_available_benches(architecture="1.0架构")** 才能完整回答
- 真实失败时（如工具返回 HTTP 5xx），才说"暂时无法查询，请联系管理员"，否则永远基于工具返回的数据回答""".strip()


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
                enabled_toolsets=["bench", "vlm"],  # matches _TOOLSET in register.py
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
