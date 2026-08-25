"""
Unit tests for scraper/brightdata_client.py.

Covers three layers, all without real network I/O:

  1. Module-level pure helpers: _parse_iso_date, _parse_duration, _parse_int.
  2. BrightDataClient._parse_response and _make_error_results as pure methods
     (constructed with session=None, semaphore=None per the project's own
     testing convention - _parse_response never touches self.session/semaphore).
  3. BrightDataClient._trigger_and_collect / bulk_fetch as an integration path,
     with aiohttp's transport intercepted via aioresponses (no real HTTP) -
     specifically targeting the input-URL substring-matching logic called out
     as a risk area, plus the trigger/poll failure branches.

NOTE ON SCOPE: fetch_profile() / _fetch_single_with_retry() (the single-URL
sync "scrape" endpoint, used only for retries) are NOT covered here - the task
this file was written for scoped testing to bulk_fetch's pipeline and the pure
parsing helpers. That single-URL path shares _parse_response but has its own
HTTP status handling that would need its own aioresponses harness.
"""
from __future__ import annotations

import asyncio
import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scraper import brightdata_client as bd_mod
from scraper.brightdata_client import (
    BrightDataAuthError,
    BrightDataClient,
    _parse_duration,
    _parse_int,
    _parse_iso_date,
)


def _client(token: str = "test-token") -> BrightDataClient:
    """A BrightDataClient safe for pure-function testing: no real session/semaphore needed."""
    return BrightDataClient(api_token=token, session=None, semaphore=None)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _parse_iso_date
# ---------------------------------------------------------------------------

class TestParseIsoDate:
    def test_year_and_month(self):
        assert _parse_iso_date("2020-05") == (2020, 5)

    def test_year_only(self):
        assert _parse_iso_date("2020") == (2020, None)

    def test_none_input(self):
        assert _parse_iso_date(None) == (None, None)

    def test_empty_string_input(self):
        assert _parse_iso_date("") == (None, None)

    def test_full_iso_date_with_day_ignores_day(self):
        assert _parse_iso_date("2020-05-15") == (2020, 5)

    def test_non_digit_year_returns_none_year(self):
        assert _parse_iso_date("unknown") == (None, None)

    def test_non_digit_month_returns_none_month(self):
        assert _parse_iso_date("2020-XX") == (2020, None)


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------

class TestParseDuration:
    def test_realistic_duration_with_en_dash_and_middle_dot(self):
        # Uses the real Bright Data separator characters: U+2013 (en dash) and
        # U+00B7 (middle dot), exactly as shown in the module's own docstring.
        duration = "Jan 2020 – Mar 2023 · 3 yrs 2 mos"
        assert _parse_duration(duration) == (2020, 1, 2023, 3, False)

    def test_present_duration_marks_is_current_with_none_end(self):
        result = _parse_duration("2020 - Present")
        assert result == (2020, None, None, None, True)

    def test_malformed_string_with_no_dash_returns_all_none_tuple(self):
        # No "-" anywhere (before or after normalization) => len(parts) < 2
        # short-circuit. This documents actual behavior: no crash, just nulls.
        assert _parse_duration("Freelance Consultant") == (None, None, None, None, False)

    def test_empty_string_returns_all_none_tuple(self):
        assert _parse_duration("") == (None, None, None, None, False)

    def test_none_returns_all_none_tuple(self):
        assert _parse_duration(None) == (None, None, None, None, False)

    def test_plain_hyphen_variant(self):
        assert _parse_duration("Jan 2020 - Mar 2023") == (2020, 1, 2023, 3, False)

    def test_en_dash_variant_is_normalized(self):
        assert _parse_duration("Jan 2020 – Mar 2023") == (2020, 1, 2023, 3, False)

    def test_em_dash_variant_is_normalized(self):
        assert _parse_duration("Jan 2020 — Mar 2023") == (2020, 1, 2023, 3, False)

    def test_year_only_present_range(self):
        assert _parse_duration("2018 - 2022") == (2018, None, 2022, None, False)


# ---------------------------------------------------------------------------
# _parse_int
# ---------------------------------------------------------------------------

