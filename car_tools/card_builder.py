"""车辆预约业务专用卡片构建器（5 套卡片）。

与 ocl/card_builder.py 的关系：
- ocl/card_builder.py 保留 —— 给 Agent 路径的 LLM 输出兜底（默认行为）
- car_tools/card_builder.py 是**业务专用**卡片，给 Layer 0.5/0.6/状态机
  工具返回用，比通用卡片更精准（按钮 + 摘要 + 表格）。

卡片清单：
1. vehicles_card       — fetch_available_vehicles 成功（带 [选N] 按钮）
2. missing_fields_card — _dry_run 缺字段（无按钮，引导用户回复）
3. confirm_card        — _dry_run 全部齐全（[确认] [取消] 按钮）
4. success_card        — _commit_vehicle_reservation 成功（无按钮，展示详情 + 调度员）
5. fail_card           — 任何工具返回 {"error": ...}（无按钮）
"""
import json
from typing import Optional

# 状态码 → 中文 badge
_STATUS_BADGE = {
    "待审批":  "🟡待审批",
    "已批准":  "🟢已批准",
    "已驳回":  "🔴已驳回",
    "已取消":  "⚪已取消",
    "已归还":  "✅已归还",
}


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr() -> dict:
    return {"tag": "hr"}


def _button(text: str, value: dict, btype: str = "default") -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": btype, "value": value}


def _action(buttons: list[dict]) -> dict:
    return {"tag": "action", "actions": buttons}


def _card_base(elements: list[dict]) -> dict:
    return {"config": {"wide_screen_mode": True}, "elements": elements}


# ── 1. 车辆列表卡片（带 [选N] 按钮） ──────────────────────────────────────

# 用户约定的显示限制（CLAUDE.md / 产品需求）：
#   - 默认各芯片（platform）最多返回 3 辆
#   - 总共最多返回 10 辆
#   - 适用于所有"查可用车辆"查询
DEFAULT_VEHICLES_PER_CHIP: int = 3
DEFAULT_VEHICLES_MAX_TOTAL: int = 10


def _limit_vehicles_for_display(
    vehicles: list[dict],
    *,
    per_chip: int = DEFAULT_VEHICLES_PER_CHIP,
    max_total: int = DEFAULT_VEHICLES_MAX_TOTAL,
) -> list[dict]:
    """按 platform 分组取 top-N，跨平台总数 cap。

    行为：
    - 同一 platform 内只取前 per_chip 辆（保持原序）
    - 各 platform 取完后合并，按 platform 名字典序拼接
    - 若合并后 > max_total，截断到 max_total
    """
    if len(vehicles) <= max_total and all(
        sum(1 for v in vehicles if (v.get("platform") or "") == p) <= per_chip
        for p in {v.get("platform") for v in vehicles}
    ):
        # 全部都已经在限制内，直接返回
        return vehicles

    # 按 platform 分桶
    by_platform: dict[str, list[dict]] = {}
    for v in vehicles:
        plat = v.get("platform") or "未知"
        by_platform.setdefault(plat, []).append(v)

    # 每个 platform 取 top per_chip
    limited: list[dict] = []
    for plat in sorted(by_platform.keys()):
        limited.extend(by_platform[plat][:per_chip])
    # 截断到 max_total
    return limited[:max_total]


def _vehicle_last_n(vno: str, n: int = 6) -> str:
    """车辆编号后 N 位（不足全部）。用于卡片表格只显示后六位。"""
    return vno[-n:] if vno else ""


