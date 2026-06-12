import json
import logging
import re
import time
import contextvars
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config.settings import settings
from feishu.ws_client import event_queue
from feishu import sender
from feishu import notify
from bot.agent_pool import agent_pool
from bot import dry_run_state
from infra.metrics import metrics
from ocl.pipeline import apply as ocl_apply
from ocl.tool_guard import set_current_user, set_current_email
from ocl import identity
from bot.identity_admin import get_admin as get_identity_admin
from ocl import tool_capture
from bench_tools import handlers as bench_handlers

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="agent-worker")

# Phrases that trigger the dry-run confirm/cancel handler (matched
# case-insensitively against the user message after stripping whitespace
# and trailing punctuation). Per the 车辆预约 reference flow, the
# user confirms by replying with these exact words.
_CONFIRM_PHRASES = ("确认", "确定", "ok", "yes", "yep", "yeah")
_CANCEL_PHRASES = ("取消", "放弃", "算了")

_EMPTY_REPLY = "您好，请输入文字消息，我来为您解答。"
_TIMEOUT_REPLY = "抱歉，响应超时（>120s），请稍后重试。"
_ERROR_REPLY = "抱歉，处理您的消息时出现了错误，请稍后再试。"
_INPUT_TOO_LONG_REPLY = "抱歉，消息过长（超过 8000 字），请分段发送。"
_NON_PLATFORM_REPLY_TEMPLATE = "您还不是平台用户（您的 open_id: {open_id}），请联系管理员开通。管理员可在飞书发「设置角色 {open_id} 1|2|3」设置您的权限。"
_MAX_INPUT_CHARS = 8000

# ── Layer 0: Instant replies for simple intents (no agent, <1ms) ──────────

_SIMPLE_REPLIES: list[tuple[re.Pattern, str]] = [
    # Greetings
    (re.compile(r'^(你好|hi|hello|hey|嗨|哈[啰咯]|早上好|下午好|晚上好|good\s*morning|good\s*afternoon|good\s*evening)[\s!！。.]*$', re.IGNORECASE),
     "你好！我是 DMZ智能体助手，可以帮你处理台架预约和 VLM精标数据查询。\n输入「帮助」了解我能做什么。"),
    # Gratitude
    (re.compile(r'^(谢谢|感谢|thanks|thank\s*you|3q|多谢|谢了|辛苦)[\s!！。.]*$', re.IGNORECASE),
     "不客气！有需要随时找我。"),
    # Farewell
    (re.compile(r'^(再见|bye|拜拜|88|回见|下次聊)[\s!！。.]*$', re.IGNORECASE),
     "再见！有需要随时找我。"),
    # Status check
    (re.compile(r'^(在吗|在不在|在线吗)[\s!！。.?？]*$'),
     "在的！有什么可以帮您的？"),
    # Identity
    (re.compile(r'^(你是谁|你叫什么|你是做什么的|你能做什么|介绍一下你自己)[\s!！。.?？]*$'),
     "我是台架预约助手，可以帮您：\n• 查询台架架构\n• 查询可用台架\n• 预约 / 取消 / 归还台架\n• 查询我的预约\n• （调度员/管理员）审批预约、查询待审批\n\n输入「我的权限」查看当前角色。"),
    # Help
    (re.compile(r'^(帮助|help|怎么用|怎么操作|使用说明|功能)[\s!！。.?？]*$', re.IGNORECASE),
     "📋 我能帮你做的事情：\n\n🔍 查询类\n• 查询台架架构\n• 查询可用台架\n• 查询我的预约\n\n✏️ 操作类\n• 预约台架（先查可用台架拿到编号）\n• 取消待审批的预约\n• 归还已批准的台架\n\n🛡️ 调度员/管理员\n• 审批预约\n• 查询待审批列表\n\n💡 输入「我的权限」查看当前角色"),
    # Acknowledgments
    (re.compile(r'^(好[的]?|ok|嗯|哦|知道了|明白了|懂了|收到|了解|got\s*it)[\s!！。.]*$', re.IGNORECASE),
     "好的，有问题随时找我。"),
]

# ──身份 /管理员意图正则 ────────────────────────────────