class TestParseInt:
    def test_comma_separated_string(self):
        assert _parse_int("1,234") == 1234

    def test_plus_suffix_string(self):
        assert _parse_int("500+") == 500

    def test_k_suffix_string(self):
        assert _parse_int("3K") == 3000

    def test_none_returns_none(self):
        assert _parse_int(None) is None

    def test_garbage_non_numeric_string_returns_none_not_raises(self):
        assert _parse_int("not-a-number") is None

    def test_empty_string_returns_none(self):
        assert _parse_int("") is None

    def test_plain_int_passthrough(self):
        assert _parse_int(500) == 500


# ---------------------------------------------------------------------------
# BrightDataClient._parse_response - experience[] branch logic
# ---------------------------------------------------------------------------

class TestParseResponseExperience:
    def test_start_date_present_uses_iso_path_and_ignores_duration_string(self):
        data = {
            "experience": [{
                "title": "Engineer",
                "company": "Acme",
                "start_date": "2020-01-15",
                "end_date": None,
                "duration": "Jan 1999 - Dec 1999",  # must be ignored: start_date wins
            }]
        }
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.starts_at_year == 2020
        assert exp.starts_at_month == 1
        assert exp.ends_at_year is None
        assert exp.ends_at_month is None
        assert exp.is_current is True  # no end_date => current

    def test_start_date_with_end_date_is_not_current(self):
        data = {
            "experience": [{
                "title": "Engineer",
                "company": "Acme",
                "start_date": "2016-06-01",
                "end_date": "2020-02-15",
            }]
        }
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.starts_at_year == 2016
        assert exp.starts_at_month == 6
        assert exp.ends_at_year == 2020
        assert exp.ends_at_month == 2
        assert exp.is_current is False

    def test_missing_start_date_falls_back_to_duration_string_path(self):
        data = {
            "experience": [{
                "title": "Engineer",
                "company": "OldCo",
                "duration": "Jun 2015 – Dec 2018 · 3 yrs 6 mos",
            }]
        }
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.starts_at_year == 2015
        assert exp.starts_at_month == 6
        assert exp.ends_at_year == 2018
        assert exp.ends_at_month == 12
        assert exp.is_current is False

    def test_empty_string_start_date_falls_back_to_duration_string_path(self):
        # exp.get("start_date") on "" is falsy, same as missing -> else branch.
        data = {
            "experience": [{
                "title": "Engineer",
                "company": "X",
                "start_date": "",
                "duration": "2020 - Present",
            }]
        }
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.starts_at_year == 2020
        assert exp.is_current is True

    def test_company_as_nested_dict(self):
        data = {"experience": [{"title": "Eng", "company": {"name": "Acme"}}]}
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.company == "Acme"

    def test_company_as_plain_string(self):
        data = {"experience": [{"title": "Eng", "company": "Acme"}]}
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.company == "Acme"

    def test_company_missing_defaults_to_empty_string(self):
        data = {"experience": [{"title": "Eng"}]}
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.company == ""

    def test_company_dict_missing_name_key_defaults_to_empty_string(self):
        data = {"experience": [{"title": "Eng", "company": {"linkedin_url": "x"}}]}
        exp = _client()._parse_response("url", data).experiences[0]
        assert exp.company == ""

    def test_missing_experience_key_yields_empty_list(self):
        profile = _client()._parse_response("url", {})
        assert profile.experiences == []

    def test_none_experience_value_does_not_crash(self):
        # data.get("experience") or [] handles an explicit None value cleanly.
        profile = _client()._parse_response("url", {"experience": None})
        assert profile.experiences == []


# ---------------------------------------------------------------------------
# BrightDataClient._parse_response - education / educations_details branch
# ---------------------------------------------------------------------------

