"""Unit tests for ocl/pipeline.py."""
import pytest
from unittest.mock import patch
import ocl.pipeline as pipeline
from ocl.pipeline import OclResult


def test_clean_response_passes_pipeline():
    result = pipeline.apply("用户列表：张三、李四、王五", "ou_user")
    assert not result.blocked
    assert "张三" in result.text


def test_empty_response_is_blocked():
    result = pipeline.apply("", "ou_user")
    assert result.blocked
    assert result.block_reason == "empty_response"


def test_whitespace_only_response_is_blocked():
    result = pipeline.apply("   \n  ", "ou_user")
    assert result.blocked


def test_content_block_returns_blocked_result():
    result = pipeline.apply("这涉及政党的争议问题", "ou_user")
    assert result.blocked
    assert "政党" in "这涉及政党的争议问题"  # ensure our trigger word is present


def test_long_response_is_truncated_not_blocked():
    long_text = "这是一段很长的回复内容。" * 500
    with patch("ocl.length_limiter._MAX_CHARS", 100):
        result = pipeline.apply(long_text, "ou_user")
    assert not result.blocked
    assert "[...内容已截断" in result.text


def test_pipeline_exception_fails_open(monkeypatch):
    """If an internal error occurs, pipeline returns the original text (fail-open)."""
    import ocl.format_control as fc
    monkeypatch.setattr(fc, "apply", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    result = pipeline.apply("正常回复", "ou_user")
    # fail-open: text is passed through, not blocked
    assert not result.blocked
    assert result.text == "正常回复"


def test_format_block_short_circuits_before_content():
    """Empty response should not reach content_filter."""
    import ocl.content_filter as cf
    calls = []
    orig_check = cf.check

    def tracking_check(text):
        calls.append(text)
        return orig_check(text)

    with patch.object(cf, "check", side_effect=tracking_check):
        result = pipeline.apply("", "ou_user")
    assert result.blocked
    assert result.block_reason == "empty_response"
    assert len(calls) == 0  # content_filter never called


def test_content_block_returns_block_message(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "OCL_CONTENT_BLOCK_MESSAGE", "自定义封锁提示")
    result = pipeline.apply("这涉及政党的争议问题", "ou_user")
    assert result.blocked
    assert result.text == "自定义封锁提示"


def test_content_block_has_higher_priority_than_length():
    """A blocked response should not be truncated — block takes priority."""
    long_blocked = ("政党" * 30) + ("very long safe text after the trigger. " * 100)
    with patch("ocl.length_limiter._MAX_CHARS", 50):
        result = pipeline.apply(long_blocked, "ou_user")
    assert result.blocked
    assert "[...内容已截断" not in result.text  # truncation never ran


# ── B5: interactive card ─────────────────────────────────────────────────────

def test_apply_returns_card_with_captured():
    captured = [{"tool": "fetch_user_reservation", "result": {"code": 200, "data": [
        {"vehicleNo": "PNV332", "startTime": "2099-01-01 09:00:00", "endTime": "2099-01-01 10:00:00",
         "taskName": "t", "status": "待审批"}]}}]
    res = pipeline.apply("您有1条预约", "ou_x", captured=captured)
    assert res.card is not None


def test_apply_blocked_has_no_card():
    res = pipeline.apply("", "ou_x", captured=[])
    assert res.blocked is True
    assert res.card is None


def test_apply_plain_text_still_builds_card_without_action():
    """Plain text WITH structured captured data → card."""
    res = pipeline.apply("**你好**", "ou_x", captured=[
        {"tool": "fetch_available_vehicles", "result": {"code": 200, "data": ["PNV332"]}}
    ])
    assert res.card is not None


def test_apply_always_builds_card_even_without_captured():
    """User-facing requirement 2026-06-10: every LLM reply renders as a card,
    even when no tool ran. Empty captured → single-element card."""
    res = pipeline.apply("**你好**", "ou_x", captured=[])
    assert res.card is not None
    # Single-element card: text div only, no buttons / hr / data block
    assert len(res.card["elements"]) == 1
    assert res.card["elements"][0]["tag"] == "div"
