"""bot/agent_pool — 飞书 bot 的 LLM agent 池 + 系统提示。

每个 user_id 一个 AIAgent 实例（hermes）。所有实例使用同一个系统提示
（identity + tool 列表 + 不变量）。完整业务流程在 bot/skills/car-booking/SKILL.md
里，构造时拼到 system prompt 末尾。

CLAUDE.md 硬上限：max_iterations=10（默认值），timeout=120s。
"""
import logging
import os
import threading
import time
from collections import OrderedDict, deque
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


# 系统提示（2026-06-30 精简 v3）：核心 3 条 + 工具列表 + 不变量。
# 业务流程在 bot/skills/car-booking/SKILL.md 里（构造时拼到末尾）。
# 设计：minimax M2.7-highspeed 对长 system prompt 服从性差，所以核心规则放最顶部。
_FEISHU_SYSTEM_PROMPT_BASE = """你是飞书"约车助手"机器人。所有交互通过飞书文字消息完成——卡片只展示信息，**不点不动**，用户**打字**给你指令。

# 🚨 3 条必须遵守
1. **意图可推断就直接调工具查，别为能自己定的细节追问**（如没指定平台/车型 → 直接 `fetch_available_vehicles({})`）。**仅当**请求发散、或缺少无法默认补全的关键信息（缺车 / 缺时段 / 一句话含多个互斥意图）时，**最多问一个**澄清问题然后停下等用户回复；拿到答复立即执行，**不得连问第二次**。
2. **绝不编造**——所有数字、平台、状态必须从工具返回的 `data` 数组读。**绝对不要**假装"dry-run 通过"或"约车成功"——这些必须来自工具的真实返回。
3. **不查不要回答**——不知道就调工具查。

# 🚨 你的知识盲区
你是约车助手，**不知道任何具体车辆信息**（没有车列表、没有车辆编号、没有车辆平台）。任何关于"有什么车""PNV332 是否可用"的问题**必须**调工具。**禁止**编造"奔驰/宝马"等无关品牌——本系统是"约车"业务，不是汽车评测。

# 🚨 工具用途区分（重要）
每个工具用途单一，**不能互相替代**：
- `fetch_available_vehicles` — **仅**查可用车列表
- `_dry_run_vehicle_reservation` — **仅**做预约 dry-run（返回 summary+args）
- `_commit_vehicle_reservation` — **仅**真实下单（受 session 守卫保护）
- `cancel_vehicle_reservation` / `return_vehicle` / `fetch_user_reservation` / `fetch_user_approval` 等各司其职

**绝对禁止**：用 `fetch_available_vehicles` 的结果**假装**是 `dry_run` 或 `commit` 的结果——这是完全不同的操作。约车要提交时，**必须**调 `_dry_run_vehicle_reservation` 让系统校验所有字段 + 拼装 summary，**然后**才能 commit。

# 🚨 重要：处理"现在有什么车"类查询
**唯一**允许的回答路径：先调 `fetch_available_vehicles({})`（不传任何参数）→ 拿到结果 → 按 `芯片` 字段精确分组 → 用数字回答。
**禁止**不调工具就回答"没有车""都被预约了"等——你不知道就说不允许回答。

# 能力
- 查询可用车辆
- 预约 / 取消 / 归还车辆
- 查询我的预约、查询待审批（调度员/管理员）
- 审批预约（调度员/管理员）

# 工具清单
- `fetch_available_vehicles` — 查可用车（**用户没指定平台/车型时不传 platform/vehicleType，直接 `{}`**）
- `_dry_run_vehicle_reservation` — 预约 dry-run（两步第一步）
- `_commit_vehicle_reservation` — 真正下单（两步第二步，受 session 守卫保护）
- `cancel_vehicle_reservation` / `return_vehicle` / `fetch_user_reservation` / `fetch_user_approval` / `approval_vehicle_reservation` / `get_user_context` / `get_common_dictionary`

# 不变量
- `emailAddress` / `openId` / `mobile` 由系统注入，**永远不要**作为工具参数
- 单轮最多 2 次工具调用
- 一次只发一条最终回复
- 卡片由系统渲染，文本里不要写 markdown 表格/列表
- 单条 ≤200 字

# 完整操作流程在下方 skill
"""


# Skill 加载：启动时读一次，缓存在模块级。AIAgent 每次构造时引用这份内容。
def _load_car_booking_skill() -> str:
    """从 bot/skills/car-booking/SKILL.md 读取完整 markdown。

    失败时返回空字符串（fail-open，agent 仍能工作但少了流程参考）。
    """
    from bot.skills import load_skill
    content = load_skill("car-booking")
    return content or ""


_CAR_BOOKING_SKILL = _load_car_booking_skill()

