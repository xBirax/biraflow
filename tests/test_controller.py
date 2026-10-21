"""End-to-end controller flows with a fake mic, fake transcriber and fake inserter."""
import threading
import time

import numpy as np
import pytest

from biraflow import controller as ctl
from biraflow.config import Paths, Settings
from biraflow.textproc import Replacement


class FakeRecorder:
    device = ""
    level = 0.0
    peak = 0.1
    keep_ready = 0
    missing_device = False

    def __init__(self):
        self.started = 0
        self.aborted = 0

    def start(self):
        self.started += 1

    def stop(self):
        return np.full(16000, 0.1, dtype=np.float32)  # 1 s of "speech"

    def abort(self):
        self.aborted += 1

    def close(self):
        pass


class FakeUi:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a))

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    paths = Paths(tmp_path, tmp_path)
    paths.ensure()
    monkeypatch.setattr("biraflow.config.PROJECT_ROOT", tmp_path / "noproject")
    settings = Settings()
    settings.hotkey.keys = "f24"
    settings.cleanup.provider = "none"
    settings.ui.sounds = False
    settings.history.learn_from_edits = False  # never read the real focused window in tests
    settings.output.smart_insert = False
    ui = FakeUi()
    c = ctl.Controller(paths, settings, ui)
    c.recorder = FakeRecorder()
    inserted = []
    gate = threading.Event()
    gate.set()

    def fake_transcribe(wav, cfg, env, paths_, token, notices=None):
        assert wav.exists()
        while not gate.wait(0.01):
            token.check()
        token.check()
        return "olá [BLANK_AUDIO] mundo"

    def fake_insert(text, cfg, check, target=None):
        check()
        inserted.append(text)

    spoken = {"text": "olá [BLANK_AUDIO] mundo"}

    cfgs = []

    def fake_transcribe2(wav, cfg, env, paths_, token, notices=None):
        cfgs.append(cfg)
        fake_transcribe(wav, cfg, env, paths_, token)
        if spoken.get("error"):
            raise ctl.TranscriptionError(spoken["error"])
        return spoken["text"]

    monkeypatch.setattr(ctl, "transcribe", fake_transcribe2)
    monkeypatch.setattr(ctl.inserter, "insert", fake_insert)
    monkeypatch.setattr(ctl.inserter, "press_enter", lambda: inserted.append("<ENTER>"))
    monkeypatch.setattr(ctl.appcontext, "foreground", lambda: ctl.AppContext("whatsapp.exe", "WhatsApp"))
    # never depend on the real keyboard (the user may be holding Ctrl while the tests run)
    monkeypatch.setattr(ctl.inserter, "wait_for_modifiers_released", lambda timeout=1.5: True)
    monkeypatch.setattr(ctl.win32, "session_locked", lambda: False)
    c.spoken = spoken
    c.cfgs = cfgs
    t = threading.Thread(target=c.run, daemon=True)
    t.start()
    yield c, ui, inserted, gate, paths
    c.post("quit")
    t.join(2)


def key(c, kind, at=None):
    c.post(kind, time.monotonic() if at is None else at)


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_hold_to_talk_inserts_sanitized_text(rig):
    c, ui, inserted, _, paths = rig
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "release", t0 + 1.0)
    assert wait_for(lambda: inserted)
    assert inserted == ["Olá mundo"]
    assert wait_for(lambda: c.last_text == "Olá mundo")
    assert "recording" in ui.names() and "busy" in ui.names()
    assert not list(paths.tmp_dir.glob("rec-*"))   # temp audio deleted


def test_double_tap_handsfree_then_tap_to_stop(rig):
    c, ui, inserted, _, _ = rig
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "release", t0 + 0.05)
    key(c, "press", t0 + 0.15)
    key(c, "release", t0 + 0.2)
    assert wait_for(lambda: any(n == "recording" and a[0].value == "handsfree" for n, a in ui.calls))
    time.sleep(0.6)  # well past the double-tap window: still recording
    assert inserted == []
    key(c, "press")
    key(c, "release")
    assert wait_for(lambda: inserted == ["Olá mundo"])


