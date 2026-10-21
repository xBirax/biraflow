"""The settings window, driven for real but invisible (background mode: off to the side,
transparent, click-through, never activated), so these tests never take the focus from
whatever the user is doing. The window is built with `layout=False` (see the `ui` fixture):
its widgets exist and can be read, but Tk never measures or paints them."""
import json
import tkinter as tk

import pytest

from biraflow import config
from biraflow.config import Paths, Settings
from biraflow.store import Dictionary, History
from biraflow.textproc import Replacement


class Stub:
    pass


@pytest.fixture
def root(tk_root):
    return tk_root


def _window(tmp_path, monkeypatch, root, layout: bool, **kw):
    """Build the settings window for a test. `layout` goes straight to SettingsWindow: the
    tests here read widget state (values, texts, which manager holds a widget), never
    pixels, so they skip the first layout and paint, which costs seconds per window on a
    busy PC."""
    from biraflow import settings_ui

    paths = Paths(tmp_path, tmp_path)
    paths.ensure()
    monkeypatch.setattr(settings_ui.setup_local, "has_nvidia_gpu", lambda: False)
    app = Stub()
    app.root, app.paths, app.enabled = root, paths, True
    app.controller = Stub()
    app.controller.settings, _ = config.load_settings(paths)
    app.controller.history = History(paths.history)
    app.settings_window = None
    app.engine_summary = lambda: "Transcrição: local · Limpeza: none"
    app.start_capture = app.stop_capture = app.open_settings_file = lambda *a: None
    app.open_data_folder = app.open_log = app.open_readme = lambda *a: None
    app.applied = []
    app.apply_settings = lambda *a: app.applied.append(a)
    app.reopened = 0
    app.show_settings = lambda *a: setattr(app, "reopened", app.reopened + 1)
    app.state = {}
    w = settings_ui.SettingsWindow(app, background=True, layout=layout, **kw)
    yield w, app, paths, root
    if w.top.winfo_exists():
        w.close()


@pytest.fixture
def ui(tmp_path, monkeypatch, root):
    """The window with its widget tree only: nothing measured, mapped or painted."""
    yield from _window(tmp_path, monkeypatch, root, layout=False)


@pytest.fixture
def ui_shown(tmp_path, monkeypatch, root):
    """The same window, laid out and mapped off-screen. Only for a test that looks at the
    window itself (its geometry, what Tk has mapped), not at its content."""
    yield from _window(tmp_path, monkeypatch, root, layout=True)


@pytest.fixture
def ui_lazy(tmp_path, monkeypatch, root):
    """Built the way the real window is: the start page first, the others later."""
    yield from _window(tmp_path, monkeypatch, root, layout=False, lazy=True)


def test_lazy_window_builds_pages_on_demand(ui_lazy):
    from biraflow.settings_ui import NAV_KEYS

    w, app, *_ = ui_lazy
    assert list(dict(w.pages)) == ["general"]           # only the start page before the window shows
    assert "history" in w.pages                          # the tray can still open any page
    w.show("history")
    built = list(dict(w.pages))
    assert built == list(NAV_KEYS[:NAV_KEYS.index("history") + 1])   # in order: later pages reach into earlier ones
    assert w.collect() == app.controller.settings       # Salvar before the rest was built
    assert list(dict(w.pages)) == list(NAV_KEYS)


def test_lazy_window_closes_before_every_page_exists(ui_lazy):
    w, *_ = ui_lazy
    assert not w.dirty()                                 # nothing changed yet: closing asks nothing
    w.close()                                            # no AttributeError from a page not built yet
    assert not w.top.winfo_exists()


def test_every_page_builds_and_collects_the_same_settings(ui):
    w, app, *_ = ui
    for key in w.pages:
        w.show(key)
    assert w.collect() == app.controller.settings
    assert not w.dirty()


def test_changes_are_detected_and_saved(ui):
    w, app, *_ = ui
    w.v_sounds.set(not w.v_sounds.get())
    assert w.dirty()
    w.save()
    settings = app.applied[0][0]
    assert settings.ui.sounds is not app.controller.settings.ui.sounds


