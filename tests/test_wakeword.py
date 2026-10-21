import numpy as np

from biraflow import wakeword as ww

VOCAB = {"ei", "hei", "bi", "ra", "vira", "beira", "ok", "manda"}


def test_variants_spell_the_phrase_with_known_words():
    v = ww.variants("Ei Bira", VOCAB.__contains__)
    assert "ei bi ra" in v and "ei vira" in v and "hei beira" in v
    assert all(not p.startswith(("e ", "o ")) for p in v)     # "e vira" is everyday speech
    assert ww.variants("Ei Xyzzq", VOCAB.__contains__) == []    # nothing sounds like it


def test_confirmed_only_when_whisper_heard_the_phrase():
    assert ww.confirmed(" Ei Bira, manda o arquivo", "Ei Bira")
    assert ww.confirmed("Eibira.", "ei bira")
    assert not ww.confirmed(" Ei, vira aqui.", "Ei Bira")
    assert not ww.confirmed("", "Ei Bira")


def test_strip_phrase():
    assert ww.strip_phrase("Ei, Bira, manda o arquivo.", "Ei Bira") == "Manda o arquivo."
    assert ww.strip_phrase("Olha só. Ei Bira. Preciso de ajuda", "Ei Bira") == "Preciso de ajuda"
    assert ww.strip_phrase("Ei Bira", "Ei Bira") == ""
    assert ww.strip_phrase("manda o arquivo", "Ei Bira") == "manda o arquivo"
    # accents ignored, as in confirmed(): the phrase never ends up in the pasted text
    assert ww.strip_phrase("Ola, Flow, manda o arquivo", "Olá Flow") == "Manda o arquivo"
    assert ww.strip_phrase("Olá Flow. Oi", "Ola Flow") == "Oi"


def test_a_microphone_missing_at_start_is_retried(monkeypatch):
    import sys
    import time
    import types

    class Model:
        def __init__(self, path):
            pass

        def vosk_model_find_word(self, w):
            return 0 if w in VOCAB else -1

    fake = types.SimpleNamespace(Model=Model, KaldiRecognizer=lambda *a: object(), SetLogLevel=lambda n: None)
    monkeypatch.setitem(sys.modules, "vosk", fake)
    lst = ww.WakeListener(ww.Path("."), "Ei Bira")
    monkeypatch.setattr(lst, "_open_stream", lambda: (_ for _ in ()).throw(RuntimeError("microfone indisponível")))
    lst.start()
    try:
        end = time.monotonic() + 3
        while lst.status == "parado" and time.monotonic() < end:
            time.sleep(0.01)
        assert lst.status == "sem microfone (tentando de novo)"
        assert lst._thread.is_alive()               # still running: the 10 s reopen picks the mic up
    finally:
        lst.stop()


def test_take_over_flushes_the_call_then_forwards():
    lst = ww.WakeListener(ww.Path("."), "Ei Bira")
    got = []
    assert not lst.take_over(got.append)            # nothing confirmed yet
    lst._pending = [np.ones(3, np.float32)]
    assert lst.take_over(got.append)
    assert len(got) == 1 and lst._sink is not None
    lst.release()
    assert lst._sink is None and lst._pending is None
