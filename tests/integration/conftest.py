"""Integration tests: real engines on synthetic pt-BR audio (docs/PLANO-REUNIOES.md §11.3).

Opt-in: `pytest -m integration -s` (the -s shows the measurements). Fixtures come from
scripts/make-test-audio.ps1 (folder in BIRAFLOW_FIXTURES, else %TEMP%\\biraflow-fixtures).
The user's settings, .env and local model are read, never written, and keys are never printed;
a test is skipped when what it needs (fixtures, a key, the local model) is missing.
"""
import importlib.util
import os
from dataclasses import replace
from pathlib import Path

import pytest

from biraflow import config
from biraflow import transcribe as tr
from biraflow.config import Paths

ROOT = Path(__file__).resolve().parents[2]
_HOME = os.environ.get("BIRAFLOW_HOME")   # read now: tests/conftest.py hides it from every test


@pytest.fixture(scope="session")
def fixtures() -> Path:
    folder = Path(os.environ.get("BIRAFLOW_FIXTURES") or Path(os.environ.get("TEMP", ".")) / "biraflow-fixtures")
    if not (folder / "fala-30s.wav").is_file():
        pytest.skip(f"sem fixtures em {folder}: rode scripts\\make-test-audio.ps1 (-OutDir) e BIRAFLOW_FIXTURES")
    return folder


@pytest.fixture(scope="session")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def user():
    """(paths, settings, env) of the user's installation, read-only."""
    paths = Paths(Path(_HOME), Path(_HOME)) if _HOME else Paths.default()
    settings = (config.parse_settings(config.read_text(paths.settings))[0] if paths.settings.is_file()
                else config.Settings())
    return paths, settings, config.load_env(paths)


@pytest.fixture
def run_paths(tmp_path) -> Paths:
    """Temp files of the run: never the user's tmp folder."""
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    return p


@pytest.fixture(scope="session", autouse=True)
def _free_the_gpu():
    yield
    from biraflow.whisper_server import SERVER

    SERVER.stop()


@pytest.fixture
def engine_cfg(user):
    """engine_cfg("groq"): the user's transcription settings on that engine, in Portuguese (the
    fixtures' language), the local model by absolute path; skips when it is not set up."""
    return lambda engine: _engine_cfg(user, engine)


@pytest.fixture
def any_engine(user):
    """Groq when there is a key, else the local model (skips when neither is set up)."""
    try:
        return _engine_cfg(user, "groq")
    except pytest.skip.Exception:
        return _engine_cfg(user, "local")


def local_on_gpu(cfg, tmp: Path) -> None:
    """A meeting played 8x faster than real time needs the GPU server: whisper-cli on a CPU
    takes ~50 s per 90 s piece (fine live, a backlog at 8x). Skips without it."""
    from biraflow.whisper_server import SERVER

    if not cfg.keep_loaded or SERVER.ensure(Path(cfg.whisper_cli), Path(cfg.local_model), cfg, tmp) is None:
        pytest.skip("o modelo local roda na CPU aqui (whisper.cpp sem GPU): rápido o bastante ao vivo, não a 8x")


@pytest.fixture
def meeting_engine(user, tmp_path):
    """meeting_engine("groq" | "local" | "any"): a configuration fast enough for a meeting at 8x."""
    def pick(engine):
        if engine == "any":
            try:
                return _engine_cfg(user, "groq")
            except pytest.skip.Exception:
                engine = "local"
        cfg = _engine_cfg(user, engine)
        if engine == "local":
            local_on_gpu(cfg, tmp_path)
        return cfg

    return pick


def _engine_cfg(user, engine: str):
    paths, settings, env = user
    cfg = replace(settings.transcription, engine=engine, language="pt", context="")
    if engine == "local":
        exe, model = tr.find_whisper_cli(cfg, paths), tr.resolve_model(cfg, paths)
        if exe is None or not model.is_file():
            pytest.skip("modelo local (whisper.cpp) não instalado")
        return replace(cfg, whisper_cli=str(exe), local_model=str(model))
    if not env.get(tr.API[engine][1]):
        pytest.skip(f"sem {tr.API[engine][1]} no .env")
    return cfg


@pytest.fixture(scope="session")
def anchors():
    """anchors(fixture.wav): the anchor words the fixture speaks, in order."""
    import json

    return lambda wav: json.loads(wav.with_suffix(".json").read_text(encoding="utf-8-sig"))["anchors"]
