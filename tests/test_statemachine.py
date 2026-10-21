from biraflow.statemachine import (CANCEL, DISCARD, FINISH, MODE, START, DictationMachine,
                                      Mode, Phase)


def kinds(actions):
    return [a.kind for a in actions]


def make():
    return DictationMachine(tap_max=0.25, double_tap_window=0.4, max_seconds=300)


def test_hold_to_talk():
    m = make()
    assert kinds(m.press(0.0)) == [START]
    assert m.phase is Phase.PRESSED
    acts = m.release(2.0)
    assert kinds(acts) == [FINISH] and acts[0].mode is Mode.HOLD
    assert m.phase is Phase.IDLE              # free for the next dictation at once


def test_single_short_tap_is_dropped_after_window():
    m = make()
    m.press(0.0)
    assert m.release(0.1) == []
    assert m.phase is Phase.AWAIT_SECOND_TAP
    assert m.tick(0.3) == []
    acts = m.tick(0.51)
    assert kinds(acts) == [DISCARD] and acts[0].reason == "tap"
    assert m.phase is Phase.IDLE


def test_double_tap_enters_handsfree_and_next_tap_stops():
    m = make()
    m.press(0.0)
    m.release(0.1)
    acts = m.press(0.3)
    assert kinds(acts) == [MODE] and acts[0].mode is Mode.HANDSFREE
    assert m.release(0.35) == []          # releasing the second tap changes nothing
    assert m.tick(60) == []
    acts = m.press(61)
    assert kinds(acts) == [FINISH] and acts[0].mode is Mode.HANDSFREE
    assert m.release(61.1) == []
    assert m.phase is Phase.IDLE


def test_handsfree_auto_stops_at_max_measured_from_start():
    m = make()
    m.press(10.0)
    m.release(10.1)
    m.press(10.3)
    assert m.tick(309.9) == []
    acts = m.tick(310.0)
    assert kinds(acts) == [FINISH] and acts[0].reason == "timeout"
    assert m.phase is Phase.IDLE


def test_hold_is_capped_too():
    m = make()
    m.press(0.0)
    acts = m.tick(300.0)
    assert kinds(acts) == [FINISH] and acts[0].mode is Mode.HOLD
    assert m.release(301) == []  # the late release is ignored


def test_second_tap_too_late_starts_a_new_hold():
    m = make()
    m.press(0.0)
    m.release(0.1)
    acts = m.press(0.6)  # window ended at 0.5; tick wasn't called in between
    assert kinds(acts) == [DISCARD, START]
    assert m.phase is Phase.PRESSED


def test_escape_drops_recording_in_every_recording_phase():
    for setup in (
        lambda m: m.press(0.0),
        lambda m: (m.press(0.0), m.release(0.1)),
        lambda m: (m.press(0.0), m.release(0.1), m.press(0.2)),
    ):
        m = make()
        setup(m)
        acts = m.escape(0.25)
        assert kinds(acts) == [DISCARD] and acts[0].reason == "esc"
        assert m.phase is Phase.IDLE
        assert m.release(0.3) == []


def test_escape_cancels_running_jobs_only_when_busy():
    m = make()
    m.press(0.0)
    m.release(1.0)
    m.busy = True
    assert kinds(m.escape(1.5)) == [CANCEL]
    m.busy = False
    assert m.escape(1.6) == []  # nothing left to cancel


def test_next_dictation_can_start_while_previous_transcribes():
    m = make()
    m.press(0.0)
    m.release(1.0)
    m.busy = True
    assert kinds(m.press(1.2)) == [START]
    acts = m.escape(1.3)                 # Esc while recording drops the recording,
    assert kinds(acts) == [DISCARD]      # not the transcription running behind it
    assert m.phase is Phase.IDLE


def test_other_key_discards_hold_and_pending_tap_but_not_handsfree():
    m = make()
    m.press(0.0)
    assert kinds(m.other_key(0.05)) == [DISCARD]   # e.g. hotkey used as a shortcut
    m = make()
    m.press(0.0)
    m.release(0.1)
    assert kinds(m.other_key(0.2)) == [DISCARD]    # user is typing after a stray tap
    m = make()
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)
    assert m.other_key(1.0) == []                  # typing during hands-free is fine
    assert m.phase is Phase.HANDSFREE


def test_command_mode_from_hold_finishes_as_command_even_if_short():
    m = make()
    m.press(0.0)
    acts = m.command(0.05)
    assert kinds(acts) == [MODE] and acts[0].mode is Mode.COMMAND
    assert m.command(0.06) == []               # only once
    acts = m.release(0.1)                     # shorter than tap_max, still finishes
    assert kinds(acts) == [FINISH] and acts[0].mode is Mode.COMMAND
    m.press(1.0)
    acts = m.release(2.0)
    assert acts[0].mode is Mode.HOLD           # the flag does not leak into the next one


def test_command_in_handsfree_and_ignored_when_idle():
    m = make()
    assert m.command(0.0) == []
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)
    assert kinds(m.command(1.0)) == [MODE]
    acts = m.press(2.0)
    assert acts[0].mode is Mode.COMMAND



def test_finish_handsfree_from_outside():
    from biraflow.statemachine import FINISH, DictationMachine, Mode

    m = DictationMachine(tap_max=0.2, double_tap_window=0.4)
    assert m.finish_handsfree("silence") == []            # idle: nothing to stop
    m.press(0.0)
    m.release(0.05)
    m.press(0.2)                                          # double tap: hands-free
    actions = m.finish_handsfree("silence")
    assert [a.kind for a in actions] == [FINISH] and actions[0].mode is Mode.HANDSFREE
    assert not m.recording
