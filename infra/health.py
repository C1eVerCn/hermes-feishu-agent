from fastapi import FastAPI
from feishu.ws_client import ws_connected
from bot.agent_pool import agent_pool
from infra.metrics import metrics

app = FastAPI(docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "ws_connected": ws_connected.is_set(),
        "agent_pool_size": agent_pool.size(),
        "metrics": metrics.snapshot(),
    }
