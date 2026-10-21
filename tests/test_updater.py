import hashlib
import http.server
import json
import threading

import pytest

from biraflow import updater as up


def test_versions():
    assert up.parse_version("v1.2.10") == (1, 2, 10)
    assert up.is_newer("1.0.1", "1.0.0")
    assert up.is_newer("1.10.0", "1.9.9")
    assert not up.is_newer("1.0.0", "1.0.0")
    assert not up.is_newer("0.9", "1.0.0")


@pytest.fixture
def server(tmp_path):
    payload = b"MZ fake installer bytes"
    (tmp_path / "BiraFlow-Setup-9.9.9.exe").write_bytes(payload)
    state = {"sha": hashlib.sha256(payload).hexdigest()}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def publish(version="9.9.9", sha=None):
        (tmp_path / "latest.json").write_text(json.dumps({
            "version": version, "url": f"{base}/BiraFlow-Setup-9.9.9.exe", "sha256": sha or state["sha"]}),
            encoding="utf-8")

    publish()
    # a short poll interval: shutdown() waits for the loop to notice (0.5 s per test with
    # the default, and seven tests here share this fixture)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield base + "/latest.json", publish
    httpd.shutdown()


def make(tmp_path, url, signature=("UnknownError", "AB12"), pinned="AB12"):
    installs = []
    u = up.Updater(tmp_path / "dl", url=lambda: url, enabled=lambda: True, is_idle=lambda: True,
                   notify=lambda *a: None, quit_app=lambda: installs.append("quit"))
    u._install = lambda installer, release: installs.append(installer.name)
    return u, installs


def test_downloads_verifies_and_installs(tmp_path, server, monkeypatch):
    url, publish = server
    monkeypatch.setattr(up, "signature", lambda p: ("UnknownError", "AB12"))
    monkeypatch.setattr(up, "file_version", lambda p: "9.9.9")
    monkeypatch.setattr(up._build_info, "SIGNER_THUMBPRINT", "ab12")
    u, installs = make(tmp_path, url)
    u.check()
    assert installs == ["BiraFlow-Setup-9.9.9.exe"], u.status


def test_up_to_date_does_nothing(tmp_path, server, monkeypatch):
    url, publish = server
    publish(version=up.__version__)
    u, installs = make(tmp_path, url)
    assert "mais recente" in u.check() and installs == []


@pytest.mark.parametrize("problem", ["sha", "unsigned", "tampered", "other_cert", "no_pin"])
def test_refuses_anything_not_ours(tmp_path, server, monkeypatch, problem):
    url, publish = server
    sig = {"unsigned": ("NotSigned", ""), "tampered": ("HashMismatch", "AB12"),
           "other_cert": ("Valid", "FFFF")}.get(problem, ("Valid", "AB12"))
    monkeypatch.setattr(up, "signature", lambda p: sig)
    monkeypatch.setattr(up._build_info, "SIGNER_THUMBPRINT", "" if problem == "no_pin" else "AB12")
    if problem == "sha":
        publish(sha="0" * 64)
    u, installs = make(tmp_path, url)
    status = u.check()
    assert installs == [] and "Não foi possível atualizar" in status


def test_refuses_an_installer_of_another_version(tmp_path, server, monkeypatch):
    """The manifest is not signed: a signed but older installer behind a "9.9.9" manifest
    would downgrade the app (and reinstall on every check)."""
    url, publish = server
    monkeypatch.setattr(up, "signature", lambda p: ("Valid", "AB12"))
    monkeypatch.setattr(up, "file_version", lambda p: "1.0.0")
    monkeypatch.setattr(up._build_info, "SIGNER_THUMBPRINT", "AB12")
    u, installs = make(tmp_path, url)
    assert "1.0.0" in u.check() and installs == []
    assert not list((tmp_path / "dl").glob("*.exe"))


def test_no_channel_configured(tmp_path):
    u, installs = make(tmp_path, "")
    assert "Nenhum canal" in u.check() and installs == []


@pytest.mark.parametrize("installer", ["http://localhost.evil.example/x.exe", "http://127.0.0.1.nip.io/x.exe",
                                       "ftp://exemplo.com/x.exe", "http://exemplo.com/x.exe"])
def test_installer_url_must_be_https(tmp_path, server, installer):
    url, publish = server
    (tmp_path / "latest.json").write_text(json.dumps({"version": "9.9.9", "url": installer, "sha256": "0" * 64}),
                                          encoding="utf-8")
    with pytest.raises(up.UpdateError, match="HTTPS"):
        up.fetch_manifest(url)
