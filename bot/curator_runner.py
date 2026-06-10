"""DMZ Curator Runner — Phase 3 自进化（只读 wrapper）。

设计铁律：
1. 只读 — 不修改任何业务代码 / Skill / OCL 规则
2. 仅产出建议写入 data/curator/suggestions/
3. 启动方式：bot/agent_pool.py 在 AIAgent 闲置时调 maybe_run_dmz_curator()
"""
import json
import os
import time
import logging
import hashlib
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)
_lock = threading.Lock()

BLOCKED_PATHS = [
    "bot/dmz_memory.py",
    "bot/feedback.py",
    "bot/curator_runner.py",
    "ocl/permission.py",
    "ocl/format_control.py",
    "ocl/content_filter.py",
    "ocl/length_limiter.py",
    "config/settings.py",
]


def _suggestions_dir():
    return Path(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "curator", "suggestions"))


def _ensure_dir():
    d = _suggestions_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash_suggestion(s):
    return hashlib.sha256(json.dumps(s, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _safe_get_toolset_names():
    try:
        from tools.registry import registry
    except Exception as e:
        log.warning("curator_registry_import_failed err=%s", e)
        return ()
    return ("bench", "vlm")


def _scan_one_toolset(toolset):
    from tools.registry import registry
    findings = []
    for tname in registry.get_tool_names_for_toolset(toolset):
        try:
            schema = registry.get_schema(tname) or {}
        except Exception as e:
            log.warning("curator_schema_failed tool=%s err=%s", tname, e)
            continue
        desc = schema.get("description", "") or ""
        props = schema.get("parameters", {}).get("properties", {}) or {}
        if not desc.strip():
            findings.append({"tool": tname, "issue": "empty_description", "toolset": toolset})
        if "emailAddress" in props:
            findings.append({"tool": tname, "issue": "schema_leaks_email", "toolset": toolset})
    return findings


def _scan_tools():
    findings = []
    for toolset in _safe_get_toolset_names():
        findings.extend(_scan_one_toolset(toolset))
    return findings


def _scan_skills():
    findings = []
    skills_root = Path(os.path.expanduser("~/.claude/skills"))
    if not skills_root.exists():
        return findings
    for f in skills_root.rglob("SKILL.md"):
        try:
            text = f.read_text(encoding="utf-8")
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            age_days = (datetime.now() - mtime).days
            if age_days > 60:
                findings.append({"file": str(f), "age_days": age_days, "issue": "stale_skill"})
            if "STOP" not in text and "红旗" not in text:
                findings.append({"file": str(f), "issue": "missing_red_flags_section"})
        except Exception as e:
            log.warning("curator_scan_skill_failed file=%s err=%s", f, e)
    return findings


def _scan_feedback_patterns():
    findings = []
    fb_root = Path(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "feedback"))
    ops_dir = fb_root / "operations"
    if not ops_dir.exists():
        return findings
    error_counter = {}
    for f in ops_dir.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if (not ev.get("success")) and ev.get("error"):
                err = ev["error"][:50]
                error_counter[err] = error_counter.get(err, 0) + 1
    for err, count in error_counter.items():
        if count >= 3:
            findings.append({"error_pattern": err, "count": count, "issue": "high_frequency_error"})
    return findings


def run_review():
    started = time.time()
    suggestions = []
    for fn in (_scan_tools, _scan_skills, _scan_feedback_patterns):
        try:
            suggestions.extend(fn())
        except Exception as e:
            log.warning("curator_scan_failed fn=%s err=%s", fn.__name__, e)
    d = _ensure_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    out_file = d / (today + ".jsonl")
    with _lock:
        with out_file.open("a", encoding="utf-8") as f:
            for s in suggestions:
                record = {
                    "ts": time.time(),
                    "hash": _hash_suggestion(s),
                    "suggestion": s,
                    "status": "pending",
                }
                f.write(json.dumps(record, ensure_ascii=False) + chr(10))
    return {
        "started_at": started,
        "duration_sec": time.time() - started,
        "scan_types": ["tools", "skills", "feedback_patterns"],
        "suggestions_count": len(suggestions),
        "suggestions": suggestions,
        "output_file": str(out_file),
    }


_last_run_at = {}
DEFAULT_INTERVAL_HOURS = 24


def maybe_run_dmz_curator(interval_hours=DEFAULT_INTERVAL_HOURS):
    now = time.time()
    last = _last_run_at.get("review", 0)
    if now - last < interval_hours * 3600:
        return None
    with _lock:
        _last_run_at["review"] = now
    try:
        return run_review()
    except Exception as e:
        log.error("curator_run_failed err=%s", e)
        return None
