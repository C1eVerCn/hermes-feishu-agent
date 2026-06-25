"""tests for bot/intent_router — Tier-2 LLM 意图路由器（离线，monkeypatch _complete）。"""
import json
import pytest

from bot import intent_router
from bot.intent_router import RouteResult, classify, _normalize, _extract_json


@pytest.fixture
def fake_llm(monkeypatch):
    """monkeypatch _complete 返回指定 JSON 文本。"""
    holder = {"raw": "{}"}
    monkeypatch.setattr(intent_router, "_complete", lambda messages: holder["raw"])
    return holder


# ── JSON 抠取 ────────────────────────────────────────────────────────────
def test_extract_json_plain():
    assert _extract_json('{"intent":"book"}') == {"intent": "book"}


def test_extract_json_codeblock():
    assert _extract_json('```json\n{"intent":"book"}\n```') == {"intent": "book"}


def test_extract_json_with_noise():
    assert _extract_json('好的：{"intent":"book","confidence":0.9} 仅供参考')["intent"] == "book"


def test_extract_json_garbage():
    assert _extract_json("这不是 json") == {}


# ── 归一化（防漂移闸）─────────────────────────────────────────────────────
def test_normalize_invalid_intent_to_unknown():
    assert _normalize({"intent": "delete_database", "confidence": 0.9}).intent == "unknown"


def test_normalize_clamps_confidence():
    assert _normalize({"intent": "book", "confidence": 5}).confidence == 1.0
    assert _normalize({"intent": "book", "confidence": -1}).confidence == 0.0


def test_normalize_filters_unknown_slots():
    r = _normalize({"intent": "book", "confidence": 0.9,
                    "slots": {"vehicle_no": "PNV332", "evil_field": "x",
                              "emailAddress": "a@b.com"}})
    assert r.slots == {"vehicle_no": "PNV332"}  # 只保留白名单槽位


def test_normalize_platform_must_be_valid():
    r = _normalize({"intent": "book", "confidence": 0.9,
                    "slots": {"platform": "Xavier"}})
    assert r.slots["platform"] == "Xavier"
    r2 = _normalize({"intent": "book", "confidence": 0.9,
                     "slots": {"platform": "FakeChip"}})
    assert "platform" not in r2.slots


def test_normalize_duration_coerced_to_int():
    r = _normalize({"intent": "book", "confidence": 0.9,
                    "slots": {"duration_minutes": "120"}})
    assert r.slots["duration_minutes"] == 120


def test_normalize_slots_only_for_book():
    r = _normalize({"intent": "query_vehicles", "confidence": 0.9,
                    "slots": {"vehicle_no": "PNV332"}})
    assert r.slots == {}  # 非 slot-intent 不保留槽位


# ── mutation 槽位（cancel/return/approve）────────────────────────────────
def test_normalize_cancel_keeps_identifier():
    r = _normalize({"intent": "cancel", "confidence": 0.9,
                    "slots": {"vehicle_no": "PNV332", "vehicle_type_detail": "DM2"}})
    assert r.slots == {"vehicle_no": "PNV332"}  # 只留 mutation 白名单键


def test_normalize_approve_coerces_approved():
    assert _normalize({"intent": "approve", "confidence": 0.9,
                       "slots": {"vehicle_no": "PNV1", "approved": "批准"}}).slots["approved"] is True
    assert _normalize({"intent": "approve", "confidence": 0.9,
                       "slots": {"vehicle_no": "PNV1", "approved": "驳回"}}).slots["approved"] is False
    assert _normalize({"intent": "approve", "confidence": 0.9,
                       "slots": {"vehicle_no": "PNV1", "approved": True}}).slots["approved"] is True
    # 无法判定 → 丢弃该槽位
    assert "approved" not in _normalize({"intent": "approve", "confidence": 0.9,
                                         "slots": {"vehicle_no": "PNV1", "approved": "也许吧"}}).slots


def test_normalize_cancel_by_reservation_id():
    r = _normalize({"intent": "cancel", "confidence": 0.9,
                    "slots": {"reservation_id": "R-123"}})
    assert r.slots == {"reservation_id": "R-123"}


# ── is_confident ─────────────────────────────────────────────────────────
def test_is_confident():
    assert RouteResult(intent="book", confidence=0.8).is_confident
    assert not RouteResult(intent="book", confidence=0.4).is_confident
    assert not RouteResult(intent="unknown", confidence=0.99).is_confident


# ── classify 端到端（mocked LLM）──────────────────────────────────────────
def test_classify_book_with_slots(fake_llm):
    fake_llm["raw"] = json.dumps({
        "intent": "book",
        "slots": {"vehicle_type_detail": "DM2", "platform": "Orin",
                  "duration_minutes": 120, "task_name": "标定"},
        "confidence": 0.9,
    })
    r = classify("明天想用俩小时那辆DM2的Orin车跑个标定")
    assert r.intent == "book"
    assert r.slots["vehicle_type_detail"] == "DM2"
    assert r.slots["platform"] == "Orin"
    assert r.slots["duration_minutes"] == 120
    assert r.is_confident


def test_classify_query(fake_llm):
    fake_llm["raw"] = '{"intent":"query_reservations","confidence":0.95}'
    assert classify("我最近都约了啥来着").intent == "query_reservations"


def test_classify_chitchat(fake_llm):
    fake_llm["raw"] = '{"intent":"chitchat","confidence":0.99}'
    assert classify("今天天气咋样").intent == "chitchat"


def test_classify_fails_open_on_llm_error(monkeypatch):
    def boom(messages):
        raise RuntimeError("network down")
    monkeypatch.setattr(intent_router, "_complete", boom)
    r = classify("帮我整辆车")
    assert r.intent == "unknown"  # fail-open
    assert not r.is_confident


def test_classify_empty_text():
    assert classify("").intent == "unknown"
