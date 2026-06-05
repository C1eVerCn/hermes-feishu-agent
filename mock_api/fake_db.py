"""In-memory seed data + data access for the test-bench reservation domain.
Pure data layer — no HTTP. Routes call these and wrap into {code,message,data}.
"""
import uuid

from mock_api.state_machine import can_transition, STATUS_DESC

ARCHITECTURES = ["1.0架构", "1.5架构", "3.0架构", "L3架构", "L4架构"]

# email → user
users: dict[str, dict] = {}
# group_id → group
groups: dict[str, dict] = {}
# bench_no → bench
benches: dict[str, dict] = {}
# list of reservation dicts
reservations: list[dict] = []


def new_id() -> str:
    return uuid.uuid4().hex


def _seed() -> None:
    users.clear(); groups.clear(); benches.clear(); reservations.clear()

    # ── groups + schedulers ──────────────────────────────────────────────
    for i in range(1, 6):
        gid = f"G{i}"
        groups[gid] = {
            "id": gid, "name": f"分组{i}",
            "scheduler_emails": [f"scheduler{i}@example.com"],
        }
    # 分组 G5 故意无调度员（触发「分组暂无调度员」）
    groups["G5"]["scheduler_emails"] = []

    # ── users ────────────────────────────────────────────────────────────
    # 普通用户（role=1）
    users["zhangsan@example.com"] = {"email": "zhangsan@example.com", "name": "张三", "role": 1, "group_id": "G1"}
    users["lisi@example.com"]     = {"email": "lisi@example.com",     "name": "李四", "role": 1, "group_id": "G2"}
    # 调度员（role=2），各负责一个分组
    for i in range(1, 6):
        email = f"scheduler{i}@example.com"
        users[email] = {"email": email, "name": f"调度员{i}", "role": 2, "group_id": f"G{i}"}
    # 管理员（role=3）
    users["admin@example.com"] = {"email": "admin@example.com", "name": "王五", "role": 3, "group_id": "G1"}
    users["chenyihang@immotors.com"] = {"email": "chenyihang@immotors.com", "name": "谌一航", "role": 3, "group_id": "G1"}

    # ── 30 benches ───────────────────────────────────────────────────────
    for n in range(1, 31):
        bench_no = f"TJ{n:03d}"
        arch = ARCHITECTURES[n % len(ARCHITECTURES)]
        group_id = f"G{(n % 5) + 1}"
        status = 1
        if n % 7 == 0:      # 少量不可用
            status = 0
        if n in (29, 30):   # 少量未分组（触发「未分配任何分组」）
            group_id = None
        benches[bench_no] = {
            "bench_no": bench_no, "architecture": arch, "status": status,
            "group_id": group_id, "need_parking_test": 1 if n % 2 == 0 else 0,
        }

    # ── 预置预约记录 ──────────────────────────────────────────────────────
    _add_reservation(users["zhangsan@example.com"], "TJ001",
                     "2099-02-01 09:00:00", "2099-02-01 10:00:00", "发动机性能测试", "验证性能", "", status=0)
    _add_reservation(users["zhangsan@example.com"], "TJ006",
                     "2099-03-01 09:00:00", "2099-03-01 12:00:00", "整车标定", "标定", "", status=1,
                     reviewer="调度员1", reviewer_remark="同意")


def _add_reservation(user, bench_no, start, end, task, purpose, remark, *, status=0,
                     reviewer="", reviewer_remark="") -> dict:
    rec = {
        "id": new_id(),
        "testBenchId": bench_no.lower(),
        "benchNo": bench_no,
        "employeeId": user["email"],
        "employeeName": user["name"],
        "startTime": start, "endTime": end,
        "taskName": task, "testPurpose": purpose, "remark": remark,
        "status": status, "statusDesc": STATUS_DESC[status],
        "createTime": "2026-01-01 00:00:00",
        "reviewerName": reviewer, "reviewerRemark": reviewer_remark,
        "group_id": benches[bench_no]["group_id"],
    }
    reservations.append(rec)
    return rec


def reset() -> None:
    _seed()


# ── queries ──────────────────────────────────────────────────────────────

def list_architectures() -> list[str]:
    return list(ARCHITECTURES)


def get_user(email: str) -> dict | None:
    return users.get(email)


def available_benches(user: dict, architecture, need_parking_test) -> list[str]:
    out = []
    for bn, b in benches.items():
        if b["status"] != 1:
            continue
        if b["group_id"] is None:
            continue
        # 非管理员仅同组
        if user["role"] != 3 and b["group_id"] != user["group_id"]:
            continue
        if architecture and b["architecture"] != architecture:
            continue
        if need_parking_test is not None and b["need_parking_test"] != need_parking_test:
            continue
        out.append(bn)
    return out


def find_reservations(*, employee_email=None, bench_no=None, status=None,
                      task_name=None, group_ids=None) -> list[dict]:
    out = []
    for r in reservations:
        if employee_email and r["employeeId"] != employee_email:
            continue
        if bench_no and r["benchNo"] != bench_no:
            continue
        if status is not None and r["status"] != status:
            continue
        if task_name and task_name not in r["taskName"]:
            continue
        if group_ids is not None and r["group_id"] not in group_ids:
            continue
        out.append(r)
    return out


# ── mutations ──────────────────────────────────────────────────────────────

def create_reservation(*, user, bench_no, start_time, end_time,
                       task_name, test_purpose, remark) -> dict:
    return _add_reservation(user, bench_no, start_time, end_time,
                            task_name, test_purpose, remark, status=0)


def transition(rec: dict, target: int, *, reviewer="", reviewer_remark="") -> dict:
    if not can_transition(rec["status"], target):
        raise ValueError(f"invalid_transition: {rec['status']} → {target}")
    rec["status"] = target
    rec["statusDesc"] = STATUS_DESC[target]
    if reviewer:
        rec["reviewerName"] = reviewer
    if reviewer_remark:
        rec["reviewerRemark"] = reviewer_remark
    return rec


# seed on import
_seed()
