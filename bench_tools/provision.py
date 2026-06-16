"""台架预约平台用户自动开通（auto-provision）。

权限模型（与 bot/handler 一致）：能给机器人发消息 = 在飞书后台「可见范围」内
= 默认普通用户。台架预约后端（@9013, dmz-fmp）独立按 emailAddress 鉴权，要求
用户先在其 employee 表里存在（否则「当前用户不是平台用户，请联系管理员添加」）。

本模块在首次接触时，用服务账号 JWT（sub=234234, role 3 管理员）调
`/fmp/employee/insertEmployee`，把用户登记为 role-1 工程师并挂到所有台架分组，
使其立即可查询 / 可预约本组台架。

约束（取自 dmz-fmp EmployeeServiceImpl.insertEmployee）：
- employeeName / role / mobile 必填；mobile、email 必须唯一；重复 email → 「邮箱已被注册」
- 后端只校验 mobile 非空 + 唯一（无格式正则）；机器人拿不到真实手机号，故用
  email 的 sha256 派生稳定占位号（同邮箱恒定、跨邮箱唯一、不撞真实号段）
- 非幂等：重复 email 报错而非 upsert → 我们把「已被注册」视为已开通

best-effort：失败只 log 不抛、不阻断消息。后台线程池异步执行（不阻塞消费线程，
满足「WS 回调必须立即返回」不变量）。进程内缓存已开通 email，避免重复调用。
"""
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from config.settings import settings
from bench_tools.jwt_auth import auth_headers

log = logging.getLogger(__name__)

BASE = settings.BENCH_API_BASE_URL
_INSERT_PATH = "/fmp/employee/insertEmployee"

# 默认挂到的台架分组（普通用户只能约本组台架；挂全部组 = 可约全部台架，符合
# 「两个组都加」的决策）。可用环境变量 BENCH_DEFAULT_GROUP_IDS（逗号分隔）覆盖，
# 分组 id 变化时无需改代码。
_DEFAULT_GROUP_IDS = [
    "9252e7bf-007a-41d5-8ad9-754a7eff16cd",  # 台架一组
    "f18aae22-6e3b-4f72-9781-34f0a654fcf3",  # 台架二组
]

_provisioned: set[str] = set()
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bench-provision")


def _group_ids() -> list[str]:
    raw = getattr(settings, "BENCH_DEFAULT_GROUP_IDS", "") or ""
    ids = [g.strip() for g in raw.split(",") if g.strip()]
    return ids or _DEFAULT_GROUP_IDS


def _placeholder_mobile(email: str) -> str:
    """后端要求 mobile 非空且唯一，但机器人只有邮箱。用 email 的 sha256 派生
    11 位稳定占位号：'9' + 10 位。'9' 开头不与真实手机号段（1xx）冲突。"""
    h = int(hashlib.sha256(email.encode("utf-8")).hexdigest(), 16)
    return "9" + str(h % 10_000_000_000).zfill(10)


def provision_now(email: str, name: str) -> dict:
    """同步开通一个用户，返回 {ok, status, message}。更新进程内缓存。
    用于：handler 异步调用的实际执行体 + 存量用户手动补登 + 测试。"""
    if not email:
        return {"ok": False, "status": "skip", "message": "empty email"}
    with _lock:
        if email in _provisioned:
            return {"ok": True, "status": "cached", "message": ""}
    body = {
        "employeeName": name or email.split("@")[0],
        "role": 1,
        "mobile": _placeholder_mobile(email),
        "emailAddress": email,
        "testBenchGroupIds": _group_ids(),
        "accountStatus": 1,
    }
    try:
        r = httpx.post(f"{BASE}{_INSERT_PATH}", headers=auth_headers(), json=body, timeout=10)
    except httpx.HTTPError as e:
        log.warning("bench_provision_http_failed email=%s err=%s", email, e)
        return {"ok": False, "status": "http_error", "message": str(e)}
    try:
        data = r.json()
    except ValueError:
        data = {}
    msg = str(data.get("message", "")) or r.text[:200]
    # dmz-fmp 行为：成功 → code 200；邮箱被占 → code 500 + message「邮箱已被注册」。
    # 后者我们视为「已开通」（满足幂等），不再调后端。
    code = data.get("code")
    if code == 200:
        with _lock:
            _provisioned.add(email)
        log.info("bench_provision_ok email=%s name=%s status=created", email, name)
        return {"ok": True, "status": "created", "message": msg}
    if "已被注册" in msg or "已注册" in msg:
        with _lock:
            _provisioned.add(email)
        log.info("bench_provision_ok email=%s name=%s status=already", email, name)
        return {"ok": True, "status": "already", "message": msg}
    log.warning("bench_provision_rejected email=%s code=%s msg=%s", email, code, msg)
    return {"ok": False, "status": "rejected", "message": msg}


def ensure_bench_user(email: str, name: str) -> None:
    """best-effort、异步、幂等地确保用户已在台架平台开通。
    进程内已缓存则直接返回（不提交任务）；否则丢到后台线程池执行，绝不阻塞调用方。"""
    if not email:
        return
    with _lock:
        if email in _provisioned:
            return
    try:
        _executor.submit(provision_now, email, name)
    except RuntimeError:
        # 解释器关闭等极端情况下线程池已停 —— 退化为忽略（best-effort）
        log.warning("bench_provision_submit_failed email=%s", email)
