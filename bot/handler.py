"""bot/handler — 飞书消息路由总枢纽（2026-06-30 改造：单路径全对话流）。

历史：之前是 two-tier 路由（Tier-1 确定性 + Tier-2 LLM 分类 + Agent 兜底），
且 book/query/return/cancel 等意图会被喂给 FSM。本次改造：彻底拆除 FSM / 卡片交互，
所有消息（含查询）→ LLM agent 自由推理 + 调工具 + 展示结果。卡片只做展示不交互。

路由（单路径）：
    event → 提取 user_id, chat_id, text
    → 输入校验
    → replies.match_simple_intent（问候/帮助/能力介绍）→ 命中即返
    → _resolve_identity 解析 email/role/name/mobile
    → set_current_caller(CallerIdentity) + set_current_session(session_id)
    → role==0 → 陌生人友好提示，return
    → replies.handle_identity_query（精确正则）→ 命中即返
    → replies.handle_admin_command（精确正则）→ 命中即返
    → _run_agent（统一兜底，承接所有业务请求）
    finally: 清 contextvars
"""
import contextvars
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config.settings import settings
from feishu.ws_client import event_queue
from feishu import sender
from feishu import notify
from bot.agent_pool import agent_pool
from bot import replies
from infra.metrics import metrics
from ocl.pipeline import apply as ocl_apply
from ocl.tool_guard import (
    set_current_caller, set_current_session, CallerIdentity,
)
from bot.identity_admin import get_admin as get_identity_admin
from ocl import identity
from ocl import tool_capture

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="agent-worker")

_EMPTY_REPLY = "您好，请输入文字消息，我来为您解答。"
_TIMEOUT_REPLY = "抱歉，响应超时（>120s），请稍后重试。"
_ERROR_REPLY = "抱歉，处理您的消息时出现了错误，请稍后再试。"
_INPUT_TOO_LONG_REPLY = "抱歉，消息过长（超过 8000 字），请分段发送。"
_STRANGER_REPLY = "无法识别您的身份（未获取到 open_id），请在飞书私聊中直接发消息后重试。"
_MAX_INPUT_CHARS = 8000

# Skill 注入：每个 session 首次调用时把 car-booking SKILL.md 拼到 user message 前
# 后续轮不再注入（agent.history 已记住）。SM marker 是不可见控制 token
# 防 LLM 把 marker 当成内容回复。失败时 fail-open（返空字符串，agent 仍工作）。
_SKILL_INJECTED_SESSIONS: set[str] = set()
_SKILL_INJECTION_MARKER = "[skill:car-booking]"


def _maybe_inject_skill(session_id: str) -> str:
    """每个 session 首次调用时返回 skill 文本（带 marker）。后续返空。

    失败 / skill 文件缺失 / 加载异常 → 返空（fail-open）。
    """
    if session_id in _SKILL_INJECTED_SESSIONS:
        return ""
    from bot.skills import load_skill
    content = load_skill("car-booking")
    if not content:
        return ""
    _SKILL_INJECTED_SESSIONS.add(session_id)
    return (f"{_SKILL_INJECTION_MARKER}\n"
            "以下是你此次会话的完整操作手册（已读，对话中按此行事）：\n\n"
            f"{content}\n\n"
            "— — — — — — — — — — — — — — — — — — — — — — — — —\n"
            "以下是用户本轮输入：\n")


# ── 入口 ─────────────────────────────────────────────────────────────────
def start_consumer() -> None:
    log.info("Event consumer started")
    while True:
        data: P2ImMessageReceiveV1 = event_queue.get()
        try:
            _handle(data)
        except Exception:
            log.exception("Unhandled error in consumer for message_id=%s",
                          data.event.message.message_id)
        finally:
            event_queue.task_done()