def build_vehicles_card(vehicles: list[dict], *,
                         summary: Optional[str] = None,
                         query_label: Optional[str] = None) -> dict:
    """渲染可用车辆列表 + 「选 1..N」按钮。

    显示规则（按产品要求）：
    - 每芯片（Xavier / ADCU / Orin / Thor）最多 3 辆
    - 总共最多 10 辆
    - 表格只显示「序号」+「车辆编号后六位」（车牌号等其他字段在点选后由
      select_vehicle callback 读出，不直接展示）
    - 「选 N」按钮的 value 里编码完整 vehicle_no 等信息供 callback 反查

    vehicles: list[Vehicle.model_dump()]，至少含 vehicle_no / vehicle_type / platform。
    query_label: 用户查询条件（如「大Fcar-Thor」），会作为小标题展示在卡片上。
    """
    if not vehicles:
        elements = [_div("📋 当前没有可用车辆。请尝试调整时间或车辆类型。")]
        return _card_base(elements)

    limited = _limit_vehicles_for_display(vehicles)

    title = summary or f"📋 共 {len(limited)} 辆可用车辆"
    head_lines = [f"**{title}**"]
    if query_label:
        head_lines.append(f"查询条件：**{query_label}**")
    head_lines.append("请选择要预约的车辆（10 分钟内未选自动作废）：")
    elements: list[dict] = [_div("\n".join(head_lines))]

    # 表格：仅显示 序号 + 后六位
    lines = ["| 序号 | 车辆编号（后六位） |",
             "|------|--------------------|"]
    for i, v in enumerate(limited, 1):
        vno = v.get("vehicle_no", "")
        last6 = _vehicle_last_n(vno, 6)
        lines.append(f"| {i} | `{last6}` |")
    elements.append(_div("\n".join(lines)))

    # 选 N 按钮（按钮 value 里编码完整信息，callback 时反查）
    buttons = [
        _button(f"选 {i+1}",
                {"action": "select_vehicle",
                 "vehicle_no": v.get("vehicle_no", ""),
                 "vehicle_type": v.get("vehicle_type", ""),
                 "platform": v.get("platform", ""),
                 "license_plate": v.get("license_plate", ""),
                 "vin": v.get("vin", "")},
                "primary")
        for i, v in enumerate(limited)
    ]
    buttons.append(_button("取消", {"action": "cancel_flow"}, "danger"))
    elements.append(_action(buttons))
    return _card_base(elements)


# ── 2. 缺字段卡片（无按钮，引导用户回复） ──────────────────────────────────

def build_missing_fields_card(summary: str) -> dict:
    """_dry_run 缺字段时让用户补字段。无按钮（用户文本回复）。"""
    return _card_base([_div(summary)])


# ── 3. 确认卡片（[确认] [取消]） ───────────────────────────────────────────

def build_confirm_card(summary: str, args: dict) -> dict:
    """dry_run 全部齐全时的确认卡片。args 透传到 confirm button 的 value。"""
    elements: list[dict] = [
        _div(f"**请确认以下预约信息：**\n\n{summary}\n\n"
             f"确认后系统将自动通知调度员审批。"),
        _hr(),
        _div("（10 分钟内未回复本次预约将自动作废）"),
    ]
    # value 必须可被 lark JSON 序列化
    value = {
        "action": "confirm_booking",
        "vehicleNo":    args.get("vehicle_no") or args.get("vehicleNo", ""),
        "vehicleType":  args.get("vehicle_type") or args.get("vehicleType", ""),
        "platform":     args.get("platform", ""),
        "licensePlate": args.get("license_plate") or args.get("licensePlate", ""),
        "startTime":    args.get("start_time") or args.get("startTime", ""),
        "endTime":      args.get("end_time") or args.get("endTime", ""),
        "taskName":     args.get("task_name") or args.get("taskName", ""),
        "location":     args.get("location", ""),
        "remark":       args.get("remark", ""),
        "vin":          args.get("vin", ""),
    }
    elements.append(_action([
        _button("确认预约", value, "primary"),
        _button("取消", {"action": "cancel_flow"}, "danger"),
    ]))
    return _card_base(elements)


# ── 4. 成功卡片（无按钮，展示详情 + 调度员） ────────────────────────────────

def build_success_card(result: dict) -> dict:
    """_commit_vehicle_reservation 成功时展示详情 + 调度员列表。"""
    dispatchers = result.get("dispatchers") or []
    dispatcher_text = "（无）"
    if dispatchers:
        dispatcher_text = "\n".join(
            f"• {d.get('name','')} ({d.get('email','')})"
            for d in dispatchers
        )

    elements: list[dict] = [
        _div("✅ **预约提交成功，等待调度员审批**"),
        _hr(),
        _div(
            f"**车辆编号**：{result.get('vehicle_no','')}\n"
            f"**类型**：{result.get('vehicle_type','')}\n"
            f"**平台**：{result.get('platform','')}\n"
            f"**车牌**：{result.get('license_plate') or '-'}\n"
            f"**开始**：{result.get('start_time','')}\n"
            f"**结束**：{result.get('end_time','')}\n"
            f"**任务**：{result.get('task_name','')}\n"
            f"**地点**：{result.get('location','')}"
        ),
        _hr(),
        _div(f"**审批人（调度员）**：\n{dispatcher_text}"),
    ]
    return _card_base(elements)


# ── 5. 失败卡片（无按钮，展示错误） ────────────────────────────────────────

def build_fail_card(error: str, *, context: str = "") -> dict:
    head = "❌ **操作失败**"
    if context:
        head += f"（{context}）"
    return _card_base([
        _div(f"{head}\n\n{error}\n\n请调整后重试或联系管理员。"),
    ])
