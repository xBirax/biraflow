import json

import numpy as np
import pytest
import requests

from biraflow import http
from biraflow import transcribe as tr
from biraflow.audio import write_wav
from biraflow.cancel import CancelToken
from biraflow.config import TranscriptionCfg


def test_whisper_cli_gets_accents_it_can_read(tmp_path):
    """whisper.cpp aborts on a model path with accents and misreads an accented prompt:
    the path goes as its ASCII short name, the prompt as UTF-8 bytes."""
    folder = tmp_path / "modelos-ação"
    folder.mkdir()
    model = folder / "m.bin"
    model.write_bytes(b"x")
    cfg = TranscriptionCfg(vocabulary=["Conceição", "Ávila"])
    cmd = tr.whisper_command(tmp_path / "whisper-cli.exe", model, tmp_path / "a.wav", tmp_path / "a", cfg)
    assert cmd[cmd.index("--prompt") + 1].encode("mbcs") == "Conceição, Ávila.".encode("utf-8")
    assert tr.argv_path(tmp_path / "plain.bin") == str(tmp_path / "plain.bin")
    arg = cmd[cmd.index("-m") + 1]
    if not arg.isascii():
        pytest.skip("8.3 short names are off on this drive")
    assert model.samefile(arg)


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_whisper_server_error_reply_is_an_error(tmp_path, monkeypatch):
    """whisper-server answers HTTP 200 {"error": ...} when it cannot read the audio: that must
    fall back to whisper-cli, not paste nothing."""
    from biraflow.whisper_server import LocalServer

    wav = tmp_path / "a.wav"
    write_wav(wav, np.zeros(1600, np.float32))
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(200, {"error": "failed to read audio data"}))
    with pytest.raises(RuntimeError, match="failed to read audio data"):
        LocalServer().transcribe("http://127.0.0.1:1", wav, TranscriptionCfg(), CancelToken())


def test_api_error_never_carries_the_key():
    r = Resp(401, {"error": {"message": "Incorrect API key provided: sk-proj-abc1****wxyz. Check it."}})
    msg = http.error_message(r)
    assert "sk-" not in msg and "wxyz" not in msg and "Incorrect API key" in msg
    assert "gsk_" not in http.error_message(Resp(401, {"error": {"message": "bad key gsk_12AbCd"}}))


def test_gateway_media_type_follows_the_file(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(http, "post", lambda url, headers=None, data=None, timeout=None:
                        sent.append(json.loads(data)["mediaType"]) or Resp(200, {"text": "ok"}))
    for name in ("a.webm", "a.m4a", "a.wav"):
        (tmp_path / name).write_bytes(b"x")
        tr.transcribe_api("gateway", tmp_path / name, TranscriptionCfg(engine="gateway"), {"AI_GATEWAY_API_KEY": "k"})
    assert sent == ["audio/webm", "audio/mp4", "audio/wav"]


def test_idle_connections_are_not_trusted(monkeypatch):
    clock, closed = [1000.0], []

    class FakeSession:
        def close(self):
            closed.append(clock[0])

        def head(self, *a, **k):
            pass

        def post(self, *a, **k):
            return "ok"

    monkeypatch.setattr(http, "session", lambda: FakeSession())
    monkeypatch.setattr(http.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(http, "_last_used", 0.0)
    http.warm("https://x")                       # idle since the app started: start fresh
    http.warm("https://y")                       # the second host must not drop the first's connection
    assert closed == [1000.0]
    clock[0] += 60
    assert http.post("https://x") == "ok"
    clock[0] += 100                              # 100 s after the last use: still trusted
    http.warm("https://x")
    assert closed == [1000.0]
    clock[0] += http.IDLE_RESET + 1
    http.warm("https://x")
    assert len(closed) == 2


def test_gateway_upload_timeout_counts_the_base64_body(tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x" * 2_000_000)
    cfg = TranscriptionCfg(timeout_seconds=20)
    assert tr.upload_timeout(f, cfg) == (pytest.approx(9.0), 20.0)            # 3 s + 3 s per MB
    assert tr.upload_timeout(f, cfg, 1.34)[0] == pytest.approx(3 + 3 * 2 * 1.34)


def test_whisper_form_is_the_same_for_api_and_server():
    cfg = TranscriptionCfg(language="pt", vocabulary=["Bling"])
    form = tr.whisper_form(cfg, model="whisper-large-v3-turbo", response_format="json")
    assert list(form.items()) == [("model", "whisper-large-v3-turbo"), ("response_format", "json"),
                                  ("temperature", "0"), ("language", "pt"), ("prompt", "Bling.")]
    assert "language" not in tr.whisper_form(TranscriptionCfg(language="auto"), response_format="json")
