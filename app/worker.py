"""
Background worker: executes download jobs using 10_10.py logic.
Each job runs in its own thread.
"""

import sys
import os
import io
import threading
from typing import Dict, Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage

_running: Dict[str, threading.Thread] = {}
_cancel_flags: Dict[str, threading.Event] = {}

def _ee_init(ee, account: dict, project_id: str):
    """Initialize Earth Engine with the given account credentials."""
    creds = account.get("credentials") or {}
    acc_type = account.get("type", "service_account")

    if acc_type == "service_account":
        service_email = creds.get("client_email", "")
        if not service_email:
            raise RuntimeError("Service account JSON не содержит client_email")
        import tempfile, json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            _json.dump(creds, f)
            key_file = f.name
        try:
            sa_creds = ee.ServiceAccountCredentials(service_email, key_file)
            ee.Initialize(credentials=sa_creds, project=project_id)
        finally:
            try:
                os.remove(key_file)
            except Exception:
                pass

    elif acc_type == "user":
        import tempfile, json as _json
        import google.oauth2.credentials
        token_data = creds if isinstance(creds, dict) else {}
        google_creds = google.oauth2.credentials.Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
        )
        ee.Initialize(credentials=google_creds, project=project_id)

    else:
        ee.Initialize(project=project_id)

class _LogWriter(io.TextIOBase):
    """Redirects print() output to job log in DB."""

    def __init__(self, job_id: str, original_stdout):
        self.job_id = job_id
        self._orig = original_stdout

    def write(self, s: str) -> int:
        if s and s != "\n":
            for line in s.splitlines():
                if line.strip():
                    storage.append_log(self.job_id, line)
        self._orig.write(s)
        return len(s)

    def flush(self):
        self._orig.flush()

