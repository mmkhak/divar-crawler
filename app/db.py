"""SQLite persistence. Tokens stay in the local data directory."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL,
    proxy TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    daily_cap INTEGER NOT NULL DEFAULT 50,
    views_today INTEGER NOT NULL DEFAULT 0,
    views_day TEXT,
    delay_min REAL NOT NULL DEFAULT 20,
    delay_max REAL NOT NULL DEFAULT 40,
    last_error TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '[]',
    city_id TEXT NOT NULL,
    city_name TEXT NOT NULL,
    title_filter TEXT NOT NULL DEFAULT '[]',
    desc_filter TEXT NOT NULL DEFAULT '[]',
    max_pages INTEGER NOT NULL DEFAULT 5,
    fetch_details INTEGER NOT NULL DEFAULT 1,
    fetch_phones INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'paused',
    query_index INTEGER NOT NULL DEFAULT 0,
    page INTEGER NOT NULL DEFAULT 0,
    pagination_json TEXT,
    query_done INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    token TEXT NOT NULL,
    title TEXT,
    district TEXT,
    price_text TEXT,
    snippet TEXT,
    description TEXT,
    phone TEXT,
    phone_status TEXT NOT NULL DEFAULT 'none',
    contact_uuid TEXT,
    link TEXT,
    red_text TEXT,
    found_by TEXT,
    specs_json TEXT,
    breadcrumb TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, token)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

DEFAULTS = {
    "anon_rps": "1",
    "default_daily_cap": "50",
    "delay_min": "20",
    "delay_max": "40",
}


def now_iso() -> str:
    return datetime.now(TEHRAN).isoformat(timespec="seconds")


def today_key() -> str:
    return datetime.now(TEHRAN).date().isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        for key, value in DEFAULTS.items():
            self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def setting(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else DEFAULTS.get(key, "")

    def settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
            return {row["key"]: row["value"] for row in rows}

    def update_settings(self, values: dict[str, str]) -> dict[str, str]:
        with self._lock:
            for key, value in values.items():
                if key in DEFAULTS:
                    self._conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)),
                    )
            self._conn.commit()
        return self.settings()

    def add_log(self, job_id: int | None, level: str, message: str) -> dict[str, Any]:
        stamp = now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO logs(job_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (job_id, level, message, stamp),
            )
            self._conn.commit()
            return {"id": cur.lastrowid, "job_id": job_id, "level": level, "message": message, "created_at": stamp}

    def recent_logs(self, limit: int = 200, job_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if job_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                    (job_id, limit),
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def create_job(self, fields: dict[str, Any]) -> dict[str, Any]:
        stamp = now_iso()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO jobs(
                    name, category, keywords, city_id, city_name, title_filter, desc_filter,
                    max_pages, fetch_details, fetch_phones, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'paused', ?, ?)""",
                (
                    fields["name"],
                    fields["category"],
                    json.dumps(fields["keywords"], ensure_ascii=False),
                    str(fields["city_id"]),
                    fields["city_name"],
                    json.dumps(fields["title_filter"], ensure_ascii=False),
                    json.dumps(fields["desc_filter"], ensure_ascii=False),
                    int(fields["max_pages"]),
                    1 if fields["fetch_details"] else 0,
                    1 if fields["fetch_phones"] else 0,
                    stamp,
                    stamp,
                ),
            )
            self._conn.commit()
            job_id = cur.lastrowid
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job(row) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
        return [_job(row) for row in rows]

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        if "pagination" in fields:
            fields["pagination_json"] = json.dumps(fields.pop("pagination"), ensure_ascii=False)
        cols = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id)
            )
            self._conn.commit()

    def set_status(self, job_id: int, status: str, error: str | None = None) -> None:
        self.update_job(job_id, status=status, error=error)

    def jobs_to_resume(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status='running'"
            ).fetchall()
        return [row["id"] for row in rows]

    def upsert_listing(self, job_id: int, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        stamp = now_iso()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM listings WHERE job_id=? AND token=?",
                (job_id, record["token"]),
            ).fetchone()
            if existing:
                return dict(existing), False
            self._conn.execute(
                """INSERT INTO listings(
                    job_id, token, title, district, price_text, snippet, description, phone,
                    phone_status, contact_uuid, link, red_text, found_by, specs_json, breadcrumb, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    record["token"],
                    record.get("title"),
                    record.get("district"),
                    record.get("price_text"),
                    record.get("snippet"),
                    record.get("description"),
                    record.get("phone"),
                    record.get("phone_status") or "none",
                    record.get("contact_uuid"),
                    record.get("link"),
                    record.get("red_text"),
                    record.get("found_by"),
                    record.get("specs_json"),
                    record.get("breadcrumb"),
                    stamp,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM listings WHERE job_id=? AND token=?",
                (job_id, record["token"]),
            ).fetchone()
        return dict(row), True

    def update_listing(self, listing_id: int, **fields: Any) -> dict[str, Any]:
        cols = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE listings SET {cols} WHERE id=?", (*fields.values(), listing_id)
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        return dict(row)

    def pending_phones(self, job_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM listings
                   WHERE job_id=? AND phone_status='pending'
                   ORDER BY id LIMIT ?""",
                (job_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def query_listings(
        self,
        job_id: int | None = None,
        q: str = "",
        sort: str = "created_at",
        direction: str = "desc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        allowed = {
            "created_at",
            "title",
            "district",
            "price_text",
            "phone",
            "job_id",
        }
        column = sort if sort in allowed else "created_at"
        order = "ASC" if direction.lower() == "asc" else "DESC"
        sql = "SELECT * FROM listings WHERE 1=1"
        args: list[Any] = []
        if job_id is not None:
            sql += " AND job_id=?"
            args.append(job_id)
        if q.strip():
            like = f"%{q.strip()}%"
            sql += " AND (title LIKE ? OR district LIKE ? OR phone LIKE ? OR snippet LIKE ? OR description LIKE ? OR token LIKE ?)"
            args.extend([like, like, like, like, like, like])
        sql += f" ORDER BY {column} {order}, id DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def job_counts(self, job_id: int) -> dict[str, int]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) c FROM listings WHERE job_id=?", (job_id,)
            ).fetchone()["c"]
            details = self._conn.execute(
                "SELECT COUNT(*) c FROM listings WHERE job_id=? AND description IS NOT NULL AND description != ''",
                (job_id,),
            ).fetchone()["c"]
            phones = self._conn.execute(
                "SELECT COUNT(*) c FROM listings WHERE job_id=? AND phone IS NOT NULL AND phone != ''",
                (job_id,),
            ).fetchone()["c"]
        return {"listings": total, "with_description": details, "with_phone": phones}

    def add_account(self, fields: dict[str, Any]) -> dict[str, Any]:
        stamp = now_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO accounts(
                    phone, token, proxy, status, daily_cap, views_today, views_day,
                    delay_min, delay_max, created_at
                ) VALUES (?, ?, ?, 'active', ?, 0, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    token=excluded.token, proxy=excluded.proxy, status='active',
                    daily_cap=excluded.daily_cap, delay_min=excluded.delay_min,
                    delay_max=excluded.delay_max, last_error=NULL""",
                (
                    fields["phone"],
                    fields["token"],
                    fields.get("proxy") or None,
                    int(fields.get("daily_cap") or 50),
                    today_key(),
                    float(fields.get("delay_min") or 20),
                    float(fields.get("delay_max") or 40),
                    stamp,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE phone=?", (fields["phone"],)
            ).fetchone()
        return _public_account(row)

    def list_accounts(self) -> list[dict[str, Any]]:
        self._roll_daily_caps()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [_public_account(row) for row in rows]

    def account_secrets(self) -> list[dict[str, Any]]:
        self._roll_daily_caps()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def update_account(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE accounts SET {cols} WHERE id=?", (*fields.values(), account_id)
            )
            self._conn.commit()

    def delete_account(self, account_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self._conn.commit()

    def _roll_daily_caps(self) -> None:
        day = today_key()
        with self._lock:
            self._conn.execute(
                """UPDATE accounts
                   SET views_today=0, views_day=?,
                       status=CASE WHEN status='limited' THEN 'active' ELSE status END
                   WHERE views_day IS NULL OR views_day!=?""",
                (day, day),
            )
            self._conn.commit()


def _job(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["keywords"] = json.loads(data.pop("keywords") or "[]")
    data["title_filter"] = json.loads(data.pop("title_filter") or "[]")
    data["desc_filter"] = json.loads(data.pop("desc_filter") or "[]")
    raw = data.pop("pagination_json")
    data["pagination"] = json.loads(raw) if raw else None
    data["fetch_details"] = bool(data["fetch_details"])
    data["fetch_phones"] = bool(data["fetch_phones"])
    data["query_done"] = bool(data["query_done"])
    return data


def _public_account(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    token = data.pop("token") or ""
    data["token_preview"] = (token[:4] + "…") if token else ""
    data["has_token"] = bool(token)
    return data