_MY_PERMS = re.compile(r'我的权限|查看.*权限|我的角色')
# Admin command: "设置角色 ou_xxx 2"
_ADMIN_SET_ROLE = re.compile(r'^设置角色\s+(\S+)\s+([123])$')

_ROLE_NAME = {0: "非平台用户", 1: "普通用户", 2: "调度员", 3: "管理员"}


def _admin_ids() -> set[str]:
    raw = getattr(settings, "OCL_ADMIN_USER_IDS", "")
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def _is_admin(user_id: str) -> bool:
    return user_id in _admin_ids() or identity.role_of(user_id) == 3


def _handle_identity_query(text: str, user_id: str) -> str:
    """回复「我的权限」类查询。不暴露 open_id/邮箱/姓名等个人识别信息。

    例外：role=0 用户需把 open_id 告知管理员才能开通——保留显示。
    """
    if _MY_PERMS.search(text):
        admin = get_identity_admin()
        role = admin.get_role(user_id)
        if role == 0:
            # 待审核用户需自报 open_id 给管理员 → 例外保留
            return (f"您当前是【待审核】用户，无平台权限。\n"
                    f"请联系管理员开通，并提供您的 open_id：\n"
                    f"{user_id}")
        role_name = {1: "普通用户", 2: "调度员", 3: "管理员"}.get(role, "未知")
        caps = {
            1: "可查询台架/VLM 数据、预约/取消/归还台架。",
            2: "可查询台架/VLM 数据、预约/取消/归还台架、审批预约、下载 VLM 元数据。",
            3: "拥有全部权限（含跨组审批、触发 VLM 同步）。",
        }.get(role, "")
        return f"您是【{role_name}】。\n{caps}"
    return ""


def _handle_admin_command(text: str, user_id: str) -> str:
    if not _is_admin(user_id):
        return ""
    m = _ADMIN_SET_ROLE.match(text)
    if m:
        target, role = m.group(1), int(m.group(2))
        admin = get_identity_admin()
        ok, msg = admin.set_role(target, role, operator=user_id, note="via_feishu_admin_command")
        if not ok:
            return f"设置失败：{msg}"
        return f"已设置 {target} 的角色为 {_ROLE_NAME[role]}。"
    return ""


