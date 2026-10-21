import pytest

from biraflow.hotkey import VK_ESCAPE, ChordTracker, HotkeyError, parse_hotkey

LCTRL, RCTRL, LWIN, CAPS, SPACE, C = 0xA2, 0xA3, 0x5B, 0x14, 0x20, 0x43


def test_parse_names():
    hk = parse_hotkey("Ctrl + Win")
    assert hk.parts == (frozenset({0xA2, 0xA3}), frozenset({0x5B, 0x5C}))
    assert hk.needs_mask
    assert hk.suppressible == frozenset()
    assert parse_hotkey("right ctrl").parts == (frozenset({RCTRL}),)
    assert parse_hotkey("F9").parts == (frozenset({0x78}),)
    assert parse_hotkey("f24").parts == (frozenset({0x87}),)
    assert parse_hotkey("vkE9").parts == (frozenset({0xE9}),)
    assert parse_hotkey("ctrl+alt+space").suppressible == frozenset({SPACE})
    assert not parse_hotkey("rctrl").needs_mask


@pytest.mark.parametrize("spec", ["fn", "esc", "", "ctrl+", "ctrl+ctrl", "hyper", "f25"])
def test_parse_rejects(spec):
    with pytest.raises(HotkeyError):
        parse_hotkey(spec)


def test_injection_vks_puts_modifiers_first():
    assert parse_hotkey("v+ctrl").injection_vks() == [LCTRL, 0x56]
    assert parse_hotkey("shift+insert").injection_vks() == [0xA0, 0x2D]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def tracker(spec, suppress=True):
    events = []
    clock = Clock()
    tr = ChordTracker(parse_hotkey(spec), lambda kind, t: events.append(kind), suppress, clock)
    return tr, events, clock


def test_chord_press_release_and_repeats():
    tr, ev, _ = tracker("ctrl+win")
    tr.handle(LCTRL, True)
    assert ev == []
    tr.handle(LWIN, True)
    assert ev == ["press"]
    tr.handle(LWIN, True)  # auto-repeat
    tr.handle(LCTRL, True)
    assert ev == ["press"]
    tr.handle(LCTRL, False)
    assert ev == ["press", "release"]
    tr.handle(LWIN, False)
    assert ev == ["press", "release"]


def test_esc_and_other_keys_reported_never_swallowed():
    tr, ev, _ = tracker("rctrl")
    assert tr.handle(VK_ESCAPE, True) is False
    assert tr.handle(VK_ESCAPE, True) is False  # repeat: no second event
    assert tr.handle(C, True) is False
    assert ev == ["esc", "other"]


def test_modifier_hotkey_never_swallowed():
    tr, _, _ = tracker("rctrl")
    assert tr.handle(RCTRL, True) is False
    assert tr.handle(RCTRL, False) is False


def test_capslock_swallowed_down_repeat_and_up():
    tr, ev, _ = tracker("capslock")
    assert tr.handle(CAPS, True) is True
    assert tr.handle(CAPS, True) is True
    assert tr.handle(CAPS, False) is True
    assert ev == ["press", "release"]


def test_suppression_only_when_chord_completes():
    tr, ev, _ = tracker("ctrl+space")
    assert tr.handle(SPACE, True) is False      # plain space is typed normally
    assert tr.handle(SPACE, False) is False
    tr.handle(LCTRL, True)
    assert tr.handle(SPACE, True) is True
    assert tr.handle(SPACE, False) is True
    assert ev == ["other", "press", "release"]   # the plain space was typing


def test_suppress_off():
    tr, _, _ = tracker("capslock", suppress=False)
    assert tr.handle(CAPS, True) is False


def test_disabled_reports_nothing_and_swallows_nothing():
    tr, ev, _ = tracker("capslock")
    tr.set_enabled(False)
    assert tr.handle(CAPS, True) is False
    tr.handle(CAPS, False)
    assert ev == []


def test_stale_keys_released_only_when_silent_and_up():
    tr, ev, clock = tracker("ctrl+win")
    tr.handle(LCTRL, True)
    tr.handle(LWIN, True)
    clock.t = 1.0
    tr.drop_stale(lambda vk: False)
    assert ev == ["press"]                     # still within auto-repeat window
    clock.t = 3.0
    tr.drop_stale(lambda vk: True)
    assert ev == ["press"]                     # OS says it's down: keep
    tr.drop_stale(lambda vk: False)
    assert ev == ["press", "release"]          # missed key-up (e.g. after Win+L)
    assert not tr.active


def test_chord_capture_and_spec_roundtrip():
    from biraflow.hotkey import ChordCapture, chord_spec, display_hotkey

    cap = ChordCapture()
    for vk, down in [(LCTRL, True), (LWIN, True), (LWIN, True), (LWIN, False)]:
        cap.feed(vk, down)
    assert not cap.done
    cap.feed(LCTRL, False)
    assert cap.done and cap.spec == "ctrl+win"
    assert parse_hotkey(cap.spec).parts == parse_hotkey("ctrl+win").parts

    assert chord_spec([RCTRL]) == "rctrl"
    assert chord_spec([0xA5, SPACE]) == "alt+space"
    assert chord_spec([0x78]) == "f9"
    assert chord_spec([CAPS]) == "capslock"
    assert chord_spec([0xE9]) == "vkE9"
    for vks in ([RCTRL], [LCTRL, 0xA0, C], [0x87], [0x60], [0xE9], [CAPS]):
        parse_hotkey(chord_spec(vks))  # always a valid spec

    cap = ChordCapture()
    assert cap.feed(0xA0, False) is False      # Shift held from before: its key-up passes
    assert cap.feed(LCTRL, True) is True
    assert cap.feed(VK_ESCAPE, True) is True
    assert cap.done and cap.cancelled
    assert cap.feed(LCTRL, False) is False     # after done, everything passes

    assert display_hotkey("ctrl+win") == "Ctrl + Win"
    assert display_hotkey("rctrl") == "Ctrl direito"
    assert display_hotkey("ctrl+alt+space") == "Ctrl + Alt + Espaço"
    assert display_hotkey("f9") == "F9"


def test_extra_chords_command_and_paste_last():
    events = []
    clock = Clock()
    tr = ChordTracker(parse_hotkey("ctrl+win"), lambda k, t: events.append(k), True, clock,
                      extras={"command": parse_hotkey("ctrl+win+alt"), "paste_last": parse_hotkey("shift+alt+z")})
    LALT, LSHIFT, Z = 0xA4, 0xA0, 0x5A
    tr.handle(LCTRL, True)
    tr.handle(LWIN, True)
    tr.handle(LALT, True)            # adding Alt is not an "other" key
    assert events == ["press", "command"]
    tr.handle(LALT, False)
    tr.handle(LWIN, False)
    tr.handle(LCTRL, False)
    assert events == ["press", "command", "release"]

    events.clear()
    tr.handle(LALT, True)            # Alt first, then Ctrl+Win: both at once
    tr.handle(LCTRL, True)
    tr.handle(LWIN, True)
    assert events == ["press", "command"]
    for vk in (LWIN, LCTRL, LALT):
        tr.handle(vk, False)

    events.clear()
    tr.handle(LSHIFT, True)
    tr.handle(LALT, True)
    assert tr.handle(Z, True) is True    # the Z of Shift+Alt+Z is not typed
    assert events == ["paste_last"]
    assert tr.handle(Z, False) is True
    assert tr.handle(Z, True) is True and events == ["paste_last", "paste_last"]
