import json
import logging
import queue
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from config.settings import settings
from infra.dedup import dedup
from infra.metrics import metrics

log = logging.getLogger(__name__)

# 由 agent/handler.py消费的共享队列
event_queue: queue.Queue = queue.Queue(maxsize=1000)

# infra/health.py读取的标志位
ws_connected = threading.Event()

# Card-action callback, injected by main.py (keeps feishu/ free of bot/ imports).
# Signature: (open_id: str, value: dict, chat_id: str) -> tuple[str, dict | None]
# (toast_text, updated_card). chat_id is the chat the card was sent in
# (operator's open_id for DM cards, oc_xxx for room cards). Currently passed
# for logging/diagnostics; the handler replies via the returned toast/card,
# not by sending to chat_id. Kept in the signature so a future
# send-extra-message flow can use it without re-plumbing the WS layer.
_card_action_handler = None


def set_card_action_handler(fn) -> None:
    """注入确定性卡片回调处理（bot.card_action_handler.handle）。"""
    global _card_action_handler
    _card_action_handler = fn


def _on_message(data: P2ImMessageReceiveV1) -> None:
    """快速回调 —只入队，不阻塞。"""
    msg = data.event.message

    #按 message_id 去重（图片事件每条消息会产生多个 event_id）
    if dedup.is_duplicate(msg.message_id):
        return

    metrics.inc("messages_received")
    try:
        event_queue.put_nowait(data)
    except queue.Full:
        log.warning("event_queue full, dropping message_id=%s", msg.message_id)
        metrics.inc("errors_queue_full")


def _extract_card_action(data: P2CardActionTrigger):
    """从 lark卡片动作事件中取 (open_id, value, chat_id).

    chat_id comes from `context.open_chat_id` — the chat where the card was
    sent (operator's open_id for DM cards, oc_xxx for room cards). Currently
    used for logging/diagnostics only.

    2026-06-18 兼容 select_static：用户在下拉里选中某项时，飞书把选项 key
    放在 `data.event.action.option`（早期 lark-oapi 版本叫 `form_value`），
    而非放进外层 value['value']。这里把 option 归一化到 value['value']，
    让下游 card_action_handler / _handle_fsm_button 走原 fsm_select 路径，
    不用改业务代码。按钮路径不受影响（按钮回调的 value 已经有 'value' 键）。
    """
    op = data.event.operator
    open_id = getattr(op, "open_id", "") or ""
    value = getattr(data.event.action, "value", None) or {}
    action = data.event.action
    action_tag = getattr(action, "tag", None)
    if isinstance(value, dict) and not value.get("value"):
        # 2026-06-18 select_static + form 兼容：lark-oapi CallBackAction 字段
        # (lark_oapi/event/callback/model/p2_card_action_trigger.py:38-66):
        #   option:     Optional[str]            ← 单选 select_static 选中 key
        #   options:    Optional[List[str]]      ← 多选 select_static 选中 keys
        #   form_value: Optional[Dict[str, Any]] ← Card 2.0 form submit（input/textarea 字段）
        #   tag:        Optional[str]            ← 触发元素 tag（"button" / "form" / "select_static"）
        # 优先级：form_value 第一个 string > option > options[0]
        # 2026-06-24 review fix：只对 tag 明确是 form/select 的 action 注入 value；
        # 普通 button (tag='button' 无 value 字段是合法的，比如 cancel_flow) 不动，
        # 避免被 option/form_value 污染成无效文本。
        if action_tag in ("form", "select_static"):
            # 1) form_value Dict：取第一个 string 值（如 {"task_input": "MFF"} → "MFF"）
            form_value = getattr(action, "form_value", None)
            if isinstance(form_value, dict) and form_value:
                for v in form_value.values():
                    if isinstance(v, str) and v:
                        value = {**value, "value": v}
                        break
            # 2) 还没 value → 退回 select_static option 路径
            if not value.get("value"):
                def _first_str(*names):
                    for n in names:
                        v = getattr(action, n, None)
                        if isinstance(v, str) and v:
                            return v
                        if isinstance(v, list) and v and isinstance(v[0], str):
                            return v[0]
                    return None
                option = _first_str("option", "options")
                if option:
                    value = {**value, "value": option}
    ctx = getattr(data.event, "context", None)
    chat_id = getattr(ctx, "open_chat_id", "") or ""
    log.info("card_action_received open_id=%s chat_id=%s value_keys=%s",
             open_id, chat_id, list(value.keys()) if isinstance(value, dict) else None)
    return open_id, value, chat_id


def _build_card_action_response(toast_text: str, updated_card):
    payload = {"toast": {"type": "info", "content": toast_text}}
    if updated_card is not None:
        payload["card"] = {"type": "raw", "data": updated_card}
    return P2CardActionTriggerResponse(payload)


def _toast_text(resp) -> str:
    """Extract toast text from a response (test helper / introspection)."""
    toast = getattr(resp, "toast", None)
    return getattr(toast, "content", "") or ""


def _on_card_action(data: P2CardActionTrigger):
    """同步卡片回调：执行确定性动作，返回 toast。"""
    try:
        open_id, value, chat_id = _extract_card_action(data)
        if _card_action_handler is None:
            toast_text, updated_card = "操作处理未就绪，请稍后重试", None
        else:
            toast_text, updated_card = _card_action_handler(open_id, value, chat_id)
    except Exception:
        log.exception("card action handling failed")
        toast_text, updated_card = "操作处理失败，请稍后重试", None
    return _build_card_action_response(toast_text, updated_card)


def _build_client() -> lark.ws.Client:
    event_handler = (
        lark.EventDispatcherHandler.builder(
            encrypt_key=settings.FEISHU_ENCRYPT_KEY,
            verification_token=settings.FEISHU_VERIFY_TOKEN,
        )
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_card_action_trigger(_on_card_action)
        .build()
    )
    return lark.ws.Client(
        app_id=settings.FEISHU_APP_ID,
        app_secret=settings.FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.WARNING,
    )


def start_ws_supervision() -> None:
    """
    Outer supervision loop. lark-oapi retries ~7 times internally then exits.
    This loop restarts it with exponential backoff (2 s → 60 s cap).
    """
    delay = 2
    max_delay = 60

    while True:
        log.info("Starting WebSocket client")
        ws_connected.clear()
        try:
            client = _build_client()
            ws_connected.set()
            metrics.inc("ws_reconnects")
            client.start()  # blocks until connection dies
        except Exception as exc:
            log.error("WebSocket exited with error: %s. Reconnecting in %ss", exc, delay)
        else:
            log.warning("WebSocket exited cleanly. Reconnecting in %ss", delay)
        finally:
            ws_connected.clear()

        time.sleep(delay)
        delay = min(delay * 2, max_delay)
