"""
Tests for utils/storage.py: Database, CSVWriter, JSONLinesWriter.

These exercise the real SQLite engine, real CSV files, and real JSONL files on
disk via pytest's tmp_path fixture - no mocking. Every assertion that checks
persisted data reads it back with a *fresh* sqlite3.connect() / plain file
read, independent of the Database/Writer classes under test, so a bug in the
read path can't mask a bug in the write path (or vice versa).
"""
from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from scraper.models import Education, LinkedInProfile, WorkExperience
from utils.storage import CSVWriter, Database, JSONLinesWriter


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _success_profile(
    url: str = "https://www.linkedin.com/in/janedoe",
    full_name: str = "Jane Doe",
    exp_companies: tuple[str, ...] = ("Acme", "OldCo"),
    edu_schools: tuple[str, ...] = ("MIT",),
) -> LinkedInProfile:
    """A fetch_status='success' profile with N experiences / M education entries."""
    experiences = [
        WorkExperience(
            company=company,
            company_linkedin_url=f"https://linkedin.com/company/{company.lower()}",
            title=f"Engineer at {company}",
            description=f"Worked at {company}",
            location="Remote",
            starts_at_year=2010 + i,
            ends_at_year=None if i == 0 else 2010 + i + 2,
            is_current=(i == 0),
        )
        for i, company in enumerate(exp_companies)
    ]
    education = [
        Education(
            school=school,
            school_linkedin_url=f"https://linkedin.com/school/{school.lower()}",
            degree="BSc",
            field_of_study="Computer Science",
            grade="3.9",
            activities="Chess club",
            starts_at_year=2005 + i,
            ends_at_year=2009 + i,
        )
        for i, school in enumerate(edu_schools)
    ]
    return LinkedInProfile(
        linkedin_url=url,
        full_name=full_name,
        first_name=full_name.split()[0],
        last_name=full_name.split()[-1],
        headline="Software Engineer",
        summary="Builds things.",
        location="San Francisco, CA",
        country="United States",
        city="San Francisco",
        profile_pic_url="https://example.com/pic.jpg",
        connections=500,
        follower_count=1200,
        experiences=experiences,
        education=education,
        skills=["Python", "SQL"],
        languages=["English", "French"],
        source_api="scrapingdog",
        fetch_status="success",
        error_message="",
    )


def _failed_profile(
    url: str = "https://www.linkedin.com/in/ghost",
    experiences: list[WorkExperience] | None = None,
    education: list[Education] | None = None,
) -> LinkedInProfile:
    return LinkedInProfile(
        linkedin_url=url,
        fetch_status="failed",
        error_message="404 not found",
        experiences=experiences or [],
        education=education or [],
    )