def test_esc_while_recording_drops_audio(rig):
    c, ui, inserted, _, _ = rig
    key(c, "press")
    key(c, "esc")
    key(c, "release")
    assert wait_for(lambda: ("flash", ("Cancelado", "info", 900)) in ui.calls)
    time.sleep(0.2)
    assert inserted == [] and c.recorder.aborted >= 1


def test_esc_while_transcribing_cancels_job(rig):
    c, ui, inserted, gate, _ = rig
    gate.clear()  # transcription hangs until cancelled
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "release", t0 + 1.0)
    assert wait_for(lambda: "busy" in ui.names())
    key(c, "esc")
    gate.set()
    time.sleep(0.3)
    assert inserted == []
    # and the machine is free for the next dictation
    t1 = time.monotonic()
    key(c, "press", t1)
    key(c, "release", t1 + 1.0)
    assert wait_for(lambda: inserted == ["Olá mundo"])


def test_short_tap_is_ignored_quietly(rig):
    c, ui, inserted, _, _ = rig
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "release", t0 + 0.05)
    assert wait_for(lambda: "hide" in ui.names(), 2)
    assert inserted == []


def test_silence_is_not_transcribed(rig):
    c, ui, inserted, _, _ = rig
    c.recorder.stop = lambda: np.zeros(16000, dtype=np.float32)
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "release", t0 + 1.0)
    assert wait_for(lambda: any(n == "flash" and a[0] == "Nenhuma fala detectada" for n, a in ui.calls))
    assert inserted == []


def test_disable_stops_recording_and_ignores_hotkey(rig):
    c, ui, inserted, _, _ = rig
    key(c, "press")
    c.post("set_enabled", False)
    assert wait_for(lambda: not c.enabled and c.recorder.aborted >= 1)
    assert wait_for(lambda: ("status", ("disabled",)) in ui.calls)


def hold(c):
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "release", t0 + 1.0)


def test_snippets_replacements_send_and_history(rig):
    from biraflow.store import Dictionary, Replacement, Snippet

    c, ui, inserted, _, paths = rig
    Dictionary([Replacement("ese", "esse")], [Snippet("meu email", "bira@exemplo.com")]).save(paths.dictionary)
    c.spoken["text"] = "manda ese arquivo pro meu e-mail vírgula por favor pressione enter"
    hold(c)
    assert wait_for(lambda: len(inserted) == 2)
    assert inserted == ["Manda esse arquivo pro bira@exemplo.com, por favor", "<ENTER>"]
    assert wait_for(lambda: c.history.entries())                    # saved right after the paste
    entry = c.history.entries()[0]
    assert entry["app"] == "whatsapp.exe" and entry["text"] == inserted[0] and entry["mode"] == "hold"
    assert wait_for(lambda: c.stats.read()["dictations"] == 1)   # saved right after the history


def test_history_never_keeps_nothing(rig):
    c, _, inserted, _, paths = rig
    c.history.keep = "never"
    hold(c)
    assert wait_for(lambda: inserted)
    assert wait_for(lambda: c.stats.read()["words"] == 2)          # counters only
    assert c.history.entries() == [] and not paths.history.exists()


def test_command_mode_rewrites_selection(rig, monkeypatch):
    c, ui, inserted, _, _ = rig
    seen = {}

    def fake_command(self, instruction, selection, token):
        seen["args"] = (instruction, selection)
        return "TEXTO REESCRITO"

    monkeypatch.setattr(ctl.Cleaner, "active", property(lambda self: True))
    monkeypatch.setattr(ctl.Cleaner, "command", fake_command)
    monkeypatch.setattr(ctl.inserter, "snapshot", lambda: [])
    monkeypatch.setattr(ctl.inserter, "copy_selection", lambda: "texto selecionado")
    c.spoken["text"] = "deixe mais formal"
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "command", t0 + 0.05)
    key(c, "release", t0 + 0.1)           # short, but command mode always finishes
    assert wait_for(lambda: inserted == ["TEXTO REESCRITO"])
    assert seen["args"] == ("deixe mais formal", "texto selecionado")
    assert any(n == "recording" and a[0].value == "command" for n, a in ui.calls)
    assert c.history.entries()[0]["mode"] == "command"


