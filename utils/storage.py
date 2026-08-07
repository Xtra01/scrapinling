"""
Storage utilities: SQLite checkpoint/resume + CSV/JSON output writers.
"""
import csv
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from scraper.models import LinkedInProfile

logger = logging.getLogger(__name__)


# ─── SQLite Database ─────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_url    TEXT    UNIQUE NOT NULL,
    full_name       TEXT,
    first_name      TEXT,
    last_name       TEXT,
    headline        TEXT,
    summary         TEXT,
    location        TEXT,
    country         TEXT,
    city            TEXT,
    profile_pic_url TEXT,
    connections     INTEGER,
    follower_count  INTEGER,
    skills          TEXT,
    languages       TEXT,
    source_api      TEXT,
    fetch_status    TEXT    NOT NULL DEFAULT 'pending',
    error_message   TEXT,
    raw_json        TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS experiences (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id              INTEGER NOT NULL REFERENCES profiles(id),
    linkedin_url            TEXT NOT NULL,
    exp_index               INTEGER,
    company                 TEXT,
    company_linkedin_url    TEXT,
    title                   TEXT,
    description             TEXT,
    location                TEXT,
    start_date              TEXT,
    end_date                TEXT,
    is_current              INTEGER
);

CREATE TABLE IF NOT EXISTS education (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id              INTEGER NOT NULL REFERENCES profiles(id),
    linkedin_url            TEXT NOT NULL,
    edu_index               INTEGER,
    school                  TEXT,
    school_linkedin_url     TEXT,
    degree                  TEXT,
    field_of_study          TEXT,
    grade                   TEXT,
    activities              TEXT,
    start_year              INTEGER,
    end_year                INTEGER
);

CREATE TABLE IF NOT EXISTS job_status (
    linkedin_url    TEXT    PRIMARY KEY,
    status          TEXT    NOT NULL DEFAULT 'pending',
    attempts        INTEGER DEFAULT 0,
    last_attempt    DATETIME
);

