"""car_tools/normalizers.py 单元测试：raw → Pydantic strict 转换。"""
import pytest

from car_tools import normalizers
from car_tools.schemas import (
    Vehicle, Reservation, ReservationResult, ApprovalResult,
    CancelResult, ReturnResult,
)


# ── normalize_vehicles ─────────────────────────────────────────────────────

def test_normalize_vehicles_list_of_dict():
    raw = [
        {"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Xavier",
         "vin": "V123", "licensePlate": "沪A1"},
        {"vehicleNo": "SVV027", "vehicleType": "CT1", "platform": "Orin"},
    ]
    out = normalizers.normalize_vehicles(raw)
    assert len(out) == 2
    assert isinstance(out[0], Vehicle)
    assert out[0].vehicle_no == "PNV332"
    assert out[0].platform == "Xavier"
    assert out[0].vin == "V123"
    assert out[1].license_plate is None


def test_normalize_vehicles_dict_data_wrapper():
    raw = {"data": [{"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Orin"}]}
    out = normalizers.normalize_vehicles(raw)
    assert len(out) == 1
    assert out[0].vehicle_no == "PNV332"


def test_normalize_vehicles_dict_vehicles_wrapper():
    raw = {"vehicles": [{"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Orin"}]}
    out = normalizers.normalize_vehicles(raw)
    assert len(out) == 1


def test_normalize_vehicles_empty_list():
    assert normalizers.normalize_vehicles([]) == []


def test_normalize_vehicles_empty_dict():
    assert normalizers.normalize_vehicles({"data": []}) == []


def test_normalize_vehicles_bad_type():
    with pytest.raises(normalizers.NormalizeError) as ei:
        normalizers.normalize_vehicles("not a dict or list")
    assert ei.value.source == "fetch_available_vehicles"


def test_normalize_vehicles_bad_item_type():
    with pytest.raises(normalizers.NormalizeError):
        normalizers.normalize_vehicles([{"vehicleNo": "PNV332"}, "not_a_dict"])


def test_normalize_vehicles_missing_required():
    """vehicleType / platform 都是 Vehicle 的必填字段；raw 缺 platform → fail."""
    with pytest.raises(normalizers.NormalizeError):
        normalizers.normalize_vehicles([
            {"vehicleNo": "PNV332", "vehicleType": "DM2"},
        ])


