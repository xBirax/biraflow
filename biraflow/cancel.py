"""Cancellation for a dictation job (Esc while transcribing)."""
from __future__ import annotations

import subprocess
import threading


class Cancelled(Exception):
    pass


class CancelToken:
    """Set by Esc. Kills an attached whisper.cpp process at once; HTTP calls can't be
    interrupted mid-flight, so their results are dropped by check() instead."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
            if self._event.is_set():
                _kill(proc)

    def detach(self) -> None:
        with self._lock:
            self._proc = None

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            if self._proc is not None:
                _kill(self._proc)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
