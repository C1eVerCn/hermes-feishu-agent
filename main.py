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


def main() -> None:
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

    consumer_thread = threading.Thread(target=start_consumer, daemon=True, name="consumer")
    ws_thread = threading.Thread(target=start_ws_supervision, daemon=True, name="ws-supervisor")

    consumer_thread.start()
    ws_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=settings.HTTP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
