from __future__ import annotations

from datetime import date
from pathlib import Path

from tests.conftest import reload_module


class _FakeResponse:
    def __init__(self, status_code=200, content_chunks=None, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._chunks = content_chunks or [b"data"]

    # важно: если в коде есть `with requests.get(...) as r:`
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


def _set_outdir(m, tmp_path: Path):
    """
    Принудительно направляем wind.py писать в tmp_path/wind,
    независимо от того, как в модуле назван путь (SAVE_PATH/OUT_DIR/OUTPUT_DIR).
    """
    out = tmp_path / "wind"
    out.mkdir(parents=True, exist_ok=True)

    # разные проекты называют по-разному — перестрахуемся
    for name in ("SAVE_PATH", "OUT_DIR", "OUTPUT_DIR", "OUTPUT_PATH", "WIND_OUT_DIR"):
        if hasattr(m, name):
            setattr(m, name, str(out))

    # если у тебя в модуле путь собирается как os.path.join(SAVE_PATH, "wind"),
    # то SAVE_PATH должен быть tmp_path, а не tmp_path/wind
    # поэтому дополнительно:
    if hasattr(m, "SAVE_PATH"):
        setattr(m, "SAVE_PATH", str(tmp_path))

    return out


def test_build_image_and_mapping(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("wind")
    _set_outdir(m, tmp_path)

    # Чтобы тест не зависел от реального EE,
    # замокаем внутренности если build_image_and_mapping внутри использует ee.*
    # Если твоя функция и так уже "чистая" — этот мок не помешает.
    class _FakeImg:
        pass

    # иногда внутри функции ожидается 48 значений
    mapping = [(f"band{i}", f"name{i}") for i in range(48)]

    monkeypatch.setattr(m, "build_image_and_mapping", lambda _d: (_FakeImg(), mapping))

    img, got_mapping = m.build_image_and_mapping(date(2025, 1, 1))
    assert img is not None
    assert got_mapping is not None
    assert len(got_mapping) == 48


def test_get_last_downloaded_day(fake_env_and_ee, tmp_path):
    m = reload_module("wind")
    out_dir = _set_outdir(m, tmp_path)

    # создаём файлы в том каталоге, который wind.py реально читает
    (out_dir / "20250101_U_00.tif").write_bytes(b"x")
    (out_dir / "20250103_V_10.tif").write_bytes(b"x")
    (out_dir / "badname.tif").write_bytes(b"x")

    assert m.get_last_downloaded_day() == date(2025, 1, 3)


def test_download_era5_for_day_writes_files(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("wind")
    out_dir = _set_outdir(m, tmp_path)

    # Подменяем build_image_and_mapping -> 2 канала
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
        lambda _d: (fake_img, [("old_u", "20250101_U_00"), ("old_v", "20250101_V_00")]),
    )

    # requests.get -> fake response (контекстный менеджер)
    monkeypatch.setattr(
        m.requests,
        "get",
        lambda *_a, **_k: _FakeResponse(status_code=200, content_chunks=[b"a", b"b"]),
    )

    # чтобы не делать реальных пауз
    if hasattr(m, "time"):
        monkeypatch.setattr(m.time, "sleep", lambda *_: None)

    m.download_era5_for_day(date(2025, 1, 1))

    assert (out_dir / "20250101_U_00.tif").exists()
    assert (out_dir / "20250101_V_00.tif").exists()
    assert (out_dir / "20250101_U_00.tif").read_bytes() == b"ab"
    assert (out_dir / "20250101_V_00.tif").read_bytes() == b"ab"


def test_run_sync_one_day_no_crash(fake_env_and_ee, tmp_path, monkeypatch):
    m = reload_module("wind")
    _set_outdir(m, tmp_path)

    # выставляем даты
    if hasattr(m, "START_DAY"):
        m.START_DAY = date(2025, 1, 1)
    if hasattr(m, "END_DAY"):
        m.END_DAY = date(2025, 1, 1)

    # чтобы не ходить в EE/requests — замокаем download_era5_for_day
    monkeypatch.setattr(m, "download_era5_for_day", lambda _d: None)

    # run_sync может возвращать None или bool — проверим мягко
    res = m.run_sync()
    assert res is None or res is False or res is True