def start_consumer() -> None:
    """阻塞的消费循环。运行在专用线程。"""
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
    sender_info = data.event.sender
    user_id = sender_info.sender_id.open_id
    chat_id = msg.chat_id

    # Seed the email→open_id cache (this is the first time we see the
    # sender in this process). Feishu v3 contact API removed email-based
    # user lookup entirely, so the only reliable way to map email→open_id
    # is from message events. Calling remember_open_id with just open_id
    # also keys it by open_id for direct lookups.
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

    # ──身份闸（最早期）：先 resolve role，让所有路径看到一致的角色 ─
    # 简化模型：「飞书能拿到 email → 默认 role=1」；admin 显式覆盖永远胜出
    # （auto_register 幂等不会改已有 admin 设置；set_role 只在 role==0 时升级）。
    admin = get_identity_admin()
    email = identity.email_of(user_id)
    name = identity.name_of(user_id)
    if email:
        admin.auto_register(user_id, email=email, name=name)
        # BUGFIX (#8): if the user already has a record but Feishu now
        # returns a DIFFERENT email (user changed their primary email
        # in Feishu), refresh the stored email/name. Without this, the
        # bench API would receive the stale email and silently misroute
        # the reservation under the wrong account.
        existing = admin.get(user_id) or {}
        if existing.get("email") and existing["email"] != email:
            admin.update_profile(user_id, email=email, name=name or existing.get("name", ""),
                                 operator="auto_email_refresh")
        if admin.get_role(user_id) == 0:
            admin.set_role(
                user_id, 1,
                operator="auto_email_verified",
                note=f"feishu returned email {email}",
            )
    role = admin.get_role(user_id)
    if admin.get(user_id):
        email = admin.get(user_id).get("email", "") or email
        name = admin.get(user_id).get("name", "") or name

    # ── Layer 0: Simple intent — instant reply, no agent ──────────────────
    instant = _match_simple_intent(text)
    if instant:
        sender.send_text_as_card(chat_id, instant)
        return

    # ── Layer 0.5: Fast path for known single-tool queries ────────────────
    # Inspired by the car-booking reference's "LLM only classifies, graph
    # calls tools" design. For the common cases ("查询可用台架", "我的预约"),
    # we skip the 2-call LLM dance (decision + summary, ~30s) and call the
    # tool directly via bench_handlers (already wrapped with guarded() for
    # permission + email injection). Expected latency: 30s → <1s.
    # Unmatched queries fall through to the hermes-agent path.
    fast = _try_fast_path(text, user_id, email, role)
    if fast is not None:
        if fast.blocked:
            sender.send_text_as_card(chat_id, fast.text)
        elif fast.card is not None:
            sender.send_card(chat_id, fast.card)
        else:
            sender.send_text_as_card(chat_id, fast.text or _ERROR_REPLY)
        return

    # ── Layer 0.6: Reservation fast path ──────────────────────────────────
    # For "预约 TJ001, 从明天下午5点到后天晚上8点, 任务是测试, 目的是感知压测",
    # extract args via regex and call dry_run_reserve_bench directly. The
    # confirm card still shows all args so the user can verify before
    # clicking 确认. ~30s LLM call → ~100ms for the common case.
    # Anything ambiguous (e.g. "下午" without a time) falls through to
    # the LLM for clarification.
    resv = _try_reserve_fast_path(text, user_id, email)
    if resv is not None:
        if resv.blocked:
            sender.send_text_as_card(chat_id, resv.text)
        elif resv.card is not None:
            sender.send_card(chat_id, resv.card)
        else:
            sender.send_text_as_card(chat_id, resv.text or _ERROR_REPLY)
        return

    # ── Identity query / admin command (bypass agent) ─────────────────────
    identity_response = _handle_identity_query(text, user_id)
    if identity_response:
        sender.send_text_as_card(chat_id, identity_response)
        return

    admin_response = _handle_admin_command(text, user_id)
    if admin_response:
        sender.send_text_as_card(chat_id, admin_response)
        return

    # ── 身份闸：role=0 才拒绝进入 agent（resolve 已在 _handle 入口完成） ─
    if role == 0:
        sender.send_text_as_card(chat_id,
            f"您还不是平台用户（您的 open_id: {user_id}）。\n"
            "可能原因：飞书 Contact API 未返回您的邮箱（隐私设置或 app 权限不足）。\n"
            "请联系管理员手动开通，或在飞书开发者后台确认机器人有「获取用户邮箱」权限。"
        )
        return

    # ── Dry-run confirm/cancel interceptor (per 车辆预约 reference flow) ─
    # If the user has a pending dry_run_reserve_bench and replies with
    # one of the confirm/cancel phrases, execute deterministically and
    # post real message cards (not toasts) — bypassing the LLM.
    pending = dry_run_state.get(user_id)
    if pending:
        norm = text.strip().strip("「」『』[]\"'").lower()
        if norm in _CANCEL_PHRASES:
            dry_run_state.clear(user_id)
            sender.send_text_as_card(chat_id, "已取消本次预约。")
            return
        if norm in _CONFIRM_PHRASES:
            _execute_confirmed_reserve(chat_id, user_id, email, pending)
            return
        # Otherwise fall through to LLM (user might want to amend args)

    # ── Agent call ──────────────────────────────────────────────────────────
    # No streaming: just wait for the LLM and post the final response.
    # Common single-tool queries are handled by the fast-path above
    # (Layer 0.5 / 0.6) and return in <1s; only complex/free-form requests
    # reach the LLM here, and those are intrinsically multi-call (30s+).
    session_id = f"feishu_{user_id}"
    start = time.monotonic()
    captured: list[dict] = []
    try:
        t0 = time.monotonic()
        agent = agent_pool.get_or_create(user_id)
        log.info("trace[agent_pool.get_or_create] took=%.2fs", time.monotonic() - t0)
        set_current_user(user_id)
        set_current_email(email)
        # contextvars 不会跨线程自动传播——必须在提交任务前 copy_context，
        # 再让 worker 在副本 context 里跑 agent.chat()，否则 worker 里
        # get_current_email() 拿到空串，POST body 缺 emailAddress。
        ctx = contextvars.copy_context()
        tool_capture.clear(session_id)
        t1 = time.monotonic()
        future = _executor.submit(ctx.run, agent.chat, text)
        log.info("trace[executor.submit] took=%.2fs", time.monotonic() - t1)
        t2 = time.monotonic()
        response: str = future.result(timeout=settings.AGENT_TIMEOUT_SECONDS)
        log.info("trace[future.result] took=%.2fs", time.monotonic() - t2)
        captured = tool_capture.read(session_id)
        latency = time.monotonic() - start
        metrics.record("llm_latency_seconds", latency)
        metrics.inc("messages_processed")
        log.info("processed message_id=%s latency=%.2fs", msg.message_id, latency)
    except FuturesTimeout:
        log.error("Agent timeout for user_id=%s message_id=%s", user_id, msg.message_id)
        metrics.inc("errors_timeout")
        response = _TIMEOUT_REPLY
    except Exception:
        log.exception("Agent error for user_id=%s message_id=%s", user_id, msg.message_id)
        metrics.inc("errors_agent")
        response = _ERROR_REPLY
    finally:
        set_current_user("")
        set_current_email("")
        tool_capture.clear(session_id)

    # ──OCL流水线 ─────────────────────────────────────────────
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

    # ── Persist pending dry_run state for the confirm-text interceptor ──
    # If the LLM's most recent call was a dry_run_reserve_bench, save the
    # args under user_id so the next "确认" / "取消" reply is handled
    # without bouncing through the LLM.
    for entry in reversed(captured):
        if entry.get("tool") == "dry_run_reserve_bench":
            res = entry.get("result") or {}
            if isinstance(res, dict) and res.get("dry_run") and not res.get("missing_fields"):
                dry_run_state.save(user_id, res.get("args") or {})
                break
            # Latest dry_run had missing fields — keep looking for an
            # earlier complete one in the same turn.