def test_command_mode_without_llm_reports_error(rig):
    c, ui, inserted, _, _ = rig
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "command", t0 + 0.05)
    key(c, "release", t0 + 1.0)
    assert wait_for(lambda: any(n == "error" and "modo comando" in a[0] for n, a in ui.calls))
    assert inserted == []


def test_paste_last(rig):
    c, _, inserted, _, _ = rig
    c.post("paste_last", time.monotonic())
    hold(c)
    assert wait_for(lambda: inserted == ["Olá mundo"])
    assert wait_for(lambda: not c._jobs)        # paste-last waits for running jobs to finish
    c.post("paste_last", time.monotonic())
    assert wait_for(lambda: inserted == ["Olá mundo", "Olá mundo"])


def test_second_dictation_while_first_transcribes_keeps_order(rig, monkeypatch):
    c, ui, inserted, gate, _ = rig
    texts = iter(["primeira frase", "segunda frase"])
    first = {"hold": True}

    def ordered_transcribe(wav, cfg, env, paths_, token, notices=None):
        text = next(texts)
        while text == "primeira frase" and first["hold"]:   # first one is slow
            token.check()
            time.sleep(0.01)
        return text

    monkeypatch.setattr(ctl, "transcribe", ordered_transcribe)
    hold(c)
    assert wait_for(lambda: c.machine.busy)
    hold(c)                                   # starts at once, no waiting
    time.sleep(0.4)
    assert inserted == []                     # the second must not jump the queue
    first["hold"] = False
    assert wait_for(lambda: inserted == ["Primeira frase", "Segunda frase"])
    assert wait_for(lambda: not c.machine.busy)


def test_esc_cancels_every_pending_transcription(rig, monkeypatch):
    c, ui, inserted, _, _ = rig

    def slow(wav, cfg, env, paths_, token, notices=None):
        while True:
            token.check()
            time.sleep(0.01)

    monkeypatch.setattr(ctl, "transcribe", slow)
    hold(c)
    hold(c)
    assert wait_for(lambda: len(c._jobs) == 2)
    key(c, "esc")
    assert wait_for(lambda: not c._jobs and not c.machine.busy)
    time.sleep(0.2)
    assert inserted == []


def test_focus_moved_keeps_text_on_clipboard_instead_of_pasting(rig, monkeypatch):
    c, ui, inserted, _, _ = rig

    def refuse(text, cfg, check, target=None):
        raise ctl.inserter.NotPasted("A janela mudou: o texto ficou na área de transferência (Ctrl+V).")

    monkeypatch.setattr(ctl.inserter, "insert", refuse)
    hold(c)
    assert wait_for(lambda: any(n == "flash" and "janela mudou" in a[0] for n, a in ui.calls))
    assert c.last_text == "Olá mundo" and not c._jobs
    assert c.history.entries()[0]["text"] == "Olá mundo"


def test_extra_hotkey_validation():
    from biraflow.hotkey import parse_hotkey as ph

    main = ph("ctrl+win")
    assert ctl.extra_hotkey_problem("command", ph("ctrl+win+alt"), main) is None
    assert ctl.extra_hotkey_problem("command", ph("ctrl+shift+space"), main)   # unrelated chord
    assert ctl.extra_hotkey_problem("command", ph("ctrl+win"), main)           # same chord
    assert ctl.extra_hotkey_problem("paste_last", ph("shift+alt+z"), main) is None
    assert ctl.extra_hotkey_problem("paste_last", ph("ctrl+win+v"), main)      # would start dictation
    assert ctl.extra_hotkey_problem("paste_last", ph("ctrl"), main)            # inside the main chord
    s = Settings()
    s.hotkey.keys = "rctrl"                    # default command (ctrl+win+alt) no longer fits
    errors = []
    extras = ctl.Controller._parse_extras(s, errors)
    assert "command" not in extras and "paste_last" in extras and errors


