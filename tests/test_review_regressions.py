"""Cases found in the 1.0.4 code review, kept so they never come back."""
import threading

import pytest

from biraflow import textproc as tp


def _finish(raw):
    p = tp.prepare(raw, local=True, spoken=True, send_phrases=["pressione enter"], snippets=[])
    return tp.finish(p.text, p, [])


@pytest.mark.parametrize("raw", [
    "Escolha o emoji certo para a mensagem.",
    "Ele mandou um emoji. Ok, vamos lá.",
    "Não use emoji. Certo?",
    "Mandei um emoji de presente para ela.",
    "Lançamos a nova linha.",
    "Chegamos ao ponto final.",
    "Gostei da nova linha, mas é cara.",
    "O preço da arroba subiu ponto amanhã a gente vende.",
    "Vou de A a Z no relatório.",
])
def test_ordinary_sentences_are_left_alone(raw):
    assert _finish(raw) == raw


def test_spoken_dots_in_email_name():
    assert _finish("joao ponto silva arroba gmail ponto com") == "Joao.silva@gmail.com"


def test_cancel_needs_a_pause():
    assert not tp.wants_cancel("Me explica como cancelar o ditado")
    assert tp.wants_cancel("isso aqui, cancelar ditado.")


def test_smart_insert_keeps_capital_on_new_line_and_no_space_before_punctuation():
    assert tp.adapt_to_context("Espero que esteja bem.", "Olá João,\n\n") == "Espero que esteja bem."
    assert tp.adapt_to_context("Segundo item", "- primeiro item\n") == "Segundo item"
    assert tp.adapt_to_context("?", "Tudo bem") == "?"


def test_ansi_settings_and_env_files_do_not_crash(tmp_path):
    from biraflow import config

    paths = config.Paths(tmp_path, tmp_path)
    paths.settings.write_bytes('[cleanup]\nextra_instructions = "Use português"\n'.encode("cp1252"))
    settings, errors = config.load_settings(paths)
    assert settings.cleanup.extra_instructions == "Use português" and not errors
    paths.env.write_bytes("# chave da Groq (não compartilhe)\nGROQ_API_KEY=gsk_x\n".encode("cp1252"))
    assert config.load_env(paths)["GROQ_API_KEY"] == "gsk_x"


def test_settings_file_keeps_its_newlines_through_backup(tmp_path):
    from biraflow import config

    paths = config.Paths(tmp_path, tmp_path)
    config.save_settings(paths, config.Settings())
    config.load_settings(paths)
    assert b"\r\r\n" not in config.backup_path(paths).read_bytes()


def test_dictionary_saves_from_many_threads(tmp_path):
    from biraflow.store import Dictionary

    path = tmp_path / "dictionary.json"
    errors = []

    def worker(i):
        try:
            for _ in range(20):
                d = Dictionary.load(path)
                d.add_replacements([tp.Replacement(f"w{i}", f"W{i}")])
                d.save(path)
        except Exception as e:  # PermissionError / FileNotFoundError before the fix
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert not list(tmp_path.glob("*.tmp"))


def test_update_manifest_cannot_name_a_path():
    from biraflow import updater as up

    class R:
        def __init__(self, data):
            import json
            self.content = json.dumps(data).encode()

        def raise_for_status(self):
            pass

    good = {"version": "1.0.5", "url": "https://x/BiraFlow-Setup-1.0.5.exe", "sha256": "a" * 64}

    def fetch(data):
        import biraflow.http as http
        orig = http.session
        http.session = lambda: type("S", (), {"get": lambda self, *a, **k: R(data)})()
        try:
            return up.fetch_manifest("https://x/latest.json")
        finally:
            http.session = orig

    assert fetch(good).version == "1.0.5"
    for bad in ({**good, "version": "9.9\\..\\..\\Startup\\x"}, {**good, "version": "1.0:ads"},
                {**good, "sha256": "zz"}, {**good, "url": "http://evil/x.exe"}):
        with pytest.raises(up.UpdateError):
            fetch(bad)


def test_rejected_signatures(monkeypatch):
    from biraflow import updater as up

    for status in ("NotTrusted", "HashMismatch", "NotSigned"):
        monkeypatch.setattr(up, "signature", lambda p, s=status: (s, "AB12"))
        with pytest.raises(up.UpdateError):
            up.verify_signature(None, pinned="AB12")
    monkeypatch.setattr(up, "signature", lambda p: ("UnknownError", "AB12"))
    up.verify_signature(None, pinned="ab12")


def test_merge_keeps_edits_made_in_the_file_meanwhile():
    from biraflow.config import Settings
    from biraflow.settings_ui import merge_changes

    at_open = Settings()
    current = Settings()
    current.history.max_items = 900          # edited in Notepad while the window was open
    edited = Settings()
    edited.ui.sounds = False                 # changed in the window
    merged = merge_changes(current, at_open, edited)
    assert merged.history.max_items == 900 and merged.ui.sounds is False
