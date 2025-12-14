from unittest.mock import MagicMock, patch

import download_all


@patch("download_all.ee.ImageCollection")
@patch("download_all.requests.get")
def test_download_gas(mock_requests_get, mock_img_col):

    # -----------------------------
    # Настраиваем mock ImageCollection
    # -----------------------------
    mock_img = MagicMock()
    mock_img.getDownloadURL.return_value = "http://fake-url/test.tif"

    mock_col = MagicMock()
    mock_col.select.return_value = mock_col
    mock_col.filterDate.return_value = mock_col
    mock_col.mean.return_value = mock_img
    mock_img_col.return_value = mock_col

    # -----------------------------
    # Настраиваем mock requests.get
    # -----------------------------
    mock_requests_get.return_value = MagicMock(content=b"FAKE_TIF_DATA")

    # -----------------------------
    # Переопределяем параметры
    # -----------------------------
    download_all.gases = {"CH4": ("dataset", "band")}
    download_all.START_DATE = "2025-11-20"
    download_all.END_DATE = "2025-11-21"
    download_all.SAVE_PATH = "/tmp"

    download_all.region = MagicMock()

    # -----------------------------
    # Запуск цикла скачивания
    # -----------------------------
    for gas_name, (dataset, band) in download_all.gases.items():
        col = mock_img_col(dataset)
        img = (
            col.select(band)
            .filterDate(download_all.START_DATE, download_all.END_DATE)
            .mean()
            .clip(download_all.region)
        )

        url = img.getDownloadURL(
            {"scale": 7000, "region": download_all.region, "format": "GEO_TIFF"}
        )
        r = mock_requests_get(url)

        # -----------------------------
        # Проверка
        # -----------------------------
        assert r.content == b"FAKE_TIF_DATA"
