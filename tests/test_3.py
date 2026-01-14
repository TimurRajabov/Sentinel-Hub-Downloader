from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from tests.conftest import reload_module


class _FakeResponse:
    def __init__(self, status_code=200, content_chunks=None, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._chunks = content_chunks or [b"data"]
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code} {self.reason}")

    def iter_content(self, chunk_size=1024):
        for c in self._chunks:
            yield c


class _FakeImage:
    def __init__(self, url="http://example.com/file.tif"):
        self._url = url

    def getDownloadURL(self, _params):
        return self._url


def test_parse_day(fake_env_and_ee):
    m = reload_module("download_all")
    assert m.parse_day(None) is None
    assert m.parse_day("2025-01-01") == date(2025, 1, 1)


def test_get_last_downloaded_date(fake_env_and_ee, tmp_path):
    m = reload_module("download_all")

    gas = "CH4"
    gas_dir = tmp_path / gas
    gas_dir.mkdir(parents=True, exist_ok=True)

    (gas_dir / "CH4_2025-01-01.tif").write_bytes(b"x")
    (gas_dir / "CH4_2025-01-03.tif").write_bytes(b"x")
    (gas_dir / "random.txt").write_text("x")

    assert m.get_last_downloaded_date(gas) == date(2025, 1, 3)


def test_backoff_sleep_no_wait(fake_env_and_ee, monkeypatch):
    m = reload_module("download_all")
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    monkeypatch.setattr(m.random, "random", lambda: 0.0)
    m._backoff_sleep(1)


def test_download_geotiff_success_writes_file(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("download_all")

    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    monkeypatch.setattr(m.random, "random", lambda: 0.0)

    def fake_get(_url, stream=True, timeout=0):
        return _FakeResponse(status_code=200, content_chunks=[b"hello", b"world"])

    monkeypatch.setattr(m.requests, "get", fake_get)

    out = tmp_path / "out.tif"
    m.download_geotiff_with_retries(_FakeImage(), str(out))
    assert out.exists()
    assert out.read_bytes() == b"helloworld"


def test_download_geotiff_retries_then_success(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("download_all")

    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    monkeypatch.setattr(m.random, "random", lambda: 0.0)

    calls = {"n": 0}

    def fake_get(_url, stream=True, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(status_code=500, reason="Server Error")
        return _FakeResponse(status_code=200, content_chunks=[b"x"])

    monkeypatch.setattr(m.requests, "get", fake_get)

    out = tmp_path / "retry_ok.tif"
    m.download_geotiff_with_retries(_FakeImage(), str(out))
    assert calls["n"] == 2
    assert out.exists()
    assert out.read_bytes() == b"x"


def test_run_sync_one_gas_one_day_happy_path(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("download_all")


    m.gases = {"CH4": ("DUMMY", "BAND")}
    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 1)

    class _FakeSize:
        def getInfo(self):
            return 1

    class _FakeCollection:
        def select(self, *_a, **_k):
            return self

        def filterDate(self, *_a, **_k):
            return self

        def size(self):
            return _FakeSize()

        def mean(self):
            return SimpleNamespace(clip=lambda _r: _FakeImage())

    monkeypatch.setattr(m.ee, "ImageCollection", lambda *_a, **_k: _FakeCollection())

    def fake_download(_img, outfile):
        with open(outfile, "wb") as f:
            f.write(b"ok")

    monkeypatch.setattr(m, "download_geotiff_with_retries", fake_download)

    had_failures = m.run_sync()
    assert had_failures is False

    p = tmp_path / "CH4" / "CH4_2025-01-01.tif"
    assert p.exists()
