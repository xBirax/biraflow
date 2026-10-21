import threading

import pytest

from biraflow import setup_local


class FakeResponse:
    def __init__(self, payload=None, chunks=(), length=None, status=200):
        self._payload = payload
        self._chunks = chunks
        self.status_code = status
        self.headers = {"content-length": str(length)} if length is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, chunk_size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_find_asset_skips_releases_without_binaries(monkeypatch):
    import requests

    releases = [
        {"tag_name": "v1.9.5", "assets": []},
        {"tag_name": "b5454", "assets": [
            {"name": "whisper-bin-Win32.zip", "browser_download_url": "u32", "size": 1},
            {"name": "whisper-bin-x64.zip", "browser_download_url": "u64", "size": 8},
            {"name": "whisper-bin-win-cuda-12.4.0-x64.zip", "browser_download_url": "ucuda", "size": 9},
        ]},
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(releases))
    assert setup_local.find_asset("cpu") == ("b5454", "whisper-bin-x64.zip", "u64", 8)
    assert setup_local.find_asset("cuda12")[2] == "ucuda"
    with pytest.raises(setup_local.SetupError):
        setup_local.find_asset("blas")


def test_download_complete_incomplete_and_cancelled(tmp_path, monkeypatch):
    import requests

    seen = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(chunks=[b"ab", b"cd"], length=4))
    dest = tmp_path / "f.bin"
    setup_local.download("u", dest, "x", lambda *a: seen.append(a), threading.Event())
    assert dest.read_bytes() == b"abcd" and seen[-1] == ("x", 4, 4)

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(chunks=[b"ab"], length=4))
    with pytest.raises(setup_local.SetupError):
        setup_local.download("u", tmp_path / "g.bin", "x", lambda *a: None, threading.Event())
    assert not (tmp_path / "g.bin").exists() and not (tmp_path / "g.bin.part").exists()

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(setup_local.SetupCancelled):
        setup_local.download("u", tmp_path / "h.bin", "x", lambda *a: None, cancel)
    assert list(tmp_path.glob("h.bin*")) == []


def test_model_name_is_validated(tmp_path):
    from biraflow.config import Paths

    with pytest.raises(setup_local.SetupError):
        setup_local.download_model(Paths(tmp_path, tmp_path), "../evil", lambda *a: None, threading.Event())


def test_unzipped_yields_the_marker_folder_and_cleans_up(tmp_path, monkeypatch):
    import zipfile

    def fake_download(url, dest, label, progress, cancel):
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("pkg/bin/whisper-cli.exe", "x")
            z.writestr("pkg/bin/ggml.dll", "y")

    monkeypatch.setattr(setup_local, "download", fake_download)
    zip_path = tmp_path / "build.zip"
    with setup_local.unzipped("u", zip_path, "x", "whisper-cli.exe", "faltou", None, threading.Event()) as folder:
        assert sorted(p.name for p in folder.iterdir()) == ["ggml.dll", "whisper-cli.exe"]
    assert not zip_path.exists() and not (tmp_path / "build-extract").exists()
    with pytest.raises(setup_local.SetupError, match="faltou"):
        with setup_local.unzipped("u", zip_path, "x", "final.mdl", "faltou", None, threading.Event()):
            pass
    assert list(tmp_path.iterdir()) == []                        # nothing left behind after the error either