class TestParseResponseEducation:
    def test_educations_details_preferred_over_education(self):
        data = {
            "educations_details": [{"school": "MIT"}],
            "education": [{"school": "Should Not Be Used"}],
        }
        education = _client()._parse_response("url", data).education
        assert len(education) == 1
        assert education[0].school == "MIT"

    def test_falls_back_to_education_when_educations_details_absent(self):
        data = {"education": [{"school": "Harvard"}]}
        education = _client()._parse_response("url", data).education
        assert education[0].school == "Harvard"

    def test_falls_back_to_education_when_educations_details_is_empty_list(self):
        # [] is falsy, so `data.get("educations_details") or data.get("education")`
        # skips straight past it to the education[] fallback.
        data = {"educations_details": [], "education": [{"school": "Harvard"}]}
        education = _client()._parse_response("url", data).education
        assert education[0].school == "Harvard"

    def test_string_entry_in_education_list_becomes_school_only(self):
        data = {"education": ["Just A School Name"]}
        education = _client()._parse_response("url", data).education
        assert education[0].school == "Just A School Name"
        assert education[0].degree == ""
        assert education[0].starts_at_year is None

    def test_fallback_keys_and_gpa_to_grade_string_conversion(self):
        data = {
            "education": [
                {
                    "school_name": "Yale",
                    "degree_name": "MBA",
                    "major": "Business",
                    "from_year": 2005,
                    "to_year": "2007",
                    "gpa": 3.8,
                    "activities_and_societies": "Debate club",
                },
            ],
        }
        edu = _client()._parse_response("url", data).education[0]
        assert edu.school == "Yale"
        assert edu.degree == "MBA"
        assert edu.field_of_study == "Business"
        assert edu.starts_at_year == 2005
        assert edu.ends_at_year == 2007
        assert edu.grade == "3.8"
        assert edu.activities == "Debate club"

    def test_start_year_and_end_year_string_digits_coerced_to_int(self):
        data = {"educations_details": [{"school": "MIT", "start_year": "2010", "end_year": "2014"}]}
        edu = _client()._parse_response("url", data).education[0]
        assert edu.starts_at_year == 2010
        assert edu.ends_at_year == 2014
        assert isinstance(edu.starts_at_year, int)
        assert isinstance(edu.ends_at_year, int)

    def test_non_digit_year_strings_do_not_crash_and_yield_none(self):
        data = {"educations_details": [{"school": "MIT", "start_year": "unknown", "end_year": "N/A"}]}
        edu = _client()._parse_response("url", data).education[0]
        assert edu.starts_at_year is None
        assert edu.ends_at_year is None

    def test_missing_education_and_educations_details_yields_empty_list(self):
        profile = _client()._parse_response("url", {})
        assert profile.education == []


# ---------------------------------------------------------------------------
# BrightDataClient._parse_response - skills[] and languages[]
# ---------------------------------------------------------------------------

class TestParseResponseSkills:
    def test_skills_as_list_of_plain_strings(self):
        data = {"skills": ["Python", "SQL"]}
        assert _client()._parse_response("url", data).skills == ["Python", "SQL"]

    def test_skills_as_list_of_dicts(self):
        data = {"skills": [{"name": "Python"}, {"name": "SQL"}]}
        assert _client()._parse_response("url", data).skills == ["Python", "SQL"]

    def test_skills_mixed_dicts_and_strings_with_falsy_entries_filtered(self):
        data = {"skills": [{"name": "Python"}, "SQL", {"name": None}, "", None]}
        assert _client()._parse_response("url", data).skills == ["Python", "SQL"]

    def test_missing_skills_yields_empty_list(self):
        assert _client()._parse_response("url", {}).skills == []


class TestParseResponseLanguages:
    def test_languages_mixed_shapes_normalized(self):
        data = {"languages": [{"name": "English"}, {"language": "French"}, "German"]}
        assert _client()._parse_response("url", data).languages == ["English", "French", "German"]

    def test_languages_falsy_entries_filtered(self):
        data = {"languages": [{"name": "English"}, "", None]}
        assert _client()._parse_response("url", data).languages == ["English"]

    def test_languages_dict_with_all_none_values_is_filtered_out(self):
        """
        Regression test: the languages fallback chain is
        `lang.get("name") or lang.get("language") or ""`. When a language
        dict has both keys missing/None, it now falls through to "" (not the
        raw dict itself), which the later `[lang for lang in languages if
        lang]` cleanup filter correctly drops - no raw dict object leaks into
        profile.languages.
        """
        data = {"languages": [{"name": None, "language": None}]}
        result = _client()._parse_response("url", data).languages
        assert result == []

    def test_languages_empty_dict_is_correctly_filtered_out(self):
        # Contrast case: {} is itself falsy (unlike {"name": None, ...}), so the
        # `or lang` fallback yields a falsy value and IS dropped by the filter.
        data = {"languages": [{}, "English"]}
        assert _client()._parse_response("url", data).languages == ["English"]

    def test_missing_languages_yields_empty_list(self):
        assert _client()._parse_response("url", {}).languages == []


