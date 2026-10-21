import pytest


@pytest.fixture(autouse=True)
def _fresh_network_state(monkeypatch):
    """Each test starts online, with no rate-limited model left over from another test,
    and never sees the user's data folder (BIRAFLOW_HOME set on their machine)."""
    from biraflow import cleanup, transcribe

    monkeypatch.delenv("BIRAFLOW_HOME", raising=False)
    monkeypatch.delenv("WHISPERFLOW_HOME", raising=False)
    monkeypatch.setattr(transcribe, "_offline_until", 0.0)
    monkeypatch.setattr(cleanup, "_cooldown", {})


@pytest.fixture(scope="session")
def tk_root(pytestconfig):
    """One Tk for the whole run: creating and destroying several in a row makes Tcl fail
    to start on Windows now and then."""
    import tkinter as tk

    import sv_ttk

    # Tcl binds its std channels to the handles it finds at startup; created under pytest's
    # setup capture, those handles are closed later and the theme's `source` fails on some
    # PCs ("couldn't read file ... No error"). Start Tcl with capture off and load the theme.
    with pytestconfig.pluginmanager.getplugin("capturemanager").global_and_fixture_disabled():
        r = tk.Tk()
        r.withdraw()
        sv_ttk.set_theme("light")
    yield r
    r.destroy()
