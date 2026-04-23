"""FastAPI application — Sentinel Hub Downloader."""

import logging
import os
import uuid
import json
import shutil
import tempfile
import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import storage, worker
from app import bot as telegram_bot

logger = logging.getLogger(__name__)

storage.init_db()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    bot_app = telegram_bot.build_bot()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    poller = asyncio.create_task(telegram_bot.notification_loop(bot_app.bot))
    logger.info("Telegram bot started")
    yield
    poller.cancel()
    try:
        await poller
    except asyncio.CancelledError:
        pass
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()
    logger.info("Telegram bot stopped")


app = FastAPI(title="Sentinel Hub Downloader", version="1.0.0", lifespan=lifespan)

class SettingsIn(BaseModel):
    project_id: str = ""
    save_path: str = "/data"
    scale_m: str = "10"
    max_cloud: str = "20"
    max_workers: str = "4"
    win_main: str = "3"
    win_fill: str = "15"
    dates_dir: str = ""

class CancelIn(BaseModel):
    job_id: str

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/settings")
def get_settings():
    return storage.get_settings()

@app.post("/api/settings")
def save_settings(s: SettingsIn):
    storage.save_settings(s.dict())
    return {"ok": True}

@app.get("/api/accounts")
def list_accounts():
    return storage.list_accounts()

@app.post("/api/accounts")
async def create_account(
    name: str = Form(...),
    acc_type: str = Form("service_account"),
    project_id: str = Form(""),
    credentials_file: Optional[UploadFile] = File(None),
    credentials_text: Optional[str] = Form(None),
):
    raw = None
    if credentials_file and credentials_file.filename:
        content = await credentials_file.read()
        raw = content.decode("utf-8")
    elif credentials_text:
        raw = credentials_text

    if not raw:
        raise HTTPException(400, "Нужно загрузить JSON файл или вставить credentials текст")

    try:
        creds = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Не удалось распарсить JSON credentials")

    if not project_id and isinstance(creds, dict):
        project_id = creds.get("project_id", "")

    account_id = uuid.uuid4().hex[:10]
    account = storage.create_account(account_id, name, acc_type, project_id, creds)
    return account

@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str):
    storage.delete_account(account_id)
    return {"ok": True}

@app.post("/api/accounts/{account_id}/set-default")
def set_default_account(account_id: str):
    acc = storage.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    storage.set_default_account(account_id)
    return {"ok": True}

@app.get("/api/jobs")
def list_jobs():
    return storage.list_jobs()

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job

@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    worker.cancel(job_id)
    storage.delete_job(job_id)
    return {"ok": True}

@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    worker.cancel(job_id)
    storage.update_job(job_id, status="cancelling")
    return {"ok": True}

@app.get("/api/jobs/{job_id}/log")
def job_log(job_id: str):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"log": job.get("log", "")}

@app.post("/api/submit")
async def submit_job(
    geojson_file: Optional[UploadFile] = File(None),
    geojson_text: Optional[str] = Form(None),
    date: Optional[str] = Form(None),
    date_start: Optional[str] = Form(None),
    date_end: Optional[str] = Form(None),
    date_step: Optional[str] = Form(None),
    shape_name: Optional[str] = Form(None),
    product: str = Form("RGB"),
    custom_bands: Optional[str] = Form(None),
    max_cloud: Optional[str] = Form(None),
    win_main: Optional[str] = Form(None),
    win_fill: Optional[str] = Form(None),
    only_missing: Optional[str] = Form("false"),
    single_raster: Optional[str] = Form("false"),
    account_id: Optional[str] = Form(None),
):
    """Accept form data, save GeoJSON to disk, enqueue job."""

    geojson_path = None

    if geojson_file and geojson_file.filename:
        ext = os.path.splitext(geojson_file.filename)[1].lower()
        tmp_name = f"{uuid.uuid4().hex}{ext}"
        geojson_path = os.path.join(UPLOADS_DIR, tmp_name)
        content = await geojson_file.read()
        with open(geojson_path, "wb") as f:
            f.write(content)
    elif geojson_text:
        tmp_name = f"{uuid.uuid4().hex}.geojson"
        geojson_path = os.path.join(UPLOADS_DIR, tmp_name)
        with open(geojson_path, "w", encoding="utf-8") as f:
            f.write(geojson_text)

    if not geojson_path:
        raise HTTPException(400, "Нужно загрузить GeoJSON файл или передать geojson_text")

    settings = storage.get_settings()
    params = {
        "geojson_path": geojson_path,
        "date": date or "",
        "date_start": date_start or "",
        "date_end": date_end or "",
        "date_step": date_step or "1",
        "shape_name": shape_name or "",
        "product": product,
        "custom_bands": custom_bands or "",
        "max_cloud": max_cloud or settings.get("max_cloud", "20"),
        "win_main": win_main or settings.get("win_main", "3"),
        "win_fill": win_fill or settings.get("win_fill", "15"),
        "only_missing": (only_missing or "false").lower() == "true",
        "single_raster": (single_raster or "false").lower() == "true",
        "hole_heal_m": 0,
        "account_id": account_id or "",
        "project_id": settings.get("project_id", ""),
        "save_path": settings.get("save_path", "/data"),
        "scale_m": settings.get("scale_m", "10"),
        "max_workers": settings.get("max_workers", "4"),
    }

    job_id = uuid.uuid4().hex[:12]
    storage.create_job(job_id, params)
    worker.submit(job_id, params)

    return {"job_id": job_id, "status": "pending"}

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/favicon.ico")
def favicon():
    return JSONResponse({}, status_code=204)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
