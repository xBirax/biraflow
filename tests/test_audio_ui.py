"""The "Transcrever áudio" window, invisible (background mode), with a fake transcriber."""
import time

from biraflow import audiofile
from biraflow.config import Paths, Settings
from biraflow.store import History


class Stub:
    pass


def test_dropped_voice_note_is_transcribed_and_kept_in_history(tmp_path, tk_root, monkeypatch):
    from biraflow.audio_ui import AudioWindow

    paths = Paths(tmp_path, tmp_path)
    paths.ensure()
    note = tmp_path / "PTT-20261015-WA0001.opus"
    note.write_bytes(b"OggS fake")
    seen = []

    def fake(path, settings, env, paths_, token=None):
        seen.append(path)
        return audiofile.Result("Oi Kate, chego às dez.", 4.0, "groq", 0.5)

    monkeypatch.setattr(audiofile, "transcribe_file", fake)
    monkeypatch.setattr(audiofile, "duration", lambda p: 4.0)
    (tmp_path / "dictionary.json").write_text(
        '{"replacements": [{"heard": "Kate", "write": "Kayth"}], "snippets": []}', encoding="utf-8")
    app = Stub()
    app.root, app.paths, app.settings_window, app.audio_window = tk_root, paths, None, None
    app.controller = Stub()
    app.controller.settings = Settings()
    app.controller.history = History(paths.history)
    w = AudioWindow(app, background=True)
    try:
        w.v_autocopy.set(False)
        w.add([note, tmp_path / "planilha.xlsx"])
        end = time.monotonic() + 5
        while w.job is not None and time.monotonic() < end:
            tk_root.update()
            time.sleep(0.02)
        text = w.text.get("1.0", "end")
        assert seen == [note]
        assert "Oi Kayth, chego às dez." in text        # your dictionary's corrections apply
        assert "Pronto" in w.l_status.cget("text")
        assert w.b_copy.instate(["!disabled"])
        assert app.controller.history.entries()[0]["mode"] == "file"
    finally:
        w.close()


def test_copy_takes_your_edit_and_closing_cancels_the_transcription(tmp_path, tk_root, monkeypatch):
    import threading

    from biraflow import inserter
    from biraflow.audio_ui import AudioWindow

    paths = Paths(tmp_path, tmp_path)
    paths.ensure()
    first, second = tmp_path / "a.opus", tmp_path / "b.opus"
    first.write_bytes(b"OggS fake")
    second.write_bytes(b"OggS fake")
    started, tokens = threading.Event(), []

    def fake(path, settings, env, paths_, token=None):
        if path == second:   # a long file: runs until the window cancels it
            tokens.append(token)
            started.set()
            while not token.cancelled:
                time.sleep(0.01)
            token.check()
        return audiofile.Result("Texto original.", 4.0, "groq", 0.5)

    copied = []
    monkeypatch.setattr(audiofile, "transcribe_file", fake)
    monkeypatch.setattr(audiofile, "duration", lambda p: 4.0)
    monkeypatch.setattr(inserter, "set_text", copied.append)
    app = Stub()
    app.root, app.paths, app.settings_window, app.audio_window = tk_root, paths, None, None
    app.controller = Stub()
    app.controller.settings = Settings()
    app.controller.history = History(paths.history)
    w = AudioWindow(app, background=True)
    w.v_autocopy.set(False)
    w.add([first])
    end = time.monotonic() + 5
    while w.job is not None and time.monotonic() < end:
        tk_root.update()
        time.sleep(0.02)
    w.copy()
    assert copied[-1] == "Texto original."
    w.text.insert("end", " Corrigido.")             # your edit is what gets copied
    w.copy()
    assert copied[-1].endswith("Texto original. Corrigido.")
    w.add([second])
    assert started.wait(5)
    w.close()
    assert tokens[0].cancelled
