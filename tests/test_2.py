# tests/test_2.py
import os


class _FakeResponse:
    def __init__(self, status_code=200, content_chunks=None, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._chunks = content_chunks or [b"x"]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code} {self.reason}")

    def iter_content(self, chunk_size=1024 * 1024):
        for c in self._chunks:
            yield c


def download_with_retries(get_fn, url: str, outfile: str, max_retries: int = 3) -> bool:
    """
    Локальная "чистая" реализация твоей идеи:
    - на 5xx/429 ретраим
    - пишем во временный .part и делаем os.replace
    """
    for attempt in range(1, max_retries + 1):
        try:
            with get_fn(url, stream=True, timeout=1) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    raise Exception(f"retryable {r.status_code}")
                r.raise_for_status()

                tmp = outfile + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content():
                        f.write(chunk)
                os.replace(tmp, outfile)

            return True
        except Exception:
            if attempt < max_retries:
                continue
            return False


def test_download_success(tmp_path):
    out = tmp_path / "a.tif"

    def fake_get(_url, stream=True, timeout=0):
        return _FakeResponse(200, [b"hello", b"world"])

    ok = download_with_retries(fake_get, "http://x", str(out), max_retries=2)
    assert ok is True
    assert out.exists()
    assert out.read_bytes() == b"helloworld"


def test_download_retry_then_success(tmp_path):
    out = tmp_path / "b.tif"
    calls = {"n": 0}

    def fake_get(_url, stream=True, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(500, [b"x"], reason="Server Error")
        return _FakeResponse(200, [b"ok"])

    ok = download_with_retries(fake_get, "http://x", str(out), max_retries=3)
    assert ok is True
    assert calls["n"] == 2
    assert out.read_bytes() == b"ok"


def test_download_fail_after_retries(tmp_path):
    out = tmp_path / "c.tif"

    def fake_get(_url, stream=True, timeout=0):
        return _FakeResponse(503, [b"x"], reason="Service Unavailable")

    ok = download_with_retries(fake_get, "http://x", str(out), max_retries=2)
    assert ok is False
    assert not out.exists()
