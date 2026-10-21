"""Meeting mode in the app (Fase 5 of docs/PLANO-REUNIOES.md): settings, tray, App.toggle_meeting,
quit, the updater and the result window. No audio device, no network, no focus taken.
Ids T5.xx follow the plan."""
import threading
import time

import pytest

from biraflow import config, meeting as mt
from biraflow.app import App
from biraflow.cleanup import Cleaner
from biraflow.config import Paths, Settings
from biraflow.store import History

NOTES = ("# Orçamento\n## Resumo\nFalamos do orçamento.\n## Decisões\n- Aprovado [00:00:05]\n"
         "## Próximos passos\n- [ ] Ana — enviar planilha — sexta\n## Pontos em aberto\nNenhum.")


class Stub:
    pass


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    return p


def said(paths, status="pronta"):
    m = mt.MeetingFolder.create(mt.meetings_dir(paths))
    m.add_piece(0.0, 90.0)
    m.set_piece(0, status="feita")
    m.append([{"t0": 5.0, "t1": 9.0, "who": "Eu", "text": "Vamos aprovar o orçamento.", "piece": 0, "run": 1}])
    m.set(status=status)
    return m


# --- settings ------------------------------------------------------------------------

def test_meeting_defaults_and_roundtrip():                                   # T5.01
    assert "[meeting]" in config.DEFAULT_SETTINGS_TOML
    s, errors = config.parse_settings(config.DEFAULT_SETTINGS_TOML)
    assert errors == [] and s.meeting == config.MeetingCfg()
    s.meeting.system_audio, s.meeting.keep_audio, s.meeting.auto_notes, s.meeting.max_hours = False, False, False, 6
    back, errors = config.parse_settings(config.render_settings(s))
    assert errors == [] and back.meeting == s.meeting


def test_meeting_invalid_values():                                           # T5.02
    s, errors = config.parse_settings('[meeting]\nmax_hours = "x"\n')
    assert s.meeting.max_hours == 4 and any("max_hours" in e for e in errors)
    assert config.parse_settings("[meeting]\nmax_hours = 0\n")[0].meeting.max_hours == 1
    assert config.parse_settings("[meeting]\nmax_hours = 20\n")[0].meeting.max_hours == 8


# --- the App, without Tk, hook or devices ----------------------------------------------

class FakeSession:
    made: list = []

    def __init__(self, paths, settings, env, **kw):
        self.paths, self.kw, self.stopped, self.auto_notes = paths, kw, [], kw["auto_notes"]
        self.elapsed = 754.0
        FakeSession.made.append(self)

    def start(self):
        time.sleep(0.2)   # opening the devices takes a moment: clicks meanwhile are ignored
        self.folder = self.meeting = said(self.paths, status="gravando")

    def stop(self, wait=60.0):
        self.stopped.append(wait)
        mt.finish(self.folder, True)
        return self.folder


@pytest.fixture
def app(paths, monkeypatch):
    from biraflow import win32

    FakeSession.made = []
    monkeypatch.setattr(mt, "MeetingSession", FakeSession)
    asked = []
    monkeypatch.setattr(win32, "confirm", lambda text, title="": asked.append(text) or True)
    a = App.__new__(App)
    a.paths, a.state = paths, {}
    a.controller = Stub()
    a.controller.settings = Settings()
    a.controller.machine = Stub()
    a.controller.machine.recording = False
    a.controller._jobs = []
    a.controller.posted = []
    a.controller.post = lambda *ev: a.controller.posted.append(ev)
    a.ui = Stub()
    a.ui.errors, a.ui.calls = [], []
    a.ui.error = a.ui.errors.append
    a.ui.refresh_menu = lambda: None
    a.ui._call = a.ui.calls.append
    a.tray = Stub()
    a.tray.notes = []
    a.tray.notify = a.tray.notes.append
    a.root = Stub()
    a.root.quit = lambda: None
    a.asked = asked
    yield a
    a.meeting = None