def _do_post_reserve_actions(chat_id: str, user_id: str, email: str,
                              args: dict, parsed: dict) -> None:
    """Side-effects after a successful reserve_bench:
    1. Notify every dispatcher in the bench's group (best-effort).
    2. Post the applicant confirmation card (notes if dispatcher notify failed).
    3. Persist (reservation_id, applicant_open_id) for later approval DM.

    Caller must hold set_current_user/email — this reads get_current_email()
    transitively via _find_reservation_id → list_my_reservations.
    """
    from bot import card_action_handler as _cah
    from ocl import identity as _identity
    from bot import reservation_store
    bench_no = args.get("benchNo", "")
    start = args.get("startTime", "")
    end = args.get("endTime", "")
    task = args.get("taskName", "")
    purpose = args.get("testPurpose", "")

    api_msg = parsed.get("message", "")
    n_ok = _cah._notify_dispatchers_for_reservation(
        bench_no, start, _identity.name_of(user_id) or email, email, api_msg)

    notify_note = ""
    if n_ok == 0:
        notify_note = "\n\n注：预约已提交成功，但调度员通知发送失败，请人工知会审批人。"
    applicant_text = (
        f"【台架预约申请已提交，将通知管理员进行审批】\n\n"
        f"台架编号：{bench_no}\n"
        f"开始时间：{start}\n"
        f"结束时间：{end}\n"
        f"任务名称：{task}\n"
        f"测试目的：{purpose}{notify_note}"
    )
    sender.send_text_as_card(chat_id, applicant_text)

    rid = _cah._find_reservation_id(bench_no, start, end, email)
    if rid:
        reservation_store.save(rid, user_id, email, bench_no, start, end, task)


def _execute_confirmed_reserve(chat_id: str, user_id: str, email: str,
                                args: dict) -> None:
    """Orchestrate a confirmed reserve: dry-run clear → permission gate →
    set thread-local context → call real reserve_bench → parse + dispatch
    on success / error. The whole flow runs inside a single try/finally
    so the context vars stay valid until _find_reservation_id finishes
    (see code review finding #1)."""
    dry_run_state.clear(user_id)
    args = {**args, "dry_run": False}

    from ocl import permission
    if not permission.is_tool_permitted(user_id, "reserve_bench"):
        sender.send_text_as_card(chat_id, "权限不足，无法提交预约。")
        return

    set_current_user(user_id)
    set_current_email(email)
    try:
        raw = bench_handlers.reserve_bench(args)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = {"error": raw}

        if "error" in parsed:
            # Failure — single explanatory card, no toast
            sender.send_text_as_card(chat_id, f"❌ 预约失败\n{parsed['error']}\n\n请调整后重新预约。")
            return

        if parsed.get("code") != 200:
            sender.send_text_as_card(chat_id, f"❌ 预约失败\n{parsed.get('message','未知错误')}")
            return

        _do_post_reserve_actions(chat_id, user_id, email, args, parsed)
    finally:
        set_current_user("")
        set_current_email("")


