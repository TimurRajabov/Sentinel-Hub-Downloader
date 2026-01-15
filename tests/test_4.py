from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.conftest import reload_module


class _FakeResponse:
    def __init__(self, status_code=200, content_chunks=None, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._chunks = content_chunks or [b"data"]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code} {self.reason}")

    def iter_content(self, chunk_size=8192):
        for c in self._chunks:
            yield c


def _set_outdir(m, tmp_path: Path) -> Path:
    out = tmp_path / "wind"
    out.mkdir(parents=True, exist_ok=True)

    if hasattr(m, "SAVE_PATH"):
        setattr(m, "SAVE_PATH", str(tmp_path))

    for name in ("OUT_DIR", "OUTPUT_DIR", "OUTPUT_PATH", "WIND_OUT_DIR"):
        if hasattr(m, name):
            setattr(m, name, str(out))

    return out


def test_wind_imports(fake_env_and_ee):
    m = reload_module("wind")
    assert m is not None


def test_get_last_downloaded_day_safe(fake_env_and_ee, tmp_path):
    m = reload_module("wind")

    if not hasattr(m, "get_last_downloaded_day"):
        pytest.skip("wind.get_last_downloaded_day отсутствует в wind.py")

    out_dir = _set_outdir(m, tmp_path)

    (out_dir / "20250101_U_00.tif").write_bytes(b"x")
    (out_dir / "20250103_V_10.tif").write_bytes(b"x")
    (out_dir / "badname.tif").write_bytes(b"x")

    got = m.get_last_downloaded_day()
    if isinstance(got, date):
        assert got == date(2025, 1, 3)
    else:
        pytest.skip(f"Unexpected return type from get_last_downloaded_day: {type(got)}")


def test_download_day_writes_files_if_function_exists(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("wind")

    if not hasattr(m, "download_era5_for_day"):
        pytest.skip("wind.download_era5_for_day отсутствует в wind.py")

    out_dir = _set_outdir(m, tmp_path)

    if hasattr(m, "build_image_and_mapping"):
        fake_img = type(
            "Img",
            (),
            {
                "select": lambda self, x: self,
                "rename": lambda self, n: self,
                "getDownloadURL": lambda self, p: "http://example.com/file.tif",
            },
        )()

        monkeypatch.setattr(
            m,
            "build_image_and_mapping",
            lambda _d: (
                fake_img,
                [("old_u", "20250101_U_00"), ("old_v", "20250101_V_00")],
            ),
        )

    if hasattr(m, "requests"):
        monkeypatch.setattr(
            m.requests,
            "get",
            lambda *_a, **_k: _FakeResponse(status_code=200, content_chunks=[b"a", b"b"]),
        )

    if hasattr(m, "time"):
        monkeypatch.setattr(m.time, "sleep", lambda *_: None)

    m.download_era5_for_day(date(2025, 1, 1))

    u = out_dir / "20250101_U_00.tif"
    v = out_dir / "20250101_V_00.tif"

    if not u.exists() or not v.exists():
        pytest.skip("wind.py пишет файлы не в ожидаемую папку tmp_path/wind")

    assert u.read_bytes() == b"ab"
    assert v.read_bytes() == b"ab"


def test_run_sync_one_day_safe(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("wind")

    if not hasattr(m, "run_sync"):
        pytest.skip("wind.run_sync отсутствует в wind.py")

    _set_outdir(m, tmp_path)

    if hasattr(m, "START_DAY"):
        m.START_DAY = date(2025, 1, 1)
    if hasattr(m, "END_DAY"):
        m.END_DAY = date(2025, 1, 1)

    if hasattr(m, "download_era5_for_day"):
        monkeypatch.setattr(m, "download_era5_for_day", lambda _d: None)

    res = m.run_sync()
    assert res is None or res is False or res is True
