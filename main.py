"""
Entry point. Starts three concurrent components:
  1. FastAPI health server (main thread, via uvicorn)
  2. Event consumer thread (processes queue → agent → sender)
  3. WebSocket supervision thread (lark-oapi, reconnects on failure)
"""
import threading
import uvicorn

from config.settings import settings
from feishu.ws_client import start_ws_supervision
from bot.handler import start_consumer
from infra.health import app


def main() -> None:
    consumer_thread = threading.Thread(target=start_consumer, daemon=True, name="consumer")
    ws_thread = threading.Thread(target=start_ws_supervision, daemon=True, name="ws-supervisor")

    consumer_thread.start()
    ws_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=settings.HTTP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