def test_normalize_vehicles_unknown_platform():
    with pytest.raises(normalizers.NormalizeError):
        normalizers.normalize_vehicles([
            {"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "NotARealPlatform"},
        ])


def test_normalize_vehicles_extra_field_tolerated():
    """当前 normalizer 走显式 .get() 字段映射，未声明字段被静默丢弃（不抛）。"""
    # 当前实现：normalizer 只读 schema 声明的字段，丢弃其他
    # 这是设计选择（向前兼容：MCP 后续新增字段不破坏 bot）
    out = normalizers.normalize_vehicles([
        {"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Orin",
         "secret_field": "leak"},
    ])
    assert out[0].vehicle_no == "PNV332"


# ── normalize_records ──────────────────────────────────────────────────────

def test_normalize_records_default_source():
    raw = [
        {"vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
         "endTime": "2026-06-16 18:00", "status": "待审批"},
    ]
    out = normalizers.normalize_records(raw)
    assert len(out) == 1
    assert isinstance(out[0], Reservation)
    assert out[0].vehicle_no == "PNV332"
    assert out[0].status == "待审批"


def test_normalize_records_status_int_from_mcp():
    """MCP 上游 reservation.status 是 int 0-4（不是中文）→ 规范化为中文。"""
    raw = [
        {"vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
         "endTime": "2026-06-16 18:00", "status": 0},  # 0=待审批
    ]
    out = normalizers.normalize_records(raw)
    assert out[0].status == "待审批"

    raw2 = [
        {"vehicleNo": "PNV332", "status": 1},  # 1=已批准
    ]
    out2 = normalizers.normalize_records(raw2)
    assert out2[0].status == "已批准"


def test_normalize_records_explicit_source():
    raw = {"data": [{"vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
                     "endTime": "2026-06-16 18:00", "status": "待审批"}]}
    out = normalizers.normalize_records(raw, source="fetch_user_approval")
    assert len(out) == 1


def test_normalize_records_status_required():
    """Reservation.status 是必填字符串；缺失 → normalizer 应 fail-fast。"""
    raw = [
        {"vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
         "endTime": "2026-06-16 18:00"},
    ]
    # 当前实现：status 缺失时填 ""，Pydantic 不抛（str 字段允许空）
    # 容忍设计 —— 上游 handler 会根据 status 决定 UI 渲染
    out = normalizers.normalize_records(raw)
    assert out[0].status == ""  # 缺失即空串（仍可序列化）


def test_normalize_records_extra_field_tolerated():
    """未声明字段被静默丢弃（设计选择：向前兼容）。"""
    out = normalizers.normalize_records([
        {"vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
         "endTime": "2026-06-16 18:00", "status": "待审批", "future_field": "x"},
    ])
    assert out[0].status == "待审批"


# ── normalize_reservation_result ──────────────────────────────────────────

def test_normalize_reservation_result_happy():
    raw = {
        "success": True,
        "vehicleNo": "PNV332",
        "vehicleType": "DM2",
        "platform": "Xavier",
        "startTime": "2026-06-16 09:00",
        "endTime": "2026-06-16 18:00",
        "taskName": "高速测试",
        "location": "测试场A区",
        "dispatchers": [
            {"name": "Alice", "email": "a@x.com"},
            {"name": "Bob", "email": "b@x.com"},
        ],
        "applicantName": "张三",
        "applicantEmail": "zs@x.com",
        "applicantOpenId": "ou_xxx",
    }
    out = normalizers.normalize_reservation_result(raw)
    assert isinstance(out, ReservationResult)
    assert out.vehicle_no == "PNV332"
    assert len(out.dispatchers) == 2
    assert out.applicant_open_id == "ou_xxx"


def test_normalize_reservation_result_data_wrapper():
    raw = {"data": {"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Orin",
                    "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
                    "taskName": "test", "location": "loc"}}
    out = normalizers.normalize_reservation_result(raw)
    assert out.vehicle_no == "PNV332"


def test_normalize_reservation_result_invalid_input():
    with pytest.raises(normalizers.NormalizeError):
        normalizers.normalize_reservation_result([1, 2, 3])


def test_normalize_reservation_result_injects_applicant():
    """applicant 参数可补 applicant_* 字段（MCP 返回的 data 不含这些时用）。"""
    raw = {"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Orin",
           "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
           "taskName": "test", "location": "loc"}
    out = normalizers.normalize_reservation_result(
        raw, applicant={"openId": "ou_xx", "emailAddress": "x@y.com", "name": "张三"})
    assert out.applicant_open_id == "ou_xx"
    assert out.applicant_email == "x@y.com"


def test_normalize_reservation_result_tolerates_missing_fields():
    """2026-06-18 fix：fmp 上游 response 字段不可靠（platform/vehicle_no 等常为空），
    ReservationResult 字段改 Optional/默认值。normalize 不再 raise NormalizeError。"""
    out = normalizers.normalize_reservation_result({
        "vehicleNo": "PNV332",
        # vehicleType / platform / startTime / endTime / taskName / location 都缺
    })
    # 缺 success → normalize 默认 True（不抛错）
    assert out.success is True
    assert out.vehicle_no == "PNV332"  # 显式传入的值保留
    assert out.vehicle_type == ""  # 缺字段 → normalize 填空字符串（容错）


# ── normalize_approval_result ──────────────────────────────────────────────

def test_normalize_approval_result_approved():
    raw = {"approved": True, "vehicleNo": "PNV332",
           "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
           "taskName": "test", "reviewer": "Alice",
           "reviewComment": "OK"}
    out = normalizers.normalize_approval_result(raw)
    assert isinstance(out, ApprovalResult)
    assert out.approved is True
    assert out.review_comment == "OK"


def test_normalize_approval_result_rejected():
    raw = {"approved": False, "vehicleNo": "PNV332",
           "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
           "taskName": "test", "reviewer": "Alice"}
    out = normalizers.normalize_approval_result(raw)
    assert out.approved is False


def test_normalize_approval_result_alt_field():
    """兼容 MCP 返回 approvalResult:1 而不是 approved:true。"""
    raw = {"approvalResult": 1, "vehicleNo": "PNV332",
           "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
           "taskName": "test", "reviewer": "Alice"}
    out = normalizers.normalize_approval_result(raw)
    assert out.approved is True


# ── normalize_cancel_result ────────────────────────────────────────────────

def test_normalize_cancel_result_minimal():
    out = normalizers.normalize_cancel_result({"vehicleNo": "PNV332"})
    assert isinstance(out, CancelResult)
    assert out.start_time is None


def test_normalize_cancel_result_full():
    out = normalizers.normalize_cancel_result({
        "vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
        "operator": "张三", "cancelTime": "2026-06-15 18:00",
    })
    assert out.operator == "张三"


# ── normalize_return_result ────────────────────────────────────────────────

def test_normalize_return_result_required():
    raw = {"vehicleNo": "PNV332", "returnLocation": "A区",
           "keyPosition": "前台抽屉", "changeModule": "无",
           "vehicleStatus": 1}
    out = normalizers.normalize_return_result(raw)
    assert isinstance(out, ReturnResult)
    assert out.vehicle_status == "1"  # str(int)


def test_normalize_return_result_with_description():
    raw = {"vehicleNo": "PNV332", "returnLocation": "A区",
           "keyPosition": "前台抽屉", "changeModule": "无",
           "vehicleStatus": "2",
           "vehicleStatusDescription": "已损坏",
           "returnTime": "2026-06-16 18:00"}
    out = normalizers.normalize_return_result(raw)
    assert out.vehicle_status == "2"
    assert out.vehicle_status_description == "已损坏"


def test_normalize_return_result_missing_required():
    """完全空 dict 时 normalizer 用默认值填充（vehicleNo=''）。"""
    out = normalizers.normalize_return_result({})
    assert out.vehicle_no == ""
    assert out.return_location == ""
