import logging
import queue
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config.settings import settings
from infra.dedup import dedup
from infra.metrics import metrics

log = logging.getLogger(__name__)

# 由 agent/handler.py消费的共享队列
event_queue: queue.Queue = queue.Queue(maxsize=1000)

# infra/health.py读取的标志位
ws_connected = threading.Event()


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


def _build_client() -> lark.ws.Client:
    # 2026-06-30 Phase 1.1：移除 p2_card_action_trigger 注册。
    # 卡片已改为只读展示，bot 不再处理按钮/select/form 回调；lark 端若有遗留回调
    # 会落进 WS 错误日志（不致命），用户看到的旧卡可继续展示但点击无响应。
    event_handler = (
        lark.EventDispatcherHandler.builder(
            encrypt_key=settings.FEISHU_ENCRYPT_KEY,
            verification_token=settings.FEISHU_VERIFY_TOKEN,
        )
        .register_p2_im_message_receive_v1(_on_message)
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
