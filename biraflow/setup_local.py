"""Install whisper.cpp and Whisper models from inside the app (what setup-whisper.ps1
does), with progress and cancellation. Sources: the official ggml-org/whisper.cpp
GitHub releases and the ggerganov/whisper.cpp model repository on Hugging Face."""
from __future__ import annotations

import contextlib
import functools
import os
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Callable

from .config import Paths

RELEASES_API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=20"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
BUILDS = {
    "cpu": ("CPU (padrão)", r"^whisper-bin-x64\.zip$"),
    "blas": ("CPU com OpenBLAS (mais rápido em CPUs modernas)", r"^whisper-blas-bin-x64\.zip$"),
    "cuda12": ("Placa NVIDIA (CUDA 12, ~700 MB, ~10× mais rápido)", r"(cublas|cuda)-12[\d.]*-?(bin-)?x64\.zip$"),
}
MODEL_SIZES_MB = {"tiny": 75, "base": 142, "small": 466, "medium": 1500, "large-v3-turbo-q8_0": 874,
                  "large-v3-turbo": 1600}
HEADERS = {"User-Agent": "biraflow-setup"}

Progress = Callable[[str, int, int], None]  # (stage label, done bytes, total bytes)


class SetupCancelled(Exception):
    pass


class SetupError(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def has_nvidia_gpu() -> bool:
    """An NVIDIA card with a working driver (nvidia-smi ships with the driver). Cached:
    the app asks once at start, in the background, so the settings window never waits."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "-L"], capture_output=True, text=True, timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        return out.returncode == 0 and "GPU" in out.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def installed_build(paths: Paths) -> str:
    if (paths.whisper_dir / "ggml-cuda.dll").exists():
        return "cuda12"
    if (paths.whisper_dir / "libopenblas.dll").exists() or (paths.whisper_dir / "openblas.dll").exists():
        return "blas"
    return "cpu" if (paths.whisper_dir / "whisper-cli.exe").exists() else ""


def find_asset(build: str = "cpu") -> tuple[str, str, str, int]:
    """(release tag, asset name, download url, size) of the newest release with the build."""
    import requests

    pattern = re.compile(BUILDS[build][1])
    r = requests.get(RELEASES_API, headers=HEADERS, timeout=15)
    r.raise_for_status()
    for release in r.json():
        for asset in release.get("assets", []):
            if pattern.search(asset["name"]):
                return release["tag_name"], asset["name"], asset["browser_download_url"], asset.get("size", 0)
    raise SetupError(f"Nenhum release recente do whisper.cpp tem o pacote \"{build}\".")


def download(url: str, dest: Path, label: str, progress: Progress, cancel: threading.Event) -> None:
    """Stream to dest.part, then rename: an interrupted download never looks complete."""
    import requests

    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, headers=HEADERS, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if cancel.is_set():
                        raise SetupCancelled()
                    f.write(chunk)
                    done += len(chunk)
                    progress(label, done, total)
        if total and done != total:
            raise SetupError(f"Download incompleto de {dest.name} ({done} de {total} bytes).")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


@contextlib.contextmanager
def unzipped(url: str, zip_path: Path, label: str, marker: str, missing: str,
             progress: Progress, cancel: threading.Event):
    """Download a zip and extract it; yields the folder that holds `marker` (SetupError(missing)
    when none does). The zip and the extracted files are gone afterwards, also after an error."""
    extract = zip_path.with_name(zip_path.stem + "-extract")
    try:
        download(url, zip_path, label, progress, cancel)
        shutil.rmtree(extract, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract)
        found = next(extract.rglob(marker), None)
        if found is None:
            raise SetupError(missing)
        yield found.parent
    finally:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(extract, ignore_errors=True)


def install_whisper(paths: Paths, build: str, progress: Progress, cancel: threading.Event) -> str:
    tag, name, url, _ = find_asset(build)
    with unzipped(url, paths.tmp_dir / name, f"whisper.cpp {tag}", "whisper-cli.exe",
                  f"whisper-cli.exe não está dentro de {name}.", progress, cancel) as folder:
        from .whisper_server import SERVER

        SERVER.stop()   # it keeps whisper-server.exe and the CUDA DLLs open: copying would fail halfway
        # a fresh folder: a CPU build over a CUDA one must not keep ggml-cuda.dll around
        # (it would still be loaded, and installed_build() would report "cuda12")
        shutil.rmtree(paths.whisper_dir, ignore_errors=True)
        paths.whisper_dir.mkdir(parents=True, exist_ok=True)
        for item in folder.iterdir():
            target = paths.whisper_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    return tag


def download_model(paths: Paths, name: str, progress: Progress, cancel: threading.Event) -> Path:
    if not re.fullmatch(r"[\w.\-]+", name):
        raise SetupError(f"Nome de modelo inválido: {name}")
    dest = paths.models_dir / f"ggml-{name}.bin"
    download(MODEL_URL.format(name=name), dest, f"modelo {name}", progress, cancel)
    if dest.stat().st_size < 10_000_000:
        dest.unlink()
        raise SetupError(f"O arquivo baixado para o modelo {name} é pequeno demais.")
    return dest


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0