# ---------------------------------------------------------------------------
# BrightDataClient._parse_response - top-level scalar fields
# ---------------------------------------------------------------------------

class TestParseResponseTopLevelFields:
    def test_full_name_split_into_first_and_last(self):
        profile = _client()._parse_response("url", {"name": "Jane Q Doe"})
        assert profile.full_name == "Jane Q Doe"
        assert profile.first_name == "Jane"
        assert profile.last_name == "Q Doe"

    def test_single_word_name_yields_empty_last_name(self):
        profile = _client()._parse_response("url", {"name": "Cher"})
        assert profile.first_name == "Cher"
        assert profile.last_name == ""

    def test_missing_name_defaults_all_name_fields_to_empty_string(self):
        profile = _client()._parse_response("url", {})
        assert profile.full_name == ""
        assert profile.first_name == ""
        assert profile.last_name == ""

    def test_city_preferred_over_location_for_location_field(self):
        data = {"city": "San Francisco", "location": "Bay Area"}
        assert _client()._parse_response("url", data).location == "San Francisco"

    def test_location_falls_back_when_city_absent(self):
        data = {"location": "Remote"}
        profile = _client()._parse_response("url", data)
        assert profile.location == "Remote"
        assert profile.city == ""

    def test_connections_as_plus_suffix_string(self):
        assert _client()._parse_response("url", {"connections": "500+"}).connections == 500

    def test_followers_as_comma_separated_string(self):
        assert _client()._parse_response("url", {"followers": "1,234"}).follower_count == 1234

    def test_connections_and_followers_missing_default_to_none(self):
        profile = _client()._parse_response("url", {})
        assert profile.connections is None
        assert profile.follower_count is None

    def test_headline_summary_avatar_country_mapped(self):
        data = {
            "position": "Senior Engineer",
            "about": "Bio text.",
            "avatar": "https://example.com/a.jpg",
            "country_code": "US",
        }
        profile = _client()._parse_response("url", data)
        assert profile.headline == "Senior Engineer"
        assert profile.summary == "Bio text."
        assert profile.profile_pic_url == "https://example.com/a.jpg"
        assert profile.country == "US"

    def test_linkedin_url_is_passed_through_verbatim(self):
        profile = _client()._parse_response("https://www.linkedin.com/in/janedoe", {})
        assert profile.linkedin_url == "https://www.linkedin.com/in/janedoe"

    def test_source_api_and_fetch_status_always_set(self):
        profile = _client()._parse_response("url", {})
        assert profile.source_api == "brightdata"
        assert profile.fetch_status == "success"

    def test_completely_empty_data_dict_does_not_crash(self):
        profile = _client()._parse_response("url", {})
        assert profile.fetch_status == "success"
        assert profile.experiences == []
        assert profile.education == []
        assert profile.skills == []
        assert profile.languages == []


# ---------------------------------------------------------------------------
# BrightDataClient._parse_response - full realistic combined profile
# ---------------------------------------------------------------------------

