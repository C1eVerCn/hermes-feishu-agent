from unittest.mock import patch, MagicMock
from feishu.sender import _chunk_text, send


def test_short_text_is_single_chunk():
    chunks = _chunk_text("hello")
    assert chunks == ["hello"]


def test_long_text_is_chunked():
    text = "word " * 1000  # ~5000 chars
    chunks = _chunk_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 3900


def test_chunks_cover_full_text():
    text = "word " * 1000
    chunks = _chunk_text(text)
    reconstructed = " ".join(c.strip() for c in chunks)
    assert set(reconstructed.split()) == set(text.split())


def test_send_adds_chunk_labels_for_long_text():
    text = "x" * 8000
    sent_texts = []

    with patch("feishu.sender._send_one", side_effect=lambda chat, t, **kw: sent_texts.append(t)):
        with patch("time.sleep"):
            send("chat_001", text)

    assert sent_texts[0].startswith("[1/")
    assert sent_texts[1].startswith("[2/")


# ── B6: interactive card ─────────────────────────────────────────────────────

def test_send_card_builds_interactive_payload():
    import json
    import feishu.sender as sender
    card = {"config": {"wide_screen_mode": True}, "elements": [{"tag": "div"}]}
    fake_resp = MagicMock()
    fake_resp.success.return_value = True
    captured = {}

    def fake_create(req):
        captured["req"] = req
        return fake_resp

    with patch.object(sender._client.im.v1.message, "create", side_effect=fake_create), \
         patch("time.sleep"):
        sender.send_card("oc_chat", card)

    body = captured["req"].request_body
    assert body.msg_type == "interactive"
    assert json.loads(body.content)["elements"][0]["tag"] == "div"