def _match_simple_intent(text: str) -> str:
    """把文本与简单意图正则匹配。命中则返回回复字符串，否则返回 ''。"""
    for pattern, reply in _SIMPLE_REPLIES:
        if pattern.search(text):
            return reply
    return ""


# ── Fast path: bypass hermes-agent for known single-tool queries ───────────
# Inspired by the car-booking reference (state graph with LLM only for
# classification). The LLM does 2 calls per turn in the ReAct flow
# (decision + summary, ~30s for minimax M2.7-highspeed). For the most
# common queries, the LLM is unnecessary — a regex match + a direct
# tool call + OCL formatting is enough and finishes in <1s.
#
# We do NOT bypass hermes-agent for reserve/cancel/return flows — those
# need the LLM's natural-language understanding to extract complex args
# (vehicle type, platform, time, task name, etc.) and benefit from the
# dry_run → confirm two-step interaction that the LLM drives.
#
# Each entry: (compiled regex, tool_name, args_extractor). The regex is
# matched against the trimmed input; the args_extractor builds the dict
# passed to bench_handlers.<tool_name>(args). Patterns are anchored at
# the end with $ to avoid false matches like "查询可用台架的剩余数量"
# (which the LLM handles better).

_FAST_PATH_PATTERNS: list[tuple[re.Pattern, str, "callable"]] = [
    # ── list_available_benches ──
    (re.compile(r'^(查询|查看|看看|有什么|列出|看)(\s*(所有|可用))?\s*台架(\s*(列表|号|编号))?[\s!！。.]*$'),
     'list_available_benches', lambda m: {}),
    # "台架列表" / "台架号" — noun-phrase variant
    (re.compile(r'^台架(\s*(列表|号|编号))?[\s!！。.]*$'),
     'list_available_benches', lambda m: {}),
    # "查询 1.0 架构台架" / "看看 1.5 架构的台架"
    (re.compile(r'^(查询|查看|看看)\s*([\d.]+|L\d+)\s*架构(\s*的?\s*台架)?[\s!！。.]*$'),
     'list_available_benches',
     lambda m: {'architecture': (m.group(2) if m.group(2).startswith('L') else m.group(2)) + '架构'}),
    # "L3 架构的台架" / "1.0 架构台架" — noun phrase (no verb)
    (re.compile(r'^([\d.]+|L\d+)\s*架构(\s*的?\s*台架)?[\s!！。.]*$'),
     'list_available_benches',
     lambda m: {'architecture': (m.group(1) if m.group(1).startswith('L') else m.group(1)) + '架构'}),

    # ── list_architectures ──
    (re.compile(r'^(查询|查看|看看)(\s*(所有|可用))?\s*(台架)?架构(\s*列表)?[\s!！。.]*$'),
     'list_architectures', lambda m: {}),
    (re.compile(r'^(台架架构|架构列表)[\s!！。.]*$'),
     'list_architectures', lambda m: {}),

    # ── list_my_reservations ──
    (re.compile(r'^(我的|看看我的|查看我的|看下我的)\s*(预约|记录|预约记录|所有预约)[\s!！。.]*$'),
     'list_my_reservations', lambda m: {}),

    # ── list_my_approvals ──
    (re.compile(r'^(我的|看看我的|查看我的)?\s*(待审批|待我审批|审批列表|待审批列表)[\s!！。.]*$'),
     'list_my_approvals', lambda m: {}),
]


