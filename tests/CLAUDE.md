# tests/

## Structure

- `unit/` — pure unit tests; no network, no filesystem, no env vars required; mock everything external
- `integration/` — require a live `.env` and (for mock_api tests) a running FastAPI server

## Rules

- Unit tests must pass with `pytest tests/unit/` and zero env vars set
- Each unit test file covers exactly one module (`test_dedup.py` → `infra/dedup.py`, etc.)
- Use `unittest.mock.patch` to isolate; never mock the module under test — only its dependencies
- Integration tests are skipped automatically if required env vars are absent (`pytest.importorskip` pattern)

## Success criteria for each module

- `test_dedup.py`: duplicate returns False on second call; TTL expiry evicts entry; LRU evicts oldest beyond max_size
- `test_sender.py`: text > 4096 chars is chunked; chunk labels are `[1/N]`; 429 triggers exponential backoff
- `test_agent_pool.py`: same `user_id` returns same instance; LRU evicts correctly at `max_size`; thread-safe under concurrent access
- `test_metrics.py`: counters increment correctly; histogram p50/p95 within ±5% of true value

## What NOT to do

- Do not write tests that depend on the order other tests run
- Do not test hermes-agent or lark-oapi internals — test only the behavior of our wrapper code
- Do not use `time.sleep` in unit tests — use `freezegun` or mock `time.monotonic` instead