def _handle(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    user_id = data.event.sender.sender_id.open_id
    chat_id = msg.chat_id

    if user_id:
        notify.remember_open_id(user_id, email="")

    log.info("received message_id=%s chat_id=%s user_id=%s", msg.message_id, chat_id, user_id)

    text = _extract_text(msg)
    if not text:
        sender.send_text_as_card(chat_id, _EMPTY_REPLY)
        return
    if len(text) > _MAX_INPUT_CHARS:
        sender.send_text_as_card(chat_id, _INPUT_TOO_LONG_REPLY)
        return

    # ── Layer 0: 即时文案（先于身份闸，避免问候被 18s 飞书 API 阻塞）──────────
    instant = replies.match_simple_intent(text)
    if instant:
        sender.send_text_as_card(chat_id, instant)
        return

    # ── 身份闸：解析 email/role/mobile + 注入 CallerIdentity ───────────────
    role, email, name, mobile = _resolve_identity(user_id)
    if user_id and email:
        # 用本 app 的 open_id 播种 email↔open_id（DM 解析优先用它，避免 identity_map
        # 里残留的跨 app open_id 被选中导致 99992361 open_id cross app 发送失败）。
        notify.remember_open_id(user_id, email=email)
    set_current_caller(CallerIdentity(openid=user_id, email=email, mobile=mobile or None))
    # 2026-06-30 Phase 0.4：注入 session_id 给 commit 守卫使用
    set_current_session(f"feishu_{user_id}" if user_id else "")

    # ── 2026-06-30 fast_path：固定查询绕过 LLM 直接调工具 ─────────────────
    # minimax M2.7-highspeed 经常"猜答"而非调工具，对"查可用车/我的预约/待审批"
    # 这类**纯查询**直接调工具，绕过 LLM 路径（省 5-13s + 数据 100% 准确）。
    fast = _try_fast_query(text, user_id, role)
    if fast:
        log.info("fast_path_handled text=%r", text[:50])
        sender.send_text_as_card(chat_id, fast)
        return

    # ── 身份/管理员命令（精确正则）────────────────────────────────────
    identity_response = replies.handle_identity_query(text, user_id)
    if identity_response:
        sender.send_text_as_card(chat_id, identity_response)
        return
    admin_response = replies.handle_admin_command(text, user_id)
    if admin_response:
        sender.send_text_as_card(chat_id, admin_response)
        return

    if role == 0:
        sender.send_text_as_card(chat_id, _STRANGER_REPLY)
        return

    # ── Agent 路径（兜底，最大自由度）────────────────────────────────────
    _run_agent(chat_id, user_id, role, name, text, msg.message_id)


def _resolve_identity(user_id: str) -> tuple[int, str, str, str]:
    """auto-register + 角色解析 + email/name/mobile 回填。返回 (role, email, name, mobile)。

    角色来源：**identity_map.json 是唯一事实源**（管理员用「设置角色」维护）。
    背景（2026-06-26）：后端 fmp 0617 分支把 RBAC 重构成多角色（sys_user_role + RoleHelper），
    ``get_user_context`` 返回的 Employee **不再带 role 字段**——故无法再从后端同步角色（且按
    用户要求「不动 MCP」，后端也没有暴露角色的开放接口）。因此回归主设计：identity_map 粗粒度
    门控、后端按 emailAddress 细粒度校验，两道闸独立。可见范围内未建档用户默认工程师(role=1)。
    最后叠加 OCL_ADMIN_USER_IDS 白名单（bot 级管理员命令兜底）。手机号是第二识别符（邮箱为主、
    上游仍按 emailAddress 鉴权）。
    """
    admin = get_identity_admin()
    email = identity.email_of(user_id)
    name = identity.name_of(user_id)
    mobile = identity.mobile_of(user_id)
    if user_id:
        admin.auto_register(user_id, email=email, name=name, mobile=mobile)
        existing = admin.get(user_id) or {}
        if (email and existing.get("email") != email) or \
           (mobile and existing.get("mobile") != mobile):
            admin.update_profile(
                user_id,
                email=email or existing.get("email", ""),
                name=name or existing.get("name", ""),
                mobile=mobile or existing.get("mobile", ""),
                operator="auto_email_sync",
            )
        # 可见范围内未建档用户（本地 role=0）默认工程师(role=1)；调度员/管理员等由「设置角色」显式指派。
        if admin.get_role(user_id) == 0:
            admin.set_role(user_id, 1, operator="auto_in_scope",
                           note=f"in-visible-scope default; email={'yes' if email else 'none'}")
    role = admin.get_role(user_id)
    role = replies.resolve_role_with_env_admin(admin, user_id, role)
    rec = admin.get(user_id)
    if rec:
        email = rec.get("email", "") or email
        name = rec.get("name", "") or name
        mobile = rec.get("mobile", "") or mobile
    return role, email, name, mobile


def _now_preamble() -> str:
    """每轮注入当前时间（取代常驻 system prompt 里写死的旧时间，避免池化 agent 时间漂移）。"""
    from bot.agent_pool import _now_cn
    return (f"当前时间：{_now_cn()}\n"
            "（相对日期换算：今天=N，今天+1=明天，今天+2=后天；周X=星期X）\n")


def _run_agent(chat_id: str, user_id: str, role: int, name: str,
               text: str, message_id: str) -> None:
    """完整 agent 路径：取多轮历史 → AIAgent.run_conversation → OCL pipeline → 卡片 + 后处理。

    2026-06-30 多轮：经 agent_pool.get_history(user_id) 取最近 N 轮喂进
    run_conversation(conversation_history=...)，成功后 append_turn 写回；时间由
    _now_preamble 每轮注入（不再写死在常驻 system prompt 里）。
    """
    session_id = f"feishu_{user_id}"
    start = time.monotonic()
    captured: list[dict] = []
    response = ""
    # 2026-06-30 流式输出：先发一个占位卡片 → agent.stream_delta_callback 每收到
    # 一段文字就 PATCH 一次占位 → 用户看到"打字机"效果，感知延迟从 26s → 几秒。
    from feishu.sender import StreamCard
    stream = StreamCard(chat_id)
    try:
        agent = agent_pool.get_or_create(user_id)
        ctx = contextvars.copy_context()  # contextvars 不跨线程自动传播
        tool_capture.clear(session_id)
        # 2026-06-30：skill 已合并到 agent system prompt（构造时一次性注入），
        # 这里不再做 per-call 注入。_maybe_inject_skill 保留为 no-op 兼容。
        agent_input = (_now_preamble()
                       + replies.identity_preamble(user_id, role, name) + text)

        def _on_delta(delta: str) -> None:
            # 工具调用过程中也会触发 delta（空字符串），StreamCard 内部会忽略
            stream.append(delta)

        hist = agent_pool.get_history(user_id)   # 最近 N 轮（不含本轮）

        def _invoke():
            return agent.run_conversation(
                agent_input, conversation_history=hist, stream_callback=_on_delta)

        future = _executor.submit(ctx.run, _invoke)
        result = future.result(timeout=settings.AGENT_TIMEOUT_SECONDS)
        response = result["final_response"] if isinstance(result, dict) else str(result)
        captured = tool_capture.read(session_id)
        # 成功一轮：把原始用户文本 + 助手回复写入多轮历史（跳过 preamble/时间）
        agent_pool.append_turn(
            user_id,
            {"role": "user", "content": text},
            {"role": "assistant", "content": response})
        latency = time.monotonic() - start
        metrics.record("llm_latency_seconds", latency)
        metrics.inc("messages_processed")
        log.info("processed message_id=%s latency=%.2fs", message_id, latency)
    except FuturesTimeout:
        log.error("Agent timeout for user_id=%s message_id=%s", user_id, message_id)
        metrics.inc("errors_timeout")
        response = _TIMEOUT_REPLY
    except Exception:
        log.exception("Agent error for user_id=%s message_id=%s", user_id, message_id)
        metrics.inc("errors_agent")
        response = _ERROR_REPLY
    finally:
        set_current_caller(CallerIdentity())
        set_current_session("")
        tool_capture.clear(session_id)

    ocl_result = ocl_apply(response or "", user_id, captured=captured)
    if ocl_result.blocked:
        metrics.inc("errors_ocl_blocked")
    # 2026-06-30 流式输出：用 PATCH 更新占位卡片（用户看到"打字机"效果），
    # 而不是再发一条新消息。如果 OCL 阶段判定为 blocked（不应再展示），
    # 就用普通发送（不更新占位）。
    if ocl_result.card is not None and not ocl_result.blocked:
        try:
            stream.finalize_with_card(ocl_result.card)
        except AttributeError:
            # 兼容旧 StreamCard（finalize 只接受 text）
            stream.finalize(ocl_result.text or _ERROR_REPLY)
    else:
        stream.finalize(ocl_result.text or _ERROR_REPLY)

    # 审批完成通知申请人（双保险：approval handler 内部也 DM；这里扫 captured 兜底）
    _notify_applicants_from_captured(captured)


# ── fast_path：固定查询绕过 LLM ─────────────────────────────────────────
import re
import json as _json

# 查询可用车（不指定平台/车型）："现在有什么车"、"查可用车"、"车有哪些"
_FAST_QUERY_AVAILABLE_RE = re.compile(
    r"(有什么车|可用车|车能用|车有哪些|车列表|查(一下|看)?车|可约车|在用车)"
)
# 我的预约
_FAST_QUERY_MY_RESV_RE = re.compile(r"(我.{0,2}预约|查我.{0,2}预约|我的预约|我的记录)")
# 待审批（仅 admin/调度员）
_FAST_QUERY_PENDING_RE = re.compile(r"(待审批|我的审批|审批列表|待我审批)")


def _try_fast_query(text: str, user_id: str, role: int) -> str | None:
    """对纯查询意图，绕过 LLM 直接调 car_tools/handlers。
    命中 + 调通 → 返回格式化文本（admin role 时加 meta 提示）。
    不命中或工具失败 → 返 None（让 LLM 路径兜底）。
    """
    from car_tools import handlers

    # 1. 查可用车
    if _FAST_QUERY_AVAILABLE_RE.search(text):
        try:
            raw = handlers.fetch_available_vehicles({})
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            log.warning("fast_path fetch_available_vehicles failed: %s", e)
            return None
        if not isinstance(parsed, list):
            return None
        # 按 platform 字段精确分组（skill 4.1 节；handler normalizer 已把
        # fmp 的"芯片"字段映射成 snake_case 的 platform）
        from collections import Counter
        plats = Counter(v.get("platform", "?") for v in parsed)
        if not plats:
            return ("📋 当前没有可用车辆（返回 0 条）。\n\n"
                    "💡 可换个平台（如「查 Orin 的车」）或具体时段（明天 9-12 点）让我帮你查。")
        lines = [f"📋 **共 {len(parsed)} 辆可用**，按平台分布：\n"]
        for plat in ("Orin", "Thor", "Xavier", "ADCU"):
            n = plats.get(plat, 0)
            if n > 0:
                lines.append(f"  • {plat}: **{n} 辆**")
        # 抽样前 3 个车辆编号
        samples = [v.get("vehicle_no", "?")[-6:] for v in parsed[:3]]
        if samples:
            lines.append(f"\n样例车辆：{', '.join(samples)}")
        lines.append("\n💡 可指定平台（如「只看 Orin」）或时段让我精确筛选。")
        return "\n".join(lines)

    # 2. 我的预约
    if _FAST_QUERY_MY_RESV_RE.search(text):
        try:
            raw = handlers.fetch_user_reservation({})
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            log.warning("fast_path fetch_user_reservation failed: %s", e)
            return None
        if not isinstance(parsed, list):
            return None
        if not parsed:
            return "📋 你当前没有预约记录。"
        lines = [f"📋 **你的预约（共 {len(parsed)} 条）**：\n"]
        for r in parsed[:10]:
            vno = r.get("vehicle_no", "?")[-6:]
            st = r.get("start_time", "?")
            et = r.get("end_time", "?")
            status = r.get("status", "-")
            task = r.get("task_name", "-")
            lines.append(f"  • `{vno}` {st} ~ {et} · {status} · {task}")
        return "\n".join(lines)

    # 3. 待审批（仅调度员/管理员）
    if _FAST_QUERY_PENDING_RE.search(text) and role in (2, 3, 5):
        try:
            raw = handlers.fetch_user_approval({})
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            log.warning("fast_path fetch_user_approval failed: %s", e)
            return None
        if not isinstance(parsed, list):
            return None
        if not parsed:
            return "📋 当前没有待审批的预约。"
        lines = [f"📋 **待审批（共 {len(parsed)} 条）**：\n"]
        for r in parsed[:10]:
            vno = r.get("vehicle_no", "?")[-6:]
            st = r.get("start_time", "?")
            et = r.get("end_time", "?")
            employee = r.get("employee_name", "-")
            lines.append(f"  • `{vno}` {st} ~ {et} · 申请人: {employee}")
        lines.append("\n💡 回复「审批 <车辆编号> 通过/拒绝」处理某条。")
        return "\n".join(lines)

    return None


def _notify_applicants_from_captured(captured: list) -> None:
    """扫描本轮 captured，找 approval_vehicle_reservation 成功结果 → DM 申请人。"""
    from car_tools import notify_applicant as _notify_applicant
    for entry in captured:
        if entry.get("tool") != "approval_vehicle_reservation":
            continue
        res = entry.get("result")
        if not isinstance(res, dict) or "error" in res:
            continue
        result_dict = res.get("data") if isinstance(res.get("data"), dict) else res
        if not isinstance(result_dict, dict) or "approved" not in result_dict:
            continue
        rid = result_dict.get("reservation_id") or result_dict.get("reservationId") or ""
        try:
            _notify_applicant.submit_approval_to_applicant(
                result_dict, reservation_id=rid,
                vehicle_no=result_dict.get("vehicle_no", ""),
                start_time=result_dict.get("start_time", ""),
            )
        except Exception:
            log.exception("notify_applicant_dispatch_failed rid=%s", rid)


# ── 渲染辅助 ──────────────────────────────────────────────────────────────
def _extract_text(msg) -> str:
    if msg.message_type != "text":
        return ""
    try:
        content = json.loads(msg.content)
        return content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return ""
