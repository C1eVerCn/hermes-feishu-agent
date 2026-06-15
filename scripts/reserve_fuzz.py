#!/usr/bin/env python3
"""DMZ 智能体 · 台架预约自动化测试引擎（metamorphic fuzz）。

模拟「不同方式 / 口吻」预约同一台架，把每条话术喂进**真实**的预约抽取路径
（bot.handler._try_reserve_fast_path → 中文时间解析 → dry_run_reserve_bench），
检查系统是否把语义等价的不同说法**一致且正确**地抽成同一组预约参数。

核心思想（变形测试 / metamorphic）：对同一个「意图预约」，几十种surface 说法
**必须**抽出同一组 (benchNo, startTime, endTime, taskName, testPurpose)。任何
不一致 / 抽错 / 崩溃，都是 bug。

全程离线：dry_run_reserve_bench 不联网（仅校验+归一化），时间用冻结的 now。

分类：
  PASS        抽取正确（与期望一致）
  MISPARSE    抽到了但值不对          → 真 bug ❌
  MISSED      该抽出却没抽出（被问/落到 LLM）→ 真 bug ❌（仅 expect_full 场景）
  CRASH       抛异常                  → 真 bug ❌
  FALLTHROUGH 未命中快速路径，落到 LLM  → 覆盖缺口（信息项，非 bug）
  ASK         主动追问缺失字段         → 期望内（非 bug）

用法：
  python scripts/reserve_fuzz.py            # 跑全部场景，打印报告
  python scripts/reserve_fuzz.py -v         # 同时列出每条 PASS
  python scripts/reserve_fuzz.py --json
退出码：发现 MISPARSE/MISSED/CRASH 则 1，否则 0。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import itertools
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FEISHU_APP_ID", "x")
os.environ.setdefault("FEISHU_APP_SECRET", "x")
os.environ.setdefault("MINIMAX_API_KEY", "x")

import bot.handler as H               # noqa: E402
import bench_tools.handlers as BH      # noqa: E402
from ocl.tool_guard import set_current_email, set_current_user  # noqa: E402
from bot import dry_run_state          # noqa: E402

_USER = "ou_fuzz"
_EMAIL = "fuzz@example.com"


# ── 冻结时间 ────────────────────────────────────────────────────────────────
# handler._try_reserve_fast_path 内部做 now_cn = datetime.now() + 8h。
# 我们把 datetime.now() 冻结，使 now_cn 等于场景设定的「当前 CN 时间」。
class _FrozenDT(_dt.datetime):
    _cn_now = _dt.datetime(2026, 6, 15, 10, 0, 0)  # 周一 10:00 CN

    @classmethod
    def now(cls, tz=None):
        return cls._cn_now - _dt.timedelta(hours=8)


def _freeze(cn_now: _dt.datetime) -> None:
    _FrozenDT._cn_now = cn_now
    H.datetime = _FrozenDT


# ── 捕获 dry_run 收到的 args（在真实 handler 上加一层记录） ──────────────────
_captured: dict = {}
_real_dry_run = BH.dry_run_reserve_bench


def _spy_dry_run(args: dict, **kw):
    _captured["args"] = dict(args)
    return _real_dry_run(args, **kw)


BH.dry_run_reserve_bench = _spy_dry_run


# ── 场景与期望 ──────────────────────────────────────────────────────────────
@dataclass
class Phrasing:
    text: str
    expect: str          # "full" | "ask" | "fallthrough_ok"
    bench: str = ""
    start: str = ""
    end: str = ""
    task: str = ""
    purpose: str = ""
    note: str = ""


@dataclass
class Outcome:
    phrasing: Phrasing
    kind: str            # PASS / MISPARSE / MISSED / CRASH / FALLTHROUGH / ASK
    detail: str = ""


# 命中快速路径的动词前缀（handler._try_reserve_fast_path 的入口正则）
_FASTPATH_VERBS = ["预约", "帮我预约", "我要预约", "我想预约", "帮我订", "我想订"]
# 自然但**不**命中快速路径的口吻（预期 fallthrough → LLM，属覆盖缺口而非 bug）
_OTHER_VERBS = ["麻烦预约", "能帮我预约一下", "约一下", "我需要预约", "帮忙订一下"]


def _build_full_scenarios() -> list[Phrasing]:
    """同一意图：CT001，明天下午5点 → 后天晚上8点，任务标定，目的感知压测。
    now = 2026-06-15 10:00 → 明天=06-16，后天=06-17。"""
    bench, start, end = "CT001", "2026-06-16 17:00:00", "2026-06-17 20:00:00"
    task, purpose = "标定", "感知压测"

    # 语义等价的时间说法（都应解析成上面的 start/end）
    time_forms = [
        "从明天下午5点到后天晚上8点",
        "从明天下午5点到后天晚上8点钟",
        "明天下午5点到后天晚上8点",
        "明天17点到后天20点",
        "从明天17:00到后天20:00",
        "从明天下午五点到后天晚上八点",   # 中文数字（当前解析器可能不支持→暴露）
    ]
    tail_forms = [
        ("，任务是{t}，目的是{p}", "full"),
        ("，目的是{p}，任务是{t}", "full"),   # 反序
        (" 任务{t} 目的{p}", "full"),
        ("，任务是{t}", "partial"),            # 缺目的 → 时间应抽对，dry_run 追问目的
        ("", "partial"),                       # 缺任务+目的 → 时间应抽对，dry_run 追问
    ]
    out: list[Phrasing] = []
    for verb, tf, (tail, kind) in itertools.product(_FASTPATH_VERBS, time_forms, tail_forms):
        text = f"{verb}{bench}，{tf}{tail.format(t=task, p=purpose)}"
        out.append(Phrasing(text, kind, bench, start, end,
                            task if "{t}" in tail else "",
                            purpose if "{p}" in tail else "",
                            note=tf))
    # 不同口吻（预期 fallthrough）
    for verb in _OTHER_VERBS:
        out.append(Phrasing(
            f"{verb}{bench}，从明天下午5点到后天晚上8点，任务是{task}，目的是{purpose}",
            "fallthrough_ok", bench, start, end, task, purpose, note="tone"))
    return out


def _build_edge_scenarios() -> list[Phrasing]:
    """边界 / 易错时间表达。now=2026-06-15 10:00。"""
    E = []
    # 同日范围，结束无日期标记 → 结束继承开始日
    E.append(Phrasing("预约TJ001，从今天下午2点到4点，任务是A，目的是B",
                      "full", "TJ001", "2026-06-15 14:00:00", "2026-06-15 16:00:00", "A", "B",
                      note="同日范围end继承"))
    # 跨午夜
    E.append(Phrasing("预约TJ002，从今天晚上10点到凌晨2点，任务是A，目的是B",
                      "full", "TJ002", "2026-06-15 22:00:00", "2026-06-16 02:00:00", "A", "B",
                      note="跨午夜→次日"))
    # 具体日期
    E.append(Phrasing("预约TB001，从6月20号上午9点到6月20号下午6点，任务是A，目的是B",
                      "full", "TB001", "2026-06-20 09:00:00", "2026-06-20 18:00:00", "A", "B",
                      note="X月X号"))
    # 长台架号 + 无逗号
    E.append(Phrasing("预约TJ052503 从明天上午9点 到 明天上午11点 任务是A 目的是B",
                      "full", "TJ052503", "2026-06-16 09:00:00", "2026-06-16 11:00:00", "A", "B",
                      note="长编号+空格分隔"))
    # 半点（当前解析器丢分钟 → 预期 MISPARSE，暴露已知限制）
    E.append(Phrasing("预约CT001，从明天下午5点半到明天晚上8点，任务是A，目的是B",
                      "full", "CT001", "2026-06-16 17:30:00", "2026-06-16 20:00:00", "A", "B",
                      note="半点（分钟）"))
    # 带分钟的冒号
    E.append(Phrasing("预约CT001，从明天17:30到明天20:00，任务是A，目的是B",
                      "full", "CT001", "2026-06-16 17:30:00", "2026-06-16 20:00:00", "A", "B",
                      note="HH:MM 分钟"))
    return E


def _build_adversarial() -> list[Phrasing]:
    """应当被拒/追问，不应编造时间。"""
    A = []
    A.append(Phrasing("预约CT001", "ask", note="只有台架号→追问时间"))
    A.append(Phrasing("预约，任务是A目的是B", "ask", note="无台架号→追问"))
    A.append(Phrasing("预约CT001，从某个时间到另一个时间，任务是A，目的是B",
                      "ask", note="不可解析时间→追问，不得编造"))
    A.append(Phrasing("预约CT001，从明天下午5点到明天下午3点，任务是A，目的是B",
                      "ask", note="结束早于开始→拒绝"))
    return A


# ── 执行一条话术 ────────────────────────────────────────────────────────────
def _run_one(p: Phrasing, cn_now: _dt.datetime) -> Outcome:
    _freeze(cn_now)
    _captured.clear()
    set_current_user(_USER)
    set_current_email(_EMAIL)
    dry_run_state.clear(_USER)
    try:
        res = H._try_reserve_fast_path(p.text, _USER, _EMAIL)
    except Exception as e:  # noqa: BLE001
        return Outcome(p, "CRASH", f"{type(e).__name__}: {e}")
    finally:
        set_current_user("")
        set_current_email("")

    args = _captured.get("args")

    # 落到 LLM
    if res is None:
        if p.expect == "fallthrough_ok":
            return Outcome(p, "FALLTHROUGH", "未命中快速路径（口吻），交 LLM")
        return Outcome(p, "MISSED", "返回 None：本应快速路径处理却落到 LLM")

    reached_dry_run = args is not None
    # 快速路径自己的「追问」(card=None 文本)：缺台架/时间不可解析/end<=start
    asked = (res.card is None)

    if p.expect == "ask":
        # 期望快速路径在 dry_run 前主动追问（不得编造时间推进）
        if asked and not reached_dry_run:
            return Outcome(p, "ASK", _trim(res.text))
        return Outcome(p, "MISPARSE",
                       f"期望追问，却推进到确认；抽到 args={args}")

    # full / partial / (命中的 fallthrough)：bench+起止时间都应被正确抽出。
    # partial 仅缺 任务/目的——这是正常的，dry_run 会出「缺字段」卡片追问，
    # 所以只校验 bench/start/end（以及该话术明确给出的 task/purpose）。
    if not reached_dry_run:
        return Outcome(p, "MISSED",
                       f"本应抽出起止时间，却在 dry_run 前被拦（asked={asked}）：{_trim(res.text)}")

    diffs = []
    for fname, want in (("benchNo", p.bench), ("startTime", p.start),
                        ("endTime", p.end)):
        got = args.get(fname, "")
        if want and got != want:
            diffs.append(f"{fname}: 期望 {want!r} 得到 {got!r}")
    if p.task and args.get("taskName", "") != p.task:
        diffs.append(f"taskName: 期望 {p.task!r} 得到 {args.get('taskName','')!r}")
    if p.purpose and args.get("testPurpose", "") != p.purpose:
        diffs.append(f"testPurpose: 期望 {p.purpose!r} 得到 {args.get('testPurpose','')!r}")

    if diffs:
        return Outcome(p, "MISPARSE", "; ".join(diffs))
    return Outcome(p, "PASS", "")


def _trim(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ⏎ ")
    return s[:n] + ("…" if len(s) > n else "")


def _sig(detail: str) -> str:
    """把一条 bug 详情压成「根因签名」，用于聚合组合爆炸出的同类失败。"""
    d = (detail or "").replace("\n", " ")
    for marker, label in (
        ("请告知", "时间范围未识别（缺『从』前缀？）"),
        ("无法识别起止时间", "起止时间无法解析（中文数字/格式不支持？）"),
        ("结束时间早于", "结束≤开始判定"),
        ("startTime", "时间抽取值错误"),
        ("benchNo", "台架号抽取错误"),
        ("taskName", "任务名抽取错误"),
        ("testPurpose", "测试目的抽取错误"),
        ("编造", "本应追问却推进"),
    ):
        if marker in d:
            return label
    return d[:50]


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="台架预约自动化测试引擎")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cn_now = _dt.datetime(2026, 6, 15, 10, 0, 0)
    phrasings = (_build_full_scenarios() + _build_edge_scenarios()
                 + _build_adversarial())
    outcomes = [_run_one(p, cn_now) for p in phrasings]

    buckets: dict[str, list[Outcome]] = {}
    for o in outcomes:
        buckets.setdefault(o.kind, []).append(o)

    bug_kinds = ("MISPARSE", "MISSED", "CRASH")
    n_bugs = sum(len(buckets.get(k, [])) for k in bug_kinds)

    if args.json:
        print(json.dumps({
            "total": len(outcomes),
            "by_kind": {k: len(v) for k, v in buckets.items()},
            "bugs": [{"kind": o.kind, "text": o.phrasing.text, "detail": o.detail}
                     for o in outcomes if o.kind in bug_kinds],
        }, ensure_ascii=False, indent=2))
        return 1 if n_bugs else 0

    print("\n=== 台架预约自动化测试引擎 ===")
    print(f"共生成话术 {len(outcomes)} 条\n")
    order = ["MISPARSE", "MISSED", "CRASH", "ASK", "FALLTHROUGH", "PASS"]
    icon = {"PASS": "✅", "ASK": "💬", "FALLTHROUGH": "↪️",
            "MISPARSE": "❌", "MISSED": "❌", "CRASH": "💥"}
    for k in order:
        items = buckets.get(k, [])
        if not items:
            continue
        print(f"{icon[k]} {k}: {len(items)}")

    # 真 bug 按「根因签名」聚合，避免组合爆炸刷屏
    if n_bugs:
        print("\n──── 真 bug 根因聚合 ────")
        sigs: dict[str, list[Outcome]] = {}
        for o in outcomes:
            if o.kind in bug_kinds:
                sig = f"[{o.kind}] " + _sig(o.detail)
                sigs.setdefault(sig, []).append(o)
        for i, (sig, items) in enumerate(sorted(sigs.items(), key=lambda x: -len(x[1])), 1):
            print(f"\n{i}. {sig}  （{len(items)} 条话术命中）")
            for o in items[:2]:
                print(f"     例：「{o.phrasing.text}」")
            if len(items) > 2:
                print(f"     …另有 {len(items) - 2} 条同类")

    if args.verbose:
        print("\n──── PASS 明细 ────")
        for o in buckets.get("PASS", []):
            print(f"  ✅ 「{o.phrasing.text}」")

    fall = buckets.get("FALLTHROUGH", [])
    if fall:
        print(f"\nℹ️ 覆盖缺口：{len(fall)} 条自然口吻未命中快速路径（落 LLM，功能正常但慢）。")
    print(f"\n真 bug（MISPARSE/MISSED/CRASH）：{n_bugs} 个，"
          + f"聚合为 {len(set(_sig(o.detail) for o in outcomes if o.kind in bug_kinds))} 类根因"
            if n_bugs else "未发现 🎉")
    return 1 if n_bugs else 0


if __name__ == "__main__":
    sys.exit(main())
