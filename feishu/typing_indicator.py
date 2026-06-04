import threading
import logging
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from config.settings import settings

log = logging.getLogger(__name__)

_client = lark.Client.builder() \
    .app_id(settings.FEISHU_APP_ID) \
    .app_secret(settings.FEISHU_APP_SECRET) \
    .build()

PLACEHOLDER_TEXT = "⏳ 正在处理，请稍候..."
THRESHOLD_SECONDS = 2.0


class TypingIndicator:
    """
    Sends a placeholder message if the LLM takes longer than THRESHOLD_SECONDS.
    Call start() before the LLM call, stop() after.
    """

    def __init__(self, chat_id: str) -> None:
        self._chat_id = chat_id
        self._timer: threading.Timer | None = None
        self._placeholder_message_id: str | None = None

    def start(self) -> None:
        self._timer = threading.Timer(THRESHOLD_SECONDS, self._send_placeholder)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _send_placeholder(self) -> None:
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self._chat_id)
                .msg_type("text")
                .content(json.dumps({"text": PLACEHOLDER_TEXT}))
                .build()
            ).build()

        resp = _client.im.v1.message.create(req)
        if resp.success():
            self._placeholder_message_id = resp.data.message_id
        else:
            # Non-critical: log and continue without placeholder
            log.debug("typing indicator send failed: %s", resp.msg)
