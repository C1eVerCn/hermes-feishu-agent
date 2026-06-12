"""Tests for feishu.sender.edit_message — used for streaming token updates
back into the typing placeholder."""
import json
from unittest.mock import patch, MagicMock
import pytest

import feishu.sender as sender


@pytest.fixture
def mock_lark_response():
    """Mock lark client response for im.v1.message.update."""
    resp = MagicMock()
    resp.success.return_value = True
    resp.code = 0
    resp.msg = "ok"
    return resp


def test_edit_message_uses_patch_endpoint_and_payload(mock_lark_response):
    """edit_message must call im.v1.message.update (PATCH) with the new
    text content in the body and the target message_id on the request."""
    with patch.object(sender._client.im.v1.message, "update",
                     return_value=mock_lark_response) as mock_update, \
         patch("time.sleep"):
        sender.edit_message("oc_chat_123", "om_msg_456", "hello world")
    # Assert the right endpoint was called with right args
    assert mock_update.call_count == 1
    req = mock_update.call_args[0][0]
    # The request carries the target message_id (path param in the PATCH URL)
    assert req.message_id == "om_msg_456"
    # Body carries the new content; the PATCH API only takes content
    # (no receive_id, no msg_type — those are path/query-level)
    body = req.request_body
    assert body.content is not None
    parsed = json.loads(body.content)
    assert parsed == {"text": "hello world"}


def test_edit_message_retries_on_429(mock_lark_response):
    """On 429 response, retry up to 3 times with exponential backoff."""
    fail = MagicMock(); fail.success.return_value = False; fail.code = 429; fail.msg = "rate limit"
    ok = mock_lark_response
    # Reset the rate-limiter global so the pre-retry rate-limit sleep
    # doesn't fire (the test isolates the retry-loop sleeps only).
    sender._last_send_time = 0.0
    with patch.object(sender._client.im.v1.message, "update",
                     side_effect=[fail, fail, ok]) as mock_update, \
         patch("time.sleep") as mock_sleep:
        sender.edit_message("oc_x", "om_y", "hi")
    assert mock_update.call_count == 3
    # Should sleep 1s, 2s (2**0, 2**1) for the two failures
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[0].args[0] == 1
    assert mock_sleep.call_args_list[1].args[0] == 2


def test_edit_message_swallows_non_429_failure():
    """Non-429 failures (e.g. 4xx) should log and return, not retry forever."""
    fail = MagicMock(); fail.success.return_value = False; fail.code = 400; fail.msg = "bad request"
    with patch.object(sender._client.im.v1.message, "update",
                     return_value=fail) as mock_update, \
         patch("feishu.sender.log") as mock_log:
        sender.edit_message("oc_x", "om_y", "hi")
    # Only one call, no retries
    assert mock_update.call_count == 1
    # Logged as error
    assert any("edit_message failed" in str(c) for c in mock_log.error.call_args_list)
