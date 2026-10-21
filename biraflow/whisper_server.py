"""Keep the Whisper model loaded on the GPU between dictations.

Starting whisper-cli for every dictation reloads the model (≈0.5–0.7 s with CUDA),
while the GPU then needs only ≈0.25 s to transcribe. whisper.cpp's own
whisper-server.exe keeps the model in VRAM; we run it on 127.0.0.1 only, tie its
lifetime to ours with a Windows job object (it dies with Bira Flow, even on a crash),
and only use it when it actually runs on a GPU: on CPU the per-dictation whisper-cli
with an adaptive audio context is faster.
"""
from __future__ import annotations

import ctypes
import logging
import socket
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .cancel import CancelToken
from .config import Paths, TranscriptionCfg

log = logging.getLogger(__name__)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
_kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9


def _kill_on_close_job():
    job = _kernel32.CreateJobObjectW(None, None)
    if job:
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                          ctypes.byref(info), ctypes.sizeof(info))
    return job


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


GPU_MARKERS = ("using CUDA", "CUDA0 total size", "using Vulkan", "using SYCL")


class LocalServer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._key: tuple | None = None
        self._url = ""
        self._cpu_only_key: tuple | None = None  # don't keep retrying a setup with no GPU
        self._job = None
        # When the cloud transcribes and this server is only the offline fallback, free
        # the GPU memory after this many idle seconds (None: keep it loaded).
        self.idle_limit: float | None = None
        self._retry_at: tuple = (None, 0.0)
        self._last_used = time.monotonic()
        self._watchdog: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _key_for(self, exe: Path, model: Path, cfg: TranscriptionCfg) -> tuple:
        return (str(exe), str(model), cfg.language or "auto", cfg.threads, cfg.local_fast)

    def ensure(self, exe: Path, model: Path, cfg: TranscriptionCfg, log_dir: Path,
               timeout: float = 60.0) -> str | None:
        """URL of a GPU-backed server for this configuration, starting it if needed;
        None if the build has no usable GPU (the caller then uses whisper-cli)."""
        server_exe = exe.with_name("whisper-server.exe")
        if not server_exe.is_file():
            return None
        key = self._key_for(exe, model, cfg)
        # another thread may be starting the server (prewarm: up to a minute when the GPU
        # is slow): wait at most `timeout` for it, then let the caller use whisper-cli
        if not self._lock.acquire(timeout=timeout):
            log.warning("whisper-server is still starting; using whisper-cli this time")
            return None
        try:
            if key == self._cpu_only_key:
                return None
            if self._retry_at[0] == key and time.monotonic() < self._retry_at[1]:
                return None
            if self.running and key == self._key:
                return self._url
            self._stop_locked()
            port = _free_port()
            threads = cfg.threads or 8
            from .transcribe import argv_path

            cmd = [str(server_exe), "-m", argv_path(model), "--host", "127.0.0.1", "--port", str(port),
                   "-l", cfg.language or "auto", "-t", str(threads), "-nt"]
            if cfg.local_fast:
                cmd += ["-bs", "1", "-bo", "1"]
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "whisper-server.log"
            with open(log_path, "wb") as out:
                proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(server_exe.parent),
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            if self._job is None:
                self._job = _kill_on_close_job()
            if self._job:
                _kernel32.AssignProcessToJobObject(self._job, int(proc._handle))
            url = f"http://127.0.0.1:{port}"
            t0 = time.monotonic()
            if not self._wait_ready(proc, url, timeout):
                proc.kill()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    pass
                # maybe only for now (VRAM taken by a game, a port race): try again in 5 min
                log.warning("whisper-server did not start; using whisper-cli")
                self._retry_at = (key, time.monotonic() + 300)
                return None
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if not any(m in text for m in GPU_MARKERS):
                proc.kill()
                log.info("whisper-server has no GPU here; using whisper-cli (faster on CPU)")
                self._cpu_only_key = key
                return None
            self._proc, self._key, self._url = proc, key, url
            self._last_used = time.monotonic()
            log.info("whisper-server ready on the GPU in %.1fs (%s)", time.monotonic() - t0, model.name)
            if self._watchdog is None:
                self._watchdog = threading.Thread(target=self._watch_idle, name="whisper-idle", daemon=True)
                self._watchdog.start()
            return url
        finally:
            self._lock.release()

    @staticmethod
    def _wait_ready(proc: subprocess.Popen, url: str, timeout: float) -> bool:
        import requests

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if proc.poll() is not None:
                return False
            try:
                if requests.get(url + "/", timeout=1).status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(0.15)
        return False

    def transcribe(self, url: str, wav: Path, cfg: TranscriptionCfg, token: CancelToken, timed: bool = False):
        """Text, or (timed) segments with their times: the server runs with -nt, so a timed
        request turns timestamps back on for itself."""
        import requests

        from .transcribe import segments_from, wav_seconds, whisper_form

        data = whisper_form(cfg, response_format="verbose_json" if timed else "json")
        if timed:
            data["no_timestamps"] = "false"
        self._last_used = time.monotonic()
        with open(wav, "rb") as f:
            r = requests.post(url + "/inference", files={"file": ("audio.wav", f, "audio/wav")}, data=data,
                              timeout=(3, max(30, cfg.timeout_seconds, 2 * wav_seconds(wav))))
        self._last_used = time.monotonic()
        token.check()
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:   # "failed to read audio data" comes back as HTTP 200
            raise RuntimeError(f"whisper-server: {payload['error']}")
        return segments_from(payload, wav_seconds(wav)) if timed else payload.get("text", "")

    def idle_for(self) -> float:
        return time.monotonic() - self._last_used

    def stop_if_idle(self) -> bool:
        """Free the GPU when the server has been unused for `idle_limit` seconds."""
        limit = self.idle_limit
        if limit is None or not self.running or self.idle_for() < limit:
            return False
        with self._lock:
            if self.running and self.idle_for() >= limit:
                log.info("whisper-server idle for %.0f min; unloading it from the GPU", limit / 60)
                self._stop_locked()
                return True
        return False

    def _watch_idle(self) -> None:
        while True:
            time.sleep(30)
            try:
                self.stop_if_idle()
            except Exception:
                log.exception("whisper-server idle check")

    def _stop_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            try:
                self._proc.wait(3)
            except subprocess.TimeoutExpired:
                pass
        self._proc, self._key, self._url = None, None, ""

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


SERVER = LocalServer()


def prewarm(cfg: TranscriptionCfg, paths: Paths) -> None:
    """Load the model on the GPU in the background, so the first dictation is fast too."""
    from .transcribe import find_whisper_cli, resolve_model

    if not cfg.keep_loaded or cfg.engine not in ("local", "auto"):
        return
    exe, model = find_whisper_cli(cfg, paths), resolve_model(cfg, paths)
    if exe is None or not model.is_file():
        return

    def run() -> None:
        try:
            SERVER.ensure(exe, model, cfg, paths.tmp_dir)
        except Exception:
            log.exception("prewarming whisper-server")

    threading.Thread(target=run, name="whisper-prewarm", daemon=True).start()