class TestParseResponseRealisticProfile:
    def test_full_realistic_response_exercises_both_experience_branches(self):
        data = {
            "name": "Jane Q Doe",
            "city": "San Francisco",
            "country_code": "US",
            "position": "Senior Software Engineer",
            "about": "Backend engineer with 10 years experience.",
            "avatar": "https://example.com/avatar.jpg",
            "followers": "1,200",
            "connections": "500+",
            "experience": [
                {
                    # Branch 1: ISO start_date/end_date path (current job).
                    "title": "Senior Software Engineer",
                    "company": {"name": "Acme Corp", "linkedin_url": "https://linkedin.com/company/acme"},
                    "company_linkedin_url": "https://linkedin.com/company/acme",
                    "location": "San Francisco, CA",
                    "duration": "Jan 2020 - Present",  # must be ignored: start_date wins
                    "start_date": "2020-01-15",
                    "end_date": None,
                    "description": "Leads the platform team.",
                },
                {
                    # Branch 2: duration-string fallback path (no start_date key).
                    "title": "Software Engineer",
                    "company": "OldCo",  # plain string, not a dict
                    "duration": "Jun 2015 – Dec 2018 · 3 yrs 6 mos",
                    "description": "",
                },
            ],
            "educations_details": [
                {
                    "school": "MIT",
                    "school_linkedin_url": "https://linkedin.com/school/mit",
                    "degree": "BSc",
                    "field_of_study": "Computer Science",
                    "start_year": "2010",
                    "end_year": 2014,
                    "grade": "3.9",
                }
            ],
            "education": [
                {"school": "Should not be used - educations_details takes priority"}
            ],
            "skills": [{"name": "Python"}, "SQL", {"name": None}, ""],
            "languages": [{"name": "English"}, {"language": "French"}, "German"],
        }

        profile = _client()._parse_response("https://linkedin.com/in/janedoe", data)

        assert profile.fetch_status == "success"
        assert profile.source_api == "brightdata"
        assert profile.full_name == "Jane Q Doe"
        assert profile.first_name == "Jane"
        assert profile.last_name == "Q Doe"
        assert profile.headline == "Senior Software Engineer"
        assert profile.city == "San Francisco"
        assert profile.country == "US"
        assert profile.location == "San Francisco"
        assert profile.connections == 500
        assert profile.follower_count == 1200

        assert len(profile.experiences) == 2
        exp0, exp1 = profile.experiences

        assert exp0.company == "Acme Corp"
        assert exp0.company_linkedin_url == "https://linkedin.com/company/acme"
        assert exp0.title == "Senior Software Engineer"
        assert exp0.location == "San Francisco, CA"
        assert exp0.description == "Leads the platform team."
        assert exp0.starts_at_year == 2020
        assert exp0.starts_at_month == 1
        assert exp0.ends_at_year is None
        assert exp0.is_current is True

        assert exp1.company == "OldCo"
        assert exp1.starts_at_year == 2015
        assert exp1.starts_at_month == 6
        assert exp1.ends_at_year == 2018
        assert exp1.ends_at_month == 12
        assert exp1.is_current is False

        assert len(profile.education) == 1
        edu = profile.education[0]
        assert edu.school == "MIT"
        assert edu.degree == "BSc"
        assert edu.field_of_study == "Computer Science"
        assert edu.starts_at_year == 2010
        assert edu.ends_at_year == 2014
        assert edu.grade == "3.9"

        assert profile.skills == ["Python", "SQL"]
        assert profile.languages == ["English", "French", "German"]


# ---------------------------------------------------------------------------
# BrightDataClient._make_error_results
# ---------------------------------------------------------------------------

class TestMakeErrorResults:
    def test_basic_error_results_for_each_url(self):
        client = _client()
        results = client._make_error_results(["url1", "url2"], "boom")

        assert set(results.keys()) == {"url1", "url2"}
        for url, profile in results.items():
            assert profile.linkedin_url == url
            assert profile.fetch_status == "failed"
            assert profile.error_message == "boom"
            assert profile.source_api == "brightdata"

    def test_empty_url_list_yields_empty_dict(self):
        assert _client()._make_error_results([], "boom") == {}

    def test_duplicate_urls_collapse_to_one_entry(self):
        results = _client()._make_error_results(["dup", "dup"], "x")
        assert len(results) == 1
        assert results["dup"].error_message == "x"


# ---------------------------------------------------------------------------
# BrightDataClient._trigger_and_collect / bulk_fetch - integration via
# aioresponses (intercepts aiohttp at the transport layer; no real network).
# ---------------------------------------------------------------------------

TRIGGER_RE = re.compile(r"^https://api\.brightdata\.com/datasets/v3/trigger")


def _progress_re(snapshot_id: str) -> re.Pattern:
    return re.compile(rf"^https://api\.brightdata\.com/datasets/v3/progress/{snapshot_id}")


