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
STREAM_PATCH_MIN_INTERVAL = 0.5  # 流式 PATCH 最小间隔（避免触发限流）
STREAM_PATCH_MIN_CHARS = 5       # 攒到 ≥5 字符才 PATCH（减少 PATCH 次数）

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
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": [{"tag": "div",
                                "text": {"tag": "lark_md", "content": text}}]},
    }
    send_card(chat_id, card)


def send_to_user(open_id: str, text: str) -> bool:
    """按 open_id 给用户发私信。返回 True 仅当所有分块都发送成功。

    BUGFIX: 过去返回 None，调用方 notify._notify_dispatchers_sync 用
    `if send_to_user(...)` 统计成功数 → 永远为假 → 即使送达也报「调度员通知失败」。
    """
    ok = True
    chunks = _chunk_text(text)
    for i, chunk in enumerate(chunks):
        prefix = f"[{i + 1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        if not _send_one_to_user(open_id, prefix + chunk):
            ok = False
        if i < len(chunks) - 1:
            time.sleep(RATE_LIMIT_INTERVAL)
    return ok


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


def patch_card(message_id: str, card: dict, max_retries: int = 2) -> bool:
    """PATCH 一条已发送的 interactive card（用 message_id 定位）。

    用于流式输出：先 send_card 占位 → LLM 每生成一段就 patch_card 一次更新
    内容，用户看到「打字机」效果。

    限流：PATCH 走飞书 API，仍受 5 msg/s 限制。调用方应做节流
    （每 0.5s 最多 1 次，每次 ≥5 字符），避免触发 429。
    """
    content = json.dumps(card, ensure_ascii=False)
    for attempt in range(max_retries):
        req = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            ).build()
        try:
            resp = _client.im.v1.message.patch(req)
        except Exception as e:
            log.warning("patch_card exception message_id=%s err=%s", message_id, e)
            return False
        if resp.success():
            return True
        if resp.code == 429:
            wait = 2 ** attempt
            log.warning("patch_card rate limited (429) message_id=%s retry_in=%ss", message_id, wait)
            time.sleep(wait)
            continue
        log.warning("patch_card failed message_id=%s code=%s msg=%s",
                    message_id, resp.code, resp.msg)
        return False
    log.warning("patch_card failed after %d retries message_id=%s", max_retries, message_id)
    return False


class StreamCard:
    """流式输出占位卡片 + 增量 PATCH 控制器。

    用法：
        sc = StreamCard(chat_id)        # 立即 send_card 一个"..."占位
        for chunk in llm_stream:
            sc.append(chunk)              # 内部节流：每 0.5s 最多 1 次 PATCH
        sc.finalize("完整回复")           # 发送最终完整版（保证消息最终完整）

    节流策略：
    - 累积 ≥STREAM_PATCH_MIN_CHARS=5 字符才考虑 PATCH
    - 距离上次 PATCH <STREAM_PATCH_MIN_INTERVAL=0.5s 则等
    - PATCH 失败不重试（让 finalize 用 send_card 兜底发新消息）
    """

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        # 占位卡片：先发出去拿到 message_id
        self.message_id: str | None = None
        self._buf: str = ""
        self._last_patch: float = 0.0
        # 先发"..."占位（让飞书侧有可 PATCH 的目标）
        self._placeholder_card = {
            "schema": "2.0", "config": {"wide_screen_mode": True},
            "body": {"elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "…"}}]},
        }
        try:
            self._create_placeholder()
        except Exception as e:
            log.warning("StreamCard placeholder create failed: %s", e)

    def _create_placeholder(self) -> None:
        # 复用 send_card 拿到 message_id
        # send_card 不返回 message_id，包一层
        # 用更直接的 CreateMessageRequest 走
        content = json.dumps(self._placeholder_card, ensure_ascii=False)
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self.chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            ).build()
        resp = _client.im.v1.message.create(req)
        if resp.success() and resp.data and resp.data.message_id:
            self.message_id = resp.data.message_id
        else:
            log.warning("StreamCard placeholder create resp failed code=%s msg=%s",
                        resp.code, resp.msg)

    def append(self, text: str) -> None:
        """追加 LLM 输出片段。自动节流 PATCH。"""
        if not text or not self.message_id:
            return
        self._buf += text
        now = time.monotonic()
        if (len(self._buf) >= STREAM_PATCH_MIN_CHARS
                and (now - self._last_patch) >= STREAM_PATCH_MIN_INTERVAL):
            self._flush()
            self._last_patch = now

    def _flush(self) -> None:
        if not self.message_id or not self._buf:
            return
        card = {
            "schema": "2.0", "config": {"wide_screen_mode": True},
            "body": {"elements": [{"tag": "div",
                                    "text": {"tag": "lark_md", "content": self._buf}}]},
        }
        patch_card(self.message_id, card)

    def finalize(self, full_text: str) -> None:
        """最终完整版 PATCH 一次（保证消息内容完整 + 包含所有遗漏部分）。"""
        if not self.message_id:
            # 占位都没成功，fallback 普通发送
            send_text_as_card(self.chat_id, full_text)
            return
        # PATCH 最终完整版（覆盖占位 + 累积 buffer）
        card = {
            "schema": "2.0", "config": {"wide_screen_mode": True},
            "body": {"elements": [{"tag": "div",
                                    "text": {"tag": "lark_md", "content": full_text}}]},
        }
        if not patch_card(self.message_id, card):
            # PATCH 失败 → 退而求其次发新消息
            log.warning("StreamCard finalize patch failed, sending fresh message")
            send_text_as_card(self.chat_id, full_text)

    def finalize_with_card(self, card: dict) -> None:
        """用完整 card 对象 PATCH（OCL 已生成好的最终卡片）。"""
        if not self.message_id:
            send_card(self.chat_id, card)
            return
        if not patch_card(self.message_id, card):
            log.warning("StreamCard finalize_with_card patch failed, sending fresh card")
            send_card(self.chat_id, card)


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


def _send_one_to_user(open_id: str, text: str, max_retries: int = 3) -> bool:
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
            return True

        if resp.code == 429:
            wait = 2 ** attempt
            log.warning("Feishu rate limited (429), retrying in %ss", wait)
            time.sleep(wait)
            continue

        log.error("Feishu send_to_user failed: code=%s msg=%s", resp.code, resp.msg)
        return False

    log.error("Feishu send_to_user failed after %d retries for open_id=%s", max_retries, open_id)
    return False


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
