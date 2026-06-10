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


# ── Internal marker stripping (added 2026-06-09 — bug fix) ──────────────────

def test_strips_xin_xiaoxi_turn_marker():
    """minimax-M3 sometimes emits '新消息' as a turn boundary. Must be stripped."""
    result = fc.apply("新消息\n查询结果：共 3 条。")
    assert "新消息" not in result.text
    assert "查询结果：共 3 条。" in result.text


def test_strips_xin_huihua_marker():
    result = fc.apply("新会话\n你好")
    assert "新会话" not in result.text
    assert "你好" in result.text


def test_strips_system_role_marker():
    result = fc.apply("System: 内部状态\n用户回复")
    assert "System" not in result.text
    assert "用户回复" in result.text


def test_strips_human_assistant_markers():
    result = fc.apply("Human: 问\nAssistant: 答\n结论 OK")
    assert "Human" not in result.text
    assert "Assistant" not in result.text
    assert "结论 OK" in result.text


def test_strips_hr_separator():
    result = fc.apply("上段\n---\n下段")
    assert "---" not in result.text
    assert "上段" in result.text
    assert "下段" in result.text


# ── Hallucinated tool-call JSON stripping (added 2026-06-09 — bug fix) ──────

def test_strips_hallucinated_tool_call_json_tool_form():
    """When enabled_toolsets is misconfigured, LLM falls back to text-mode
    'function call' syntax. Must be stripped, not passed to the user."""
    text = (
        "查询台架架构\n"
        '{"tool": "list_architectures", "parameters": {}}\n'
        "查询可用台架"
    )
    result = fc.apply(text)
    assert '"tool"' not in result.text
    assert "list_architectures" not in result.text
    assert "查询台架架构" in result.text
    assert "查询可用台架" in result.text


def test_strips_hallucinated_tool_call_json_name_form():
    """OpenAI-style name/arguments variant."""
    text = (
        '好的，我来查。\n'
        '{"name": "list_available_benches", "arguments": {}}\n'
        "请稍等"
    )
    result = fc.apply(text)
    assert '"name"' not in result.text
    assert "list_available_benches" not in result.text
    assert "好的，我来查。" in result.text
    assert "请稍等" in result.text


def test_strips_multiline_pretty_printed_tool_json():
    """LLM often emits pretty-printed multi-line JSON when hallucinating tools.
    Must strip the whole block (all 4 lines), not just the opener line."""
    text = (
        "查询台架架构\n"
        "{\n"
        '  "tool": "list_available_benches",\n'
        '  "parameters": {}\n'
        "}\n"
        "请稍等"
    )
    result = fc.apply(text)
    assert '"tool"' not in result.text
    assert "list_available_benches" not in result.text
    assert "查询台架架构" in result.text
    assert "请稍等" in result.text
    # Whole JSON block (4 lines) gone, only 2 natural-language lines remain
    assert result.text.count("\n") <= 2


def test_does_not_strip_legitimate_json():
    """JSON that isn't shaped like a tool call (e.g. response data summary)
    must NOT be stripped — only the specific tool-call shape."""
    text = '查询完成：{"count": 3, "items": ["A", "B", "C"]}'
    result = fc.apply(text)
    # count/items shapes are different from tool/name+parameters/arguments
    assert result.text == text


def test_only_markers_after_strip_is_blocked():
    """If stripping leaves nothing, blocked (not silent empty send)."""
    result = fc.apply("新消息\n---\n新会话")
    assert result.blocked
    assert result.block_reason == "empty_after_strip"