def test_the_language_picker_reopens_the_window_in_the_new_language(ui):
    """Changing the language is the one setting that needs the window to be rebuilt."""
    from biraflow import i18n

    w, app, *_ = ui
    w.show("general")
    assert w.c_language.get() == "auto"
    w.c_language.var.set("English")      # what the user picks in the drop-down
    assert w.c_language.get() == "en"
    w.save()
    assert app.applied[0][0].ui.language == "en"
    assert app.reopened == 1
    assert i18n.language() == "en"
    i18n.set_language("pt-BR")     # the other tests expect the source language


def test_hotkey_shows_keycaps(ui):
    w, *_ = ui
    caps = [c.cget("text") for c in w.f_hotkey.keycap.winfo_children()]
    assert caps == ["Ctrl", "+", "Win"]
    w.f_command.var.set("")
    assert [c.cget("text") for c in w.f_command.keycap.winfo_children()] == ["Desativado"]


def test_dictionary_rows_empty_state_and_keyboard(ui):
    w, *_ = ui
    w.show("dictionary")
    assert w.tree_rep.empty.winfo_manager() == "place"        # "Nenhuma correção ainda"
    w.v_heard.set("ese")
    w.v_write.set("esse")
    w._rep_add()
    assert w.tree_rep.get_children() == ("0",) and w.tree_rep.empty.winfo_manager() == ""
    w.tree_rep.selection_set("0")
    w._rep_remove()
    assert w.dictionary.replacements == []


def test_dictionary_export_and_import(ui, tmp_path, monkeypatch):
    from tkinter import filedialog

    w, *_ = ui
    w.dictionary.add_replacements([Replacement("github", "GitHub")])
    out = tmp_path / "dic.json"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **k: str(out))
    w._dict_export()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["replacements"] == [{"heard": "github", "write": "GitHub"}]
    data["replacements"].append({"heard": "ese", "write": "esse"})
    data["snippets"] = [{"trigger": "assinatura", "text": "Bira"}]
    data["vocabulary"] = ["Kayth"]
    out.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **k: str(out))
    w._dict_import()
    assert [(r.heard, r.write) for r in w.dictionary.replacements] == [("github", "GitHub"), ("ese", "esse")]
    assert w.dictionary.snippets[0].trigger == "assinatura"
    assert "Kayth" in w.t_vocab.get("1.0", "end")


def test_history_search_by_app_and_export(ui, tmp_path, monkeypatch):
    from tkinter import filedialog

    w, app, *_ = ui
    h = app.controller.history
    h.add(raw="oi", text="Oi, tudo bem?", app="WhatsApp.exe", mode="hold", seconds=1.0)
    h.add(raw="x", text="git status", app="WindowsTerminal.exe", mode="hold", seconds=1.0)
    w.show("history")
    assert len(w.tree_hist.get_children()) == 2
    w.v_search.set("whatsapp")
    rows = [w.tree_hist.item(i, "values") for i in w.tree_hist.get_children()]
    assert len(rows) == 1 and rows[0][1] == "WhatsApp"
    w.v_search.set("zzz")
    assert w.tree_hist.empty.winfo_manager() == "place"
    out = tmp_path / "h.csv"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **k: str(out))
    w._history_export()
    text = out.read_text(encoding="utf-8-sig")
    assert "Oi, tudo bem?" in text and "Terminal" in text


def test_new_microphone_options_round_trip(ui):
    w, *_ = ui
    w.v_ready.set(True)
    w.v_hf_silence.set("120")
    s = w.collect()
    assert s.recording.keep_mic_ready == 30 and s.recording.handsfree_silence_stop == 120
    w.v_hf_silence.set("abc")
    with pytest.raises(ValueError):
        w.collect()


