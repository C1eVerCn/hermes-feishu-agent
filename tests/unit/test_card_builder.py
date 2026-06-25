"""Tests for ocl/card_builder.py — the agent-path text→card renderer.

History: this file used to test bench/architecture/reservation/dry_run data
blocks, but those branches were dead code from the pre-merge bench domain and
were removed 2026-06-25 (the car domain renders structured data via
car_tools/card_builder instead). `build_card` now renders text only.
"""
from ocl.card_builder import build_card


def _div_texts(card):
    out = []
    for el in card["elements"]:
        if el.get("tag") == "div" and "text" in el:
            out.append(el["text"]["content"])
    return "\n".join(out)


def test_no_header():
    card = build_card("你好", [])
    assert "header" not in card


def test_summary_block_present():
    card = build_card("# 结果\n**好的**", [])
    assert "**结果**" in _div_texts(card)
    assert "**好的**" in _div_texts(card)


def test_builds_card_even_with_empty_captured():
    """Every LLM reply renders as a card, even when no tool ran."""
    card = build_card("你好", [])
    assert "elements" in card
    assert len(card["elements"]) >= 1
    # First element is the text div
    assert card["elements"][0]["tag"] == "div"


def test_builds_card_with_none_captured():
    """`captured` is optional / ignored — must not crash when omitted."""
    card = build_card("你好")
    assert card["elements"][0]["text"]["content"]


def test_renders_text_only_ignores_captured():
    """Captured tool results are no longer inspected; only text is rendered."""
    captured = [{"tool": "fetch_user_reservation", "result": {"code": 200, "data": []}}]
    card = build_card("您当前没有预约记录。", captured)
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    assert len(divs) == 1
    assert "没有预约记录" in _div_texts(card)
    # No action buttons are ever produced by the agent-path renderer.
    assert all(e.get("tag") != "action" for e in card["elements"])