def test_learned_event_adds_rule_and_flashes(rig):
    from biraflow.store import Dictionary
    from biraflow.textproc import Replacement

    c, ui, _, _, paths = rig
    c.post("learned", [Replacement("transcriber", "transcrever")])
    assert wait_for(lambda: any(n == "flash" and "transcriber → transcrever" in a[0] for n, a in ui.calls))
    assert [(r.heard, r.write) for r in Dictionary.load(paths.dictionary).replacements] == [("transcriber", "transcrever")]


def test_undo_last_learned_correction(rig):
    from biraflow.store import Dictionary
    from biraflow.textproc import Replacement

    c, ui, _, _, paths = rig
    d = Dictionary.load(paths.dictionary)
    d.add_replacements([Replacement("github", "GitHub")])
    d.save(paths.dictionary)
    c.post("learned", [Replacement("ese", "esse"), Replacement("github", "Github")])
    assert wait_for(lambda: len(c.last_learned) == 2)
    c.post("unlearn")
    assert wait_for(lambda: not c.last_learned)
    assert wait_for(lambda: [(r.heard, r.write) for r in Dictionary.load(paths.dictionary).replacements]
                    == [("github", "GitHub")])



def dictate(c, at=None):
    t0 = time.monotonic() if at is None else at
    key(c, "press", t0)
    key(c, "release", t0 + 1.0)


def test_cancel_by_voice_inserts_nothing(rig):
    c, ui, inserted, _, _ = rig
    c.spoken["text"] = "manda isso pro João, cancelar ditado."
    dictate(c)
    assert wait_for(lambda: ("flash", ("Ditado cancelado", "info", 1500)) in ui.calls)
    assert inserted == []


def test_short_dictation_skips_the_llm(rig, monkeypatch):
    c, _, inserted, _, _ = rig
    calls = []
    monkeypatch.setattr(ctl.Cleaner, "active", property(lambda self: True))
    monkeypatch.setattr(ctl.Cleaner, "clean", lambda self, raw, *a, **k: calls.append(raw) or raw.upper())
    c.spoken["text"] = "pode ser"
    dictate(c)
    assert wait_for(lambda: inserted)
    assert inserted == ["Pode ser"] and calls == []
    c.spoken["text"] = "pode ser amanhã cedo"
    dictate(c, time.monotonic() + 3)
    assert wait_for(lambda: len(inserted) == 2)
    assert calls == ["Pode ser amanhã cedo"]


def test_whisper_gets_dictionary_hints_and_previous_sentence(rig):
    from biraflow.store import Dictionary

    c, _, inserted, _, paths = rig
    d = Dictionary.load(paths.dictionary)
    d.add_replacements([Replacement("github", "GitHub")])
    d.save(paths.dictionary)
    c.spoken["text"] = "O Kayth chegou. Vamos começar"
    dictate(c)
    assert wait_for(lambda: inserted)
    assert "GitHub" in c.cfgs[0].vocabulary and c.cfgs[0].context == ""
    dictate(c, time.monotonic() + 3)
    assert wait_for(lambda: len(inserted) == 2)
    assert c.cfgs[1].context == "Vamos começar"          # same app, a moment later


def test_ignored_app_does_not_start_recording(rig):
    c, ui, _, _, _ = rig
    c.settings.hotkey.ignore_apps = ["WhatsApp.exe"]
    key(c, "press")
    time.sleep(0.3)
    assert c.recorder.started == 0
    c.settings.hotkey.ignore_apps = []


def test_failed_transcription_keeps_audio_for_retry(rig):
    c, ui, inserted, _, _ = rig
    c.spoken["error"] = "sem internet"
    dictate(c)
    assert wait_for(lambda: c.has_failed_audio)
    assert wait_for(lambda: any(n == "flash" and "Transcrever de novo" in a[0] for n, a in ui.calls)
                    or any(n == "error" for n, a in ui.calls))
    c.spoken["error"] = ""
    c.post("retry_audio")
    assert wait_for(lambda: inserted == ["Olá mundo"])
    assert wait_for(lambda: not c.has_failed_audio)


