"""bot/handler — 飞书消息路由总枢纽（two-tier routing）。

2026-06-25 重构：意图识别收口到 :mod:`bot.intent`（单一事实源），文案/身份/管理命令
拆到 :mod:`bot.replies`，查询快速路径拆到 :mod:`bot.fast_path`。本文件只做**编排**。

路由分层（从快到慢、从确定到自由）：

- **Tier 1（确定性，零歧义，瞬回）**
  - Layer 0    问候/帮助/能力介绍（replies.match_simple_intent）
  - 身份闸     解析 email/role，注入 CallerIdentity
  - FSM 续答   用户在挂起状态：escape / 查询逃逸 / 继续推进
  - 快速路径   精确查询短语 → 直接调工具（fast_path）
  - 约车意图   intent.is_booking_intent → FSM
  - 身份/管理  我的权限 / 设置角色 / 查看用户

- **Tier 2（LLM 结构化分类，理解措辞多样/表达有问题的消息）**
  - intent_router.classify → book(带槽位) / query_* / identity / chitchat / unknown
  - book → fsm.start_booking 用槽位播种、跳到第一个缺口
  - query_* → fast_path 执行
  - unknown/低置信/cancel/return/approve → 完整 agent 自由推理（OCL 守卫）

- **Agent 路径（兜底，最大自由度）** AIAgent.chat → OCL pipeline → 卡片
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
from bot import car_state
from bot import intent
from bot import intent_router
from bot import replies
from bot import fast_path
from bot import car_booking_fsm
from bot import return_fsm
from infra.metrics import metrics
from ocl.pipeline import apply as ocl_apply
from ocl.tool_guard import set_current_caller, CallerIdentity
from ocl import identity
from bot.identity_admin import get_admin as get_identity_admin
from ocl import tool_capture
from ocl import intent_filter

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="agent-worker")

_EMPTY_REPLY = "您好，请输入文字消息，我来为您解答。"
_TIMEOUT_REPLY = "抱歉，响应超时（>120s），请稍后重试。"
_ERROR_REPLY = "抱歉，处理您的消息时出现了错误，请稍后再试。"
_INPUT_TOO_LONG_REPLY = "抱歉，消息过长（超过 8000 字），请分段发送。"
_STRANGER_REPLY = "无法识别您的身份（未获取到 open_id），请在飞书私聊中直接发消息后重试。"
_MAX_INPUT_CHARS = 8000


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

    # ── FSM 续答：用户在挂起状态时 ───────────────────────────────────────
    if car_state.get(user_id) is not None:
        if intent.is_escape(text):
            car_state.clear(user_id)
            sender.send_text_as_card(chat_id, "已取消本次操作。")
            return
        # 智能逃逸：挂起态下输入查询类语句 → 清状态走后续路由
        if intent.match_query_intent_during_fsm(text):
            car_state.clear(user_id)
            fast = fast_path.try_fast_path(text, user_id, role)
            if fast is not None:
                _send_result(chat_id, fast)
                return
            # fast-path 未精确命中（如"看一下我的预约吧"含尾缀）→ 不发占位消息
            # （会与后续 Tier-2/agent 回复重复），直接 fall through 让后面路由处理。

    # ── Tier 1: 快速路径（精确查询短语）───────────────────────────────────
    fast = fast_path.try_fast_path(text, user_id, role)
    if fast is not None:
        _send_result(chat_id, fast)
        return

    # ── Tier 1: FSM 入口（挂起中 OR 明确约车意图 OR 还车意图）─────────────
    pending_state = car_state.get(user_id)
    # 还车 FSM 续答：挂起且 intent=return 且处于 RET_* 状态
    if (pending_state and pending_state.intent == "return"
            and return_fsm.is_return_state(pending_state.state)):
        _new_state, response = return_fsm.advance(user_id, text)
        _send_fsm_response(chat_id, response)
        return
    in_fsm = bool(pending_state and pending_state.state not in ("", "START"))
    if in_fsm or intent.is_booking_intent(text):
        new_state, response = car_booking_fsm.advance(user_id, text)
        _send_fsm_response(chat_id, response)
        return
    # 还车意图（Tier-1 确定性，Minimax 不可用时也能进还车流程）
    if intent.is_return_intent(text):
        vno = intent.extract_embedded_vehicle_id(text)
        _new_state, response = return_fsm.start(user_id, {"vehicle_no": vno} if vno else {})
        _send_fsm_response(chat_id, response)
        return

    # ── Tier 1: 身份查询 / 管理员命令 ────────────────────────────────────
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

    # ── Tier 2: LLM 意图路由器（理解措辞多样/表达有问题的消息）─────────────
    if _route_with_llm(chat_id, user_id, role, text):
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


def _route_with_llm(chat_id: str, user_id: str, role: int, text: str) -> bool:
    """Tier-2：LLM 结构化分类 + 分发。返回 True 表示已处理（调用方应 return）。

    fail-open：分类器异常返回 unknown；低置信 / cancel/return/approve / unknown
    一律落到 agent（返回 False）。
    """
    try:
        decision = intent_router.classify(text)
    except Exception:
        log.warning("intent_router raised — falling through to agent", exc_info=True)
        return False

    if not decision.is_confident:
        return False  # → agent

    if decision.intent == "book":
        _new_state, response = car_booking_fsm.start_booking(user_id, decision.slots)
        _send_fsm_response(chat_id, response)
        return True

    _QUERY_TOOL = {
        "query_vehicles": "fetch_available_vehicles",
        "query_reservations": "fetch_user_reservation",
        "query_approvals": "fetch_user_approval",
    }
    tool = _QUERY_TOOL.get(decision.intent)
    if tool:
        result = fast_path.run_tool(tool, user_id, role)
        if result is not None:
            _send_result(chat_id, result)
            return True
        return False  # 权限不足/工具缺失 → agent 兜底

    if decision.intent == "identity":
        sender.send_text_as_card(chat_id, replies.identity_reply(user_id))
        return True

    if decision.intent == "chitchat":
        sender.send_text_as_card(chat_id, intent_filter.REDIRECT_MESSAGE)
        return True

    if decision.intent == "cancel":
        # 取消是危险操作 → 二次确认卡（确认后由 card_action_handler 执行）
        args = fast_path.build_mutation_args("cancel", decision.slots)
        if args is None:
            return False  # 缺车辆编号 → agent 追问
        from ocl import permission
        if not permission.role_allows(role, "cancel_vehicle_reservation"):
            return False
        from car_tools import card_builder as _cb
        sender.send_card(chat_id, _cb.build_cancel_confirm_card(decision.slots.get("vehicle_no", "")))
        return True

    if decision.intent == "approve":
        result = fast_path.run_mutation("approve", decision.slots, user_id, role)
        if result is not None:
            _send_result(chat_id, result)
            return True
        return False  # 缺识别符/权限不足 → agent

    if decision.intent == "return":
        # 还车 → 进还车表单 FSM（一步步收集 5 个必填字段 + 二次确认）。
        _new_state, response = return_fsm.start(user_id, decision.slots)
        _send_fsm_response(chat_id, response)
        return True

    # unknown → agent（自由推理）
    return False


def _run_agent(chat_id: str, user_id: str, role: int, name: str,
               text: str, message_id: str) -> None:
    """完整 agent 路径：AIAgent.chat → OCL pipeline → 卡片 + 后处理。"""
    session_id = f"feishu_{user_id}"
    start = time.monotonic()
    captured: list[dict] = []
    try:
        agent = agent_pool.get_or_create(user_id)
        ctx = contextvars.copy_context()  # contextvars 不跨线程自动传播
        tool_capture.clear(session_id)
        agent_input = replies.identity_preamble(user_id, role, name) + text
        future = _executor.submit(ctx.run, agent.chat, agent_input)
        response: str = future.result(timeout=settings.AGENT_TIMEOUT_SECONDS)
        captured = tool_capture.read(session_id)
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
        tool_capture.clear(session_id)

    ocl_result = ocl_apply(response or "", user_id, captured=captured)
    if ocl_result.blocked:
        metrics.inc("errors_ocl_blocked")
    if ocl_result.card is not None and not ocl_result.blocked:
        try:
            sender.send_card(chat_id, ocl_result.card)
        except Exception:
            log.exception("send_card failed, falling back to text-as-card")
            sender.send_text_as_card(chat_id, ocl_result.text or _ERROR_REPLY)
    else:
        sender.send_text_as_card(chat_id, ocl_result.text or _ERROR_REPLY)

    # 状态机持久化：_dry_run 完成后写入 car_state（LLM agent 路径的 booking）
    _persist_dry_run_state(user_id, captured)
    # 审批完成通知申请人
    _notify_applicants_from_captured(captured)


def _persist_dry_run_state(user_id: str, captured: list) -> None:
    """扫描 captured，最后一次成功 _dry_run_vehicle_reservation → 存 car_state。"""
    for entry in reversed(captured):
        if entry.get("tool") != "_dry_run_vehicle_reservation":
            continue
        res = entry.get("result") or {}
        if isinstance(res, dict) and res.get("dry_run") and not res.get("missing_fields"):
            args = res.get("args") or {}
            car_state.save(
                user_id, intent="booking",
                vehicle_no=args.get("vehicle_no", ""),
                vehicle_type=args.get("vehicle_type", ""),
                platform=args.get("platform", ""),
                license_plate=args.get("license_plate", ""),
                start_time=args.get("start_time", ""),
                end_time=args.get("end_time", ""),
                task_name=args.get("task_name", ""),
                location=args.get("location", ""),
                remark=args.get("remark", ""),
                vin=args.get("vin", ""),
            )
            break


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
def _send_result(chat_id: str, result: dict) -> None:
    """fast_path 返回的 {"blocked"|"card"|"text"} → 发送。"""
    if result.get("blocked"):
        sender.send_text_as_card(chat_id, result["text"])
    elif result.get("card"):
        sender.send_card(chat_id, result["card"])
    else:
        sender.send_text_as_card(chat_id, result.get("text") or _ERROR_REPLY)


def _send_fsm_response(chat_id: str, response: dict) -> None:
    """FSM / start_booking 返回的 response → 发送（card 优先，text+buttons 拼卡）。"""
    if response.get("card"):
        sender.send_card(chat_id, response["card"])
    elif response.get("text") or response.get("buttons"):
        from bot.card_action_handler import _render_fsm_response
        _toast, _card = _render_fsm_response(response)
        if _card is not None:
            sender.send_card(chat_id, _card)
        else:
            sender.send_text_as_card(chat_id, response.get("text", ""))
    else:
        sender.send_text_as_card(chat_id, _ERROR_REPLY)


def _extract_text(msg) -> str:
    if msg.message_type != "text":
        return ""
    try:
        content = json.loads(msg.content)
        return content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return ""
