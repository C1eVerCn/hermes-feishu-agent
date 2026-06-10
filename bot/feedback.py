"""用户反馈 + 卡片操作模式采集。

Phase 2 自进化：把用户对 agent 的反馈和卡片操作模式落盘，供周报分析。

安全铁律：
1. 永不存完整用户消息原文
2. 永不存 emailAddress / API key
3. 自动剥离敏感字段
4. JSONL append 模式
5. 7天前的文件自动归档
"""
import json
import os
import time
import threading
import hashlib
import re
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)
_lock = threading.Lock()

SENSITIVE_RE = re.compile(
 r"(emailAddress|password|token|api_key|cookie|secret|app_secret)"
 r"|(Bearer\s+[A-Za-z0-9\-._~+/]+=*)"
 r"|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
 re.IGNORECASE,
)


def _user_hash(user_id):
  if not user_id:
   return "anonymous"
  return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _strip_sensitive(data):
  if isinstance(data, dict):
   return {k: ("[REDACTED]" if SENSITIVE_RE.search(str(k)) else _strip_sensitive(v)) for k, v in data.items()}
  if isinstance(data, list):
   return [_strip_sensitive(x) for x in data]
  if isinstance(data, str):
   return SENSITIVE_RE.sub("[REDACTED]", data)
  return data


def _truncate(text, n=100):
  if not text:
   return ""
  return text[:n] + ("..." if len(text) > n else "")


def _root(data_dir):
  return Path(data_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "feedback"))


def _today():
  return time.strftime("%Y-%m-%d", time.localtime(time.time()))


def _ensure_dirs(root):
  for d in ("operations", "feedback", ".archive"):
   (root / d).mkdir(parents=True, exist_ok=True)


def _archive_old(root):
  cutoff = time.time() - 7 * 86400
  for sub in ("operations", "feedback"):
   d = root / sub
   if not d.exists():
     continue
   for p in list(d.glob("*.jsonl")):
     if p.stat().st_mtime < cutoff:
      shutil.move(str(p), str(root / ".archive" / p.name))


def _append_jsonl(root, subdir, event):
  event_clean = _strip_sensitive(event)
  with _lock:
   fpath = root / subdir / (_today() + ".jsonl")
   with fpath.open("a", encoding="utf-8") as f:
     f.write(json.dumps(event_clean, ensure_ascii=False) + "\n")
  _archive_old(root)


def record_card_action(user_id, action, tool, args_keys, success, error=None):
  root = _root(None)
  _ensure_dirs(root)
  event = {
   "ts": time.time(),
   "type": "card_action",
   "user_hash": _user_hash(user_id),
   "action": action,
   "tool": tool,
   "args_keys": list(args_keys) if args_keys else [],
   "success": bool(success),
   "error": _truncate(error or ""),
  }
  _append_jsonl(root, "operations", event)


def record_feedback(user_id, kind, payload):
  root = _root(None)
  _ensure_dirs(root)
  event = {
   "ts": time.time(),
   "type": "feedback",
   "user_hash": _user_hash(user_id),
   "kind": kind,
   "text": _truncate(_safe_text(payload)),
   "context_keys": _safe_keys(payload),
  }
  _append_jsonl(root, "feedback", event)


def _safe_text(payload):
  if not isinstance(payload, dict):
   return ""
  return str(payload.get("text", ""))


def _safe_keys(payload):
  if not isinstance(payload, dict):
   return []
  return sorted(payload.keys())


def _process_jsonl_file(p, cutoff, on_event):
  total =0
  for line in p.read_text(encoding="utf-8").splitlines():
   try:
    ev = json.loads(line)
    if ev.get("ts",0) < cutoff:
     continue
    on_event(ev)
    total +=1
   except Exception:
    pass
  return total


def _agg_op(ev, counters):
  counters["total"] +=1
  if ev.get("success"):
   counters["success"] +=1
  else:
   counters["fail"] +=1
  tool = ev.get("tool", "?")
  action = ev.get("action", "?")
  bt = counters["by_tool"]
  ba = counters["by_action"]
  bt[tool] = bt.get(tool,0) +1
  ba[action] = ba.get(action,0) +1


def _agg_fb(ev, counters):
  kind = ev.get("kind", "?")
  counters["total"] +=1
  bk = counters["by_kind"]
  bk[kind] = bk.get(kind,0) +1


def weekly_report(data_dir=None):
  root = _root(data_dir)
  if not root.exists():
   return {"window_days":7, "operations":{"total":0,"success":0,"fail":0,"by_tool":{},"by_action":{}}, "feedback":{"total":0,"by_kind":{}}, "generated_at": time.time()}
  cutoff = time.time() - 7 * 86400
  op_counters = {"total":0, "success":0, "fail":0, "by_tool":{}, "by_action":{}}
  fb_counters = {"total":0, "by_kind":{}}
  ops_dir = root / "operations"
  fb_dir = root / "feedback"
  if ops_dir.exists():
   for p in ops_dir.glob("*.jsonl"):
    _process_jsonl_file(p, cutoff, lambda ev: _agg_op(ev, op_counters))
  if fb_dir.exists():
   for p in fb_dir.glob("*.jsonl"):
    _process_jsonl_file(p, cutoff, lambda ev: _agg_fb(ev, fb_counters))
  return {
   "window_days":7,
   "operations": op_counters,
   "feedback": fb_counters,
   "generated_at": time.time(),
  }

