"""Automatic updates for the installed (frozen) app.

The release channel is a small JSON manifest, e.g. on GitHub Releases:
    https://github.com/<owner>/<repo>/releases/latest/download/latest.json
    {"version": "1.0.1", "url": ".../BiraFlow-Setup-1.0.1.exe", "sha256": "...", "notes": "..."}

An update is installed only if the download matches the manifest's SHA-256 AND its
Authenticode signature was made with the certificate pinned at build time
(_build_info.SIGNER_THUMBPRINT). Installation waits until no dictation is running, runs
the Inno Setup installer silently and the installer reopens the app.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import __version__, _build_info

log = logging.getLogger(__name__)

FIRST_CHECK_DELAY = 90       # seconds after start
CHECK_EVERY = 6 * 3600


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text.lstrip("vV"))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


@dataclass
class Release:
    version: str
    url: str
    sha256: str
    notes: str = ""


class UpdateError(RuntimeError):
    pass


def fetch_manifest(url: str) -> Release:
    from . import http

    r = http.session().get(url, timeout=(5, 20), headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    data = json.loads(r.content.decode("utf-8-sig"))
    try:
        release = Release(str(data["version"]), str(data["url"]), str(data["sha256"]).lower(),
                          str(data.get("notes", "")))
    except KeyError as e:
        raise UpdateError(f"manifesto de atualização incompleto (falta {e})") from e
    # the manifest is not signed: nothing in it may become a path or an unexpected URL
    if not re.fullmatch(r"\d{1,4}(\.\d{1,6}){0,3}", release.version):
        raise UpdateError("manifesto de atualização com versão inválida")
    if not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
        raise UpdateError("manifesto de atualização com SHA-256 inválido")
    u = urlsplit(release.url)
    if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))):
        raise UpdateError("o instalador precisa vir por HTTPS")
    return release


def download(release: Release, folder: Path) -> Path:
    from . import http

    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"BiraFlow-Setup-{release.version}.exe"
    if dest.resolve().parent != folder.resolve():
        raise UpdateError("nome de instalador inválido")
    part = dest.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        with http.session().get(release.url, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    digest.update(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != release.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("o arquivo baixado não confere com o SHA-256 do manifesto")
    part.replace(dest)
    return dest


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def signature(path: Path) -> tuple[str, str]:
    """(status, signer thumbprint) from Windows' Authenticode verification."""
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:BF_PATH; "
              "'{0}|{1}' -f $s.Status, $s.SignerCertificate.Thumbprint")
    import os

    # the full path: a bare "powershell" could be picked up from the current folder
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=60, env={**_env(), "BF_PATH": str(path)},
                         creationflags=subprocess.CREATE_NO_WINDOW)
    status, _, thumb = out.stdout.strip().partition("|")
    return status, thumb.strip().upper()


def file_version(path: Path) -> str:
    """ProductVersion of the installer's version resource (part of what is signed)."""
    import os

    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                          "(Get-Item -LiteralPath $env:BF_PATH).VersionInfo.ProductVersion"],
                         capture_output=True, text=True, timeout=60, env={**_env(), "BF_PATH": str(path)},
                         creationflags=subprocess.CREATE_NO_WINDOW)
    return out.stdout.strip()


def _env() -> dict:
    import os

    return dict(os.environ)


def verify_signature(path: Path, pinned: str | None = None) -> None:
    """The installer must be intact and signed with our certificate. A self-signed
    certificate the PC does not trust yet reports "UnknownError": accepted only because
    the signer is pinned by thumbprint. "NotTrusted" means the user explicitly distrusts
    the certificate, and tampered files report HashMismatch: both are refused."""
    pinned = _build_info.SIGNER_THUMBPRINT if pinned is None else pinned
    if not pinned:
        raise UpdateError("esta versão não tem certificado fixado; atualize manualmente")
    status, thumb = signature(path)
    if status not in ("Valid", "UnknownError"):
        raise UpdateError(f"assinatura inválida ({status or 'sem assinatura'})")
    if thumb != pinned.upper():
        raise UpdateError("o instalador foi assinado com outro certificado")


class Updater:
    def __init__(self, folder: Path, url: Callable[[], str], enabled: Callable[[], bool],
                 is_idle: Callable[[], bool], notify: Callable[[str, str], None],
                 quit_app: Callable[[], None]):
        self.folder = folder
        self._url = url
        self._enabled = enabled
        self._is_idle = is_idle
        self._notify = notify
        self._quit = quit_app
        self.status = "Ainda não verificado."
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        if not frozen():
            self.status = "Atualizações automáticas só funcionam na versão instalada."
            return
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(FIRST_CHECK_DELAY):
            return
        while True:
            if self._enabled():
                self.check()
            if self._stop.wait(CHECK_EVERY):
                return

    def check(self, install: bool = True) -> str:
        """Check now; download, verify and install if there is something newer."""
        if not self._lock.acquire(blocking=False):
            return self.status
        try:
            url = self._url()
            if not url:
                self.status = "Nenhum canal de atualização configurado."
                return self.status
            release = fetch_manifest(url)
            if not is_newer(release.version):
                self.status = f"Você está na versão mais recente ({__version__})."
                return self.status
            self.status = f"Baixando a versão {release.version}…"
            installer = download(release, self.folder)
            try:
                verify_signature(installer)
                inside = file_version(installer)
                if not inside or parse_version(inside) != parse_version(release.version):
                    raise UpdateError(f"o instalador é da versão {inside or '?'}, não {release.version}")
            except Exception:
                installer.unlink(missing_ok=True)   # never leave an unverified .exe behind
                raise
            self.status = f"Versão {release.version} pronta para instalar."
            if install:
                self._install(installer, release)
            return self.status
        except Exception as e:
            log.warning("update check failed: %s", e)
            self.status = f"Não foi possível atualizar: {e}"
            return self.status
        finally:
            self._lock.release()

    def _install(self, installer: Path, release: Release) -> None:
        if not frozen():   # running from the source folder: an installer would replace nothing useful
            self.status = f"Versão {release.version} baixada; a instalação automática só roda na versão instalada."
            return
        while not self._is_idle():  # never in the middle of a dictation
            if self._stop.wait(5):
                return
        if sha256_of(installer) != release.sha256:   # changed on disk while we waited
            log.warning("installer changed after verification; not installing")
            installer.unlink(missing_ok=True)
            return
        log.info("installing update %s", release.version)
        self._notify(f"Atualizando o Bira Flow para a versão {release.version}…", "info")
        time.sleep(1.5)
        subprocess.Popen([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          "/CLOSEAPPLICATIONS", "/RELAUNCH=1"],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        self._quit()  # the installer replaces the files and reopens the app
