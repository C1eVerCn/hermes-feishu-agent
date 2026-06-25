"""tests for bot/intent — 意图识别单一事实源。

合并了原 test_handler_fsm_integration.test_booking_intent_recognition_regex 的
30 个用例，但现在直接调 ``intent.is_booking_intent``（线上代码本身），而非测试里
重新实现一遍正则（旧测试用单反斜杠 reimplement，掩盖了 handler 双反斜杠 BUG）。
"""
from bot import intent


# ── escape / confirm ───────────────────────────────────────────────────
def test_escape_phrases_union():
    # handler 旧集合
    for p in ("算了", "换个", "不订了", "取消", "放弃", "不要了"):
        assert intent.is_escape(p), p
    # fsm 旧集合
    for p in ("退出", "不约了", "不选了"):
        assert intent.is_escape(p), p


def test_escape_normalizes_quotes_and_case():
    assert intent.is_escape("「算了」")
    assert intent.is_escape("  取消  ")


def test_escape_does_not_match_long_sentence():
    # 含"取消"但整句不等于 → 不误伤（精确整句匹配）
    assert not intent.is_escape("我要取消我的预约记录")


def test_confirm_phrases():
    for p in ("确认", "确定", "OK", "yes"):
        assert intent.is_confirm(p), p
    assert not intent.is_confirm("确认一下我的预约")


# ── 车辆编号 ─────────────────────────────────────────────────────────────
def test_vehicle_id_requires_digit():
    assert intent.looks_like_vehicle_id("SNV018")
    assert intent.looks_like_vehicle_id("TTVX25SPV009")
    assert intent.looks_like_vehicle_id("苏EAM0769")
    # 纯字母无数字 → 拒绝（修正 handler 旧 is_vehicle_id 漂移）
    assert not intent.looks_like_vehicle_id("ABCDE")
    # 纯中文 → 拒绝
    assert not intent.looks_like_vehicle_id("待审批记录列")
    # 太短
    assert not intent.looks_like_vehicle_id("PN1")


def test_pure_vehicle_id_case_insensitive():
    assert intent.is_pure_vehicle_id("snv018")
    assert not intent.is_pure_vehicle_id("我想约 SNV018")  # 不是整句


def test_extract_embedded_vehicle_id():
    assert intent.extract_embedded_vehicle_id("我想约一下TTVX25SPV009") == "TTVX25SPV009"
    assert intent.extract_embedded_vehicle_id("帮我约 PNV332 这辆") == "PNV332"
    assert intent.extract_embedded_vehicle_id("看下我的待审批记录") == ""


# ── booking 意图（30 用例，移植自旧 handler 测试）─────────────────────────
def test_booking_intent_recognition():
    cases = [
        # 标准变体
        ("我想约车", True), ("我要约车", True), ("我想要约一辆车", True),
        ("帮我约一下车", True), ("想预约车辆", True),
        ("请问能帮我预约一下车辆吗", True),
        ("嗯我想约车", True), ("那个我想约车", True),
        ("可以帮我约车吗", True), ("我现在想约个车", True),
        ("帮我约一辆", True), ("我想预定车", True),
        # 否定
        ("我不想约车", False), ("别约车了", False), ("取消预约", False),
        ("不要约车", False), ("算了不约了", False),
        # 不相关
        ("随便看看", False), ("车有问题", False),
        ("一辆车多少钱", False), ("我要叫车", False),
        ("帮我叫车", False), ("一辆车坏了", False),
        ("车要保养", False),
    ]
    failed = [(t, e, g) for t, e in cases if (g := intent.is_booking_intent(t)) != e]
    assert not failed, f"booking intent mismatches: {failed}"


def test_booking_intent_vehicle_id_and_type():
    assert intent.is_booking_intent("SNV018")        # 纯编号
    assert intent.is_booking_intent("DM2")           # 车型关键字
    assert intent.is_booking_intent("Xavier")        # 平台关键字
    assert intent.is_booking_intent("我想约一下TTVX25SPV009")  # 句中编号


def test_booking_intent_suppressed_by_action_words():
    """含取消/归还/审批动作词时不判为 booking（即使句中含车辆编号）→ 交 Tier-2。"""
    assert not intent.is_booking_intent("把我那个PNV332的预约取消掉")
    assert not intent.is_booking_intent("归还PNV332")
    assert not intent.is_booking_intent("批准张三的PNV332预约")
    assert not intent.is_booking_intent("驳回PNV332")


# ── 查询快速路径 ─────────────────────────────────────────────────────────
def test_match_query_vehicles():
    r = intent.match_query("查可用车辆")
    assert r and r[0] == "fetch_available_vehicles"
    r = intent.match_query("查询车辆")
    assert r and r[0] == "fetch_available_vehicles"


def test_match_query_with_type_filter():
    r = intent.match_query("DM2有什么车")
    assert r and r[0] == "fetch_available_vehicles"
    assert r[1].get("vehicleType") == "DM2"


def test_match_query_my_reservations():
    for t in ("我的预约", "查看一下我的预约", "看看我的预约记录"):
        r = intent.match_query(t)
        assert r and r[0] == "fetch_user_reservation", t


def test_match_query_my_approvals():
    for t in ("我的待审批", "看一下我的待审批列表", "查我的审批记录"):
        r = intent.match_query(t)
        assert r and r[0] == "fetch_user_approval", t


def test_match_query_none_for_booking():
    assert intent.match_query("我想约车") is None


def test_fsm_escape_query_excludes_vehicle_query():
    # 车辆查询在 FSM 中不算 escape（用户可能想进 booking）
    assert not intent.match_query_intent_during_fsm("查可用车辆")
    # 我的预约 / 待审批 算 escape
    assert intent.match_query_intent_during_fsm("我的预约")
    assert intent.match_query_intent_during_fsm("我的待审批")