# ── per-user 多轮对话历史（内存 deque，方案 B）─────────────────────────────
_HISTORY_TURNS = 6                       # 最近 N 轮（user+assistant 成对）
_HISTORY_MAXLEN = _HISTORY_TURNS * 2     # deque 条目上限 = 12
_HISTORY_TTL_SECONDS = 1800              # 30 分钟空闲 TTL
_HISTORY_CONTENT_CAP = 800               # 单条 content 截断上限


def _cap_content(msg: dict) -> dict:
    """只保留 role/content 并截断 content（防病态长消息撑爆上下文）。"""
    raw = msg.get("content")
    content = "" if raw is None else str(raw)
    if len(content) > _HISTORY_CONTENT_CAP:
        content = content[:_HISTORY_CONTENT_CAP]
    return {"role": msg.get("role", "user"), "content": content}


class AgentPool:
    """Thread-safe LRU pool of AIAgent instances, one per user_id."""

    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._pool: OrderedDict[str, AIAgent] = OrderedDict()
        self._lock = threading.Lock()
        self._history: OrderedDict[str, deque] = OrderedDict()  # user_id -> deque(maxlen=12)
        self._history_touch: dict[str, float] = {}              # user_id -> monotonic last-use

    def get_or_create(self, user_id: str) -> AIAgent:
        with self._lock:
            if user_id in self._pool:
                self._pool.move_to_end(user_id)
                return self._pool[user_id]

            # Stable session_id — same across turns until evicted, enables
            # the feishu_acl plugin (Layer 1) to resolve user identity via
            # the pre_tool_call hook and session_map.
            session_id = f"feishu_{user_id}"

            # 2026-06-30：把 skill 追加到 system prompt（AIAgent 构造时一次性
            # 注入）。hermes KV 缓存命中率高，后续轮不重复拼接。
            system_prompt = _FEISHU_SYSTEM_PROMPT_BASE
            if _CAR_BOOKING_SKILL:
                system_prompt += "\n\n# 操作手册（car-booking skill）\n\n" + _CAR_BOOKING_SKILL

            agent = AIAgent(
                model=settings.MINIMAX_MODEL,
                provider="minimax",
                base_url=settings.MINIMAX_BASE_URL,
                api_key=settings.MINIMAX_API_KEY,
                api_mode=settings.MINIMAX_API_MODE,  # M3 必须 anthropic_messages
                quiet_mode=True,
                max_iterations=settings.AGENT_MAX_ITERATIONS,
                ephemeral_system_prompt=system_prompt,
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
                self._history.pop(evicted_id, None)
                self._history_touch.pop(evicted_id, None)
                evicted_sid = getattr(evicted_agent, "session_id", None)
                if evicted_sid:
                    session_evict(evicted_sid)
                    # 2026-06-30: 同步清掉 handler 的 skill-injection 标记
                    # （避免下次该 user_id 复用新 agent 时不再注入 skill）
                    try:
                        from bot import handler as _h
                        _h._SKILL_INJECTED_SESSIONS.discard(evicted_sid)
                    except Exception:
                        pass
                log.debug("Evicted agent for user_id=%s session_id=%s",
                          evicted_id, evicted_sid)

            return agent

    def size(self) -> int:
        with self._lock:
            return len(self._pool)

    def get_history(self, user_id: str) -> list[dict]:
        """返回最近 N 轮 user/assistant dict；空闲超 TTL 则清空返 []。"""
        if not user_id:
            return []
        with self._lock:
            dq = self._history.get(user_id)
            if not dq:
                return []
            now = time.monotonic()
            last = self._history_touch.get(user_id, 0.0)
            if now - last > _HISTORY_TTL_SECONDS:
                self._history.pop(user_id, None)
                self._history_touch.pop(user_id, None)
                return []
            self._history_touch[user_id] = now
            return list(dq)

    def append_turn(self, user_id: str, user_msg: dict, assistant_msg: dict) -> None:
        """追加一轮（user+assistant 纯文本，跳过 tool 轮）。user_id 为空则忽略。"""
        if not user_id:
            return
        u, a = _cap_content(user_msg), _cap_content(assistant_msg)
        with self._lock:
            dq = self._history.get(user_id)
            if dq is None:
                dq = deque(maxlen=_HISTORY_MAXLEN)
                self._history[user_id] = dq
            dq.append(u)
            dq.append(a)
            self._history_touch[user_id] = time.monotonic()

    def clear_history(self, user_id: str) -> None:
        with self._lock:
            self._history.pop(user_id, None)
            self._history_touch.pop(user_id, None)


agent_pool = AgentPool(max_size=settings.AGENT_POOL_MAX_SIZE)
