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

# Card structure for the placeholder. We send a card (interactive) message
# rather than a plain text message because Feishu's PATCH /im/v1/messages
# API only supports updating card (interactive) messages — text messages
# cannot be edited in place. The card has a single text element that
# edit_message() rewrites with the streaming token buffer.
def _card_payload(text: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": text}},
        ],
    }


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
        # Don't delete the placeholder: Feishu IM has no delete-message API.
        # The placeholder stays as the streaming target, then gets covered
        # by edit_message into the final response.
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def edit_message(self, content_text: str) -> None:
        """Update the placeholder bubble in place with new text. Noop if
        the placeholder wasn't sent (timer didn't fire yet)."""
        if self._placeholder_message_id is None:
            return
        from feishu import sender
        sender.edit_message(self._placeholder_message_id, content_text)

    def _send_placeholder(self) -> None:
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self._chat_id)
                .msg_type("interactive")
                .content(json.dumps(_card_payload(PLACEHOLDER_TEXT), ensure_ascii=False))
                .build()
            ).build()

        resp = _client.im.v1.message.create(req)
        if resp.success():
            self._placeholder_message_id = resp.data.message_id
        else:
            #非关键：log 后继续，不发占位消息
            log.debug("typing indicator send failed: %s", resp.msg)
