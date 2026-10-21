"""Scoring for the long-dictation / meeting smoke tests (docs/PLANO-REUNIOES.md). Test tool
only: not part of the installer. Fixtures come from scripts/make-test-audio.ps1.

  python scripts/smoke.py anchors FIXTURE.wav TEXT     # TEXT: literal text, a text file, or - (stdin)
  python scripts/smoke.py speakers SEGMENTS.jsonl GROUND_TRUTH.json

anchors:  % of the fixture's anchor words found in the transcript (counted with repeats, so a
          dropped chunk of a long file shows). Exit code 1 below --min (default 90).
speakers: % of the ground truth's speech time whose "who" label matches the segments'.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path


def _words(text: str) -> Counter:
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return Counter(re.findall(r"[a-z]+", plain))


def anchor_recall(expected: list[str], text: str) -> tuple[float, list[str]]:
    want, got = Counter(expected), _words(text)
    found = sum(min(n, got[w]) for w, n in want.items())
    missing = sorted(w for w, n in want.items() if got[w] < n)
    return 100.0 * found / max(1, sum(want.values())), missing


def speaker_accuracy(segments: list[dict], truth: list[dict]) -> float:
    """% of the speech the segments cover that has the right label. Whisper ends segments
    ~0.8 s before the speech does; that uncovered tail is a timing matter (turns_on_time,
    anchors), not a wrong speaker, so it is left out."""
    covered = right = 0.0
    for g in truth:
        for s in segments:
            overlap = max(0.0, min(g["t1"], s["t1"]) - max(g["t0"], s["t0"]))
            covered += overlap
            right += overlap if s["who"] == g["who"] else 0.0
    return 100.0 * right / max(covered, 1e-9)


def _read_text(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    p = Path(arg)
    return p.read_text(encoding="utf-8") if len(arg) < 260 and p.is_file() else arg


def turns_on_time(rows: list[dict], truth: list[dict], tolerance: float = 2.0) -> float:
    """% of the ground truth's turns whose start has a transcript row starting within `tolerance` s."""
    starts = [r["t0"] for r in rows]
    hit = sum(1 for g in truth if any(abs(s - g["t0"]) <= tolerance for s in starts))
    return 100.0 * hit / max(1, len(truth))


class FileSource:
    """One channel of a WAV played on `clock` (a meeting source for headless smoke tests)."""

    def __init__(self, data, clock, speed: float = 1.0):
        self.data, self.clock, self.speed = data, clock, speed
        self.alive, self.peak = True, float(abs(data).max()) if len(data) else 0.0
        self._t = self._pos = 0

    def start(self):
        self._t = self.clock()

    def read(self):
        import numpy as np

        n = max(0, int((self.clock() - self._t) * 16000 * self.speed) - self._pos)
        out = self.data[self._pos:self._pos + n]
        if out.size < n:   # the file ended: silence, as a quiet room would give
            out = np.concatenate((out, np.zeros(n - out.size, np.float32)))
        self._pos += n
        return out

    def stop(self):
        pass


