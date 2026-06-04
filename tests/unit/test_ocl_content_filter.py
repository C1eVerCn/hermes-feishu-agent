"""Unit tests for ocl/content_filter.py."""
import pytest
import ocl.content_filter as cf


def test_clean_response_passes():
    result = cf.check("用户列表已为您查询，共 3 条记录。")
    assert not result.blocked


def test_blocked_keyword_triggers_block():
    result = cf.check("这涉及政党的问题，我来解释一下。")
    assert result.blocked
    assert result.reason


def test_api_key_pattern_triggers_block():
    result = cf.check("你的密钥是 sk-abcdefghijklmnopqrstuvwxyz123456")
    assert result.blocked


def test_bearer_token_pattern_triggers_block():
    result = cf.check("请使用 Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def 进行认证")
    assert result.blocked


def test_warn_pattern_passes_through():
    result = cf.check("根据我的训练数据，这个问题的答案是……")
    assert not result.blocked  # warn only, not block


def test_empty_response_not_blocked():
    result = cf.check("")
    assert not result.blocked  # empty is handled by format_control, not content filter


def test_mixed_case_political_keyword_blocked():
    result = cf.check("关于政党的分析报告")
    assert result.blocked


def test_keyword_in_code_block_also_blocked():
    # Content filter doesn't care about context — it scans the whole text
    result = cf.check("```python\n# 这是一个政党的代码示例\nprint('hello')\n```")
    assert result.blocked


def test_api_key_with_newline_not_matched_as_blocked():
    # sk- followed by < 20 chars should NOT trigger block
    result = cf.check("请使用 sk-short-key 进行")
    assert not result.blocked


def test_multiple_triggers_only_first_reported():
    result = cf.check("政党 sk-abcdefghijklmnopqrstuvwxyz123456")
    assert result.blocked
    assert result.reason in ("political_sensitive", "api_key_leak")
