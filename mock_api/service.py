"""Business rules for the test-bench reservation domain.
Pure functions returning (code, message, data). Routes wrap into JSON.
Date comparisons use the real current time; tests use far-future/past dates.
"""
from datetime import datetime

from mock_api import fake_db

_FMT = "%Y-%m-%d %H:%M:%S"


def _parse(dt: str):
    return datetime.strptime(dt, _FMT)


def _now():
    return datetime.now()


def reserve(email, bench_no, start_time, end_time, task_name, test_purpose, remark):
    user = fake_db.get_user(email)
    if user is None:
        return 400, "当前用户不是平台用户,请联系管理员添加", None
    if not all([bench_no, start_time, end_time, task_name, test_purpose]):
        return 400, "预约参数不能为空", None
    try:
        st, et = _parse(start_time), _parse(end_time)
    except ValueError:
        return 400, "时间格式应为 yyyy-MM-dd HH:mm:ss", None
    if st >= et:
        return 400, "预约开始时间不能晚于结束时间", None
    if st <= _now():
        return 400, "预约开始时间不能早于当前时间", None

    bench = fake_db.benches.get(bench_no)
    if bench is None:
        return 400, "台架不存在", None
    if bench["status"] != 1:
        return 400, "台架状态不可用", None
    if bench["group_id"] is None:
        return 400, "该台架未分配到任何分组,不可预约", None
    if user["role"] != 3 and bench["group_id"] != user["group_id"]:
        return 400, "您没有权限预约该台架", None
    group = fake_db.groups.get(bench["group_id"])
    if not group or not group["scheduler_emails"]:
        return 400, "该台架所属分组暂无调度员,不可预约", None

    fake_db.create_reservation(user=user, bench_no=bench_no,
                               start_time=start_time, end_time=end_time,
                               task_name=task_name, test_purpose=test_purpose, remark=remark)
    sched_lines = []
    for se in group["scheduler_emails"]:
        su = fake_db.get_user(se)
        if su:
            sched_lines.append(f"姓名:{su['name']},邮箱:{su['email']}")
    msg = "预约成功!调度员信息:\n" + "\n".join(sched_lines)
    return 200, msg, None


def cancel(email, bench_no, start_time, end_time):
    user = fake_db.get_user(email)
    if user is None:
        return 400, "当前用户不是平台用户", None
    if not bench_no:
        return 400, "台架编号不能为空", None
    recs = fake_db.find_reservations(employee_email=email, bench_no=bench_no, status=0)
    if start_time:
        recs = [r for r in recs if r["startTime"] == start_time]
    if end_time:
        recs = [r for r in recs if r["endTime"] == end_time]
    if not recs:
        return 400, "未找到待审批状态的预约记录", None
    fake_db.transition(recs[0], 3)
    return 200, "取消预约成功", None


def approve(email, bench_no, approval_result, approval_remark, start_time, end_time):
    user = fake_db.get_user(email)
    if user is None:
        return 400, "当前用户不是平台用户", None
    if user["role"] not in (2, 3):
        return 400, "您没有权限审批该台架的预约", None
    if approval_result not in (1, 2):
        return 400, "审批结果只能是1-批准或2-拒绝", None
    bench = fake_db.benches.get(bench_no)
    if bench is None:
        return 400, "台架不存在", None
    # 调度员仅审本组
    if user["role"] == 2 and bench["group_id"] != user["group_id"]:
        return 400, "您没有权限审批该台架的预约", None
    recs = fake_db.find_reservations(bench_no=bench_no, status=0)
    if start_time:
        recs = [r for r in recs if r["startTime"] == start_time]
    if end_time:
        recs = [r for r in recs if r["endTime"] == end_time]
    if not recs:
        return 400, f"未找到台架{bench_no}待审批状态的预约记录", None
    target = 1 if approval_result == 1 else 2
    for r in recs:
        fake_db.transition(r, target, reviewer=user["name"], reviewer_remark=approval_remark)
    verb = "批准" if approval_result == 1 else "拒绝"
    return 200, f"审批成功:{verb}{len(recs)}条预约记录", None


def return_bench(email, bench_no, return_location):
    user = fake_db.get_user(email)
    if user is None:
        return 400, "当前用户不是平台用户", None
    if not return_location:
        return 400, "还台地点不能为空", None
    bench = fake_db.benches.get(bench_no)
    if bench is None:
        return 400, "台架不存在", None
    recs = fake_db.find_reservations(employee_email=email, bench_no=bench_no, status=1)
    if not recs:
        return 400, "您没有预约此台架或者预约任务已结束", None
    rec = recs[0]
    fake_db.transition(rec, 4)
    rec["returnLocation"] = return_location
    return 200, "归还台架成功", None


def my_reservations(email, bench_no, start_time, end_time, task_name, status):
    user = fake_db.get_user(email)
    if user is None:
        return 400, "当前用户不是平台用户", None
    recs = fake_db.find_reservations(employee_email=email, bench_no=bench_no,
                                     status=status, task_name=task_name)
    return 200, "success", recs


def my_approvals(email, status):
    user = fake_db.get_user(email)
    if user is None:
        return 400, "当前用户不是平台用户", None
    if user["role"] not in (2, 3):
        return 400, "您不是管理员或调度员,无法查询审批列表", None
    if user["role"] == 3:
        group_ids = None  # admin sees all
    else:
        group_ids = [user["group_id"]]
    recs = fake_db.find_reservations(status=status, group_ids=group_ids)
    return 200, "success", recs


def architectures():
    return 200, "success", fake_db.list_architectures()


def available(email, architecture, need_parking_test):
    user = fake_db.get_user(email)
    if user is None:
        return 400, "当前用户不是平台用户,请联系管理员添加", None
    return 200, "success", fake_db.available_benches(user, architecture, need_parking_test)
