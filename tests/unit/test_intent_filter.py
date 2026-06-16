"""Tests for ocl.intent_filter — OCL pipeline 的闲聊脱敏闸。"""
from ocl import intent_filter


def test_weather_query_is_redirected():
    """用户原话：「今天天气如何」→ 命中「天气」+ 无领域词 → 拦截。"""
    r = intent_filter.check("今天天气如何，我需要穿外套吗？")
    assert r.redirected is True
    assert r.matched_marker == "天气"


def test_arithmetic_query_is_redirected():
    """用户原话：「1+1=？」→ 命中「1+1」+ 无领域词 → 拦截。"""
    r = intent_filter.check("1+1 等于几")
    assert r.redirected is True
    assert r.matched_marker == "1+1"


def test_joke_request_is_redirected():
    r = intent_filter.check("讲个笑话")
    assert r.redirected is True


def test_poem_request_is_redirected():
    r = intent_filter.check("给我写一首关于秋天的诗")
    assert r.redirected is True


def test_business_answer_with_marker_word_is_not_redirected():
    """「台架」和「天气」同时出现（不可能但保证安全）→ 领域词胜出，不拦截。"""
    r = intent_filter.check("TJ001 台架今天天气情况")
    # 含领域词「台架」「TJ001」 → 透传
    assert r.redirected is False


def test_pure_business_answer_not_redirected():
    """纯业务回复即使没命中闲聊标记也透传。"""
    r = intent_filter.check("当前可用台架有 TJ001 / TJ002。")
    assert r.redirected is False


def test_response_with_only_chitchat_marker_redirected():
    """「笑话」一个词 → 拦截。"""
    r = intent_filter.check("来个笑话")
    assert r.redirected is True


def test_approval_business_response_with_status_words_not_redirected():
    """业务回复常见字段：时间、状态、审批 → 不应触发。"""
    r = intent_filter.check("您有 1 条预约待审批，开始时间是 2026-07-01 09:00:00。")
    assert r.redirected is False


def test_pretend_off_topic_with_chinese_punctuation():
    r = intent_filter.check("天气怎么样？")
    assert r.redirected is True


def test_empty_response_not_redirected():
    """空响应在 format_control 已经处理，本模块不脱敏。"""
    r = intent_filter.check("")
    assert r.redirected is False


def test_redirect_message_is_human_friendly():
    """引导话术必须包含可办事项 + 可复制例句（用户要求「引导到正常流程」）。"""
    msg = intent_filter.REDIRECT_MESSAGE
    assert "台架" in msg and "VLM" in msg
    assert "查询" in msg and "预约" in msg
    # 至少一个可复制的例句（以「预约」开头）
    assert "预约" in msg


def test_no_llm_self_reference_redirects():
    """LLM 自报「我是一个AI」之类不算闲聊，业务无关键词时仍放行（避免与 content_filter 重复）。"""
    r = intent_filter.check("好的，明白了。")
    assert r.redirected is False