CREATE INDEX IF NOT EXISTS idx_profiles_url ON profiles(linkedin_url);
CREATE INDEX IF NOT EXISTS idx_exp_url ON experiences(linkedin_url);
CREATE INDEX IF NOT EXISTS idx_edu_url ON education(linkedin_url);
CREATE INDEX IF NOT EXISTS idx_job_status ON job_status(status);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_job_queue(self, urls: list[str]):
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO job_status (linkedin_url, status) VALUES (?, 'pending')",
                [(url,) for url in urls],
            )
        logger.info("Initialized %d URLs in job queue", len(urls))

    def get_pending_urls(self, limit: Optional[int] = None) -> list[str]:
        query = """
            SELECT linkedin_url FROM job_status
            WHERE status IN ('pending', 'retry')
            ORDER BY ROWID
        """
        if limit:
            query += f" LIMIT {limit}"
        with self._conn() as conn:
            rows = conn.execute(query).fetchall()
        return [row["linkedin_url"] for row in rows]

    def get_stats(self) -> dict:
        with self._conn() as conn:
            stats = {}
            for status in ("pending", "retry", "success", "failed", "not_found", "rate_limited"):
                count = conn.execute(
                    "SELECT COUNT(*) FROM job_status WHERE status = ?", (status,)
                ).fetchone()[0]
                stats[status] = count
        return stats

    def mark_in_progress(self, url: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_status SET status='in_progress', last_attempt=CURRENT_TIMESTAMP, attempts=attempts+1 WHERE linkedin_url=?",
                (url,),
            )

    def save_profile(self, profile: LinkedInProfile):
        """Upsert a fetched profile and all its experiences/education."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO profiles
                    (linkedin_url, full_name, first_name, last_name, headline, summary,
                     location, country, city, profile_pic_url, connections, follower_count,
                     skills, languages, source_api, fetch_status, error_message, raw_json, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
                ON CONFLICT(linkedin_url) DO UPDATE SET
                    full_name=excluded.full_name,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    headline=excluded.headline,
                    summary=excluded.summary,
                    location=excluded.location,
                    country=excluded.country,
                    city=excluded.city,
                    profile_pic_url=excluded.profile_pic_url,
                    connections=excluded.connections,
                    follower_count=excluded.follower_count,
                    skills=excluded.skills,
                    languages=excluded.languages,
                    source_api=excluded.source_api,
                    fetch_status=excluded.fetch_status,
                    error_message=excluded.error_message,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    profile.linkedin_url, profile.full_name, profile.first_name, profile.last_name,
                    profile.headline, profile.summary, profile.location, profile.country, profile.city,
                    profile.profile_pic_url, profile.connections, profile.follower_count,
                    json.dumps(profile.skills, ensure_ascii=False),
                    json.dumps(profile.languages, ensure_ascii=False),
                    profile.source_api, profile.fetch_status, profile.error_message,
                    profile.to_json() if profile.fetch_status == "success" else None,
                ),
            )

            profile_row = conn.execute(
                "SELECT id FROM profiles WHERE linkedin_url=?", (profile.linkedin_url,)
            ).fetchone()
            profile_id = profile_row["id"]

            if profile.fetch_status == "success":
                conn.execute("DELETE FROM experiences WHERE profile_id=?", (profile_id,))
                conn.execute("DELETE FROM education WHERE profile_id=?", (profile_id,))

                for i, exp in enumerate(profile.experiences):
                    conn.execute(
                        """INSERT INTO experiences
                           (profile_id, linkedin_url, exp_index, company, company_linkedin_url,
                            title, description, location, start_date, end_date, is_current)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            profile_id, profile.linkedin_url, i + 1,
                            exp.company, exp.company_linkedin_url,
                            exp.title, exp.description, exp.location,
                            exp.start_str(), exp.end_str(), int(exp.is_current),
                        ),
                    )

                for i, edu in enumerate(profile.education):
                    conn.execute(
                        """INSERT INTO education
                           (profile_id, linkedin_url, edu_index, school, school_linkedin_url,
                            degree, field_of_study, grade, activities, start_year, end_year)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            profile_id, profile.linkedin_url, i + 1,
                            edu.school, edu.school_linkedin_url,
                            edu.degree, edu.field_of_study, edu.grade, edu.activities,
                            edu.starts_at_year, edu.ends_at_year,
                        ),
                    )

            conn.execute(
                "UPDATE job_status SET status=?, last_attempt=CURRENT_TIMESTAMP WHERE linkedin_url=?",
                (profile.fetch_status, profile.linkedin_url),
            )


# ─── CSV Writer ──────────────────────────────────────────────────────────────────────

CSV_FIELDNAMES = [
    "linkedin_url", "full_name", "first_name", "last_name", "headline",
    "location", "country", "city", "connections",
    "source_api", "fetch_status", "error_message",
    "record_type",
    "exp_index", "exp_company", "exp_company_linkedin_url",
    "exp_title", "exp_description", "exp_location", "exp_start", "exp_end", "exp_is_current",
    "edu_index", "edu_school", "edu_school_linkedin_url",
    "edu_degree", "edu_field_of_study", "edu_grade", "edu_start_year", "edu_end_year",
]


class CSVWriter:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._writer = None

    def open(self):
        write_header = not Path(self.path).exists() or Path(self.path).stat().st_size == 0
        self._file = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._file, fieldnames=CSV_FIELDNAMES, extrasaction="ignore", lineterminator="\n",
        )
        if write_header:
            self._writer.writeheader()

    def write(self, profile: LinkedInProfile):
        if not self._writer:
            raise RuntimeError("Call open() before write()")
        for row in profile.to_flat_rows():
            self._writer.writerow(row)
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()


# ─── JSON Lines Writer ───────────────────────────────────────────────────────

class JSONLinesWriter:
    """Writes one JSON object per line (JSON Lines / NDJSON format)."""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._file = None

    def open(self):
        self._file = open(self.path, "a", encoding="utf-8")

    def write(self, profile: LinkedInProfile):
        if not self._file:
            raise RuntimeError("Call open() before write()")
        self._file.write(profile.to_json() + "\n")
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
