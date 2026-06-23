"""
Entry point. Starts four concurrent components:
  1. FastAPI health server (main thread, via uvicorn)
  2. Event consumer thread (processes queue → agent → sender)
  3. WebSocket supervision thread (lark-oapi, reconnects on failure)
  4. Agent pool pre-warm (pays ~9s of AIAgent constructor + SDK import cost
     at startup so the first user message doesn't pay it)
"""
import logging
import threading
import time
import uvicorn

from config.settings import settings
from feishu.ws_client import start_ws_supervision, set_card_action_handler
from bot.agent_pool import agent_pool
from bot.handler import start_consumer
from bot.card_action_handler import handle as card_action_handle
from infra.health import app


def _prewarm_agent_pool() -> None:
    """Pay the AIAgent constructor + OpenAI SDK import cost at startup.

    Measured: first agent_pool.get_or_create() takes ~9.4s on a fresh
    container (MCP client init, tool registry, OpenAI SDK import, prompt
    template). Pre-creating one synthetic agent here moves that cost off
    the critical path of the first real user message.

    Uses a reserved user_id ("__warmup__") that's never seen by real
    users. The agent sits in the LRU pool and is never evicted (max=100
    and the bot serves far fewer real users).
    """
    t0 = time.monotonic()
    try:
        agent_pool.get_or_create("__warmup__")
        log = logging.getLogger(__name__)
        log.info("agent_pool prewarmed in %.2fs (synthetic user)", time.monotonic() - t0)
    except Exception:
        # Pre-warm is best-effort. If it fails, the per-user warmup thread
        # (also best-effort) will retry on the first real user message.
        logging.getLogger(__name__).exception("agent_pool prewarm failed")


def _prewarm_mcp() -> None:
    """Warm up dmz-fmp-mcp connection at startup to hide Spring AI cold start.

    2026-06-18 实测：首次 MCP 调用 10935ms（Spring AI / JVM 冷启动），
    后续调用 91ms。预热一次让"首次用户消息"也快。

    2026-06-24 增强：直接调 booking_mcp_server._ensure_loop_started +
    _open_session 在后台 loop 里建好持久 session，连首次调用都 <50ms。
    """
    import concurrent.futures
    log = logging.getLogger(__name__)
    t0 = time.monotonic()
    def _do_warmup():
        from ocl.tool_guard import set_current_caller, CallerIdentity
        from car_tools.mcp_client import get_mcp_client
        set_current_caller(CallerIdentity(openid="__warmup__", email=""))
        # 触发持久 session 建立（一次 get_common_dictionary）
        client = get_mcp_client()
        client.call("get_common_dictionary", {"typeCode": "vehicle_type"})
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_warmup)
            future.result(timeout=30)
        log.info("mcp prewarmed in %.2fs (get_common_dictionary + persistent session)",
                 time.monotonic() - t0)
    except Exception:
        log.warning("mcp prewarm failed (best-effort, first call may be slow)",
                    exc_info=True)


def main() -> None:
    # 2026-06-18: 注册 faulthandler，方便 SIGUSR1 dump stack 排查卡死
    # （如 SELECT_FROM_LIST 在生产环境偶发 hang —— 之前没有 faulthandler 看不到）。
    # 用 faulthandler.register(SIGUSR1) 而非默认 enable() —— 后者会每 5 分钟
    # 自动 dump 一次到 stderr，污染 docker logs。显式 register 只在收到信号时触发。
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    # Wire root logger to stdout. Without this, `logging.getLogger(__name__)`
    # in bot/feishu/ocl has no handler attached and log.info/exception never
    # reach stdout (only lastResort at WARNING is used). `force=True` to
    # override any config the imported libraries might have left.
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        force=True,
    )

    # Inject the deterministic card-callback handler into the WS layer
    # (keeps feishu/ free of bot/ imports).
    set_card_action_handler(card_action_handle)

    # Pre-warm the agent pool BEFORE opening the health port, so /health
    # accurately reports ws_connected=true and the bot is fully ready.
    _prewarm_agent_pool()
    # 2026-06-18: 预热 dmz-fmp-mcp 连接（Spring AI 冷启动 ~10s，预热后首次用户消息也快）
    _prewarm_mcp()

    consumer_thread = threading.Thread(target=start_consumer, daemon=True, name="consumer")
    ws_thread = threading.Thread(target=start_ws_supervision, daemon=True, name="ws-supervisor")

    consumer_thread.start()
    ws_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=settings.HTTP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
