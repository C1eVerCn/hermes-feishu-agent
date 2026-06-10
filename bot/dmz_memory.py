"""DMZ智能体的记忆层 Provider。

实现 hermes MemoryProvider 协议，把用户偏好、近期操作、错误模式持久化。
用于让 LLM 跨会话记住用户习惯（如常查的台架架构、常查的VLM场景名）。

安全边界（铁律）：
- 不存 emailAddress / API key / 密码 / cookie
- 不存完整用户消息原文（仅存元数据：tool 名 + 关键参数 + 成功/失败）
- 不存 OCL 安全规则或权限配置（这些应在代码里）
- TTL 30天自动过期

存储位置：$HERMES_HOME/dmz_memory/<user_hash>/memory.json
（与 hermes 内置状态解耦，可独立清理/迁移）
"""
import json
import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

log = logging.getLogger(__name__)


_DEFAULT_TTL_DAYS =30
_MAX_RECENT_ACTIONS =20
_MAX_ERROR_PATTERNS =10


# 敏感字段黑名单 — 出现这些 key 的记录会被剥离，绝不持久化
_SENSITIVE_KEYS = {
"emailAddress", "email", "password", "app_secret", "appSecret",
"token", "access_token", "refresh_token", "cookie", "session_key",
"minimax_api_key", "feishu_app_secret", "vlm_api_key",
"FEISHU_APP_SECRET", "MINIMAX_API_KEY",
}


# 从用户消息中提取偏好（轻量关键词匹配）
_ARCH_PATTERN = re.compile(r"([1-4]\.\d架构|L[1-4]架构|架构类型)")
_BENCH_PATTERN = re.compile(r"(TJ\d{3})")
_EVENT_PATTERN = re.compile(r"(hotupdate_filter_[\w_]+)")


def _hash_user(user_id):
 "用 SHA256 取 user_id 哈希前16位做文件名（不暴露明文 open_id）。"
 if not user_id:
  return "anonymous"
 return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _strip_sensitive(d):
 "递归剔除敏感 key。"
 out = {}
 for k, v in d.items():
  if k in _SENSITIVE_KEYS:
   continue
  if isinstance(v, dict):
   out[k] = _strip_sensitive(v)
  else:
   out[k] = v
 return out


def _extract_preferences(text):
 "从消息中提取用户偏好信号。"
 prefs = {}
 archs = _ARCH_PATTERN.findall(text)
 if archs:
  prefs["architectures_mentioned"] = list(set(archs))
 benches = _BENCH_PATTERN.findall(text)
 if benches:
  prefs["benches_mentioned"] = list(set(benches))
 events = _EVENT_PATTERN.findall(text)
 if events:
  prefs["event_names_mentioned"] = list(set(events))
 return prefs


def _extract_tool_calls(messages):
 "从消息列表中提取工具调用记录（不存 args 明文，只存 key 名）。"
 if not messages:
  return []
 records = []
 for msg in messages:
  if msg.get("role") != "assistant":
   continue
  for tc in (msg.get("tool_calls") or []):
   try:
    fn = tc.get("function", {})
    name = fn.get("name", "")
    args_raw = fn.get("arguments", "{}")
    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    safe_args = {k: v for k, v in (args or {}).items() if k not in _SENSITIVE_KEYS}
    records.append({"tool": name, "args_keys": sorted(safe_args.keys())})
   except Exception:
    pass
 return records


