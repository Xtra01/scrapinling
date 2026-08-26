"""
Tests for main.py: the Click CLI (scrape/status/export commands) and its
pure helper functions (_clean_key, _export_csv, _print_api_config,
_print_db_stats). No network, no real API keys - the `scrape` command's
error paths are exercised via CliRunner; the actual fetch pipeline itself
is covered by scraper/engine.py's own tests and the project's end-to-end
smoke test, not here.
"""
from __future__ import annotations

import csv
import json
import sqlite3

import pytest
from click.testing import CliRunner

import main as main_mod
from scraper.models import Education, LinkedInProfile, WorkExperience
from utils.storage import Database


# ---------------------------------------------------------------------------
# _clean_key
# ---------------------------------------------------------------------------

class TestCleanKey:
    def test_none_returns_none(self):
        assert main_mod._clean_key(None) is None

    def test_empty_string_returns_none(self):
        assert main_mod._clean_key("") is None

    def test_placeholder_value_returns_none(self):
        assert main_mod._clean_key("your_scrapingdog_api_key_here") is None

    def test_real_key_is_returned_unchanged(self):
        assert main_mod._clean_key("sk_live_abc123") == "sk_live_abc123"

    def test_key_containing_but_not_starting_with_your_is_kept(self):
        # Only a *leading* "your_" is treated as a placeholder.
        assert main_mod._clean_key("abc_your_key_123") == "abc_your_key_123"


# ---------------------------------------------------------------------------
# _export_csv
# ---------------------------------------------------------------------------

def _make_db_with_profile(tmp_path, profile: LinkedInProfile) -> str:
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    db.init_job_queue([profile.linkedin_url])
    db.save_profile(profile)
    return db_path


