import time

import numpy as np

from biraflow.appcontext import AppContext, classify
from biraflow.audio import normalize
from biraflow.store import Dictionary, History, Stats
from biraflow.textproc import Replacement, Snippet


def test_dictionary_roundtrip_and_upsert(tmp_path):
    path = tmp_path / "dictionary.json"
    d = Dictionary([Replacement("ese", "esse")], [Snippet("meu email", "a@b.c")])
    d.save(path)
    d2 = Dictionary.load(path)
    assert d2 == d
    changed = d2.add_replacements([Replacement("ESE", "Esse"), Replacement("ese", "Esse"),
                                   Replacement("github", "GitHub")])
    assert [r.heard for r in d2.replacements] == ["ESE", "github"]
    assert len(changed) == 2
    assert Dictionary.load(tmp_path / "missing.json") == Dictionary([], [])


def test_history_retention(tmp_path):
    h = History(tmp_path / "h.json", keep="always", max_items=3)
    for i in range(5):
        h.add(raw=f"r{i}", text=f"texto {i}", app="x.exe", mode="hold", seconds=1)
    items = h.entries()
    assert [e["text"] for e in items] == ["texto 4", "texto 3", "texto 2"]
    h.update(items[0]["id"], text="corrigido")
    assert h.entries()[0]["text"] == "corrigido"
    h.delete(items[1]["id"])
    assert len(h.entries()) == 2

    h.keep = "24h"
    old = h.entries()[0]
    h.update(old["id"], ts=time.time() - 90000)
    assert all(e["id"] != old["id"] for e in h.entries())

    h.keep = "never"
    h.prune()
    assert h.entries() == [] and not (tmp_path / "h.json").exists()
    assert h.add(raw="x", text="y", app="", mode="hold", seconds=1) is None


def test_stats(tmp_path):
    s = Stats(tmp_path / "s.json")
    assert s.summary() == "Nenhum ditado ainda."
    s.add(words=150, seconds=60)
    s.add(words=50, seconds=30)
    r = s.read()
    assert r["dictations"] == 2 and r["words"] == 200
    assert round(r["wpm"]) == 133
    assert round(r["saved_minutes"], 1) == 3.5        # 200/40 = 5 min typing - 1.5 min speaking
    assert "2 ditados" in s.summary()


def test_classify_apps():
    assert classify(AppContext("whatsapp.exe", "")) == "personal"
    assert classify(AppContext("ms-teams.exe", "")) == "work"
    assert classify(AppContext("olk.exe", "")) == "email"
    assert classify(AppContext("code.exe", "x.py - Visual Studio Code")) == "code"
    assert classify(AppContext("chrome.exe", "Caixa de entrada (3) - bira@gmail.com - Gmail")) == "email"
    assert classify(AppContext("msedge.exe", "WhatsApp - Pessoal")) == "personal"
    assert classify(AppContext("applicationframehost.exe", "WhatsApp")) == "personal"
    assert classify(AppContext("notepad.exe", "Sem título")) == "other"
    assert AppContext("windowsterminal.exe").is_terminal and not AppContext("code.exe").is_terminal


def test_normalize_boosts_quiet_but_not_loud():
    quiet = np.sin(np.linspace(0, 200, 16000)).astype(np.float32) * 0.05
    out = normalize(quiet)
    assert abs(float(np.max(np.abs(out))) - 0.4) < 0.01     # capped at 8x gain
    loud = quiet * 16
    assert normalize(loud) is loud                           # already loud: untouched
    assert normalize(np.zeros(10, np.float32)).size == 10


def test_level_meter_scale_is_logarithmic():
    from biraflow.audio import level_from_rms

    assert level_from_rms(0) == 0.0
    assert level_from_rms(0.00004) == 0.0              # room noise (-88 dB): flat
    assert 0.2 < level_from_rms(0.003) < 0.4           # quiet speech still moves the bars
    assert 0.55 < level_from_rms(0.02) < 0.8           # normal speech
    assert level_from_rms(0.5) == 1.0


def test_stats_today(tmp_path):
    from biraflow.store import Stats

    s = Stats(tmp_path / "stats.json")
    assert s.today() == (0, 0)
    s.add(10, 3.0)
    s.add(5, 2.0)
    assert s.today() == (2, 15)
    assert s.read()["dictations"] == 2


def test_dictionary_that_is_not_an_object_loads_empty(tmp_path):
    from biraflow.store import Dictionary

    path = tmp_path / "dictionary.json"
    path.write_text("[1, 2]", encoding="utf-8")   # edited by hand into a list: no rules, not a crash
    d = Dictionary.load(path)
    assert d.replacements == [] and d.snippets == []


def test_dictionary_learn_adds_to_the_file_now(tmp_path):
    from biraflow.store import Dictionary, Replacement

    path = tmp_path / "dictionary.json"
    Dictionary([Replacement("ese", "esse")], []).save(path)
    assert Dictionary.learn(path, [Replacement("ese", "esse"), Replacement("cloud", "Claude")]) == \
        [Replacement("cloud", "Claude")]                     # only what changed
    assert [(r.heard, r.write) for r in Dictionary.load(path).replacements] == [("ese", "esse"), ("cloud", "Claude")]
    assert Dictionary.learn(path, [Replacement("cloud", "Claude")]) == []