# ── Reservation fast path: extract simple args via regex, call
# dry_run_reserve_bench directly. Bypasses the LLM (30s → <1s) when the
# user provides all the args the LLM would extract: bench number, start
# time, end time, task name, test purpose. Falls through to LLM when
# anything is ambiguous (e.g. "下午" without a specific time).
#
# Format expected: "预约 <BENCH>, 从 <START> 到 <END>, 任务是 X, 目的是 Y"
# (commas and the 任务是/目的是 prefixes are flexible).

_RESERVE_BENCH_RE = re.compile(r'([A-Z]{2,3}\d+)')
_RESERVE_TASK_RE = re.compile(r'任务[是为的话]?\s*([^，,。;；\n]+?)(?=(?:[，,。;；\n]|目的|$))')
_RESERVE_PURPOSE_RE = re.compile(r'目的[是为的话]?\s*([^，,。;；\n]+?)(?=(?:[，,。;；\n]|$))')


def _parse_chinese_time(text: str, now: datetime) -> Optional[datetime]:
    """Parse a Chinese time expression to an absolute datetime. Returns
    None if ambiguous. Supports: 今天/明天/后天 + 上午/下午/晚上 + N点,
    上午/下午/晚上 + N点 (assumes today), X月X号 + 上午/下午/晚上 + N点,
    今晚/明早/明晚 + N点 (defaults to 9:00 if no N given).
    """
    # Most specific first: relative day + period + N点
    m = re.search(
        r'(今|明|后)天?(?:(上午|下午|早上|晚上|中午|夜里|凌晨))?\s*(\d{1,2})\s*[点时:：]',
        text,
    )
    if m:
        day_word, period, hour_str = m.groups()
        delta_days = {"今": 0, "明": 1, "后": 2}[day_word]
        hour = int(hour_str)
        if period in ("下午",) and hour < 12:
            hour += 12
        elif period in ("晚上", "夜里", "凌晨") and hour < 12 and hour != 0:
            hour += 12
        return (now + timedelta(days=delta_days)).replace(
            hour=hour, minute=0, second=0, microsecond=0)

    # Bare day word: "明早" / "今晚" / "明晨" / "明夜" (no time)
    # Use (?<![午夜凌]) to skip when the period char is the start of a
    # longer period word like 晚上/夜里/凌晨.
    default_day_match = re.search(r'(?<![午夜凌])(今|明|后)天?(早|晚|晨|夜)(?![一-鿿])', text)
    if default_day_match:
        day_word, _ = default_day_match.groups()
        delta_days = {"今": 0, "明": 1, "后": 2}[day_word]
        return (now + timedelta(days=delta_days)).replace(hour=9, minute=0, second=0, microsecond=0)

    # Specific date + period + N点
    m = re.search(
        r'(\d{1,2})\s*月\s*(\d{1,2})\s*号?\s*(?:(上午|下午|早上|晚上|中午|夜里|凌晨))?\s*(\d{1,2})\s*[点时:：]?',
        text,
    )
    if m:
        month, day, period, hour_str = m.groups()
        month, day, hour = int(month), int(day), int(hour_str)
        if period in ("下午",) and hour < 12:
            hour += 12
        elif period in ("晚上", "夜里", "凌晨") and hour < 12 and hour != 0:
            hour += 12
        year = now.year
        try:
            return datetime(year, month, day, hour, 0, 0)
        except ValueError:
            return None

    return None


