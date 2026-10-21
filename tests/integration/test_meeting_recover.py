"""S3.3 of the plan: the app is killed in the middle of a meeting (taskkill /F); at the next start
recover() transcribes what was left and at most one piece (~90 s) of audio is lost."""
import json
import subprocess
import sys
import time
from dataclasses import asdict

from pathlib import Path

import pytest

from biraflow import meeting as mt
from biraflow.config import Settings

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]

CHILD = r"""
import json, sys, time
from pathlib import Path
import importlib.util
import soundfile as sf
from biraflow import meeting as mt
from biraflow.config import Paths, Settings, TranscriptionCfg

root, out, cfg = sys.argv[1], Path(sys.argv[2]), json.loads(sys.argv[3])
env = json.loads(sys.stdin.readline())   # API keys: never on the command line
spec = importlib.util.spec_from_file_location("smoke", Path(root) / "scripts" / "smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
stereo, _ = sf.read(sys.argv[4], dtype="float32", always_2d=True)
real0 = time.monotonic()
clock = lambda: real0 + (time.monotonic() - real0) * 8.0
settings = Settings()
settings.transcription = TranscriptionCfg(**cfg)
paths = Paths(out, out)
paths.ensure()
session = mt.MeetingSession(paths, settings, env, sources=(smoke.FileSource(stereo[:, 0], clock),
                            smoke.FileSource(stereo[:, 1], clock)), clock=clock, tick=1 / 8.0, auto_notes=False)
session.start()
print("started", flush=True)
time.sleep(3600)   # killed long before
"""


def test_killed_meeting_is_recovered(user, fixtures, run_paths, meeting_engine):
    cfg = meeting_engine("any")
    env = user[2]
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT), str(run_paths.data), json.dumps(asdict(cfg)),
                              str(fixtures / "reuniao-2vozes.wav")], cwd=str(ROOT), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        child.stdin.write(json.dumps(env) + "\n")
        child.stdin.close()
        assert child.stdout.readline().strip() == "started"
        base = mt.meetings_dir(run_paths)
        end = time.monotonic() + 120
        while time.monotonic() < end:   # ~4 pieces written (6 min of meeting at 8x = 45 s)
            # the audio files, not reuniao.json: reading it could make the child's os.replace fail
            if len(list(base.glob("*/audio/parte-*.ogg"))) >= 4:
                break
            time.sleep(0.2)
        else:
            pytest.fail("the child never wrote 4 pieces")
        time.sleep(30.0 / 8)   # 30 more seconds of meeting, not yet in a piece
    finally:
        child.kill()            # like taskkill /F: no stop(), no finish()
        child.wait(10)
    state = json.loads(next(base.glob("*/reuniao.json")).read_text(encoding="utf-8"))
    played = state["pieces"][-1]["start"] + state["pieces"][-1]["seconds"] + 30.0
    settings = Settings()
    settings.transcription = cfg
    recovered = mt.recover(run_paths, settings, env, auto_notes=False)
    assert len(recovered) == 1
    m = recovered[0]
    print(f"\nrecuperada: {m.seconds:.0f} s de {played:.0f} s tocados, "
          f"{sum('text' in r for r in m.rows())} segmentos")
    assert m.state["status"] == "pronta"
    assert {p["status"] for p in m.state["pieces"]} <= {"feita", "vazia"}
    assert played - m.seconds <= 90.0                     # lost at most one piece
    assert any("text" in r for r in m.rows())
