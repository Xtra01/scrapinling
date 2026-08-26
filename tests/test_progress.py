"""
Tests for utils/progress.py: ScraperStats (pure logic, no Rich rendering
needed) and a smoke test that make_progress() builds a usable Progress object.
"""
from __future__ import annotations

import time

import pytest

from utils.progress import ScraperStats, make_progress


class TestScraperStatsUpdate:
    def test_update_success_increments_success(self):
        stats = ScraperStats()
        stats.update("success")
        assert stats.success == 1
        assert stats.failed == 0

    def test_update_failed_increments_failed(self):
        stats = ScraperStats()
        stats.update("failed")
        assert stats.failed == 1

    def test_update_not_found_increments_not_found(self):
        stats = ScraperStats()
        stats.update("not_found")
        assert stats.not_found == 1

    def test_update_rate_limited_increments_rate_limited(self):
        stats = ScraperStats()
        stats.update("rate_limited")
        assert stats.rate_limited == 1

    def test_update_unknown_status_is_a_silent_no_op(self):
        # "pending"/"in_progress" etc. aren't tracked as terminal outcomes -
        # update() should not raise or affect any counter.
        stats = ScraperStats()
        stats.update("pending")
        assert stats.success == stats.failed == stats.not_found == stats.rate_limited == 0

    def test_multiple_updates_accumulate_independently(self):
        stats = ScraperStats()
        for status in ["success", "success", "failed", "not_found", "success"]:
            stats.update(status)
        assert stats.success == 3
        assert stats.failed == 1
        assert stats.not_found == 1


class TestScraperStatsProcessedAndRate:
    def test_processed_sums_terminal_outcomes_only(self):
        stats = ScraperStats(total=10)
        stats.update("success")
        stats.update("success")
        stats.update("failed")
        stats.update("not_found")
        assert stats.processed == 4

    def test_rate_limited_does_not_count_toward_processed(self):
        # rate_limited is tracked separately and is not (yet) treated as a
        # terminal per-URL outcome for the processed/rate calculation.
        stats = ScraperStats()
        stats.update("rate_limited")
        assert stats.processed == 0

    def test_rate_is_zero_before_any_time_elapses_is_never_negative(self):
        stats = ScraperStats()
        stats.update("success")
        assert stats.rate >= 0

    def test_rate_reflects_profiles_per_minute(self):
        stats = ScraperStats()
        stats.start_time = time.time() - 60  # pretend a full minute has passed
        for _ in range(30):
            stats.update("success")
        # 30 profiles / 1 minute elapsed ~= 30/min
        assert 25 <= stats.rate <= 35

    def test_elapsed_is_non_negative_and_increases(self):
        stats = ScraperStats()
        first = stats.elapsed
        time.sleep(0.01)
        second = stats.elapsed
        assert first >= 0
        assert second >= first


class TestScraperStatsSummaryTable:
    def test_summary_table_does_not_crash_on_fresh_stats(self):
        stats = ScraperStats(total=5)
        table = stats.summary_table()
        assert table is not None

    def test_summary_table_does_not_crash_with_zero_processed(self):
        # Guards against a ZeroDivisionError in the success-rate calculation
        # when nothing has been processed yet.
        stats = ScraperStats(total=100)
        table = stats.summary_table()
        assert table is not None

    def test_summary_table_after_full_run(self):
        stats = ScraperStats(total=3)
        stats.update("success")
        stats.update("success")
        stats.update("failed")
        table = stats.summary_table()
        assert table is not None
        assert table.row_count > 0


class TestMakeProgress:
    def test_make_progress_returns_usable_progress_object(self):
        progress = make_progress()
        task_id = progress.add_task("Testing", total=10, rate=0.0)
        progress.update(task_id, advance=1, rate=5.0)
        # No exception means the custom rate field renders correctly.
        assert task_id is not None
