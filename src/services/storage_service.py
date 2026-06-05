from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from src.models.job import Job


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db_path(settings: dict) -> Path:
    return Path(settings.get("storage", {}).get("database_path", "data/seen_jobs.db"))


def _serialize_parts(job: Job) -> str:
    return job.extracted_experience_parts


def _ensure_column(conn: sqlite3.Connection, column_name: str, column_type: str) -> None:
    """Add a column to seen_jobs if it does not already exist."""
    cursor = conn.execute("PRAGMA table_info(seen_jobs)")
    columns = {row[1] for row in cursor.fetchall()}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {column_name} {column_type}")
        conn.commit()


def init_db(settings: dict) -> None:
    path = _get_db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT UNIQUE,
                job_id TEXT,
                company TEXT,
                title TEXT,
                location TEXT,
                url TEXT,
                source_url TEXT,
                posted_date TEXT,
                description TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                extracted_experience_parts TEXT
            )
            """
        )
        # Migrations for existing databases that lack these columns.
        _ensure_column(conn, "job_id", "TEXT")
        _ensure_column(conn, "description", "TEXT")
        _ensure_column(conn, "extracted_experience_parts", "TEXT")
        conn.commit()


def is_seen(job: Job, settings: dict) -> bool:
    path = _get_db_path(settings)
    with sqlite3.connect(path) as conn:
        cursor = conn.execute("SELECT 1 FROM seen_jobs WHERE job_key = ?", (job.unique_key(),))
        return cursor.fetchone() is not None


def save_job(job: Job, settings: dict) -> None:
    save_jobs([job], settings)


def save_jobs(jobs: list[Job], settings: dict) -> None:
    if not jobs:
        return

    path = _get_db_path(settings)
    now = _now_iso()

    rows = [
        (
            job.unique_key(),
            job.job_id,
            job.company,
            job.title,
            job.location,
            job.url,
            job.source_url,
            job.posted_date,
            job.description,
            now,
            now,
            _serialize_parts(job),
        )
        for job in jobs
    ]

    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO seen_jobs (
                job_key,
                job_id,
                company,
                title,
                location,
                url,
                source_url,
                posted_date,
                description,
                first_seen_at,
                last_seen_at,
                extracted_experience_parts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_key) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                extracted_experience_parts = excluded.extracted_experience_parts
            """,
            rows,
        )
        conn.commit()


def get_new_jobs(jobs: list[Job], settings: dict) -> list[Job]:
    if not jobs:
        return []

    path = _get_db_path(settings)
    job_key_pairs = [(job, job.unique_key()) for job in jobs]
    keys = [key for _, key in job_key_pairs]

    with sqlite3.connect(path) as conn:
        placeholders = ",".join("?" for _ in keys)
        existing = set()
        if keys:
            cursor = conn.execute(
                f"SELECT job_key FROM seen_jobs WHERE job_key IN ({placeholders})", keys
            )
            existing = {row[0] for row in cursor.fetchall()}

        if existing:
            now = _now_iso()
            conn.executemany(
                "UPDATE seen_jobs SET last_seen_at = ? WHERE job_key = ?",
                [(now, key) for key in existing],
            )
            conn.commit()

    return [job for job, key in job_key_pairs if key not in existing]