def test_restore_defaults_and_geometry_memory(ui_shown, monkeypatch):
    from tkinter import messagebox

    w, app, paths, _ = ui_shown
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    w.background = False            # behave like the real window for close()
    w._restore_defaults()
    assert app.applied and app.applied[0][0] == Settings()
    assert "settings_geometry" in config.load_state(paths)


def test_level_meter_colors(root):
    from biraflow.settings_ui import DARK, LevelMeter

    m = LevelMeter(root, DARK, lambda n: n)
    m.set(1.0)
    colors = [m.canvas.itemcget(i, "fill") for i in m.items]
    assert colors[0] == DARK.ok and colors[-1] == DARK.error
    m.set(0)
    assert all(m.canvas.itemcget(i, "fill") == DARK.border for i in m.items)
    m.canvas.destroy()


def test_app_label():
    from biraflow.settings_ui import app_label

    assert app_label("adr-workspace.exe") == "Adr Workspace"
    assert app_label("msedge.exe") == "Edge"
    assert app_label("") == "—"


def test_keyboard_moves_between_pages_and_buttons_need_a_selection(ui):
    w, *_ = ui
    w.show("general")
    w._nav_step(1)
    assert w.current == "hotkey"
    w._nav_step(-2)
    assert w.current == "about"           # wraps around
    w._find_in_history()
    assert w.current == "history"
    assert all(b.instate(["disabled"]) for b in w.b_hist)       # nothing selected yet
    assert w.b_rep_remove.instate(["disabled"])


def test_wake_phrase_hint_warns_about_short_phrases(ui):
    w, *_ = ui
    w.v_wake_phrase.set("Bira")
    assert "Uma palavra só" in w.l_wake_phrase.cget("text")
    w.v_wake_phrase.set("Ei")
    assert "4 letras" in w.l_wake_phrase.cget("text")
    w.v_wake_phrase.set("Ei Bira")
    assert "Duas palavras" in w.l_wake_phrase.cget("text")


def test_meeting_tab_collects_values(ui):                                   # T5.12 (docs/PLANO-REUNIOES.md)
    w, *_ = ui
    w.show("meeting")
    w.v_meet_system.set(False)
    w.v_meet_keep.set(False)
    w.v_meet_notes.set(False)
    w.v_meet_hours.set("6")
    m = w.collect().meeting
    assert (m.system_audio, m.keep_audio, m.auto_notes, m.max_hours) == (False, False, False, 6)
    w.v_meet_hours.set("9")
    with pytest.raises(ValueError) as e:
        w.collect()
    assert e.value.args[0] == "meeting" and "entre 1 e 8" in e.value.args[1]


def test_restore_defaults_after_a_preview_keeps_the_default_pill(ui, monkeypatch):
    from tkinter import messagebox

    w, app, *_ = ui
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    pill = Stub()
    pill.position, pill.size = "top", "large"
    app.ui = Stub()
    app.ui.pill = pill
    w._placement_saved = ("top", "large")      # "Ver como fica" moved the pill before
    app.apply_settings = lambda s, *a: setattr(pill, "position", s.ui.position)
    w._restore_defaults()
    assert pill.position == Settings().ui.position



def test_background_work_never_touches_a_closed_window():
    import threading

    from biraflow.settings_ui import SettingsWindow

    class Top:
        alive = True

        def winfo_exists(self):
            return self.alive

        def after(self, ms, fn):
            pending.append(fn)

    pending, calls = [], []
    w = Stub()
    w.top = Top()
    gate = threading.Event()
    thread = SettingsWindow._background(w, gate.wait, lambda: calls.append("done"), lambda: calls.append("tick"))
    assert calls == ["tick"] and len(pending) == 1          # still running: it polls again later
    gate.set()
    thread.join(2)
    pending.pop()()
    assert calls == ["tick", "tick", "done"] and not pending
    gate.clear()
    thread = SettingsWindow._background(w, gate.wait, lambda: calls.append("late"))
    w.top.alive = False                                      # the window closed meanwhile
    gate.set()
    thread.join(2)
    pending.pop()()
    assert "late" not in calls and not pending
