import json
import logging
import time
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from config.settings import settings
from infra.metrics import metrics

log = logging.getLogger(__name__)

CHUNK_SIZE = 3900       # Feishu hard limit is 4096; leave margin
RATE_LIMIT_INTERVAL = 0.22  # ~4.5 msg/s, safely below Feishu's 5 msg/s cap

_send_lock = threading.Lock()
_last_send_time = 0.0

_client = lark.Client.builder() \
    .app_id(settings.FEISHU_APP_ID) \
    .app_secret(settings.FEISHU_APP_SECRET) \
    .build()


def send(chat_id: str, text: str) -> None:
    """向飞书群聊发文本。超过 CHUNK_SIZE 自动分块。带限流。"""
    chunks = _chunk_text(text)
    for i, chunk in enumerate(chunks):
        prefix = f"[{i + 1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        _send_one(chat_id, prefix + chunk)
        if i < len(chunks) - 1:
            time.sleep(RATE_LIMIT_INTERVAL)


def send_text_as_card(chat_id: str, text: str) -> None:
    """Wrap plain text as a single-element interactive card and send.

    Used by bot/handler.py for intent-path replies (greetings, identity query,
    admin commands, error messages) so every user-visible message renders as
    a card — consistent visual style across all paths.
    """
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div",
                      "text": {"tag": "lark_md", "content": text}}],
    }
    send_card(chat_id, card)


def send_to_user(open_id: str, text: str) -> None:
    """按 open_id 给用户发私信。"""
    chunks = _chunk_text(text)
    for i, chunk in enumerate(chunks):
        prefix = f"[{i + 1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        _send_one_to_user(open_id, prefix + chunk)
        if i < len(chunks) - 1:
            time.sleep(RATE_LIMIT_INTERVAL)


def send_card(chat_id: str, card: dict, max_retries: int = 3) -> None:
    """向群聊发送互动卡片。限流策略同文本发送。"""
    global _last_send_time
    with _send_lock:
        elapsed = time.monotonic() - _last_send_time
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        _last_send_time = time.monotonic()

    content = json.dumps(card, ensure_ascii=False)
    for attempt in range(max_retries):
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            ).build()
        resp = _client.im.v1.message.create(req)
        if resp.success():
            metrics.inc("cards_sent")
            return
        if resp.code == 429:
            time.sleep(2 ** attempt)
            continue
        log.error("Feishu send_card failed: code=%s msg=%s", resp.code, resp.msg)
        return
    log.error("Feishu send_card failed after %d retries for chat_id=%s", max_retries, chat_id)


def edit_message(message_id: str, content_text: str,
                max_retries: int = 3) -> None:
    """Update an existing Feishu message in place. Used by streaming to
    append token updates to the typing placeholder bubble.

    Mirrors send_card's 429-retry + rate-limit pattern. Non-429 failures
    are logged and dropped (the next edit_message call will retry the
    whole stream; cumulative drop is acceptable for streaming UX).

    No chat_id is needed: the Feishu PATCH /im/v1/messages/{message_id}
    endpoint identifies the target via the message_id path parameter,
    and the SDK's PatchMessageRequest / PatchMessageRequestBody builders
    do not accept a receive_id or msg_type — only the new `content` body
    field is needed.
    """
    global _last_send_time
    with _send_lock:
        elapsed = time.monotonic() - _last_send_time
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        _last_send_time = time.monotonic()

    content = json.dumps({"text": content_text}, ensure_ascii=False)
    for attempt in range(max_retries):
        req = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            ).build()
        resp = _client.im.v1.message.update(req)
        if resp.success():
            return
        if resp.code == 429:
            time.sleep(2 ** attempt)
            continue
        log.error("Feishu edit_message failed: code=%s msg=%s", resp.code, resp.msg)
        return
    log.error("Feishu edit_message failed after %d retries for msg_id=%s",
              max_retries, message_id)


def _send_one(chat_id: str, text: str, max_retries: int = 3) -> None:
    global _last_send_time

    with _send_lock:
        elapsed = time.monotonic() - _last_send_time
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        _last_send_time = time.monotonic()

    for attempt in range(max_retries):
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            ).build()

        resp = _client.im.v1.message.create(req)
        if resp.success():
            metrics.inc("messages_sent")
            return

        if resp.code == 429:
            wait = 2 ** attempt
            log.warning("Feishu rate limited (429), retrying in %ss", wait)
            time.sleep(wait)
            continue

        log.error("Feishu send failed: code=%s msg=%s", resp.code, resp.msg)
        return  # non-retryable

    log.error("Feishu send failed after %d retries for chat_id=%s", max_retries, chat_id)


def _send_one_to_user(open_id: str, text: str, max_retries: int = 3) -> None:
    global _last_send_time

    with _send_lock:
        elapsed = time.monotonic() - _last_send_time
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        _last_send_time = time.monotonic()

    for attempt in range(max_retries):
        req = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            ).build()

        resp = _client.im.v1.message.create(req)
        if resp.success():
            metrics.inc("messages_sent")
            return

        if resp.code == 429:
            wait = 2 ** attempt
            log.warning("Feishu rate limited (429), retrying in %ss", wait)
            time.sleep(wait)
            continue

        log.error("Feishu send_to_user failed: code=%s msg=%s", resp.code, resp.msg)
        return

    log.error("Feishu send_to_user failed after %d retries for open_id=%s", max_retries, open_id)


def _chunk_text(text: str) -> list[str]:
    if len(text) <= CHUNK_SIZE:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= CHUNK_SIZE:
            chunks.append(text)
            break
        # 在字数限制前的最后一个空白处断开
        split_at = text.rfind(" ", 0, CHUNK_SIZE)
        if split_at == -1:
            split_at = CHUNK_SIZE
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks
