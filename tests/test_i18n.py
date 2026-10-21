"""The interface language: catalog, fallback and the words the user speaks."""
import json

import pytest

from biraflow import i18n, textproc
from biraflow.config import Paths, load_settings, save_settings


@pytest.fixture(autouse=True)
def _default_language():
    code = i18n.language()
    yield
    i18n.set_language(code)


def test_fallback_is_the_source_text():
    i18n.set_language("pt-BR")
    assert i18n.t("Ditado ativado") == "Ditado ativado"
    assert i18n.t("") == ""
    assert i18n.t("uma frase que não existe no catálogo") == "uma frase que não existe no catálogo"


def test_english_catalog_translates():
    i18n.set_language("en")
    assert i18n.language() == "en"
    assert i18n.t("Ditado ativado") == "Dictation enabled"
    assert i18n.t("Geral") == "General"


def test_unknown_language_falls_back_to_the_source():
    i18n.set_language("xx")
    assert i18n.language() == "pt-BR"


def test_every_catalog_is_valid():
    for code in i18n.available():
        data = json.loads((i18n.LANG_DIR / f"{code}.json").read_text(encoding="utf-8"))
        assert data["code"] == code
        assert isinstance(data["strings"], dict)
        assert isinstance(data["prompts"], dict)
        assert isinstance(data["spoken"], dict)


def test_no_translation_loses_a_placeholder():
    for code in i18n.available():
        data = json.loads((i18n.LANG_DIR / f"{code}.json").read_text(encoding="utf-8"))
        for key, value in data["strings"].items():
            assert i18n.placeholders(key) == i18n.placeholders(value), f"{code}: {key!r}"


def test_detect_system_language_is_known():
    assert i18n.detect_system_language() in i18n.LANGUAGES


def test_spoken_punctuation_in_english():
    i18n.set_language("en")
    assert textproc.local_rules("hello comma world") == "Hello, world"
    assert textproc.local_rules("really question") == "Really?"


def test_spoken_email_in_english():
    i18n.set_language("en")
    assert textproc.spoken_email("joao at sign gmail dot com") == "joao@gmail.com"


def test_cancel_and_undo_in_english():
    i18n.set_language("en")
    assert textproc.wants_cancel("this is not needed, cancel dictation")
    assert not textproc.wants_cancel("cancel the meeting tomorrow")
    assert textproc.wants_undo("delete last dictation")
    assert not textproc.wants_undo("I will delete last dictation tomorrow")


def test_language_setting_round_trip(tmp_path):
    paths = Paths(tmp_path, tmp_path)
    settings, errors = load_settings(paths)
    assert settings.ui.language == "auto"
    assert not errors
    settings.ui.language = "en"
    save_settings(paths, settings)
    again, errors = load_settings(paths)
    assert again.ui.language == "en"
    assert not errors