def meeting(args) -> int:
    import os
    import time
    from dataclasses import replace

    import soundfile as sf

    from biraflow import config, meeting as mt, transcribe as tr
    from biraflow.config import Paths

    home = args.home or Path(os.environ.get("BIRAFLOW_HOME", ""))
    user = Paths(home, home) if str(home) not in ("", ".") else Paths.default()
    settings = config.parse_settings(config.read_text(user.settings))[0] if user.settings.is_file() else config.Settings()
    cfg = settings.transcription
    cfg = replace(cfg, engine=args.engine or cfg.engine, whisper_cli=str(tr.find_whisper_cli(cfg, user) or ""),
                  local_model=str(tr.resolve_model(cfg, user)))
    settings.transcription = cfg
    env = config.load_env(user)
    run = Paths(args.out, args.out)
    run.ensure()
    stereo, rate = sf.read(str(args.fixture), dtype="float32", always_2d=True)
    assert rate == 16000 and stereo.shape[1] == 2, "a 16 kHz stereo WAV"
    real0 = time.monotonic()
    clock = lambda: real0 + (time.monotonic() - real0) * args.speed   # noqa: E731
    mic = FileSource(stereo[:, 0], clock)
    pc = FileSource(stereo[:, 1], clock, speed=1.0 + args.drift)
    session = mt.MeetingSession(run, settings, env, sources=(mic, pc), clock=clock, tick=1.0 / args.speed)
    m = session.start()
    seconds = len(stereo) / 16000
    while session.elapsed < seconds:
        time.sleep(0.05)
    t_stop = time.monotonic()
    session.stop(wait=600)
    waited = time.monotonic() - t_stop
    rows = [r for r in m.rows() if "who" in r]
    statuses = [p["status"] for p in m.state["pieces"]]
    print(f"reunião: {seconds:.0f} s tocados a {args.speed:g}x em {t_stop - real0:.0f} s; resultado "
          f"{waited:.1f} s depois de encerrar; {len(statuses)} pedaços {dict((s, statuses.count(s)) for s in set(statuses))}; "
          f"{len(rows)} segmentos; motor {resolve(cfg, env)}")
    truth_file = args.fixture.with_suffix(".json")
    ok = m.state["status"] == "pronta"
    if truth_file.is_file() and "segments" in json.loads(truth_file.read_text(encoding="utf-8-sig")):
        truth = json.loads(truth_file.read_text(encoding="utf-8-sig"))["segments"]
        acc, timing = speaker_accuracy(rows, truth), turns_on_time(rows, truth)
        print(f"falantes certos: {acc:.1f}% do tempo de fala; turnos com minutagem a ±2 s: {timing:.1f}%")
        ok = ok and acc >= 95 and timing >= 90
    print(f"pasta: {m.folder}")
    return 0 if ok else 1


def resolve(cfg, env) -> str:
    from biraflow import transcribe as tr

    try:
        return tr.resolve_engine(cfg, env)
    except Exception as e:
        return f"? ({e})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("anchors")
    a.add_argument("fixture", type=Path, help="fala-*.wav (its .json lists the anchors)")
    a.add_argument("text")
    a.add_argument("--min", type=float, default=90.0)
    s = sub.add_parser("speakers")
    s.add_argument("segments", type=Path)
    s.add_argument("truth", type=Path)
    m = sub.add_parser("meeting", help="a whole meeting, headless, from a stereo WAV (left = mic, right = PC)")
    m.add_argument("fixture", type=Path, help="reuniao-2vozes.wav (its .json, if any, says who speaks when)")
    m.add_argument("--out", type=Path, required=True, help="folder for the meeting (never the user's own)")
    m.add_argument("--home", type=Path, help="Bira Flow data folder to read settings/.env/model from")
    m.add_argument("--engine", default="", help="groq / openai / gateway / local (default: the user's)")
    m.add_argument("--speed", type=float, default=8.0, help="how much faster than real time to play it")
    m.add_argument("--drift", type=float, default=0.0, help="the PC channel's clock off by this fraction (0.0005)")
    args = ap.parse_args(argv)

    if args.cmd == "meeting":
        return meeting(args)

    if args.cmd == "anchors":
        expected = json.loads(args.fixture.with_suffix(".json").read_text(encoding="utf-8-sig"))["anchors"]
        recall, missing = anchor_recall(expected, _read_text(args.text))
        print(f"anchors: {recall:.1f}% of {len(expected)}" + (f"  faltando: {', '.join(missing)}" if missing else ""))
        return 0 if recall >= args.min else 1
    lines = args.segments.read_text(encoding="utf-8-sig").splitlines()
    segments = [r for r in (json.loads(line) for line in lines if line.strip()) if "who" in r]
    truth = json.loads(args.truth.read_text(encoding="utf-8-sig"))["segments"]
    print(f"speakers: {speaker_accuracy(segments, truth):.1f}% do tempo de fala com o rotulo certo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
