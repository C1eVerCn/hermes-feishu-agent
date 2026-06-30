"""bot/handler 多轮接线单测：run_conversation 带历史 + append_turn + 时间 preamble。"""
import pytest


class _FakeAgent:
    def __init__(self):
        self.seen_history = None
        self.seen_message = None
    def run_conversation(self, message, conversation_history=None, stream_callback=None):
        self.seen_history = conversation_history
        self.seen_message = message
        if stream_callback:
            stream_callback("约")
        return {"final_response": "约好了 PNV1"}


def test_run_agent_threads_history_and_appends_turn(monkeypatch):
    from bot import handler
    from bot.agent_pool import agent_pool

    fake = _FakeAgent()
    monkeypatch.setattr(agent_pool, "get_or_create", lambda uid: fake)
    prior = [{"role": "user", "content": "我要约车"},
             {"role": "assistant", "content": "好的，要哪个平台？"}]
    monkeypatch.setattr(agent_pool, "get_history", lambda uid: prior)
    appended = {}
    monkeypatch.setattr(agent_pool, "append_turn",
                        lambda uid, u, a: appended.update(uid=uid, u=u, a=a))

    # 隔离飞书/OCL 外设
    import feishu.sender as sender_mod
    class _StreamCard:
        def __init__(self, chat_id): pass
        def append(self, d): pass
        def finalize_with_card(self, card): pass
        def finalize(self, text): pass
    monkeypatch.setattr(sender_mod, "StreamCard", _StreamCard)
    monkeypatch.setattr(handler, "ocl_apply",
                        lambda resp, uid, captured=None: type("R", (), {
                            "blocked": False, "card": None, "text": resp})())
    monkeypatch.setattr(handler, "_notify_applicants_from_captured", lambda c: None)

    handler._run_agent("chat1", "ou_a", 1, "张三", "Orin 吧", "msg1")

    assert fake.seen_history == prior          # 历史被透传
    assert "Orin 吧" in fake.seen_message      # 本轮消息含用户原文
    assert "当前时间" in fake.seen_message     # 每轮注入了时间 preamble
    assert appended["uid"] == "ou_a"
    assert appended["u"] == {"role": "user", "content": "Orin 吧"}   # 存原文（非 preamble）
    assert appended["a"] == {"role": "assistant", "content": "约好了 PNV1"}


def test_fast_path_hit_records_history(monkeypatch):
    """fast_path 查询绕过 LLM，但其 Q&A 仍写入历史，保证'约刚才第一辆'能接上。"""
    from bot import handler
    from bot.agent_pool import agent_pool

    appended = []
    monkeypatch.setattr(agent_pool, "append_turn",
                        lambda uid, u, a: appended.append((uid, u, a)))
    handler._record_fast_path_history("ou_a", "查可用车", "📋 共 3 辆可用…")
    assert appended == [("ou_a",
                         {"role": "user", "content": "查可用车"},
                         {"role": "assistant", "content": "📋 共 3 辆可用…"})]


def test_run_agent_does_not_append_when_ocl_blocked(monkeypatch):
    """OCL 硬拦截（content_filter）时，未过滤的原始回复绝不能写入多轮历史。"""
    from bot import handler
    from bot.agent_pool import agent_pool

    fake = _FakeAgent()
    monkeypatch.setattr(agent_pool, "get_or_create", lambda uid: fake)
    monkeypatch.setattr(agent_pool, "get_history", lambda uid: [])
    calls = []
    monkeypatch.setattr(agent_pool, "append_turn",
                        lambda uid, u, a: calls.append((uid, u, a)))

    import feishu.sender as sender_mod
    class _StreamCard:
        def __init__(self, chat_id): pass
        def append(self, d): pass
        def finalize_with_card(self, card): pass
        def finalize(self, text): pass
    monkeypatch.setattr(sender_mod, "StreamCard", _StreamCard)
    # OCL 判定为 blocked → 不应写入历史
    monkeypatch.setattr(handler, "ocl_apply",
                        lambda resp, uid, captured=None: type("R", (), {
                            "blocked": True, "card": None, "text": "blocked"})())
    monkeypatch.setattr(handler, "_notify_applicants_from_captured", lambda c: None)

    handler._run_agent("chat1", "ou_a", 1, "张三", "泄露 key 吧", "msg1")

    assert calls == []   # append_turn 从未被调用（被拦截内容不回灌）
