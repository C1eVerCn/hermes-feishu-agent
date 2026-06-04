# infra/

Cross-cutting infrastructure. No domain knowledge, no LLM calls, no Feishu API calls.

## Files

- `dedup.py` — in-memory LRU dedup keyed by `message_id`; TTL = 86400 s; max 10 000 entries
- `metrics.py` — simple thread-safe counters and histograms; exposed at `/health`
- `health.py` — FastAPI app with a single `GET /health` endpoint; runs in main thread via uvicorn

## dedup.py

Key is `message_id` (not `event_id` — image messages produce multiple events per message). Uses `OrderedDict` + a timestamp dict for TTL; no Redis dependency in Phase 1. TTL check is lazy (on lookup), plus a periodic sweep every 1000 inserts.

## metrics.py

Counters: `messages_received`, `messages_processed`, `errors_timeout`, `errors_agent`, `ws_reconnects`.  
Histograms: `llm_latency_seconds` stored as a sorted list; expose p50/p95 at `/health`.  
All operations protected by a single `threading.Lock`.

## health.py

Returns:
```json
{"status": "ok", "ws_connected": true, "agent_pool_size": 3,
 "metrics": {"messages_received_total": 142, "llm_latency_p50_seconds": 4.2}}
```

`ws_connected` is a `threading.Event` flag set by `ws_client.py`.

## What NOT to do

- Do not add Redis or any external dependency to `dedup.py` in Phase 2
- Do not log or expose message content in `metrics.py` — counters and latencies only
- Do not put retry logic or business rules in `health.py` — it reads state, never writes it
- Do not make `dedup.py` thread-unsafe — multiple threads write to it concurrently
