VALID_TRANSITIONS: dict[int, list[int]] = {
    0: [1, 2, 3],   # 待审批 → 批准 / 拒绝 / 取消
    1: [4],         # 已批准 → 已完成（归还）
    2: [],
    3: [],
    4: [],
}

STATUS_DESC: dict[int, str] = {
    0: "待审批", 1: "已批准", 2: "已拒绝", 3: "已取消", 4: "已完成",
}


def can_transition(cur: int, target: int) -> bool:
    return target in VALID_TRANSITIONS.get(cur, [])
