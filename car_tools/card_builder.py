"""车辆预约业务专用卡片构建器（只读展示卡）。

2026-06-29 改造：卡片只做**展示**，去掉所有交互元素（按钮 / select_static / form）。
用户通过**对话打字**操作（如回复「1」「确认」「+30」或车辆编号），由 FSM 解析。
卡片美观地呈现信息（编号列表 / 摘要 / 记录），不再承载点击/填表。

卡片清单（均无交互元素）：
1. vehicles_card       — 可用车辆列表（编号表 + 引导打字选车）
2. success_card        — 预约成功详情 + 调度员
3. fail_card           — 错误展示
4. cancel_confirm_card — 取消二次确认（引导回复「确认/算了」）
5. records_card        — 我的预约 / 待审批记录（引导打字取消）
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


def _card_base(elements: list[dict]) -> dict:
    """Card 2.0 schema 包装。

    2026-06-30 Phase 1.6：删去 _button / _action / _button_row（卡片只做展示不交互）。
    action 容器展平逻辑保留（防御性 — 飞书 Card 2.0 不支持 tag:"action" 容器）。
    """
    flat: list[dict] = []
    for e in elements:
        if isinstance(e, dict) and e.get("tag") == "action" and isinstance(e.get("actions"), list):
            flat.extend(e["actions"])
        else:
            flat.append(e)
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": flat},
    }


# ── 1. 车辆列表卡片（只读展示，引导打字选车） ──────────────────────────────

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
    """渲染可用车辆列表（只读展示卡），用户回复编号或车辆号选车。

    2026-06-30 Phase 1.6：描述从"FSM 解析"改为"agent LLM 解析"——卡片依然只展示。
    """
    if not vehicles:
        elements = [_div("📋 当前没有可用车辆。请尝试调整时间或车辆类型。")]
        return _card_base(elements)

    limited = _limit_vehicles_for_display(vehicles)

    title = summary or f"📋 共 {len(limited)} 辆可用"
    if query_label:
        title = f"📋 {query_label} · {len(limited)} 辆"
    elements: list[dict] = [_div(f"**{title}**（10 分钟内未选作废）")]

    # 表格：序号 + 后六位（紧凑 markdown）
    lines = ["| # | 编号（后六位） |",
             "|---|------------|"]
    for i, v in enumerate(limited, 1):
        last6 = _vehicle_last_n(v.get("vehicle_no", ""), 6)
        lines.append(f"| {i} | `{last6}` |")
    elements.append(_div("\n".join(lines)))
    # 引导打字
    elements.append(_div("💬 回复 **编号**（如「1」）选车，或直接报车辆号；说「算了」退出"))
    return _card_base(elements)


# ── 2. 成功卡片（无按钮，展示详情 + 调度员） ────────────────────────────────

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


# ── mutation 二次确认卡（cancel 等危险操作） ─────────────────────────────────

def build_cancel_confirm_card(vehicle_no: str, start_time: str = "") -> dict:
    """取消预约二次确认卡（只读展示，引导回复「确认/算了」）。"""
    when = f"\n⏱️ 时段：{start_time}" if start_time else ""
    return _card_base([
        _div(f"⚠️ **确认取消预约？**\n\n"
             f"🚗 车辆编号：**{vehicle_no}**{when}\n\n"
             f"取消后该预约将作废，无法恢复。\n\n"
             f"💬 回复「**确认**」取消，或「**算了**」放弃。"),
    ])


# ── 3. 预约/审批记录卡片 ──────────────────────────────────────────────────

def build_records_card(records: list, *, title: str, show_cancel: bool = False) -> dict:
    """我的预约 / 我的待审批 记录卡（只读展示）。"""
    if not records:
        return _card_base([_div(f"📋 {title}\n\n（暂无记录）")])
    _badge = {"待审批": "🟡待审批", "已批准": "🟢已批准", "已驳回": "🔴已驳回",
              "已取消": "⚪已取消", "已归还": "✅已归还", "已完成": "✅已完成"}
    elements: list[dict] = [_div(f"📋 **{title}**")]
    has_pending = False
    for r in records:
        if not isinstance(r, dict):
            continue
        status = r.get("status", "")
        badge = _badge.get(status, status or "-")
        vno = r.get("vehicle_no", "")
        # 待审批且可取消的，显示完整编号（方便打字取消）；其余显示后六位
        if show_cancel and status == "待审批" and vno:
            has_pending = True
            vno_disp = f"`{vno}`"
        else:
            vno_disp = f"`{vno[-6:] if vno and len(vno) >= 6 else vno}`"
        line = (
            f"{badge}  {vno_disp}  {r.get('platform') or '-'}\n"
            f"⏱️ {r.get('start_time','')} ~ {r.get('end_time','')}\n"
            f"📝 {r.get('task_name') or '-'}　📍 {r.get('location') or '-'}"
        )
        elements.append(_div(line))
        elements.append(_hr())
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()  # 去掉最后一条多余的分隔线
    if show_cancel and has_pending:
        elements.append(_div("💬 要取消某条待审批预约，回复「**取消 <车辆编号>**」"))
    return _card_base(elements)