def test_toggle_meeting_debounced(app):                                      # T5.06 [R25]
    for _ in range(3):
        app.toggle_meeting()
    assert wait_for(lambda: app.meeting is not None and not app._meeting_busy)
    assert len(FakeSession.made) == 1 and app.ui.errors == []
    assert app.state["meeting_consent"] is True and len(app.asked) == 1
    assert app.tray.notes.count("Aguarde: a reunião está iniciando ou encerrando.") == 2   # clicks while busy
    assert "Reunião iniciada" in app.tray.notes[-1]
    kw = FakeSession.made[0].kw
    assert kw["max_hours"] == 4 and kw["system_audio"] and kw["auto_notes"]
    app.controller.machine.recording = True                                   # T5.04: dictation goes first
    assert kw["busy"]()
    app.toggle_meeting()                                                       # stop
    assert wait_for(lambda: app.meeting is None and not app._meeting_busy)
    assert FakeSession.made[0].stopped and len(app.ui.calls) == 2              # tick + show_meeting
    app.toggle_meeting()
    assert wait_for(lambda: len(FakeSession.made) == 2 and not app._meeting_busy)
    assert len(app.asked) == 1                                                 # consent asked only once


def test_in_person_meeting_from_the_tray(app):
    app.toggle_meeting(in_person=True)
    assert wait_for(lambda: app.meeting is not None and not app._meeting_busy)
    assert FakeSession.made[0].kw["in_person"] is True
    assert "Reunião presencial iniciada" in app.tray.notes[-1] and "sem o áudio do PC" in app.tray.notes[-1]
    app.toggle_meeting()                                                       # the usual item stops it
    assert wait_for(lambda: app.meeting is None and not app._meeting_busy)
    assert FakeSession.made[0].stopped


def test_consent_refused_starts_nothing(app, monkeypatch):
    from biraflow import win32

    monkeypatch.setattr(win32, "confirm", lambda text, title="": False)
    app.toggle_meeting()
    assert wait_for(lambda: not app._meeting_busy)
    assert app.meeting is None and FakeSession.made == [] and "meeting_consent" not in app.state


def test_meeting_menu_label(app):                                            # T5.08
    assert app.meeting_menu_text() == "Iniciar reunião"
    app.meeting, app.meeting_since = FakeSession(app.paths, None, None, auto_notes=True), "14:30"
    assert app.meeting_menu_text() == "Encerrar reunião (desde 14:30)"
    from biraflow.tray import Tray

    tray = Tray.__new__(Tray)
    tray.app = app
    app.controller.stats = Stub()
    app.controller.stats.today = lambda: (0, 0)
    assert tray._tooltip("idle").endswith("Reunião gravando: 00:12:34")


def test_updater_not_idle_during_meeting(app):                              # T5.05 [R9]
    assert app.is_idle()
    app._meeting_busy = True                                                  # starting, stopping, recovering
    assert not app.is_idle()
    app._meeting_busy = False
    app.meeting = object()
    assert not app.is_idle()


def test_quit_during_meeting_stops_it_quickly(app):                         # T5.07
    session = FakeSession(app.paths, None, None, auto_notes=True)
    session.start()
    app.meeting = session
    app.quit()                                                                # off the tray thread
    assert wait_for(lambda: ("quit",) in app.controller.posted)
    assert session.stopped == [5] and app.meeting is None                     # a short wait; recover() does the rest
    app.toggle_meeting()                                                       # e.g. max_hours on the last tick
    time.sleep(0.1)
    assert len(FakeSession.made) == 1 and app.meeting is None                 # nothing starts while quitting


def test_recovery_does_not_block_a_new_meeting(app):                         # review: Fase 5 #1
    app._recovering = True
    assert not app.is_idle()
    app.toggle_meeting()
    assert wait_for(lambda: app.meeting is not None and not app._meeting_busy)


