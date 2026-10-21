"""[R30] of the plan: a meeting piece is stereo Opus at 16 kHz; libsndfile must write and read it
back with both channels, in order, and the right length (no fixtures, no network)."""
import numpy as np
import pytest
import soundfile as sf

pytestmark = pytest.mark.integration


def test_stereo_opus_roundtrip(tmp_path):
    rate, seconds = 16000, 90.0
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    left = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)      # "Eu"
    right = np.zeros_like(left)                                          # "Participantes" quiet...
    right[rate * 10:rate * 20] = 0.3 * np.sin(2 * np.pi * 220 * t[rate * 10:rate * 20])   # ...but for 10-20 s
    path = tmp_path / "parte-000.ogg"
    sf.write(str(path), np.column_stack((left, right)), rate, format="OGG", subtype="OPUS")
    data, got_rate = sf.read(str(path), dtype="float32", always_2d=True)
    assert got_rate == rate and data.shape[1] == 2
    assert abs(data.shape[0] / rate - seconds) <= 0.02
    rms = lambda x: float(np.sqrt(np.mean(x ** 2)))   # noqa: E731
    assert rms(data[:, 0]) > 0.15                                         # left kept its tone
    assert rms(data[rate * 30:rate * 80, 1]) < 0.01                       # right silent outside 10-20 s
    assert rms(data[rate * 12:rate * 18, 1]) > 0.15                       # channels not swapped
    assert path.stat().st_size < 5_760_000 / 4                            # far below the 16-bit WAV (pure tones compress worse than speech)
