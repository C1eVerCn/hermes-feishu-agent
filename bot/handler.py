import json
import logging
import re
import time
import contextvars
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config.settings import settings
from feishu.ws_client import event_queue
from feishu import sender
from bot.agent_pool import agent_pool
from infra.metrics import metrics
from ocl.pipeline import apply as ocl_apply
from ocl.tool_guard import set_current_user, set_current_email
from ocl import identity
from bot.identity_admin import get_admin as get_identity_admin
from ocl import tool_capture

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="agent-worker")

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
    if _MY_PERMS.search(text):
        email = identity.email_of(user_id)
        name = identity.name_of(user_id) or "(未识别)"
        admin = get_identity_admin()
        role = admin.get_role(user_id)
        if role == 0:
            return (f"您当前是【待审核】用户。\n"
                    f"open_id: {user_id}\n"
                    f"邮箱: {email or '(飞书未返回)'}\n"
                    f"姓名: {name}\n\n"
                    f"请联系管理员在飞书发「设置角色 {user_id} 1|2|3」开通权限。")
        role_name = {1: "普通用户", 2: "调度员", 3: "管理员"}.get(role, "未知")
        return (f"您是平台【{role_name}】（role={role}）。\n"
                f"open_id: {user_id}\n"
                f"邮箱: {email or '(飞书未返回)'}\n"
                f"姓名: {name}\n\n"
                f"可查询台架/VLM 数据、预约/取消/归还台架；"
                f"调度员/管理员额外可审批预约、触发 VLM 同步。")
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

    log.info("received message_id=%s chat_id=%s user_id=%s", msg.message_id, chat_id, user_id)

    text = _extract_text(msg)

    if not text:
        sender.send(chat_id, _EMPTY_REPLY)
        return

    if len(text) > _MAX_INPUT_CHARS:
        sender.send(chat_id, _INPUT_TOO_LONG_REPLY)
        return

    # ──身份闸（最早期）：先 resolve role，让所有路径看到一致的角色 ─
    # 简化模型：「飞书能拿到 email → 默认 role=1」；admin 显式覆盖永远胜出
    # （auto_register 幂等不会改已有 admin 设置；set_role 只在 role==0 时升级）。
    admin = get_identity_admin()
    email = identity.email_of(user_id)
    name = identity.name_of(user_id)
    if email:
        admin.auto_register(user_id, email=email, name=name)
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
        sender.send(chat_id, instant)
        return

    # ── Identity query / admin command (bypass agent) ─────────────────────
    identity_response = _handle_identity_query(text, user_id)
    if identity_response:
        sender.send(chat_id, identity_response)
        return

    admin_response = _handle_admin_command(text, user_id)
    if admin_response:
        sender.send(chat_id, admin_response)
        return

    # ── 身份闸：role=0 才拒绝进入 agent（resolve 已在 _handle 入口完成） ─
    if role == 0:
        sender.send(chat_id,
            f"您还不是平台用户（您的 open_id: {user_id}）。\n"
            "可能原因：飞书 Contact API 未返回您的邮箱（隐私设置或 app 权限不足）。\n"
            "请联系管理员手动开通，或在飞书开发者后台确认机器人有「获取用户邮箱」权限。"
        )
        return

    # ── Agent call ──────────────────────────────────────────────────────────
    session_id = f"feishu_{user_id}"  # 必须与 agent_pool 的 session_id 一致
    start = time.monotonic()
    captured: list[dict] = []
    try:
        agent = agent_pool.get_or_create(user_id)
        set_current_user(user_id)
        set_current_email(email)
        # 关键：contextvars 不会跨线程自动传播——必须在提交任务前 copy_context，
        # 再让 worker 在副本 context 里跑 agent.chat()，否则 worker 里
        # get_current_email() 拿到空串，POST body 缺 emailAddress。
        ctx = contextvars.copy_context()
        tool_capture.clear(session_id)
        future = _executor.submit(ctx.run, agent.chat, text)
        response: str = future.result(timeout=settings.AGENT_TIMEOUT_SECONDS)
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
            log.exception("send_card failed, falling back to text")
            sender.send(chat_id, ocl_result.text or _ERROR_REPLY)
    else:
        sender.send(chat_id, ocl_result.text or _ERROR_REPLY)


def _match_simple_intent(text: str) -> str:
    """把文本与简单意图正则匹配。命中则返回回复字符串，否则返回 ''。"""
    for pattern, reply in _SIMPLE_REPLIES:
        if pattern.search(text):
            return reply
    return ""


def _extract_text(msg) -> str:
    """从飞书 text消息中提取纯文本。非 text 类型返回 ''。"""
    if msg.message_type != "text":
        return ""
    try:
        content = json.loads(msg.content)
        return content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return ""
