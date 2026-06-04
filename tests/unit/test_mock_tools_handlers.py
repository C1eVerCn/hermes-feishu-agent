import json
from unittest.mock import patch, MagicMock
import mock_tools.handlers as h
import ocl.tool_guard as tg


def _fake_resp(payload):
    r = MagicMock()
    r.is_success = True
    r.json.return_value = payload
    return r


def test_reserve_injects_email_and_tolerates_kwargs():
    tg.set_current_email("zhangsan@example.com")
    with patch.object(h.httpx, "post", return_value=_fake_resp({"code": 200, "message": "ok", "data": None})) as p:
        out = h.reserve_bench({"benchNo": "TJ001", "startTime": "2099-01-01 09:00:00",
                               "endTime": "2099-01-01 10:00:00", "taskName": "t",
                               "testPurpose": "p"}, task_id="abc")  # extra kwarg
        assert json.loads(out)["code"] == 200
        sent = p.call_args.kwargs["json"]
        assert sent["emailAddress"] == "zhangsan@example.com"
        assert "benchNo" in sent


def test_list_architectures_get_no_body():
    with patch.object(h.httpx, "get", return_value=_fake_resp({"code": 200, "message": "ok", "data": ["1.0架构"]})):
        out = h.list_architectures({})
        assert "1.0架构" in out


def test_error_response_serialized():
    tg.set_current_email("zhangsan@example.com")
    bad = MagicMock(); bad.is_success = False; bad.status_code = 400; bad.text = "台架不存在"
    with patch.object(h.httpx, "post", return_value=bad):
        out = h.reserve_bench({"benchNo": "TJ999"})
        assert "error" in json.loads(out)
