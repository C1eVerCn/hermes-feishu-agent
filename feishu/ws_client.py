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
# Signature: (open_id: str, value: dict) -> tuple[str, dict | None]  (toast_text, updated_card)
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
    """从 lark卡片动作事件中取 (open_id, value)。"""
    op = data.event.operator
    open_id = getattr(op, "open_id", "") or ""
    value = getattr(data.event.action, "value", None) or {}
    return open_id, value


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
        open_id, value = _extract_card_action(data)
        if _card_action_handler is None:
            toast_text, updated_card = "操作处理未就绪，请稍后重试", None
        else:
            toast_text, updated_card = _card_action_handler(open_id, value)
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