def _try_reserve_fast_path(text: str, user_id: str, email: str):
    """Bypass the LLM for simple reservation requests. Returns the OCL
    result (with the dry_run confirm card) or an ask-user reply when
    args are missing/ambiguous. Returns None only when the text isn't
    a reservation request at all (so the LLM can handle it).

    Key design: we DO NOT fall through to the LLM for missing/ambiguous
    args. The LLM would take ~30s to "think" about what to ask; we can
    ask the user directly in <1ms with a deterministic template.

    Only dry_run_reserve_bench's missing_fields handling is allowed to
    ask for task/purpose (it presents all missing fields in a single
    card, which is better UX than one-at-a-time asks).
    """
    from ocl.pipeline import OclResult
    norm = text.strip()
    # Only handle explicit reservation requests. \b doesn't work for
    # CJK characters, so just check the prefix matches.
    if not re.match(r'^(预约|帮我预约|我要预约|我想预约|帮我订|我想订)', norm):
        return None

    # ── Stage 1: extract bench number ────────────────────────────────────
    bench_match = _RESERVE_BENCH_RE.search(norm)
    if not bench_match:
        return OclResult(
            text=("请告知要预约的台架编号。\n"
                  "例如：预约 TJ001，从明天下午5点到后天晚上8点，任务是测试，目的是感知压测"),
            blocked=False, card=None,
        )
    bench_no = bench_match.group(1)

    # ── Stage 2: extract time range ──────────────────────────────────────
    range_match = re.search(
        r'(?:从|自)\s*(.+?)\s*(?:到|至|到|~|-)\s*(.+?)(?=[，,。;；\n]|任务|目的|$)',
        norm,
    )
    if not range_match:
        return OclResult(
            text=(f"请告知 {bench_no} 的预约时间范围（开始 + 结束）。\n"
                  f"例如：从明天下午5点到后天晚上8点"),
            blocked=False, card=None,
        )
    start_text, end_text = range_match.group(1).strip(), range_match.group(2).strip()

    # ── Stage 3: parse the times into datetimes ─────────────────────────
    now_cn = datetime.now() + timedelta(hours=8)
    start_dt = _parse_chinese_time(start_text, now_cn)
    end_dt = _parse_chinese_time(end_text, now_cn)
    if not start_dt and not end_dt:
        return OclResult(
            text=("无法识别起止时间，请用具体时间表达。\n"
                  "支持：「明天下午5点」「后天晚上8点」「7月1号下午3点」"),
            blocked=False, card=None,
        )
    if not start_dt:
        return OclResult(
            text=(f"开始时间「{start_text}」无法识别，请用具体时间表达。\n"
                  f"支持：「明天下午5点」「7月1号上午9点」"),
            blocked=False, card=None,
        )
    if not end_dt:
        return OclResult(
            text=(f"结束时间「{end_text}」无法识别，请用具体时间表达。\n"
                  f"支持：「后天晚上8点」「7月1号下午6点」"),
            blocked=False, card=None,
        )
    if end_dt <= start_dt:
        return OclResult(
            text=(f"结束时间早于或等于开始时间，请重新确认：\n"
                  f"• 开始：{start_dt.strftime('%Y-%m-%d %H:%M')}\n"
                  f"• 结束：{end_dt.strftime('%Y-%m-%d %H:%M')}"),
            blocked=False, card=None,
        )

    # ── Stage 4: extract task / purpose (optional; dry_run handles missing) ─
    task_match = _RESERVE_TASK_RE.search(norm)
    purpose_match = _RESERVE_PURPOSE_RE.search(norm)
    task = task_match.group(1).strip() if task_match else ""
    purpose = purpose_match.group(1).strip() if purpose_match else ""

    # ── Stage 5: call dry_run ────────────────────────────────────────────
    args = {
        "benchNo": bench_no,
        "startTime": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if task:
        args["taskName"] = task
    if purpose:
        args["testPurpose"] = purpose

    set_current_user(user_id)
    set_current_email(email)
    from bench_tools import handlers as bench_handlers
    handler = getattr(bench_handlers, "dry_run_reserve_bench", None)
    if handler is None:
        return None

    t0 = time.monotonic()
    try:
        raw = handler(args)
    except Exception:
        log.exception("reserve_fast_path tool_failed user=%s", user_id)
        return OclResult(text=_ERROR_REPLY, blocked=False, card=None)
    metrics.inc("fast_path_hits")
    log.info("reserve_fast_path hit user=%s bench=%s latency=%.0fms",
             user_id, bench_no, (time.monotonic() - t0) * 1000)

    # Save dry_run_state so the user's next "确认" / "取消" reply hits the
    # deterministic confirm path (bypasses LLM, runs reserve_bench for real,
    # fires dispatcher notifications). Without this, the LLM path would be
    # taken — but reserve_bench isn't in the LLM's toolset, so the
    # reservation would silently never happen and no notification would fire.
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict) and parsed.get("dry_run") and not parsed.get("missing_fields"):
            dry_run_state.save(user_id, parsed.get("args") or args)
    except (json.JSONDecodeError, ValueError):
        # If parse fails, fall back to the args we sent
        dry_run_state.save(user_id, args)

    captured = [{"tool": "dry_run_reserve_bench", "result": raw}]
    return ocl_apply("请确认预约信息", user_id, captured=captured)


