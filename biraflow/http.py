"""One keep-alive HTTP session for the cloud APIs.

A fresh HTTPS connection to Groq from Brazil costs ~0.3–0.6 s of TCP + TLS handshake,
more than the transcription itself; reusing the connection removes it from every
dictation after the first. The session is warmed up when a key is configured.
"""
from __future__ import annotations

import re
import threading
import time

_lock = threading.Lock()
_session = None
_last_used = 0.0
# A pooled connection idle this long may be dead without a FIN (NAT, PC asleep, Wi-Fi): urllib3
# still sees it as open and the request fails or hangs (4 of 5 network failures in the log came
# after 8–80 idle minutes; after 5 idle minutes every dictation was 0.2–0.3 s slower).
IDLE_RESET = 120.0


def session():
    global _session
    import requests

    with _lock:
        if _session is None:
            _session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
            _session.mount("https://", adapter)
        return _session


def _touch() -> bool:
    """Mark the session used now; True if it had been idle long enough to distrust its connections."""
    global _last_used
    with _lock:
        now = time.monotonic()
        stale, _last_used = now - _last_used > IDLE_RESET, now
    return stale


def warm(url: str, headers: dict | None = None) -> None:
    """Open the connection in the background so the first dictation skips the handshake."""
    # synchronous: the warm-up of the second host must not drop the connection the first just opened
    if _touch():
        session().close()   # only empties the pools; the session stays usable

    def run() -> None:
        try:
            session().head(url, headers=headers or {}, timeout=5)
        except Exception:
            pass

    threading.Thread(target=run, name="http-warm", daemon=True).start()


def error_message(r) -> str:
    """An API's own error message, short. A 401 quotes part of the key ("Incorrect API key
    provided: sk-proj-...abcd"): it never reaches the log or the screen."""
    try:
        msg = str(r.json()["error"]["message"])
    except Exception:
        msg = r.text
    return re.sub(r"\b(?:sk|gsk|vck|xai)[-_][\w*.\-]+", "[chave]", msg)[:160]


def post(*args, **kwargs):
    _touch()
    try:
        return session().post(*args, **kwargs)
    finally:
        _touch()   # a long upload counts as use until it ends