def test_real_word_edits_are_not_learned(rig, monkeypatch):
    from biraflow import spell
    from biraflow.store import Dictionary

    c, _, _, _, paths = rig
    real = {"casa", "caso", "esse", "esta", "está", "cloud", "claude", "dr", "adr", "joão", "josé"}
    monkeypatch.setattr(spell, "is_real_word", lambda w, lang="": w.lower() in real)
    c.post("learned", [Replacement("casa", "caso"), Replacement("ese", "esse"), Replacement("esta", "está"),
                       Replacement("cloud", "Claude"), Replacement("DR", "ADR"), Replacement("João", "José")])
    assert wait_for(lambda: Dictionary.load(paths.dictionary).replacements)
    time.sleep(0.2)
    # names and acronyms Whisper wrote as real words are learned; edits between ordinary words are not
    assert {(r.heard, r.write) for r in Dictionary.load(paths.dictionary).replacements} == {
        ("ese", "esse"), ("cloud", "Claude"), ("DR", "ADR")}



def test_undo_last_dictation_by_voice(rig, monkeypatch):
    c, ui, inserted, _, _ = rig
    deleted = []
    monkeypatch.setattr(ctl.inserter, "backspace", lambda n: deleted.append(n))
    monkeypatch.setattr(ctl.win32, "foreground_root", lambda: 0)
    c.spoken["text"] = "manda o arquivo amanhã cedo"
    dictate(c)
    assert wait_for(lambda: inserted)
    c.spoken["text"] = "apagar último ditado"
    dictate(c, time.monotonic() + 3)
    assert wait_for(lambda: deleted)
    assert deleted == [len("Manda o arquivo amanhã cedo") + 1]          # + the trailing space
    assert wait_for(lambda: any(n == "flash" and a[0] == "Último ditado apagado" for n, a in ui.calls))


def test_undo_refuses_after_typing(rig, monkeypatch):
    c, ui, inserted, _, _ = rig
    deleted = []
    monkeypatch.setattr(ctl.inserter, "backspace", lambda n: deleted.append(n))
    monkeypatch.setattr(ctl.win32, "foreground_root", lambda: 0)
    c.spoken["text"] = "manda o arquivo amanhã cedo"
    dictate(c)
    assert wait_for(lambda: inserted)
    key(c, "other")                                     # the user typed something
    c.spoken["text"] = "apagar último ditado"
    dictate(c, time.monotonic() + 3)
    assert wait_for(lambda: any(n == "flash" and a[0].startswith("Nada para apagar") for n, a in ui.calls))
    assert deleted == []


def test_handsfree_stops_after_silence(rig):
    c, _, inserted, _, _ = rig
    c.settings.recording.handsfree_silence_stop = 1
    t0 = time.monotonic()
    key(c, "press", t0)
    key(c, "release", t0 + 0.05)
    key(c, "press", t0 + 0.2)                            # double tap: hands-free
    assert wait_for(lambda: inserted, timeout=6)          # stopped and transcribed by itself


def test_locked_screen_discards_recording(rig, monkeypatch):
    c, _, inserted, _, _ = rig
    monkeypatch.setattr(ctl.win32, "session_locked", lambda: True)
    key(c, "press")
    assert wait_for(lambda: c.recorder.aborted >= 1)
    assert not c.machine.recording


def test_learn_from_screenshot_on_clipboard(rig, monkeypatch):
    from PIL import Image

    from biraflow import vision
    from biraflow.store import Dictionary

    c, ui, _, _, paths = rig
    seen = {}

    def fake(image, recent, env, cfg, **kw):
        seen["size"] = image.size
        return [vision.Found("TASP", "Taspio", False)]

    monkeypatch.setattr(vision, "clipboard_image", lambda: Image.new("RGB", (40, 20)))
    monkeypatch.setattr(vision, "corrections_from_image", fake)
    c.post("learn_screenshot")
    assert wait_for(lambda: [(r.heard, r.write) for r in Dictionary.load(paths.dictionary).replacements]
                    == [("TASP", "Taspio")])
    assert seen["size"] == (40, 20)
    assert wait_for(lambda: c.last_learned and c.last_learned[0][0].write == "Taspio")