def _try_fast_path(text: str, user_id: str, email: str, role: int):
    """Bypass hermes-agent for known query patterns. Returns the OCL
    result (already run on the tool output) or None if no match.

    Returns None on:
    - no regex match (fall through to LLM)
    - regex match but tool permission denied
    - tool call raised (fall through to LLM, which can retry or error)

    The bench_handlers are already wrapped with guarded() for permission
    + email injection (Layer 2 of double-defense), so calling them
    directly from here maintains the security model.
    """
    norm = text.strip()
    if not norm:
        return None
    for pattern, tool_name, args_fn in _FAST_PATH_PATTERNS:
        m = pattern.match(norm)
        if not m:
            continue

        # Role gate (matches the OCL TOOL_MIN_ROLE table)
        from ocl.permission import TOOL_MIN_ROLE
        if TOOL_MIN_ROLE.get(tool_name, 99) > role:
            return None  # user's role can't run this tool — let LLM explain

        # Inject identity context (handlers read email via thread-local)
        set_current_user(user_id)
        set_current_email(email)

        from bench_tools import handlers as bench_handlers
        handler = getattr(bench_handlers, tool_name, None)
        if handler is None:
            log.warning("fast_path handler_missing tool=%s", tool_name)
            return None

        args = args_fn(m)
        t0 = time.monotonic()
        try:
            raw_result = handler(args)
        except Exception:
            log.exception("fast_path tool_failed tool=%s user=%s", tool_name, user_id)
            return None
        latency_ms = (time.monotonic() - t0) * 1000
        log.info("fast_path hit tool=%s user=%s latency=%.0fms",
                 tool_name, user_id, latency_ms)
        metrics.inc("fast_path_hits")

        # Parse the tool result to decide summary text + check for errors
        try:
            parsed = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except (json.JSONDecodeError, ValueError):
            parsed = {}

        # Error path: tool returned code != 200 → return a text reply
        # (not an empty card) so the user sees what went wrong.
        if not isinstance(parsed, dict) or parsed.get("code") != 200:
            from ocl.pipeline import OclResult
            err = (parsed or {}).get("msg") if isinstance(parsed, dict) else None
            return OclResult(text=err or _ERROR_REPLY, blocked=False, card=None)

        # Success path: build a short summary text + captured entry.
        # The summary is what ocl.pipeline sees as "the LLM's text" —
        # it must be non-empty or format_control will block with the
        # _EMPTY_MESSAGE ("未能生成有效回复"). card_builder then combines
        # the summary with the captured data to render the actual card.
        summary = _fast_path_summary(tool_name, parsed)
        captured = [{"tool": tool_name, "result": raw_result}]
        return ocl_apply(summary, user_id, captured=captured)
    return None


def _fast_path_summary(tool_name: str, parsed: dict) -> str:
    """Build a short summary line for the OCL card. Mirrors what the LLM
    would have written (one line + "next-step hint" per the system prompt).
    The card_builder then adds the data block + any interactive buttons.

    The summary must be non-empty (format_control blocks empty input).
    """
    data = parsed.get("data")
    if tool_name == "list_available_benches":
        n = len(data) if isinstance(data, list) else 0
        return f"📋 当前可用台架共 {n} 个（详见下方列表）。"
    if tool_name == "list_architectures":
        if isinstance(data, list):
            names = "、".join(str(x) for x in data[:10])
            return f"📋 台架架构：{names}。"
        return "📋 台架架构列表已就绪。"
    if tool_name == "list_my_reservations":
        n = len(data) if isinstance(data, list) else 0
        return f"📋 您当前有 {n} 条预约记录。"
    if tool_name == "list_my_approvals":
        n = len(data) if isinstance(data, list) else 0
        return f"📋 您当前有 {n} 条待审批记录。"
    return "📋 查询成功。"


def _extract_text(msg) -> str:
    """从飞书 text消息中提取纯文本。非 text 类型返回 ''。"""
    if msg.message_type != "text":
        return ""
    try:
        content = json.loads(msg.content)
        return content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return ""
