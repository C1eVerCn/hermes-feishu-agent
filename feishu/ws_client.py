import json
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

# Shared queue consumed by agent/handler.py
event_queue: queue.Queue = queue.Queue(maxsize=1000)

# Flag read by infra/health.py
ws_connected = threading.Event()


def _on_message(data: P2ImMessageReceiveV1) -> None:
    """Fast callback — only enqueue, never block."""
    msg = data.event.message

    # Deduplicate by message_id (image events produce multiple event_ids per message)
    if dedup.is_duplicate(msg.message_id):
        return

    metrics.inc("messages_received")
    try:
        event_queue.put_nowait(data)
    except queue.Full:
        log.warning("event_queue full, dropping message_id=%s", msg.message_id)
        metrics.inc("errors_queue_full")


def _build_client() -> lark.ws.Client:
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