def _raw_query(db_path, sql, params=()):
    """Query the sqlite file directly, bypassing Database entirely."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# job queue: init_job_queue / get_pending_urls
# ---------------------------------------------------------------------------

class TestJobQueue:
    def test_get_pending_urls_returns_exactly_what_was_queued(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        urls = [
            "https://www.linkedin.com/in/alice",
            "https://www.linkedin.com/in/bob",
            "https://www.linkedin.com/in/carol",
        ]
        db.init_job_queue(urls)

        pending = db.get_pending_urls()
        assert pending == urls  # ordered by ROWID == insertion order

    def test_init_job_queue_dedupes_repeated_urls(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        db.init_job_queue(["https://www.linkedin.com/in/alice"])
        db.init_job_queue(["https://www.linkedin.com/in/alice"])  # re-queue, e.g. resume run

        rows = _raw_query(db_path, "SELECT COUNT(*) AS c FROM job_status")
        assert rows[0]["c"] == 1
        assert db.get_pending_urls() == ["https://www.linkedin.com/in/alice"]

    def test_get_pending_urls_respects_limit(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        urls = [f"https://www.linkedin.com/in/user{i}" for i in range(5)]
        db.init_job_queue(urls)

        assert db.get_pending_urls(limit=2) == urls[:2]
        assert db.get_pending_urls(limit=100) == urls

    def test_get_pending_urls_self_heals_stuck_in_progress_rows(self, tmp_path):
        # Regression test: a URL left at 'in_progress' (process killed
        # between mark_in_progress() and save_profile()) must not be
        # permanently invisible to --resume. get_pending_urls() requeues
        # any 'in_progress' row as 'retry' before selecting.
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        urls = ["https://www.linkedin.com/in/alice", "https://www.linkedin.com/in/bob"]
        db.init_job_queue(urls)
        db.mark_in_progress(urls[0])  # simulate a worker that got killed mid-flight

        pending = db.get_pending_urls()
        assert set(pending) == set(urls)

        row = _raw_query(db_path, "SELECT status FROM job_status WHERE linkedin_url=?", (urls[0],))[0]
        assert row["status"] == "retry"

    def test_get_pending_urls_includes_failed_but_not_not_found(self, tmp_path):
        # A "failed" URL (network/API error, exhausted fallback chain) is a
        # transient failure -- --resume must retry it. A "not_found" URL
        # means every configured API confirmed no such profile exists, so
        # it's intentionally excluded: re-asking the same APIs won't change
        # the answer.
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        urls = ["https://www.linkedin.com/in/failed1", "https://www.linkedin.com/in/notfound1"]
        db.init_job_queue(urls)  # save_profile's job_status UPDATE is a no-op unless queued first
        db.save_profile(_failed_profile(url=urls[0]))
        db.save_profile(LinkedInProfile(linkedin_url=urls[1], fetch_status="not_found"))

        pending = db.get_pending_urls()
        assert "https://www.linkedin.com/in/failed1" in pending
        assert "https://www.linkedin.com/in/notfound1" not in pending

    def test_get_pending_urls_excludes_non_pending_statuses(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        urls = [
            "https://www.linkedin.com/in/pending1",
            "https://www.linkedin.com/in/success1",
            "https://www.linkedin.com/in/retry1",
        ]
        db.init_job_queue(urls)
        db.save_profile(_success_profile(url="https://www.linkedin.com/in/success1"))
        # manually push one to 'retry' the way a caller/engine would after a failure
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE job_status SET status='retry' WHERE linkedin_url=?",
            ("https://www.linkedin.com/in/retry1",),
        )
        conn.commit()
        conn.close()

        pending = db.get_pending_urls()
        assert "https://www.linkedin.com/in/pending1" in pending
        assert "https://www.linkedin.com/in/retry1" in pending  # retry is fetchable again
        assert "https://www.linkedin.com/in/success1" not in pending

    def test_database_creates_parent_directories(self, tmp_path):
        db_path = tmp_path / "nested" / "dirs" / "test.db"
        Database(str(db_path))
        assert db_path.exists()

    def test_state_persists_across_separate_database_instances(self, tmp_path):
        # Simulates the real CLI: one process queues, a later process resumes.
        db_path = tmp_path / "test.db"
        Database(str(db_path)).init_job_queue(["https://www.linkedin.com/in/alice"])

        db2 = Database(str(db_path))  # brand new instance, same file
        assert db2.get_pending_urls() == ["https://www.linkedin.com/in/alice"]


# ---------------------------------------------------------------------------
# mark_in_progress
# ---------------------------------------------------------------------------

class TestMarkInProgress:
    def test_sets_status_and_increments_attempts(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        url = "https://www.linkedin.com/in/alice"
        db.init_job_queue([url])

        db.mark_in_progress(url)
        row = _raw_query(db_path, "SELECT * FROM job_status WHERE linkedin_url=?", (url,))[0]
        assert row["status"] == "in_progress"
        assert row["attempts"] == 1
        assert row["last_attempt"] is not None

        db.mark_in_progress(url)  # simulate a retry attempt
        row = _raw_query(db_path, "SELECT * FROM job_status WHERE linkedin_url=?", (url,))[0]
        assert row["attempts"] == 2

    def test_on_url_never_queued_is_a_silent_no_op(self, tmp_path):
        # mark_in_progress is a plain UPDATE ... WHERE linkedin_url=?, not an
        # upsert, so calling it for a URL that was never init_job_queue'd
        # touches zero rows rather than creating one or raising.
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        db.mark_in_progress("https://www.linkedin.com/in/never-queued")

        rows = _raw_query(db_path, "SELECT COUNT(*) AS c FROM job_status")
        assert rows[0]["c"] == 0


# ---------------------------------------------------------------------------
# save_profile: success path, end-to-end row verification
# ---------------------------------------------------------------------------

class TestSaveProfileSuccess:
    def test_success_profile_writes_correct_rows_to_all_tables(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        profile = _success_profile()
        db.init_job_queue([profile.linkedin_url])

        db.save_profile(profile)

        # --- profiles table ---
        prof_rows = _raw_query(db_path, "SELECT * FROM profiles WHERE linkedin_url=?", (profile.linkedin_url,))
        assert len(prof_rows) == 1
        p = prof_rows[0]
        assert p["full_name"] == "Jane Doe"
        assert p["first_name"] == "Jane"
        assert p["last_name"] == "Doe"
        assert p["headline"] == "Software Engineer"
        assert p["connections"] == 500
        assert p["follower_count"] == 1200
        assert p["source_api"] == "scrapingdog"
        assert p["fetch_status"] == "success"
        assert p["error_message"] == ""
        assert json.loads(p["skills"]) == ["Python", "SQL"]
        assert json.loads(p["languages"]) == ["English", "French"]
        assert p["raw_json"] is not None
        raw = json.loads(p["raw_json"])
        assert raw["full_name"] == "Jane Doe"
        assert len(raw["experiences"]) == 2
        assert p["created_at"] is not None
        assert p["updated_at"] is not None
        profile_id = p["id"]

        # --- experiences table ---
        exp_rows = _raw_query(
            db_path,
            "SELECT * FROM experiences WHERE profile_id=? ORDER BY exp_index",
            (profile_id,),
        )
        assert len(exp_rows) == 2
        assert exp_rows[0]["exp_index"] == 1
        assert exp_rows[0]["company"] == "Acme"
        assert exp_rows[0]["title"] == "Engineer at Acme"
        assert exp_rows[0]["linkedin_url"] == profile.linkedin_url
        assert exp_rows[0]["is_current"] == 1
        assert exp_rows[0]["end_date"] == "Present"
        assert exp_rows[1]["exp_index"] == 2
        assert exp_rows[1]["company"] == "OldCo"
        assert exp_rows[1]["is_current"] == 0

        # --- education table ---
        edu_rows = _raw_query(
            db_path,
            "SELECT * FROM education WHERE profile_id=? ORDER BY edu_index",
            (profile_id,),
        )
        assert len(edu_rows) == 1
        assert edu_rows[0]["edu_index"] == 1
        assert edu_rows[0]["school"] == "MIT"
        assert edu_rows[0]["degree"] == "BSc"
        assert edu_rows[0]["start_year"] == 2005
        assert edu_rows[0]["end_year"] == 2009

        # --- job_status table ---
        job_rows = _raw_query(db_path, "SELECT * FROM job_status WHERE linkedin_url=?", (profile.linkedin_url,))
        assert len(job_rows) == 1
        assert job_rows[0]["status"] == "success"
        assert job_rows[0]["last_attempt"] is not None

    def test_is_current_boolean_stored_as_integer(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        profile = _success_profile(exp_companies=("Current Co",))
        profile.experiences[0].is_current = True
        db.save_profile(profile)

        row = _raw_query(db_path, "SELECT is_current FROM experiences")[0]
        assert row["is_current"] == 1
        assert isinstance(row["is_current"], int)


# ---------------------------------------------------------------------------
# save_profile: upsert / retry semantics (the important one)
# ---------------------------------------------------------------------------

class TestSaveProfileUpsertOnRetry:
    def test_second_save_replaces_rather_than_duplicates(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        url = "https://www.linkedin.com/in/janedoe"

        # First fetch attempt: 2 experiences, 1 education.
        first = _success_profile(url=url, exp_companies=("Acme", "OldCo"), edu_schools=("MIT",))
        db.save_profile(first)

        # Second fetch attempt for the SAME url (e.g. a resumed/re-run job),
        # with different experience/education data.
        second = _success_profile(
            url=url,
            full_name="Jane D. Doe",
            exp_companies=("NewCo",),
            edu_schools=("Stanford", "Harvard"),
        )
        db.save_profile(second)

        # profiles: still exactly one row, updated in place (no duplicate).
        prof_rows = _raw_query(db_path, "SELECT * FROM profiles WHERE linkedin_url=?", (url,))
        assert len(prof_rows) == 1
        assert prof_rows[0]["full_name"] == "Jane D. Doe"
        profile_id = prof_rows[0]["id"]

        # experiences: old rows replaced, not accumulated (1, not 2+1=3).
        exp_rows = _raw_query(db_path, "SELECT * FROM experiences WHERE profile_id=?", (profile_id,))
        assert len(exp_rows) == 1
        assert exp_rows[0]["company"] == "NewCo"
        assert "Acme" not in [r["company"] for r in exp_rows]
        assert "OldCo" not in [r["company"] for r in exp_rows]

        # also confirm nothing orphaned under linkedin_url either (belt & suspenders,
        # since experiences carries a redundant linkedin_url column alongside profile_id).
        exp_rows_by_url = _raw_query(db_path, "SELECT * FROM experiences WHERE linkedin_url=?", (url,))
        assert len(exp_rows_by_url) == 1

        # education: old rows replaced, not accumulated (2, not 1+2=3).
        edu_rows = _raw_query(db_path, "SELECT * FROM education WHERE profile_id=?", (profile_id,))
        assert len(edu_rows) == 2
        schools = {r["school"] for r in edu_rows}
        assert schools == {"Stanford", "Harvard"}
        assert "MIT" not in schools

        # no stray profile row was created and no rows are pointing at a
        # different/stale profile_id.
        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM profiles")[0]["c"] == 1

    def test_repeated_saves_of_identical_profile_stay_idempotent(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        profile = _success_profile()

        db.save_profile(profile)
        db.save_profile(profile)
        db.save_profile(profile)

        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM profiles")[0]["c"] == 1
        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM experiences")[0]["c"] == 2
        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM education")[0]["c"] == 1


# ---------------------------------------------------------------------------
# save_profile: failed path (guard on fetch_status == "success")
# ---------------------------------------------------------------------------

class TestSaveProfileFailed:
    def test_failed_profile_does_not_insert_experience_or_education_rows(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        profile = _failed_profile()
        db.init_job_queue([profile.linkedin_url])

        db.save_profile(profile)  # must not raise

        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM experiences")[0]["c"] == 0
        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM education")[0]["c"] == 0

        prof_rows = _raw_query(db_path, "SELECT * FROM profiles WHERE linkedin_url=?", (profile.linkedin_url,))
        assert len(prof_rows) == 1
        assert prof_rows[0]["fetch_status"] == "failed"
        assert prof_rows[0]["error_message"] == "404 not found"
        assert prof_rows[0]["raw_json"] is None  # to_json() is only called on success

        job_rows = _raw_query(db_path, "SELECT * FROM job_status WHERE linkedin_url=?", (profile.linkedin_url,))
        assert job_rows[0]["status"] == "failed"

    def test_failed_profile_with_nonempty_experience_list_still_skips_insert(self, tmp_path):
        # Exercises the `if profile.fetch_status == "success":` guard itself,
        # not just the trivial "empty list" case: even a failed profile that
        # somehow carries populated experiences/education must not persist them.
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        profile = _failed_profile(
            experiences=[WorkExperience(company="ShouldNotPersist")],
            education=[Education(school="ShouldNotPersist")],
        )

        db.save_profile(profile)

        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM experiences")[0]["c"] == 0
        assert _raw_query(db_path, "SELECT COUNT(*) AS c FROM education")[0]["c"] == 0

    def test_success_then_failed_retry_clears_prior_experiences(self, tmp_path):
        # The DELETE of old experiences/education now runs unconditionally
        # (before the success/failure branch), so a profile that previously
        # saved successfully and is later re-saved as "failed" (e.g. a
        # transient re-fetch error on a resumed run) no longer keeps stale
        # child rows attached to what is now a failed profile.
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        url = "https://www.linkedin.com/in/janedoe"
        db.save_profile(_success_profile(url=url))

        db.save_profile(_failed_profile(url=url))

        prof_rows = _raw_query(db_path, "SELECT * FROM profiles WHERE linkedin_url=?", (url,))
        assert prof_rows[0]["fetch_status"] == "failed"
        exp_rows = _raw_query(db_path, "SELECT * FROM experiences WHERE linkedin_url=?", (url,))
        assert len(exp_rows) == 0
        edu_rows = _raw_query(db_path, "SELECT * FROM education WHERE linkedin_url=?", (url,))
        assert len(edu_rows) == 0


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_returns_correct_counts_per_status(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        urls = [f"https://www.linkedin.com/in/user{i}" for i in range(6)]
        db.init_job_queue(urls)

        db.save_profile(_success_profile(url=urls[0]))
        db.save_profile(_success_profile(url=urls[1]))
        db.save_profile(_failed_profile(url=urls[2]))
        db.save_profile(LinkedInProfile(linkedin_url=urls[3], fetch_status="not_found"))
        db.save_profile(LinkedInProfile(linkedin_url=urls[4], fetch_status="rate_limited"))
        # urls[5] left untouched -> stays 'pending'

        stats = db.get_stats()
        assert stats["success"] == 2
        assert stats["failed"] == 1
        assert stats["not_found"] == 1
        assert stats["rate_limited"] == 1
        assert stats["pending"] == 1
        assert stats["retry"] == 0

    def test_stats_on_empty_db_are_all_zero(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        stats = db.get_stats()
        assert set(stats.keys()) == {
            "pending", "retry", "in_progress", "success", "failed", "not_found", "rate_limited",
        }
        assert all(v == 0 for v in stats.values())


# ---------------------------------------------------------------------------
# CSVWriter
# ---------------------------------------------------------------------------

class TestCSVWriter:
    def test_write_before_open_raises(self, tmp_path):
        writer = CSVWriter(str(tmp_path / "out.csv"))
        with pytest.raises(RuntimeError):
            writer.write(_success_profile())

    def test_profile_with_no_experiences_or_education_produces_exactly_one_row(self, tmp_path):
        path = tmp_path / "out.csv"
        profile = LinkedInProfile(
            linkedin_url="https://www.linkedin.com/in/empty",
            full_name="Empty Person",
            fetch_status="success",
        )
        with CSVWriter(str(path)) as writer:
            writer.write(profile)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # header + exactly one data row

        with open(path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["linkedin_url"] == "https://www.linkedin.com/in/empty"
        assert reader[0]["full_name"] == "Empty Person"
        assert reader[0]["fetch_status"] == "success"
        assert reader[0]["record_type"] == "profile"
        assert reader[0]["exp_company"] == ""

    def test_profile_with_experiences_and_education_produces_n_plus_m_rows(self, tmp_path):
        path = tmp_path / "out.csv"
        profile = _success_profile(exp_companies=("Acme", "OldCo"), edu_schools=("MIT",))
        with CSVWriter(str(path)) as writer:
            writer.write(profile)

        with open(path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 3
        assert sum(1 for r in reader if r["record_type"] == "experience") == 2
        assert sum(1 for r in reader if r["record_type"] == "education") == 1

    def test_reopening_same_path_appends_without_duplicating_header(self, tmp_path):
        path = tmp_path / "out.csv"

        writer = CSVWriter(str(path))
        writer.open()
        writer.write(LinkedInProfile(linkedin_url="https://www.linkedin.com/in/first", fetch_status="success"))
        writer.close()

        # Re-open on the same path - simulates a second engine run / resumed process
        # appending to an existing output file.
        writer2 = CSVWriter(str(path))
        writer2.open()
        writer2.write(_success_profile(url="https://www.linkedin.com/in/second"))  # 2 exp + 1 edu = 3 rows
        writer2.close()

        lines = path.read_text(encoding="utf-8").splitlines()
        header_lines = [ln for ln in lines if ln.startswith("linkedin_url,")]
        assert len(header_lines) == 1  # header written only once, not on the second open()

        with open(path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 1 + 3  # first profile's 1 row + second profile's 3 rows
        assert reader[0]["linkedin_url"] == "https://www.linkedin.com/in/first"
        assert reader[1]["linkedin_url"] == "https://www.linkedin.com/in/second"

    def test_open_on_preexisting_empty_file_still_writes_header(self, tmp_path):
        path = tmp_path / "out.csv"
        path.touch()  # exists, but zero bytes
        assert path.stat().st_size == 0

        with CSVWriter(str(path)) as writer:
            writer.write(LinkedInProfile(linkedin_url="x", fetch_status="success"))

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("linkedin_url,")
        assert len(lines) == 2

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "out.csv"
        with CSVWriter(str(path)) as writer:
            writer.write(LinkedInProfile(linkedin_url="x", fetch_status="success"))
        assert path.exists()


# ---------------------------------------------------------------------------
# JSONLinesWriter
# ---------------------------------------------------------------------------

class TestJSONLinesWriter:
    def test_write_before_open_raises(self, tmp_path):
        writer = JSONLinesWriter(str(tmp_path / "out.jsonl"))
        with pytest.raises(RuntimeError):
            writer.write(_success_profile())

    def test_each_write_appends_exactly_one_valid_json_line(self, tmp_path):
        path = tmp_path / "out.jsonl"
        p1 = _success_profile(url="https://www.linkedin.com/in/first", full_name="First Person")
        p2 = _success_profile(url="https://www.linkedin.com/in/second", full_name="Second Person")

        with JSONLinesWriter(str(path)) as writer:
            writer.write(p1)
            writer.write(p2)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

        parsed = [json.loads(line) for line in lines]  # raises if any line isn't valid JSON
        assert parsed[0]["linkedin_url"] == "https://www.linkedin.com/in/first"
        assert parsed[0]["full_name"] == "First Person"
        assert len(parsed[0]["experiences"]) == 2
        assert parsed[1]["linkedin_url"] == "https://www.linkedin.com/in/second"
        assert parsed[1]["full_name"] == "Second Person"

    def test_reopening_same_path_appends_not_overwrites(self, tmp_path):
        path = tmp_path / "out.jsonl"

        writer = JSONLinesWriter(str(path))
        writer.open()
        writer.write(_success_profile(url="https://www.linkedin.com/in/first"))
        writer.close()

        writer2 = JSONLinesWriter(str(path))
        writer2.open()
        writer2.write(_success_profile(url="https://www.linkedin.com/in/second"))
        writer2.close()

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        urls = [json.loads(ln)["linkedin_url"] for ln in lines]
        assert urls == ["https://www.linkedin.com/in/first", "https://www.linkedin.com/in/second"]

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "out.jsonl"
        with JSONLinesWriter(str(path)) as writer:
            writer.write(_success_profile())
        assert path.exists()