def _run_job(job_id: str, params: Dict[str, Any]):
    import ee
    from dotenv import load_dotenv

    load_dotenv()

    cancel_ev = _cancel_flags.get(job_id)
    orig_stdout = sys.stdout
    sys.stdout = _LogWriter(job_id, orig_stdout)

    try:
        storage.update_job(job_id, status="running")

        settings = storage.get_settings()
        project_id = params.get("project_id") or settings.get("project_id") or os.getenv("PROJECT_ID", "")
        save_path   = params.get("save_path")  or settings.get("save_path")  or os.getenv("SAVE_PATH", "/data")
        scale_m     = int(params.get("scale_m")    or settings.get("scale_m")    or 10)
        max_cloud   = int(params.get("max_cloud")   or settings.get("max_cloud")   or 20)
        max_workers = int(params.get("max_workers") or settings.get("max_workers") or 4)
        win_main    = int(params.get("win_main")    or settings.get("win_main")    or 3)
        win_fill    = int(params.get("win_fill")    or settings.get("win_fill")    or 15)

        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("ten_ten", os.path.join(ROOT, "10_10.py"))
        t10 = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(t10)

        t10.SAVE_PATH          = save_path
        t10.SCALE_M            = scale_m
        t10.MAX_CLOUD_PERCENT  = max_cloud
        t10.MAX_WORKERS        = max_workers

        account_id = params.get("account_id") or ""
        account = (storage.get_account(account_id) if account_id
                   else storage.get_default_account())

        if account:
            project_id = account["project_id"] or project_id
            _ee_init(ee, account, project_id)
            print(f"🔑 Аккаунт: {account['name']} ({account['type']}), project={project_id}")
        else:
            if not project_id:
                raise RuntimeError("Не найден аккаунт GEE. Добавьте его во вкладке Аккаунты.")
            ee.Initialize(project=project_id)
            print(f"🔑 Используются системные credentials, project={project_id}")

        t10.PROJECT_ID = project_id

        from datetime import datetime as _dt, timedelta as _td

        geojson_path = params.get("geojson_path")
        shape_name   = params.get("shape_name") or ""

        if not geojson_path or not os.path.exists(geojson_path):
            raise RuntimeError(f"GeoJSON файл не найден: {geojson_path}")

        date_start_s = params.get("date_start") or ""
        date_end_s   = params.get("date_end") or ""
        date_step    = max(1, int(params.get("date_step") or 1))
        date_single  = params.get("date") or ""

        if date_start_s and date_end_s:
            def _parse(s):
                return _dt.strptime(s.strip(), "%d.%m.%Y")
            d_start = _parse(date_start_s)
            d_end   = _parse(date_end_s)
            date_list = []
            cur = d_start
            while cur <= d_end:
                date_list.append(cur.strftime("%d.%m.%Y"))
                cur += _td(days=date_step)
            print(f"📅 Диапазон дат: {date_start_s} → {date_end_s}, шаг {date_step} дн. = {len(date_list)} дат")
        elif date_single:
            date_list = [date_single]
        else:
            date_list = [""]

        base_name = os.path.splitext(os.path.basename(geojson_path))[0]
        out_dirs = []

        product = params.get("product") or "RGB"
        custom_bands_raw = params.get("custom_bands") or ""
        custom_bands = [b.strip() for b in custom_bands_raw.split(",") if b.strip()] if custom_bands_raw else []
        single_raster = params.get("single_raster", False)
        prod_tag = product if product != "Custom" else ("Custom_" + "_".join(custom_bands or []))

        bbox_job_template = None
        if single_raster:
            try:
                import json as _json
                with open(geojson_path) as _f:
                    _gj = _json.load(_f)
                _features = _gj.get("features", [])
                _xs, _ys = [], []
                for _feat in _features:
                    _geom = _feat.get("geometry") or {}
                    _coords = _geom.get("coordinates") or []
                    def _flat(c):
                        if c and isinstance(c[0], (int, float)):
                            _xs.append(c[0]); _ys.append(c[1])
                        else:
                            for i in c: _flat(i)
                    _flat(_coords)
                if _xs and _ys:
                    _minx, _maxx = min(_xs), max(_xs)
                    _miny, _maxy = min(_ys), max(_ys)
                    bbox_job_template = {
                        "fid": 0,
                        "name": base_name,
                        "date": "",
                        "geom": {
                            "type": "Polygon",
                            "coordinates": [[
                                [_minx, _miny], [_maxx, _miny],
                                [_maxx, _maxy], [_minx, _maxy],
                                [_minx, _miny],
                            ]],
                        },
                        "props": {},
                    }
                    print(f"🗺 Единый растр: bbox всех полигонов = ({_minx:.4f},{_miny:.4f}) → ({_maxx:.4f},{_maxy:.4f})")
            except Exception as _e:
                print(f"⚠ Не удалось вычислить bbox: {_e}")

        total_dates = len(date_list)
        for date_idx, date_override in enumerate(date_list, start=1):
            if total_dates > 1:
                print(f"\n🗓 Дата {date_idx}/{total_dates}: {date_override}")

            if single_raster and bbox_job_template:
                job = dict(bbox_job_template)
                job["date"] = date_override
                jobs = [job]
                print(f"⚡ Режим единого растра: 1 запрос вместо множества полигонов")
            else:
                jobs = t10.build_jobs_from_geojson(
                    geojson_path,
                    shape_name_filter=shape_name,
                    date_override=date_override,
                )

            for job_i, job in enumerate(jobs, start=1):
                if cancel_ev and cancel_ev.is_set():
                    print("⛔ Job отменён пользователем.")
                    storage.update_job(job_id, status="cancelled")
                    return

                t10.process_job(
                    base_name=base_name,
                    job_i=job_i,
                    total=len(jobs),
                    job=job,
                    only_missing=params.get("only_missing", False),
                    win_main=win_main,
                    win_fill=win_fill,
                    hole_heal_m=int(params.get("hole_heal_m") or 0),
                    product=product,
                    custom_bands=custom_bands or None,
                )

                try:
                    eff_date = job.get("date") or date_override
                    target_day = t10.parse_ddmmyyyy(eff_date)
                    target_tag = target_day.strftime("%Y%m%d")
                    name = t10.safe_filename(str(job.get("name") or ""))
                    id_part = name if name else f"FID{job['fid']}"
                    out_dir = os.path.join(save_path, f"S2_{base_name}_{id_part}_{target_tag}_{scale_m}m")
                    out_dirs.append(out_dir)
                except Exception:
                    pass

        result_dir = out_dirs[0] if out_dirs else save_path
        storage.update_job(job_id, status="done", result_dir=result_dir)

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"❌ Ошибка: {exc}\n{tb}")
        storage.update_job(job_id, status="error", error=str(exc))

    finally:
        sys.stdout = orig_stdout
        _running.pop(job_id, None)
        _cancel_flags.pop(job_id, None)

def submit(job_id: str, params: Dict[str, Any]):
    """Start job in background thread."""
    ev = threading.Event()
    _cancel_flags[job_id] = ev
    t = threading.Thread(target=_run_job, args=(job_id, params), daemon=True, name=f"job-{job_id}")
    _running[job_id] = t
    t.start()

def cancel(job_id: str):
    ev = _cancel_flags.get(job_id)
    if ev:
        ev.set()

def is_running(job_id: str) -> bool:
    t = _running.get(job_id)
    return t is not None and t.is_alive()
