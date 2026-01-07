from __future__ import annotations

from datetime import date

from tests.conftest import reload_module


class _FakeResponse:
    def __init__(self, status_code=200, content_chunks=None, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._chunks = content_chunks or [b"data"]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code} {self.reason}")

    def iter_content(self, chunk_size=8192):
        for c in self._chunks:
            yield c


def test_build_image_and_mapping(fake_env_and_ee):
    m = reload_module("wind")

    img, mapping = m.build_image_and_mapping(date(2025, 1, 1))
    assert img is not None
    assert mapping is not None
    assert len(mapping) == 48


def test_get_last_downloaded_day(fake_env_and_ee, tmp_path):
    m = reload_module("wind")

    out = tmp_path / "wind"
    out.mkdir(parents=True, exist_ok=True)

    (out / "20250101_U_00.tif").write_bytes(b"x")
    (out / "20250103_V_10.tif").write_bytes(b"x")
    (out / "badname.tif").write_bytes(b"x")

    assert m.get_last_downloaded_day() == date(2025, 1, 3)


def test_download_era5_for_day_writes_files(fake_env_and_ee, tmp_path, monkeypatch):
    """
    Тестируем твою реальную функцию download_era5_for_day:
    - build_image_and_mapping подменяем на 2 канала
    - requests.get подменяем на fake response
    - проверяем что 2 файла создались
    """
    m = reload_module("wind")

    # Подменяем build_image_and_mapping -> 2 канала
    fake_img = type(
        "Img",
        (),
        {"select": lambda self, x: self, "rename": lambda self, n: self, "getDownloadURL": lambda self, p: "http://example.com"},
    )()
    monkeypatch.setattr(
        m,
        "build_image_and_mapping",
        lambda _d: (fake_img, [("old_u", "20250101_U_00"), ("old_v", "20250101_V_00")]),
    )

    # Подменяем requests.get
    monkeypatch.setattr(m.requests, "get", lambda *_a, **_k: _FakeResponse(200, [b"a", b"b"]))

    # Запуск на один день
    m.download_era5_for_day(date(2025, 1, 1))

    out_dir = tmp_path / "wind"
    assert (out_dir / "20250101_U_00.tif").exists()
    assert (out_dir / "20250101_V_00.tif").exists()


def test_run_sync_one_day_no_crash(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("wind")

    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 1)

    # чтобы не ходить в EE/requests — замокаем download_era5_for_day
    monkeypatch.setattr(m, "download_era5_for_day", lambda _d: None)

    # У тебя run_sync() возвращает None — так и проверяем
    assert m.run_sync() is None
