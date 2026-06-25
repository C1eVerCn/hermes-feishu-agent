"""Render the LLM **agent path** reply as a Feishu interactive card.

The deterministic car-domain cards (vehicle list / records / confirm / success)
are built in `car_tools/card_builder.py` and rendered by the fast-path / FSM /
card-action handlers — NOT here. This module only handles the agent (LLM) path:
it wraps the model's final text into a single-element card so every reply renders
with the same visual style as the intent-path replies (user-facing requirement
2026-06-10).

History: this module used to inline bench / architecture / reservation data
blocks from captured tool results, but that was the pre-merge **bench** domain.
The car domain never registered those tool names (`list_my_reservations`,
`list_available_benches`, `dry_run_reserve_bench`, …), so every special branch
here was dead. Removed 2026-06-25 — `build_card` now renders text only. The
`captured` parameter is kept for call-site compatibility (`ocl.pipeline` passes
it positionally) but is no longer inspected.
"""
from ocl.markdown_to_lark import to_lark_md


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def build_card(text: str, captured: list[dict] | None = None) -> dict:
    """Render the LLM's final text as a single-element Feishu card.

    Always returns a valid card, even for empty text. Called by `ocl.pipeline`
    for every agent-path reply. `captured` is unused (see module docstring).
    """
    return {
        "config": {"wide_screen_mode": True},
        "elements": [_div(to_lark_md(text))],
    }
