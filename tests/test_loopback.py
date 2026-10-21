"""WASAPI loopback conversion logic, without opening any audio device."""
import threading
import time

import numpy as np
import pytest

from biraflow import loopback


def test_to_mono_float_and_int():
    stereo = np.array([[0.5, -0.5], [1.0, 0.0]], "<f4")
    assert np.allclose(loopback.to_mono(stereo.tobytes(), 2, 32, True), [0.0, 0.5])
    pcm = np.array([[16384, 16384], [-32768, 0]], "<i2")
    assert np.allclose(loopback.to_mono(pcm.tobytes(), 2, 16, False), [0.5, -0.5])


def test_converter_carries_leftover_samples():
    conv = loopback.Converter(48_000)
    out = [conv.push(np.ones(n, np.float32)) for n in (1000, 1000, 2)]
    assert sum(o.size for o in out) == 2002 // 3 and conv.pending.size == 2002 % 3
    conv = loopback.Converter(44_100)  # non-integer factor: 0.1 s blocks
    total = sum(conv.push(np.zeros(441, np.float32)).size for _ in range(100))
    assert total == 16_000 and conv.pending.size == 0


def test_gap_frames_zero_fills_only_past_the_slack():
    assert loopback.gap_frames(10.0, 9.9, 48_000) == 0
    assert loopback.gap_frames(10.0, 7.0, 48_000) == 144_000


def test_gap_fill_is_capped_and_long_gaps_are_pauses():           # T0.01 [R11]
    assert loopback.gap_frames(8 * 3600, 0, 48_000) == loopback.MAX_FILL * 48_000
    lb = loopback.Loopback()
    conv = loopback.Converter(48_000)
    lb._t0 = time.monotonic() - 15            # 15 s behind: filled, 10 s at a time
    lb._catch_up(conv)
    assert lb.read().size == 10 * 16_000 and lb.pauses == []
    lb._catch_up(conv)
    assert abs(lb.read().size - 5 * 16_000) < 16_000 * 0.1
    lb._t0 -= 2 * 3600                         # the PC slept two hours: a pause, no zeros
    lb._catch_up(conv)
    assert lb.read().size == 0
    assert len(lb.pauses) == 1 and lb.pauses[0][0] == lb._emitted and abs(lb.pauses[0][1] - 7200) < 1
    lb._catch_up(conv)                         # the clock was moved past the pause
    assert lb.read().size == 0 and len(lb.pauses) == 1


def test_communications_endpoint_captured_only_when_different():   # T0.02 [R10]
    C, M = loopback.CONSOLE, loopback.COMMUNICATIONS
    assert loopback.capture_roles("{a}", "{a}") == [C]
    assert loopback.capture_roles("{a}", None) == [C]
    assert loopback.capture_roles("{a}", "{b}") == [C, M]


def test_mix_aligned_sums_what_both_have_and_carries_the_rest():
    carry = [np.zeros(0, np.float32), np.zeros(0, np.float32)]
    out, carry = loopback.mix_aligned(carry, [np.full(5, 0.5, np.float32), np.full(3, 0.25, np.float32)])
    assert np.allclose(out, 0.75) and out.size == 3 and [c.size for c in carry] == [2, 0]
    out, carry = loopback.mix_aligned(carry, [np.full(1, 0.9, np.float32), np.full(4, 0.9, np.float32)])
    assert out.size == 3 and out.max() <= 1.0 and [c.size for c in carry] == [0, 1]   # clipped, never > 1


def test_unsupported_mix_format_fails_at_open():                     # T0.03
    with pytest.raises(OSError):
        loopback.to_mono(bytes(6), 2, 24, False)


def test_a_part_that_never_opens_still_follows_the_clock(monkeypatch):
    # console + communications: a part that cannot open must not hold back the other one
    lb = loopback.Loopback()

    def broken(reopening):
        lb._stop.set()
        raise OSError("em uso exclusivo")

    monkeypatch.setattr(lb, "_session", broken)
    lb._t0 = time.monotonic() - 2
    t = threading.Thread(target=lb._run)
    t.start()
    t.join(5)
    assert abs(lb.read().size - 2 * 16_000) < 16_000 * 0.1


def test_reopen_restarts_the_gap_filling(monkeypatch):
    lb = loopback.Loopback()
    monkeypatch.setattr(lb, "_run", lambda: None)   # no device
    lb._emitted = 60 * 16_000                       # a minute captured before the reopen
    lb.start()
    lb._t0 -= 3
    lb._catch_up(loopback.Converter(48_000))
    assert abs(lb.read().size - 3 * 16_000) < 16_000 * 0.1