class DMZMemoryProvider(MemoryProvider):
 "DMZ智能体的记忆层实现。"

 name = "dmz"

 def __init__(self):
  self._data_dir = None
  self._user_id = ""
  self._session_id = ""
  self._lock = threading.Lock()
  self._mem = {}
  self._ttl_days = _DEFAULT_TTL_DAYS
  self._prefetch_cache = ""
  self._prefetch_query = ""
  self._prefetch_at =0

 def is_available(self):
  "DMZ 记忆层始终可用（无外部依赖）。"
  return True

 def initialize(self, session_id, **kwargs):
  self._session_id = session_id
  self._user_id = kwargs.get("user_id", "") or ""
  hermes_home = kwargs.get("hermes_home")
  if hermes_home:
   base = Path(hermes_home) / "dmz_memory"
  else:
   base = Path.home() / ".hermes" / "dmz_memory"
  self._data_dir = base / _hash_user(self._user_id) if self._user_id else base / "anonymous"
  self._data_dir.mkdir(parents=True, exist_ok=True)
  self._load()
  log.info("dmz_memory_init user=%s session=%s dir=%s", self._user_id[:8], session_id[:8], self._data_dir)

 def system_prompt_block(self):
  "系统提示中的静态说明（prefetch 上下文走另一条路）。"
  return "（DMZ记忆层：跨会话保留用户偏好与常见操作模式，不存敏感信息。）"

 def prefetch(self, query, *, session_id=""):
  "从记忆中召回与 query 相关的上下文。轻量关键词匹配。"
  if not query or not self._mem:
   return ""
  now = time.time()
  if query == self._prefetch_query and (now - self._prefetch_at) <30:
   return self._prefetch_cache
  arch_hits = _ARCH_PATTERN.findall(query)
  bench_hits = _BENCH_PATTERN.findall(query)
  event_hits = _EVENT_PATTERN.findall(query)
  lines = []
  prefs = self._mem.get("preferences", {})
  if arch_hits and prefs.get("architectures_mentioned"):
   lines.append("用户常查询的架构类型：" + str(prefs.get("architectures_mentioned")))
  if bench_hits and prefs.get("benches_mentioned"):
   lines.append("用户常查询的台架：" + str(prefs.get("benches_mentioned")))
  if event_hits and prefs.get("event_names_mentioned"):
   lines.append("用户常查询的VLM场景：" + str(prefs.get("event_names_mentioned")))
  recents = self._mem.get("recent_actions", [])[-3:]
  if recents:
   tools = [r.get("tool", "?") for r in recents]
   lines.append("用户最近调用：" + " ->".join(tools))
  errs = self._mem.get("error_patterns", [])
  if errs:
   top = errs[0]
   lines.append("用户曾频繁遇到：" + str(top.get("error","")) + "（" + str(top.get("count",1)) + "次）注意规避")
  result = "\n".join(lines) if lines else ""
  self._prefetch_cache = result
  self._prefetch_query = query
  self._prefetch_at = now
  return result

 def queue_prefetch(self, query, *, session_id=""):
  "下轮预取（轻量：同 prefetch）。"
  self.prefetch(query, session_id=session_id)

 def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
  "一轮对话后，把用户偏好、近期工具调用、错误模式持久化。"
  if not self._user_id:
   return
  with self._lock:
   new_prefs = _extract_preferences(user_content + " " + assistant_content)
   if new_prefs:
    cur = self._mem.setdefault("preferences", {})
    for k, v in new_prefs.items():
     merged = list(set(cur.get(k, []) + v))
     cur[k] = merged[-10:]
   tool_records = _extract_tool_calls(messages)
   if tool_records:
    recents = self._mem.setdefault("recent_actions", [])
    recents.extend(tool_records)
    self._mem["recent_actions"] = recents[-_MAX_RECENT_ACTIONS:]
   err_match = re.search(r"(HTTP[ ]?\d{3}[^\n]{0,80})", assistant_content)
   if not err_match and "权限不足" in assistant_content:
    err_match = re.match(r".{0,5}权限不足[^。]{0,40}", assistant_content)
   if err_match:
    errs = self._mem.setdefault("error_patterns", [])
    msg = err_match.group(0)[:80] if hasattr(err_match, "group") else str(err_match)[:80]
    found = False
    for e in errs:
     if e.get("error") == msg:
      e["count"] = e.get("count",1) +1
      found = True
      break
    if not found:
     errs.append({"error": msg, "count":1, "first_seen": time.time()})
    self._mem["error_patterns"] = errs[-_MAX_ERROR_PATTERNS:]
   self._mem["last_updated"] = time.time()
   self._save()

 def get_tool_schemas(self):
  "DMZ记忆层不暴露工具给 LLM（保持工具 schema 精简）。"
  return []

 def handle_tool_call(self, tool_name, args, **kwargs):
  "无工具，调用即异常。"
  raise NotImplementedError("DMZMemoryProvider has no tool: " + str(tool_name))

 def shutdown(self):
  "关闭前最后一次落盘。"
  if self._mem:
   self._save()

 def _file_path(self):
  return self._data_dir / "memory.json" if self._data_dir else Path("/tmp/dmz_memory.json")

 def _load(self):
  with self._lock:
   self._mem = {}
   p = self._file_path()
   if not p.exists():
    return
   try:
    raw = json.loads(p.read_text(encoding="utf-8"))
    self._ttl_days = int(raw.get("ttl_days", _DEFAULT_TTL_DAYS))
    last = float(raw.get("last_updated",0))
    if last and (time.time() - last) /86400 > self._ttl_days:
     log.info("dmz_memory_expired user=%s", self._user_id[:8])
     self._mem = {}
     p.unlink(missing_ok=True)
     return
    self._mem = raw
   except Exception as e:
    log.warning("dmz_memory_load_failed err=%s", e)
    self._mem = {}

 def _save(self):
  if not self._data_dir:
   return
  safe = _strip_sensitive(self._mem)
  safe["ttl_days"] = self._ttl_days
  p = self._file_path()
  try:
   p.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
  except Exception as e:
   log.warning("dmz_memory_save_failed err=%s", e)

 def clear(self):
  "清空当前用户记忆（debug / 用户遗忘权）。"
  with self._lock:
   self._mem = {}
   self._file_path().unlink(missing_ok=True)

 def snapshot(self):
  "导出当前记忆（debug / 用户查看）。"
  return dict(self._mem)


def make_provider():
 "工厂方法：hermes 插件发现后调这个返回 provider 实例。"
 return DMZMemoryProvider()