def _snapshot_re(snapshot_id: str) -> re.Pattern:
    return re.compile(rf"^https://api\.brightdata\.com/datasets/v3/snapshot/{snapshot_id}")


class TestTriggerAndCollectIntegration:
    """
    These exercise real _trigger_and_collect / bulk_fetch code paths end to
    end, with aioresponses standing in for the network. POLL_INTERVAL_SEC is
    monkeypatched to 0 so the polling loop's asyncio.sleep() doesn't actually
    wait 30s per iteration.
    """

    def test_url_matching_prefix_urls_do_not_collide(self, monkeypatch):
        """
        Regression test for a fixed correctness bug: when one input URL is a
        literal prefix of another (e.g. .../in/john and .../in/johnsmith),
        matching must resolve each returned item to the exact URL it belongs
        to (via an exact-match-first strategy) rather than colliding both
        onto the shorter one via naive substring containment.
        """
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = [
                "https://www.linkedin.com/in/john",
                "https://www.linkedin.com/in/johnsmith",
            ]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="test-token", session=session, semaphore=asyncio.Semaphore(3),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={"snapshot_id": "snap123"}, status=200)
                    m.get(_progress_re("snap123"), payload={"status": "ready"}, status=200)
                    m.get(
                        _snapshot_re("snap123"),
                        payload=[
                            {
                                "input_url": "https://www.linkedin.com/in/johnsmith",
                                "name": "John Smith",
                            },
                        ],
                        status=200,
                    )
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())

        assert set(results.keys()) == {
            "https://www.linkedin.com/in/john",
            "https://www.linkedin.com/in/johnsmith",
        }

        # Exact match wins: johnsmith's data is correctly filed under its own URL.
        correctly_filed = results["https://www.linkedin.com/in/johnsmith"]
        assert correctly_filed.fetch_status == "success"
        assert correctly_filed.full_name == "John Smith"

        # john never appeared in the batch response, so it's reported not_found
        # instead of being silently overwritten with johnsmith's data.
        untouched = results["https://www.linkedin.com/in/john"]
        assert untouched.fetch_status == "not_found"

    def test_url_matching_exact_match_preferred_over_substring(self, monkeypatch):
        """
        When the API's input_url happens to exactly equal one of our
        submitted URLs, that exact match must be used even if a substring
        candidate also technically exists.
        """
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = [
                "https://www.linkedin.com/in/john",
                "https://www.linkedin.com/in/john-smith",
            ]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="test-token", session=session, semaphore=asyncio.Semaphore(3),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={"snapshot_id": "snap456"}, status=200)
                    m.get(_progress_re("snap456"), payload={"status": "ready"}, status=200)
                    m.get(
                        _snapshot_re("snap456"),
                        payload=[
                            {"input_url": "https://www.linkedin.com/in/john", "name": "John Doe"},
                            {"input_url": "https://www.linkedin.com/in/john-smith", "name": "John Smith"},
                        ],
                        status=200,
                    )
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())
        assert results["https://www.linkedin.com/in/john"].full_name == "John Doe"
        assert results["https://www.linkedin.com/in/john-smith"].full_name == "John Smith"

    def test_no_collision_when_urls_are_unrelated_substrings(self, monkeypatch):
        # Contrast/sanity case: unrelated URLs match correctly.
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/alice", "https://www.linkedin.com/in/bob"]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(3),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={"snapshot_id": "snapABC"}, status=200)
                    m.get(_progress_re("snapABC"), payload={"status": "ready"}, status=200)
                    m.get(
                        _snapshot_re("snapABC"),
                        payload=[
                            {"input_url": "https://www.linkedin.com/in/alice", "name": "Alice"},
                            {"input_url": "https://www.linkedin.com/in/bob", "name": "Bob"},
                        ],
                        status=200,
                    )
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())
        assert results["https://www.linkedin.com/in/alice"].full_name == "Alice"
        assert results["https://www.linkedin.com/in/bob"].full_name == "Bob"
        assert all(p.fetch_status == "success" for p in results.values())

    def test_item_level_error_field_becomes_not_found(self, monkeypatch):
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/private-profile"]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(1),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={"snapshot_id": "snap456"}, status=200)
                    m.get(_progress_re("snap456"), payload={"status": "ready"}, status=200)
                    m.get(
                        _snapshot_re("snap456"),
                        payload=[
                            {
                                "input_url": "https://www.linkedin.com/in/private-profile",
                                "error": "Profile is private",
                            },
                        ],
                        status=200,
                    )
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())
        profile = results["https://www.linkedin.com/in/private-profile"]
        assert profile.fetch_status == "not_found"
        assert profile.error_message == "Profile is private"

    def test_snapshot_response_as_single_dict_gets_wrapped_in_list(self, monkeypatch):
        # `if not isinstance(raw, list): raw = [raw]` - the batch endpoint can
        # apparently hand back one bare object instead of a one-item list.
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/solo"]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(1),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={"snapshot_id": "snapSOLO"}, status=200)
                    m.get(_progress_re("snapSOLO"), payload={"status": "ready"}, status=200)
                    m.get(
                        _snapshot_re("snapSOLO"),
                        payload={"input_url": "https://www.linkedin.com/in/solo", "name": "Solo Person"},
                        status=200,
                    )
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())
        profile = results["https://www.linkedin.com/in/solo"]
        assert profile.fetch_status == "success"
        assert profile.full_name == "Solo Person"

    def test_trigger_401_raises_auth_error(self, monkeypatch):
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/x"]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(1),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, status=401)
                    return await client._trigger_and_collect(urls)

        with pytest.raises(BrightDataAuthError):
            _run(scenario())

    def test_trigger_429_yields_failed_results_for_every_url_in_batch(self, monkeypatch):
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(1),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, status=429)
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())
        assert len(results) == 2
        for profile in results.values():
            assert profile.fetch_status == "failed"
            assert "Rate limited" in profile.error_message

    def test_trigger_response_missing_snapshot_id_yields_failed_results(self, monkeypatch):
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/a"]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(1),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={}, status=200)
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())
        profile = results["https://www.linkedin.com/in/a"]
        assert profile.fetch_status == "failed"
        assert "snapshot_id" in profile.error_message

    def test_progress_failed_status_short_circuits_before_download(self, monkeypatch):
        # No download mock is registered: if the code tried to poll /snapshot
        # after seeing status=="failed", aioresponses would raise for the
        # unmatched request and this test would fail - proving it does not.
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/a"]
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(1),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={"snapshot_id": "snap789"}, status=200)
                    m.get(_progress_re("snap789"), payload={"status": "failed"}, status=200)
                    return await client._trigger_and_collect(urls)

        results = _run(scenario())
        profile = results["https://www.linkedin.com/in/a"]
        assert profile.fetch_status == "failed"
        assert profile.error_message == "Batch failed"

    def test_bulk_fetch_happy_path_aggregates_single_batch_and_invokes_callback(self, monkeypatch):
        monkeypatch.setattr(bd_mod, "POLL_INTERVAL_SEC", 0)

        async def scenario():
            urls = ["https://www.linkedin.com/in/alice", "https://www.linkedin.com/in/bob"]
            collected = []
            async with aiohttp.ClientSession() as session:
                client = BrightDataClient(
                    api_token="t", session=session, semaphore=asyncio.Semaphore(1),
                )
                with aioresponses() as m:
                    m.post(TRIGGER_RE, payload={"snapshot_id": "snapXYZ"}, status=200)
                    m.get(_progress_re("snapXYZ"), payload={"status": "ready"}, status=200)
                    m.get(
                        _snapshot_re("snapXYZ"),
                        payload=[
                            {"input_url": "https://www.linkedin.com/in/alice", "name": "Alice"},
                            {"input_url": "https://www.linkedin.com/in/bob", "name": "Bob"},
                        ],
                        status=200,
                    )
                    results = await client.bulk_fetch(urls, on_result=collected.append)
            return results, collected

        results, collected = _run(scenario())
        assert len(results) == 2
        assert results["https://www.linkedin.com/in/alice"].full_name == "Alice"
        assert results["https://www.linkedin.com/in/bob"].full_name == "Bob"
        assert all(p.fetch_status == "success" for p in results.values())
        assert len(collected) == 2
        assert {p.full_name for p in collected} == {"Alice", "Bob"}
