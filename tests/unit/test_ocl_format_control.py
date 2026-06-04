"""Unit tests for ocl/format_control.py."""
import ocl.format_control as fc


def test_strips_leading_trailing_whitespace():
    result = fc.apply("  hello  ")
    assert result.text == "hello"
    assert not result.blocked


def test_empty_response_is_blocked():
    result = fc.apply("")
    assert result.blocked


def test_whitespace_only_response_is_blocked():
    result = fc.apply("   \n\t  ")
    assert result.blocked


def test_collapses_triple_blank_lines_to_double():
    text = "第一段\n\n\n\n第二段"
    result = fc.apply(text)
    assert "\n\n\n" not in result.text
    assert "第一段" in result.text
    assert "第二段" in result.text


def test_normal_text_passes_through_unchanged():
    text = "查询结果：共 3 条记录。\n\n- 张三\n- 李四"
    result = fc.apply(text)
    assert not result.blocked
    assert result.text == text


def test_unicode_preserved_in_text():
    # Zero-width chars and mixed unicode should not cause issues
    result = fc.apply("hello​world")  # zero-width space between chars
    assert not result.blocked
    assert "​" in result.text


def test_pure_special_chars_not_empty():
    result = fc.apply("❗❗❗")
    assert not result.blocked
    assert result.text == "❗❗❗"


def test_emoji_preserved():
    result = fc.apply("✅ 操作成功 🎉")
    assert not result.blocked
    assert "✅" in result.text
    assert "🎉" in result.text
