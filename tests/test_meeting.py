"""Meeting mode (docs/PLANO-REUNIOES.md): sources and folders, without opening any audio device."""
import time

import numpy as np
import pytest

from biraflow import audio, meeting, win32
from biraflow.config import Paths


class FakeStream:
    def __init__(self, kwargs):
        self.kwargs = kwargs


@pytest.fixture
def mic_rig(monkeypatch):
    """MicSource with a fake PortAudio: `accept` decides which (rate, channels) open."""
    import sounddevice as sd

    rig = {"accept": lambda rate, ch: True, "opened": [], "closed": []}

    def open_stream(**kw):
        if not rig["accept"](kw["samplerate"], kw["channels"]):
            raise ValueError("Invalid sample rate")
        s = FakeStream(kw)
        rig["opened"].append(s)
        return s

    monkeypatch.setattr(audio, "open_input_stream", open_stream)
    monkeypatch.setattr(audio, "close_input_stream", rig["closed"].append)
    monkeypatch.setattr(win32, "microphone_blocked", lambda: False)
    monkeypatch.setattr(sd, "query_devices", lambda *a, **k: (
        {"default_samplerate": 48000.0, "max_input_channels": 2, "name": "Mic"} if a else
        [{"name": "Microfone (USB)", "max_input_channels": 1}]))
    return rig


def test_mic_source_16k_passes_audio_through(mic_rig):
    m = meeting.MicSource()
    m.start()
    assert mic_rig["opened"][0].kwargs["samplerate"] == 16000 and m.alive
    m._callback(np.full((160, 1), 0.2, np.float32), 160, None, None)
    m._callback(np.full((160, 1), -0.4, np.float32), 160, None, None)
    x = m.read()
    assert x.size == 320 and m.peak == pytest.approx(0.4) and m.read().size == 0
    m.stop()
    assert not m.alive and len(mic_rig["closed"]) == 1


def test_mic_source_native_rate_converted(mic_rig):                    # [R13]
    mic_rig["accept"] = lambda rate, ch: rate == 48000 and ch == 2
    m = meeting.MicSource()
    m.start()
    for _ in range(10):
        m._callback(np.full((480, 2), 0.1, np.float32), 480, None, None)   # 10 ms each
    assert m.read().size == 1600                                            # 0.1 s at 16 kHz


def test_mic_source_named_device_and_failures(mic_rig, monkeypatch):    # [R22]
    m = meeting.MicSource("usb")
    m.start()
    assert mic_rig["opened"][0].kwargs["device"] == 0
    mic_rig["accept"] = lambda rate, ch: False
    with pytest.raises(meeting.MeetingError, match="microfone"):
        meeting.MicSource().start()
    monkeypatch.setattr(win32, "microphone_blocked", lambda: True)
    with pytest.raises(meeting.MeetingError, match="Privacidade"):
        meeting.MicSource().start()


def test_mic_source_goes_dead_without_callbacks(mic_rig, monkeypatch):
    m = meeting.MicSource()
    m.start()
    real = time.monotonic
    monkeypatch.setattr(meeting.time, "monotonic", lambda: real() + 6)
    assert not m.alive


def test_meetings_folder(tmp_path, monkeypatch):                         # [R20]
    assert meeting.meetings_dir(Paths(tmp_path, tmp_path)) == tmp_path / "Reuniões"   # portable / tests
    monkeypatch.setattr(win32, "known_folder", lambda guid: tmp_path / "Docs")
    assert meeting.meetings_dir(Paths(tmp_path / "a", tmp_path / "b")) == tmp_path / "Docs" / "Bira Flow" / "Reuniões"
