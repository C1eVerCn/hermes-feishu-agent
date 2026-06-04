from ocl.markdown_to_lark import to_lark_md


def test_headers_become_bold():
    assert to_lark_md("# 标题") == "**标题**"
    assert to_lark_md("### 小标题") == "**小标题**"


def test_bold_preserved():
    assert "**粗**" in to_lark_md("这是 **粗** 字")


def test_bullets_preserved():
    out = to_lark_md("- a\n- b")
    assert "- a" in out and "- b" in out


def test_code_fence_stripped():
    out = to_lark_md("```\nx = 1\n```")
    assert "```" not in out
    assert "x = 1" in out


def test_multiple_blank_lines_collapsed():
    out = to_lark_md("a\n\n\n\nb")
    assert "\n\n\n" not in out