def test_retry_keeps_a_meeting_with_pending_pieces_unfinished(paths, monkeypatch):   # review: Fase 5 #2
    m = said(paths)
    m.add_piece(90.0, 90.0)
    m.set_piece(1, status="pendente")
    m.set_piece(0, status="falhou")
    seen = []

    def offline(self, i):
        seen.append(i)
        raise ConnectionError("sem rede")

    monkeypatch.setattr(mt.PieceWorker, "_transcribe", offline)
    assert mt.retry_failed(m, paths, Settings(), {}) == 2 and seen == [0, 1]
    assert m.state["status"] == "interrompida"                                # recover() tries again
    assert {p["status"] for p in m.state["pieces"]} == {"pendente"}           # offline is not "falhou"


# --- the result window --------------------------------------------------------------

@pytest.fixture
def window(paths, tk_root):
    from biraflow.audio_ui import AudioWindow

    app = Stub()
    app.root, app.paths, app.settings_window, app.audio_window = tk_root, paths, None, None
    app.controller = Stub()
    app.controller.settings = Settings()
    app.controller.history = History(paths.history)
    w = AudioWindow(app, background=True)
    yield w, tk_root
    if w.top.winfo_exists():
        w.close()


def settle(w, root, timeout=5.0):
    end = time.monotonic() + timeout
    while w.job is not None and time.monotonic() < end:
        root.update()
        time.sleep(0.02)
    root.update()


def test_show_meeting_renders(window, paths):                               # T5.09 / T5.11
    w, root = window
    m = said(paths)
    w.show_meeting(m.folder)
    root.update()
    assert w.top.title().startswith("Reunião — ") and "(1 min)" in w.top.title()
    assert "Vamos aprovar o orçamento." in w.text.get("1.0", "end")
    assert w.b_save.cget("text") == "Salvar como…" and w.meeting_actions.winfo_manager() == "pack"
    assert w.b_retry.winfo_manager() == ""                                    # nothing failed
    assert "Transcrição pronta" in w.l_status.cget("text")
    m.set_piece(0, status="falhou")
    w.show_meeting(m.folder)
    assert w.b_retry.winfo_manager() == "pack" and "não foram transcritos" in w.l_status.cget("text")
    w.clear()
    assert w.meeting is None and w.meeting_actions.winfo_manager() == ""


def test_generate_notes_button(window, paths, monkeypatch):                 # T5.10
    w, root = window
    monkeypatch.setattr(Cleaner, "_request", lambda self, system, user, timeout_s, max_tokens=2048: NOTES)
    monkeypatch.setattr(config, "load_env", lambda paths_: {"GROQ_API_KEY": "k"})
    m = said(paths)
    w.show_meeting(m.folder)
    w.meeting_notes()
    settle(w, root)
    assert (m.folder / "notas.md").read_text(encoding="utf-8") == NOTES + "\n"
    text = w.text.get("1.0", "end")
    assert text.index("Falamos do orçamento.") < text.index("Vamos aprovar o orçamento.")
    assert w.l_status.cget("text") == "Notas prontas."


def test_retry_button_transcribes_failed_pieces(window, paths, monkeypatch):
    w, root = window
    m = said(paths)
    m.set_piece(0, status="falhou")
    seen = []
    monkeypatch.setattr(mt.PieceWorker, "_transcribe",
                        lambda self, i: seen.append(i) or self.meeting.set_piece(i, status="feita"))
    monkeypatch.setattr(config, "load_env", lambda paths_: {})
    w.show_meeting(m.folder)
    w.meeting_retry()
    settle(w, root)
    assert seen == [0] and w.b_retry.winfo_manager() == ""
    assert mt.MeetingFolder.open(m.folder).state["status"] == "pronta"


def test_quit_while_stopping_does_not_stop_twice(app, monkeypatch):           # review 2 #1
    app.toggle_meeting()
    assert wait_for(lambda: app.meeting is not None and not app._meeting_busy)
    session = app.meeting
    real = session.stop
    monkeypatch.setattr(session, "stop", lambda wait=60.0: time.sleep(0.3) or real(wait))
    app.toggle_meeting()                                                       # Encerrar...
    assert wait_for(lambda: app._meeting_busy)
    app.quit()                                                                 # ...then Sair at once
    assert wait_for(lambda: ("quit",) in app.controller.posted)
    assert session.stopped == [60.0]                                           # one stop, the user's