def test_learn_from_screenshot_without_image(rig, monkeypatch):
    from biraflow import vision

    c, ui, _, _, _ = rig
    monkeypatch.setattr(vision, "clipboard_image", lambda: None)
    c.post("learn_screenshot")
    assert wait_for(lambda: any(n == "flash" and "print" in a[0] for n, a in ui.calls))


def test_voice_activation_dictates_without_the_phrase(rig, monkeypatch):
    """ "Ei Bira, manda o arquivo" spoken: the audio comes from the wake listener, the
    hotkey tap ends it, and the phrase does not reach the text."""
    from biraflow.audio import Recorder

    c, ui, inserted, _, _ = rig
    c.recorder = Recorder()          # external mode: no microphone is opened

    class FakeWake:
        busy = False

        def take_over(self, sink):
            sink(np.full(16000, 0.1, dtype=np.float32))
            return True

        def release(self):
            pass

        def stop(self):
            pass

    c.wake = FakeWake()
    c.spoken["text"] = "Ei, Bira, manda o arquivo"
    c.post("voice_call")
    assert wait_for(lambda: c.machine.recording)
    key(c, "press")                  # a tap ends a hands-free recording
    assert wait_for(lambda: inserted)
    assert inserted == ["Manda o arquivo"]


def test_failed_retry_keeps_the_other_saved_dictations(rig):
    from biraflow.audio import write_wav

    c, _, _, _, _ = rig
    c.failed_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i, n in enumerate((16000, 32000, 48000)):
        f = c.failed_dir / f"falha-{1000 + i}.wav"
        write_wav(f, np.full(n, 0.1, dtype=np.float32))
        files.append(f)
    job = ctl.Job(1, ctl.CancelToken(), ctl.Mode.HOLD, ctl.AppContext(), 3.0, retry_path=files[-1])
    for _ in range(2):
        assert c._keep_failed_audio(np.full(48000, 0.1, dtype=np.float32), job)
    assert sorted(c._failed_files()) == files      # nothing pushed out, no duplicates


def test_no_media_mute_during_meeting(rig):                                # T5.03 (docs/PLANO-REUNIOES.md)
    c, _, inserted, _, _ = rig
    muted = []
    c.muter = type("M", (), {"mute": lambda self: muted.append(1), "restore": lambda self: None})()
    c.settings.ui.mute_media = True
    c.meeting_active = lambda: True        # the meeting records what the PC plays: never silence it
    hold(c)
    assert wait_for(lambda: len(inserted) == 1) and muted == []
    c.meeting_active = lambda: False
    hold(c)
    assert wait_for(lambda: len(inserted) == 2) and muted == [1]


def test_empty_middle_job_does_not_let_the_next_paste_first(rig, monkeypatch):
    c, _, inserted, _, _ = rig
    texts = iter(["primeira frase", "", "terceira frase"])
    first = {"hold": True}

    def tr(wav, cfg, env, paths_, token, notices=None):
        text = next(texts)
        while text == "primeira frase" and first["hold"]:
            token.check()
            time.sleep(0.01)
        if not text:
            time.sleep(0.4)   # noise: comes back empty while the third job already waits on it
        return text

    monkeypatch.setattr(ctl, "transcribe", tr)
    for n in (1, 2, 3):
        hold(c)
        assert wait_for(lambda: c._job_seq == n)
    time.sleep(0.6)
    assert inserted == []          # the third waits for the first, not just for the empty second
    first["hold"] = False
    assert wait_for(lambda: len(inserted) == 2)
    assert inserted == ["Primeira frase", "Terceira frase"]


def test_esc_keeps_the_audio_of_a_long_dictation(rig):
    c, ui, inserted, gate, _ = rig
    gate.clear()                   # transcription hangs until cancelled
    c.recorder.stop = lambda: np.full(25 * 16000, 0.1, dtype=np.float32)
    hold(c)
    assert wait_for(lambda: "busy" in ui.names())
    key(c, "esc")
    assert wait_for(lambda: c.has_failed_audio)     # "Transcrever de novo" has it
    gate.set()
    assert inserted == []
