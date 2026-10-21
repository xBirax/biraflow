import threading
import time

from biraflow.editwatch import EditWatcher, corrections, locate


def test_locate_between_anchors():
    assert locate("Oi! Manda o arquivo ese aqui. Valeu", "Oi! ", ". Valeu") == "Manda o arquivo ese aqui"
    assert locate("abc", "zzz", "") is None
    assert locate("início texto", "início ", "") == "texto"


def test_corrections_only_spelling_and_few():
    assert [(r.heard, r.write) for r in corrections("o arquivo ese aqui", "o arquivo esse aqui")] == [("ese", "esse")]
    assert corrections("ele disse não", "ele disse sim") == []           # content edit
    assert corrections("a b c d e f", "a b c d e f") == []
    many = corrections("um dois três quatro cinco seis sete oito",
                       "uma duas trẽs quatra cinca seiss setee oitoo")
    assert many == []                                                    # a rewrite, not a fix


class FakeReader:
    def __init__(self, frames):
        self.frames = list(frames)
        self.reads = 0

    def text(self):
        self.reads += 1
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


def run(frames, pasted="Manda o arquivo ese aqui", fg=lambda: 7, settle=0.2, wait=2.5):
    learned = []
    done = threading.Event()
    w = EditWatcher(lambda rules: (learned.extend(rules), done.set()), reader_factory=lambda: FakeReader(frames),
                    foreground=fg, poll=0.02, land=0.01, bind_retry=0.02, settle=settle, watch_for=3)
    w.watch(pasted, 7)
    done.wait(wait)
    w.finalize()
    return [(r.heard, r.write) for r in learned]


def test_learns_after_edit_settles():
    before = "Chat: Manda o arquivo ese aqui"
    after = "Chat: Manda o arquivo esse aqui"
    assert run([before, before, after]) == [("ese", "esse")]


def test_learns_last_version_when_message_is_sent():
    sent = ""  # field cleared after Enter
    assert run(["Manda o arquivo ese aqui", "Manda o arquivo esse aqui", sent], settle=10) == [("ese", "esse")]


def test_nothing_when_app_does_not_expose_text_or_focus_moves():
    assert run(["texto que não contém o ditado"], wait=0.3) == []
    assert run(["Manda o arquivo ese aqui"], fg=lambda: 99, wait=0.3) == []


def test_finalize_learns_immediately():
    learned = []
    frames = ["Manda o arquivo ese aqui", "Manda o arquivo esse aqui"]
    reader = FakeReader(frames)
    frames = reader.frames
    w = EditWatcher(learned.extend, reader_factory=lambda: reader, foreground=lambda: 7,
                    poll=0.02, land=0.01, bind_retry=0.02, settle=60, watch_for=60)
    w.watch("Manda o arquivo ese aqui", 7)
    end = time.monotonic() + 5
    while reader.reads < 3 and time.monotonic() < end:   # until the edited text was seen
        time.sleep(0.02)
    time.sleep(0.1)
    w.finalize()          # a new dictation arrives: learn now
    assert [(r.heard, r.write) for r in learned] == [("ese", "esse")]


def test_reads_only_after_key_presses():
    learned, typed = [], [0]
    frames = ["Manda o arquivo ese aqui", "Manda o arquivo esse aqui"]
    reader = FakeReader(frames)
    w = EditWatcher(learned.extend, reader_factory=lambda: reader, foreground=lambda: 7,
                    poll=0.02, land=0.01, bind_retry=0.02, settle=0.3, watch_for=5, activity=lambda: typed[0])
    w.watch("Manda o arquivo ese aqui", 7)
    time.sleep(0.3)
    assert reader.reads == 1          # only the first read right after the paste
    typed[0] += 3                     # the user edits
    end = time.monotonic() + 3
    while not learned and time.monotonic() < end:
        time.sleep(0.02)
    assert [(r.heard, r.write) for r in learned] == [("ese", "esse")]
    assert reader.reads <= 3


def test_stop_does_not_block():
    class SlowReader:
        def text(self):
            time.sleep(2)
            return "x"

    w = EditWatcher(lambda r: None, reader_factory=SlowReader, foreground=lambda: 7, poll=0.02, land=0.01, bind_retry=0.02)
    w.watch("x", 7)
    time.sleep(0.1)
    t0 = time.monotonic()
    w.stop()
    assert time.monotonic() - t0 < 0.05


def test_normalize_browser_text():
    from biraflow.editwatch import normalize

    assert normalize("Manda\xa0o arquivo​ ese aqui\xa0\r\n") == "Manda o arquivo ese aqui \n"


class BindingReader:
    """Like UiaReader on a browser: the field shows up only after a few tries (the page's
    accessibility tree is still being built) and reports spaces as no-break spaces."""

    def __init__(self, frames, misses=2):
        self.frames, self.misses, self.binds = list(frames), misses, 0

    def bind(self, pasted):
        self.binds += 1
        if self.binds <= self.misses:
            return None
        return self.text()

    def text(self):
        from biraflow.editwatch import normalize

        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return normalize(frame)


def test_binds_to_field_once_browser_exposes_it():
    reader = BindingReader(["Manda\xa0o arquivo ese aqui\xa0", "Manda\xa0o arquivo esse aqui\xa0"])
    learned, done = [], threading.Event()
    w = EditWatcher(lambda r: (learned.extend(r), done.set()), reader_factory=lambda: reader,
                    foreground=lambda: 7, poll=0.02, land=0.01, bind_retry=0.02, settle=0.2, watch_for=5)
    w.watch("Manda o arquivo ese aqui ", 7)
    done.wait(4)
    w.finalize()
    assert reader.binds == 3
    assert [(r.heard, r.write) for r in learned] == [("ese", "esse")]


def test_default_reader_gets_the_target_window():
    import biraflow.editwatch as ew

    got = []

    class Spy:
        def __init__(self, target=0):
            got.append(target)

        def text(self):
            return None

    orig = ew.UiaReader
    ew.UiaReader = Spy
    try:
        w = ew.EditWatcher(lambda r: None, foreground=lambda: 42, poll=0.02, land=0.01, bind_retry=0.02)
        w.watch("x", 42)
        time.sleep(0.2)
        w.stop()
    finally:
        ew.UiaReader = orig
    assert got == [42]


def test_pauses_while_another_window_is_in_front():
    """Dictate, switch to another app (e.g. to take a screenshot), come back and fix it."""
    fg = {"now": 7}
    frames = ["Oi Kate, bom dia!", "Oi Kayth, bom dia!"]
    reader = FakeReader(frames)
    learned, done = [], threading.Event()
    w = EditWatcher(lambda r: (learned.extend(r), done.set()), reader_factory=lambda: reader,
                    foreground=lambda: fg["now"], poll=0.02, land=0.01, bind_retry=0.02, settle=0.2, watch_for=10)
    w.watch("Oi Kate, bom dia!", 7)
    end = time.monotonic() + 3
    while reader.reads < 1 and time.monotonic() < end:
        time.sleep(0.01)
    fg["now"] = 99                     # another window in front
    time.sleep(0.25)
    assert not learned and reader.reads == 1   # nothing read from the app while away
    fg["now"] = 7                      # back to the chat
    done.wait(3)
    w.finalize()
    assert [(r.heard, r.write) for r in learned] == [("Kate", "Kayth")]