class TestExportCsv:
    def test_profile_with_no_experience_or_education_produces_one_row(self, tmp_path):
        profile = LinkedInProfile(
            linkedin_url="https://www.linkedin.com/in/empty",
            full_name="Empty Person",
            fetch_status="success",
        )
        db_path = _make_db_with_profile(tmp_path, profile)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM profiles WHERE fetch_status='success'").fetchall()

        out_path = tmp_path / "export.csv"
        main_mod._export_csv(conn, rows, str(out_path))
        conn.close()

        with open(out_path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["linkedin_url"] == "https://www.linkedin.com/in/empty"
        assert reader[0]["full_name"] == "Empty Person"

    def test_profile_with_experience_and_education_produces_correct_rows(self, tmp_path):
        profile = LinkedInProfile(
            linkedin_url="https://www.linkedin.com/in/janedoe",
            full_name="Jane Doe",
            fetch_status="success",
            experiences=[
                WorkExperience(company="Acme", title="Engineer", starts_at_year=2020, is_current=True),
                WorkExperience(company="OldCo", title="Junior Dev", starts_at_year=2015, ends_at_year=2019),
            ],
            education=[Education(school="MIT", degree="BSc", starts_at_year=2010, ends_at_year=2014)],
        )
        db_path = _make_db_with_profile(tmp_path, profile)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM profiles WHERE fetch_status='success'").fetchall()

        out_path = tmp_path / "export.csv"
        main_mod._export_csv(conn, rows, str(out_path))
        conn.close()

        with open(out_path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 3  # 2 experiences + 1 education

        exp_rows = [r for r in reader if r["record_type"] == "experience"]
        edu_rows = [r for r in reader if r["record_type"] == "education"]
        assert len(exp_rows) == 2
        assert len(edu_rows) == 1
        assert exp_rows[0]["exp_company"] == "Acme"
        assert exp_rows[0]["exp_is_current"] == "1"
        assert edu_rows[0]["edu_school"] == "MIT"
        assert edu_rows[0]["edu_start_year"] == "2010"

    def test_multiple_profiles_export_independently(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        urls = ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"]
        db.init_job_queue(urls)
        db.save_profile(LinkedInProfile(linkedin_url=urls[0], full_name="A", fetch_status="success"))
        db.save_profile(LinkedInProfile(linkedin_url=urls[1], full_name="B", fetch_status="success"))

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM profiles WHERE fetch_status='success' ORDER BY linkedin_url").fetchall()

        out_path = tmp_path / "export.csv"
        main_mod._export_csv(conn, rows, str(out_path))
        conn.close()

        with open(out_path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 2
        assert {r["full_name"] for r in reader} == {"A", "B"}


# ---------------------------------------------------------------------------
# _print_api_config / _print_db_stats (smoke tests via Rich console capture)
# ---------------------------------------------------------------------------

class TestPrintApiConfig:
    def test_configured_and_unconfigured_apis_both_render(self, capsys):
        # Should not raise regardless of which keys are set/unset.
        main_mod._print_api_config(
            brightdata="key1", scrapingdog=None, netrows="key3",
            linkdapi=None, pdl=None, scrapin=None, rocketreach=None,
        )
        out = capsys.readouterr().out
        assert "Bright Data" in out
        assert "Scrapingdog" in out


class TestPrintDbStats:
    def test_renders_without_crashing_on_empty_db(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        main_mod._print_db_stats(db)  # should not raise

    def test_stuck_in_progress_row_shown_when_present(self, tmp_path, capsys):
        db = Database(str(tmp_path / "test.db"))
        url = "https://www.linkedin.com/in/stuck"
        db.init_job_queue([url])
        db.mark_in_progress(url)

        main_mod._print_db_stats(db)
        out = capsys.readouterr().out
        assert "Yarıda kalmış" in out

    def test_no_stuck_row_when_nothing_in_progress(self, tmp_path, capsys):
        db = Database(str(tmp_path / "test.db"))
        db.init_job_queue(["https://www.linkedin.com/in/normal"])

        main_mod._print_db_stats(db)
        out = capsys.readouterr().out
        assert "Yarıda kalmış" not in out


# ---------------------------------------------------------------------------
# CLI: scrape command error paths (no network involved)
# ---------------------------------------------------------------------------

class TestScrapeCommandErrorPaths:
    def test_no_api_key_configured_exits_with_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        input_csv = tmp_path / "links.csv"
        input_csv.write_text("linkedin_url\nhttps://www.linkedin.com/in/someone\n")

        runner = CliRunner()
        result = runner.invoke(main_mod.cli, ["scrape", "--input", str(input_csv)])

        assert result.exit_code == 1
        assert "Hiç API anahtarı yapılandırılmadı" in result.output

    def test_placeholder_key_is_treated_as_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        input_csv = tmp_path / "links.csv"
        input_csv.write_text("linkedin_url\nhttps://www.linkedin.com/in/someone\n")

        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli,
            [
                "scrape", "--input", str(input_csv),
                "--scrapingdog-key", "your_scrapingdog_api_key_here",
            ],
        )
        assert result.exit_code == 1
        assert "Hiç API anahtarı yapılandırılmadı" in result.output

    def test_no_valid_urls_in_input_exits_with_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        input_csv = tmp_path / "links.csv"
        input_csv.write_text("linkedin_url\nhttps://www.example.com/not-linkedin\n")

        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli,
            ["scrape", "--input", str(input_csv), "--scrapingdog-key", "real-key-123"],
        )
        assert result.exit_code == 1
        assert "Geçerli LinkedIn URL bulunamadı" in result.output

    def test_missing_input_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli,
            ["scrape", "--input", str(tmp_path / "does-not-exist.csv"),
             "--scrapingdog-key", "real-key-123"],
        )
        assert result.exit_code != 0

    def test_all_urls_already_processed_reports_done_and_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        input_csv = tmp_path / "links.csv"
        url = "https://www.linkedin.com/in/donealready"
        input_csv.write_text(f"linkedin_url\n{url}\n")

        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        db.init_job_queue([url])
        db.save_profile(LinkedInProfile(linkedin_url=url, fetch_status="success"))

        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli,
            [
                "scrape", "--input", str(input_csv), "--db", str(db_path),
                "--scrapingdog-key", "real-key-123", "--resume",
            ],
        )
        assert result.exit_code == 0
        assert "Tüm URL'ler işlendi" in result.output


# ---------------------------------------------------------------------------
# CLI: status command
# ---------------------------------------------------------------------------

class TestStatusCommand:
    def test_missing_db_exits_with_error(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli, ["status", "--db", str(tmp_path / "nope.db")]
        )
        assert result.exit_code == 1
        assert "Veritabanı bulunamadı" in result.output

    def test_existing_db_prints_stats_table(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))
        db.init_job_queue(["https://www.linkedin.com/in/x"])
        db.save_profile(LinkedInProfile(linkedin_url="https://www.linkedin.com/in/x", fetch_status="success"))

        runner = CliRunner()
        result = runner.invoke(main_mod.cli, ["status", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Scraping İlerlemesi" in result.output


# ---------------------------------------------------------------------------
# CLI: export command
# ---------------------------------------------------------------------------

class TestExportCommand:
    def test_missing_db_exits_with_error(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli, ["export", "--db", str(tmp_path / "nope.db")]
        )
        assert result.exit_code == 1
        assert "Veritabanı bulunamadı" in result.output

    def test_export_both_formats_writes_csv_and_jsonl(self, tmp_path):
        db_path = tmp_path / "test.db"
        url = "https://www.linkedin.com/in/exportme"
        db = Database(str(db_path))
        db.init_job_queue([url])
        db.save_profile(
            LinkedInProfile(
                linkedin_url=url, full_name="Export Me", fetch_status="success",
                experiences=[WorkExperience(company="Acme", title="Eng")],
            )
        )

        out_dir = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli,
            ["export", "--db", str(db_path), "--output-dir", str(out_dir), "--format", "both"],
        )
        assert result.exit_code == 0

        csv_path = out_dir / "export_success.csv"
        jsonl_path = out_dir / "export_success.jsonl"
        assert csv_path.exists()
        assert jsonl_path.exists()

        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["full_name"] == "Export Me"

        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["full_name"] == "Export Me"

    def test_export_filters_by_status(self, tmp_path):
        db_path = tmp_path / "test.db"
        urls = ["https://www.linkedin.com/in/ok", "https://www.linkedin.com/in/bad"]
        db = Database(str(db_path))
        db.init_job_queue(urls)
        db.save_profile(LinkedInProfile(linkedin_url=urls[0], full_name="OK", fetch_status="success"))
        db.save_profile(LinkedInProfile(linkedin_url=urls[1], fetch_status="failed", error_message="boom"))

        out_dir = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli,
            ["export", "--db", str(db_path), "--output-dir", str(out_dir),
             "--format", "csv", "--filter-status", "failed"],
        )
        assert result.exit_code == 0
        csv_path = out_dir / "export_failed.csv"
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["linkedin_url"] == urls[1]

    def test_export_json_only_does_not_write_csv(self, tmp_path):
        db_path = tmp_path / "test.db"
        url = "https://www.linkedin.com/in/jsononly"
        db = Database(str(db_path))
        db.init_job_queue([url])
        db.save_profile(LinkedInProfile(linkedin_url=url, fetch_status="success"))

        out_dir = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            main_mod.cli,
            ["export", "--db", str(db_path), "--output-dir", str(out_dir), "--format", "json"],
        )
        assert result.exit_code == 0
        assert (out_dir / "export_success.jsonl").exists()
        assert not (out_dir / "export_success.csv").exists()
