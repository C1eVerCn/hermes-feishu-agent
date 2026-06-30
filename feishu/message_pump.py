"""feishu/message_pump — 后端飞书消息出队投递。

后端（fmp）把需要发给用户的飞书消息（审批结果通知等）写入 `feishu_message_record`
队列；本模块周期性通过 MCP 拉取「最近 24h 未成功」的消息 → 解析收件人
(receiverEmail / receiverMobile) → open_id → 发送 → 回报结果。

对接 dmz-fmp-mcp 新增的两个工具：
- ``pull_pending_feishu_message(appKey, limit)`` → ``{code, data:[FeishuMessageVo...]}``
- ``report_feishu_message_result(appKey, id, status, errorMsg, sendTimeMs)``

设计要点：
- **仅当配置了 ``FEISHU_BOT_APP_KEY`` 时启用**（main.py 已 gate；未配置则线程不启动，
  功能完全 inert，不影响 bot 其余行为）。
- 收件人解析复用 :func:`feishu.notify._email_to_open_id`（优先本 app open_id，规避
  identity_map 残留的跨 app open_id）与 :func:`ocl.identity.open_id_of_mobile`。
- 投递成功/失败都回报；后端只把未成功的入队，回报成功后下轮不再返回，天然不重发。
- 单条失败不影响其余；每轮异常被吞掉并 log，循环不退出。

依赖前置（功能真正生效需）：
1. dmz-fmp-mcp **重新部署**（含 pull/report 两个新 @Tool 及其 *Url 配置）。
2. 后端 nacos 配 ``feishu.bot.openApi.appKey``，且与本 bot 的 ``FEISHU_BOT_APP_KEY`` 一致。
"""
import logging
import time

from config.settings import settings
from feishu import notify
from ocl import identity

log = logging.getLogger(__name__)

_STATUS_OK = 1
_STATUS_FAIL = 2


def _now_ms() -> int:
    return int(time.time() * 1000)


def _resolve_open_id(email: str, mobile: str) -> str:
    """收件人 email / mobile → 本 app open_id（优先 email）。解析失败返回 ""。"""
    if email:
        try:
            oid = notify._email_to_open_id(email)
            if oid:
                return oid
        except Exception:
            log.debug("email_to_open_id failed email=%s…", email[:3], exc_info=True)
    if mobile:
        try:
            oid = identity.open_id_of_mobile(mobile)
            if oid:
                return oid
        except Exception:
            log.debug("open_id_of_mobile failed", exc_info=True)
    return ""


def _deliver_one(msg: dict) -> tuple[int, str]:
    """投递一条消息，返回 (status, errorMsg)。status: 1 成功 / 2 失败。"""
    email = (msg.get("receiverEmail") or "").strip()
    mobile = (msg.get("receiverMobile") or "").strip()
    open_id = _resolve_open_id(email, mobile)
    if not open_id:
        return _STATUS_FAIL, f"无法解析收件人 open_id（email={'有' if email else '无'} mobile={'有' if mobile else '无'}）"
    title = (msg.get("title") or "").strip()
    content = (msg.get("content") or "").strip()
    text = f"{title}\n{content}" if (title and content) else (content or title)
    if not text:
        return _STATUS_FAIL, "消息标题与内容均为空"
    try:
        ok = notify.send_text_to_user(open_id, text)
    except Exception as e:
        return _STATUS_FAIL, f"发送异常: {type(e).__name__}: {e}"
    return (_STATUS_OK, "") if ok else (_STATUS_FAIL, "飞书发送失败（分块未全部成功）")


def _pull(client, app_key: str, limit: int) -> list:
    raw = client.call("pull_pending_feishu_message", {"appKey": app_key, "limit": limit})
    data = raw.get("data") if isinstance(raw, dict) else None
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


def _report(client, app_key: str, msg_id: str, status: int, error_msg: str) -> None:
    try:
        client.call("report_feishu_message_result", {
            "appKey": app_key,
            "id": msg_id,
            "status": status,
            "errorMsg": error_msg or "",
            "sendTimeMs": _now_ms(),
        })
    except Exception:
        log.warning("report_feishu_message_result failed id=%s", msg_id, exc_info=True)


def pump_once(client, app_key: str, limit: int) -> int:
    """拉取并投递一轮，返回本轮处理条数。异常吞掉（调用方循环不应因此退出）。"""
    try:
        msgs = _pull(client, app_key, limit)
    except Exception:
        log.warning("pull_pending_feishu_message failed", exc_info=True)
        return 0
    for m in msgs:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        status, err = _deliver_one(m)
        _report(client, app_key, mid, status, err)
        log.info("feishu_msg_pump delivered id=%s status=%s biz=%s", mid, status, m.get("bizType"))
    return len(msgs)


def run_pump() -> None:
    """后台线程入口：周期轮询拉取 → 投递 → 回报。

    由 main.py 在 ``FEISHU_BOT_APP_KEY`` 非空时启动。
    """
    from car_tools import mcp_client

    app_key = settings.FEISHU_BOT_APP_KEY
    interval = max(5, settings.FEISHU_MSG_POLL_INTERVAL)
    limit = settings.FEISHU_MSG_POLL_LIMIT
    if not app_key:
        log.info("feishu_message_pump 未启动（FEISHU_BOT_APP_KEY 未配置）")
        return
    client = mcp_client.get_mcp_client()
    log.info("feishu_message_pump started interval=%ss limit=%s", interval, limit)
    while True:
        try:
            n = pump_once(client, app_key, limit)
            if n:
                log.info("feishu_message_pump round done count=%s", n)
        except Exception:
            log.exception("feishu_message_pump round error")
        time.sleep(interval)
