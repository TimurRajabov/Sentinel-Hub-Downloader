import sqlite3
import threading
import json
import os
from datetime import datetime
from typing import List, Optional, Dict, Any

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sentinel_hub.db")

_WRITE_LOCK = threading.Lock()

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL DEFAULT 'service_account',
                project_id  TEXT NOT NULL DEFAULT '',
                credentials TEXT NOT NULL DEFAULT '{}',
                created_at  TEXT NOT NULL,
                is_default  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                params      TEXT NOT NULL DEFAULT '{}',
                log         TEXT NOT NULL DEFAULT '',
                result_dir  TEXT,
                error       TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_subscribers (
                chat_id    INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO settings (key, value) VALUES
                ('project_id',    ''),
                ('save_path',     '/data'),
                ('scale_m',       '10'),
                ('max_cloud',     '20'),
                ('max_workers',   '2'),
                ('win_main',      '15'),
                ('win_fill',      '45'),
                ('dates_dir',     '');
        """)

def create_job(job_id: str, params: Dict[str, Any]) -> Dict:
    now = datetime.utcnow().isoformat()
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, created_at, updated_at, status, params) VALUES (?,?,?,?,?)",
            (job_id, now, now, "pending", json.dumps(params, ensure_ascii=False)),
        )
    return get_job(job_id)

def get_job(job_id: str) -> Optional[Dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None

def list_jobs(limit: int = 200) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]

def update_job(job_id: str, **kwargs):
    allowed = {"status", "log", "result_dir", "error"}
    sets = {k: v for k, v in kwargs.items() if k in allowed}
    if not sets:
        return
    sets["updated_at"] = datetime.utcnow().isoformat()
    sql = "UPDATE jobs SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(sql, list(sets.values()) + [job_id])

def append_log(job_id: str, line: str):
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "UPDATE jobs SET log = log || ?, updated_at=? WHERE id=?",
            (line + "\n", datetime.utcnow().isoformat(), job_id),
        )

def delete_job(job_id: str):
    with _WRITE_LOCK, _conn() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

def get_settings() -> Dict[str, str]:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}

def save_settings(data: Dict[str, str]):
    with _WRITE_LOCK, _conn() as conn:
        for k, v in data.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )

def _row_to_dict(row: sqlite3.Row) -> Dict:
    d = dict(row)
    try:
        d["params"] = json.loads(d["params"])
    except Exception:
        pass
    return d

def create_account(account_id: str, name: str, acc_type: str,
                   project_id: str, credentials: Dict) -> Dict:
    now = datetime.utcnow().isoformat()
    with _WRITE_LOCK, _conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        is_default = 1 if count == 0 else 0
        conn.execute(
            "INSERT INTO accounts (id, name, type, project_id, credentials, created_at, is_default) VALUES (?,?,?,?,?,?,?)",
            (account_id, name, acc_type, project_id,
             json.dumps(credentials, ensure_ascii=False), now, is_default),
        )
    return get_account(account_id)

def get_account(account_id: str) -> Optional[Dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return _account_row(row) if row else None

def get_default_account() -> Optional[Dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE is_default=1 LIMIT 1").fetchone()
    return _account_row(row) if row else None

def list_accounts() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
    return [_account_row(r) for r in rows]

def set_default_account(account_id: str):
    with _WRITE_LOCK, _conn() as conn:
        conn.execute("UPDATE accounts SET is_default=0")
        conn.execute("UPDATE accounts SET is_default=1 WHERE id=?", (account_id,))

def delete_account(account_id: str):
    with _WRITE_LOCK, _conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))

def add_subscriber(chat_id: int):
    now = datetime.utcnow().isoformat()
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bot_subscribers (chat_id, created_at) VALUES (?,?)",
            (chat_id, now),
        )

def remove_subscriber(chat_id: int):
    with _WRITE_LOCK, _conn() as conn:
        conn.execute("DELETE FROM bot_subscribers WHERE chat_id=?", (chat_id,))

def get_subscribers() -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT chat_id FROM bot_subscribers").fetchall()
    return [r["chat_id"] for r in rows]

def _account_row(row: sqlite3.Row) -> Dict:
    d = dict(row)
    try:
        d["credentials"] = json.loads(d["credentials"])
    except Exception:
        pass
    return d
