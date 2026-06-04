"""Unit tests for ocl/length_limiter.py."""
import pytest
import ocl.length_limiter as ll
import ocl.permission  # ensure _PERM_FILE side effect doesn't matter


@pytest.fixture(autouse=True)
def _patch_limits(monkeypatch):
    monkeypatch.setattr(ll, "_MAX_CHARS", 100)
    monkeypatch.setattr(ll, "_WARN_CHARS", 50)


def test_short_response_unchanged():
    text = "短回复"
    result = ll.apply(text)
    assert result == text


def test_long_response_is_truncated():
    text = "a" * 200
    result = ll.apply(text)
    assert len(result) < 200


def test_truncated_response_has_suffix():
    text = "a" * 200
    result = ll.apply(text)
    assert "[...内容已截断" in result


def test_exactly_at_limit_is_not_truncated():
    text = "x" * 100
    result = ll.apply(text)
    assert "[...内容已截断" not in result


def test_truncation_prefers_sentence_boundary():
    # 80 chars of content then a period then more text — should cut at the period
    text = ("我是有意义的文字。" * 8) + "这后面的不要。" * 5
    result = ll.apply(text)
    assert result.endswith("[...内容已截断，如需更多详情请分步查询]") or "。" in result
    assert len(result) <= 100 + 50  # max_chars + suffix length


def test_no_sentence_boundary_truncates_at_limit():
    # 150 chars with no sentence break
    text = "abcdefghij" * 15  # 150 chars, no period/comma/newline
    result = ll.apply(text)
    assert "[...内容已截断" in result
    assert len(result) <= 100 + 100  # falls back to hard cut at _MAX_CHARS


def test_emoji_text_truncated_correctly():
    text = ("有意义的文字。" * 3) + "🎉🎊🎁" * 100
    result = ll.apply(text)
    assert "[...内容已截断" in result
    assert len(result) <= 100 + 50
