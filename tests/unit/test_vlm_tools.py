"""Unit tests for vlm_tools/handlers.py.

Mocks httpx so tests do not require the real VLM API running.
Covers all12 VLM tools + correct URL construction + JSON envelope parsing.
"""
import json
from unittest.mock import patch, MagicMock

import pytest

from vlm_tools import handlers
from vlm_tools import register as vlm_register

BASE = handlers.BASE
PREFIX = handlers._PREFIX


def _mock_response(data=None, status_code=200, text=""):
 "data is the value of the data field; auto-wraps to {code,message,data}."
 r = MagicMock()
 r.is_success =200 <= status_code <300
 r.status_code = status_code
 is_ok =200 <= status_code <300
 if is_ok:
  envelope = {"code": status_code, "message": "success", "data": data}
  r.text = text or json.dumps(envelope)
  r.json.return_value = envelope
 else:
  r.text = text or "error"
  r.json.side_effect = Exception("not json")
 return r


def test_list_event_names():
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response(["ev1","ev2"])) as m:
  out = handlers.list_event_names({})
 m.assert_called_once_with(f"{BASE}{PREFIX}/eventNames", timeout=10)
 parsed = json.loads(out)
 assert parsed["code"] ==200
 assert parsed["data"] == ["ev1","ev2"]


def test_list_camera_types():
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response(["Front","Rear"])) as m:
  out = handlers.list_camera_types({})
 m.assert_called_once_with(f"{BASE}{PREFIX}/cameraTypes", timeout=10)
 assert "Front" in out


def test_list_bags_with_filters():
 args = {"page":1, "pageSize":20, "bagName": "bag_2025", "syncStatus":1}
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response({"total":5, "list":[]})) as m:
  handlers.list_bags(args)
 url = m.call_args.args[0]
 params = m.call_args.kwargs["params"]
 assert url == f"{BASE}{PREFIX}/bags"
 assert params == {"page":1, "pageSize":20, "bagName": "bag_2025", "syncStatus":1}


def test_list_bags_strips_none():
 args = {"page":1, "pageSize":10, "bagName": None}
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response({"total":0, "list":[]})) as m:
  handlers.list_bags(args)
 params = m.call_args.kwargs["params"]
 assert params == {"page":1, "pageSize":10}


def test_get_bag():
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response({"id":1, "bagName":"bag_2025_001"})) as m:
  out = handlers.get_bag({"bagId":1})
 m.assert_called_once_with(f"{BASE}{PREFIX}/bags/1", timeout=10)
 assert "bag_2025_001" in out


def test_list_frames():
 args = {"bagId":1, "page":1, "pageSize":20, "cameraType":"Front"}
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response({"total":100, "list":[]})) as m:
  handlers.list_frames(args)
 url = m.call_args.args[0]
 params = m.call_args.kwargs["params"]
 assert url == f"{BASE}{PREFIX}/bags/1/frames"
 assert params == {"page":1, "pageSize":20, "cameraType":"Front"}


def test_get_frame():
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response({"id":1, "frameImageName":"f.jpg"})) as m:
  handlers.get_frame({"frameId":1})
 m.assert_called_once_with(f"{BASE}{PREFIX}/frames/1", timeout=10)


def test_playback_bag():
 with patch("vlm_tools.handlers.httpx.get", return_value=_mock_response([{"id":1}])) as m:
  handlers.playback_bag({"bagId":1})
 m.assert_called_once_with(f"{BASE}{PREFIX}/bags/1/playback", timeout=30)


def test_download_bag_metadata_returns_metadata_not_binary():
 mock = _mock_response({"bagId":1, "url":"https://oss-xxx/...", "fileName":"x.bag"})
 with patch("vlm_tools.handlers.httpx.get", return_value=mock) as m:
  out = handlers.download_bag_metadata({"bagId":1})
 m.assert_called_once_with(f"{BASE}{PREFIX}/bags/1/download", timeout=10)
 data = json.loads(out)["data"]
 assert data["bagId"] ==1
 assert data["url"].startswith("https://")


def test_frame_image_url_returns_metadata_not_binary():
 mock = _mock_response({"frameId":1, "url":"https://oss-xxx/f.jpg", "cameraType":"Front"})
 with patch("vlm_tools.handlers.httpx.get", return_value=mock) as m:
  out = handlers.frame_image_url({"frameId":1})
 m.assert_called_once_with(f"{BASE}{PREFIX}/frames/1/image", timeout=10)
 data = json.loads(out)["data"]
 assert data["url"].startswith("https://")


def test_sync_execute_with_max_files():
 mock = _mock_response({"jsonFileCount":50, "bagUpsertCount":48, "frameUpsertCount":1500})
 with patch("vlm_tools.handlers.httpx.post", return_value=mock) as m:
  handlers.sync_execute({"maxFiles":50})
 m.assert_called_once_with(f"{BASE}{PREFIX}/sync/execute", json={"maxFiles":50}, timeout=60)


def test_sync_execute_no_body():
 mock = _mock_response({"jsonFileCount":0, "bagUpsertCount":0, "frameUpsertCount":0})
 with patch("vlm_tools.handlers.httpx.post", return_value=mock) as m:
  handlers.sync_execute({})
 m.assert_called_once_with(f"{BASE}{PREFIX}/sync/execute", json=None, timeout=60)


def test_trigger_sync_async():
 mock = _mock_response(None)
 with patch("vlm_tools.handlers.httpx.post", return_value=mock) as m:
  handlers.trigger_sync_async({})
 m.assert_called_once_with(f"{BASE}{PREFIX}/sync/async", timeout=10)


def test_sync_status():
 mock = _mock_response({"bagConsumer":{}, "frameConsumer":{}, "dbStats":{}})
 with patch("vlm_tools.handlers.httpx.get", return_value=mock) as m:
  handlers.sync_status({})
 m.assert_called_once_with(f"{BASE}{PREFIX}/sync/status", timeout=10)


def test_http_error_returns_error_envelope():
 mock = _mock_response(data=None, status_code=500, text="internal error")
 with patch("vlm_tools.handlers.httpx.get", return_value=mock):
  out = handlers.list_event_names({})
 parsed = json.loads(out)
 assert "error" in parsed
 assert "500" in parsed["error"]


def test_all_12_tools_registered():
 expected = {
  "list_event_names","list_camera_types","list_bags","get_bag",
  "list_frames","get_frame","playback_bag",
  "download_bag_metadata","frame_image_url",
  "sync_execute","trigger_sync_async","sync_status",
 }
 from tools.registry import registry
 registered = set(registry.get_tool_names_for_toolset("vlm"))
 missing = expected - registered
 assert not missing, f"missing VLM tools: {missing}"


def test_no_email_in_schemas():
 "VLM API does not require emailAddress - schema must omit it."
 from tools.registry import registry
 for name in registry.get_tool_names_for_toolset("vlm"):
  schema = registry.get_schema(name)
  props = schema.get("parameters", {}).get("properties", {})
  assert "emailAddress" not in props, f"{name} schema leaked emailAddress"

