"""
Unit tests for scraper/models.py: WorkExperience, Education, LinkedInProfile.

These tests exercise pure dataclass logic only (to_dict, to_json, to_flat_rows,
from_pdl) - no network, no mocking needed.
"""
from __future__ import annotations

import json

import pytest

from scraper.models import Education, LinkedInProfile, WorkExperience


# ---------------------------------------------------------------------------
# WorkExperience
# ---------------------------------------------------------------------------

class TestWorkExperienceStrings:
    def test_start_str_full_date(self):
        exp = WorkExperience(starts_at_year=2020, starts_at_month=6, starts_at_day=15)
        assert exp.start_str() == "2020-6-15"

    def test_start_str_year_only(self):
        exp = WorkExperience(starts_at_year=2020)
        assert exp.start_str() == "2020"

    def test_start_str_missing(self):
        exp = WorkExperience()
        assert exp.start_str() == ""

    def test_end_str_current_ignores_end_dates(self):
        # is_current=True must short-circuit to "Present" even if end dates are set.
        exp = WorkExperience(is_current=True, ends_at_year=2099, ends_at_month=1)
        assert exp.end_str() == "Present"

    def test_end_str_current_with_no_end_dates(self):
        exp = WorkExperience(is_current=True)
        assert exp.end_str() == "Present"

    def test_end_str_past_with_full_date(self):
        exp = WorkExperience(is_current=False, ends_at_year=2022, ends_at_month=3, ends_at_day=1)
        assert exp.end_str() == "2022-3-1"

    def test_end_str_past_missing(self):
        exp = WorkExperience(is_current=False)
        assert exp.end_str() == ""

    def test_start_str_skips_falsy_zero_components(self):
        # 0 is falsy, so a (hypothetical) month/day of 0 is dropped from the join,
        # same as None would be - documents the actual behavior of the `if p` filter.
        exp = WorkExperience(starts_at_year=2020, starts_at_month=0, starts_at_day=5)
        assert exp.start_str() == "2020-5"


class TestWorkExperienceToDict:
    def test_to_dict_contains_all_fields(self):
        exp = WorkExperience(
            company="Acme",
            company_linkedin_url="https://linkedin.com/company/acme",
            title="Engineer",
            description="Built things",
            location="Remote",
            starts_at_year=2019,
            starts_at_month=1,
            starts_at_day=1,
            ends_at_year=2021,
            ends_at_month=12,
            ends_at_day=31,
            is_current=False,
        )
        d = exp.to_dict()
        assert d == {
            "company": "Acme",
            "company_linkedin_url": "https://linkedin.com/company/acme",
            "title": "Engineer",
            "description": "Built things",
            "location": "Remote",
            "starts_at_year": 2019,
            "starts_at_month": 1,
            "starts_at_day": 1,
            "ends_at_year": 2021,
            "ends_at_month": 12,
            "ends_at_day": 31,
            "is_current": False,
        }

    def test_to_dict_defaults(self):
        d = WorkExperience().to_dict()
        assert d["company"] == ""
        assert d["starts_at_year"] is None
        assert d["is_current"] is False


class TestWorkExperienceFromPdl:
    def test_full_realistic_current_job(self):
        raw = {
            "company": {"name": "Acme Corp", "linkedin_url": "linkedin.com/company/acme"},
            "title": {"name": "Senior Engineer"},
            "location_names": ["San Francisco, California, United States"],
            "start_date": "2020-01-01",
            "end_date": None,
            "is_primary": True,
        }
        exp = WorkExperience.from_pdl(raw)
        assert exp.company == "Acme Corp"
        assert exp.company_linkedin_url == "linkedin.com/company/acme"
        assert exp.title == "Senior Engineer"
        assert exp.location == "San Francisco, California, United States"
        assert exp.starts_at_year == 2020
        assert exp.ends_at_year is None
        assert exp.is_current is True
        # from_pdl parses full YYYY-MM-DD precision, not just the year.
        assert exp.starts_at_month == 1
        assert exp.starts_at_day == 1
        assert exp.description == ""

    def test_full_realistic_past_job(self):
        raw = {
            "company": {"name": "Old Co", "linkedin_url": "linkedin.com/company/oldco"},
            "title": {"name": "Junior Dev"},
            "location_names": ["Austin, Texas, United States"],
            "start_date": "2015-06-01",
            "end_date": "2018-09-30",
            "is_primary": False,
        }
        exp = WorkExperience.from_pdl(raw)
        assert exp.starts_at_year == 2015
        assert exp.starts_at_month == 6
        assert exp.starts_at_day == 1
        assert exp.ends_at_year == 2018
        assert exp.ends_at_month == 9
        assert exp.ends_at_day == 30
        assert exp.is_current is False
        assert exp.end_str() == "2018-9-30"

    def test_missing_optional_keys_defaults_cleanly(self):
        # No start_date/end_date/is_primary/location_names keys at all.
        raw = {
            "company": {"name": "Acme"},
            "title": {"name": "Engineer"},
        }
        exp = WorkExperience.from_pdl(raw)
        assert exp.starts_at_year is None
        assert exp.ends_at_year is None
        assert exp.is_current is False
        assert exp.location == ""

    def test_missing_company_and_title_keys_default_to_empty_string(self):
        # company/title keys absent entirely (not None) - .get(key, {}) falls back
        # to {} and .get("name") on {} is None, coalesced to "".
        raw = {"start_date": "2020-01-01"}
        exp = WorkExperience.from_pdl(raw)
        assert exp.company == ""
        assert exp.title == ""

    def test_empty_location_names_list_yields_empty_string(self):
        raw = {"company": {"name": "Acme"}, "title": {"name": "Eng"}, "location_names": []}
        exp = WorkExperience.from_pdl(raw)
        assert exp.location == ""

    def test_non_digit_start_date_yields_none_year(self):
        raw = {"company": {"name": "Acme"}, "title": {"name": "Eng"}, "start_date": "unknown"}
        exp = WorkExperience.from_pdl(raw)
        assert exp.starts_at_year is None

    def test_company_explicit_none_degrades_to_empty_string(self):
        """
        Regression test: when the "company" key is *present* but its value is
        explicitly None (as PDL and similar APIs commonly emit for a field
        with no data, as opposed to omitting the key), from_pdl must degrade
        to "" rather than crashing with AttributeError. Same for "title".
        """
        raw = {"company": None, "title": {"name": "Eng"}}
        exp = WorkExperience.from_pdl(raw)
        assert exp.company == ""
        assert exp.company_linkedin_url == ""
        assert exp.title == "Eng"

    def test_title_explicit_none_degrades_to_empty_string(self):
        raw = {"company": {"name": "Acme"}, "title": None}
        exp = WorkExperience.from_pdl(raw)
        assert exp.company == "Acme"
        assert exp.title == ""


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------

class TestEducationToDict:
    def test_to_dict_contains_all_fields(self):
        edu = Education(
            school="MIT",
            school_linkedin_url="linkedin.com/school/mit",
            degree="BSc",
            field_of_study="Computer Science",
            description="",
            grade="3.9",
            activities="Chess club",
            starts_at_year=2010,
            ends_at_year=2014,
        )
        d = edu.to_dict()
        assert d == {
            "school": "MIT",
            "school_linkedin_url": "linkedin.com/school/mit",
            "degree": "BSc",
            "field_of_study": "Computer Science",
            "description": "",
            "grade": "3.9",
            "activities": "Chess club",
            "starts_at_year": 2010,
            "ends_at_year": 2014,
        }

    def test_to_dict_defaults(self):
        d = Education().to_dict()
        assert d["school"] == ""
        assert d["starts_at_year"] is None
        assert d["ends_at_year"] is None


class TestEducationFromPdl:
    def test_full_realistic_entry(self):
        raw = {
            "school": {"name": "MIT", "linkedin_url": "linkedin.com/school/mit"},
            "degrees": ["Bachelor's Degree"],
            "majors": ["Computer Science"],
            "gpa": "3.9",
            "start_date": "2010-09-01",
            "end_date": "2014-06-01",
        }
        edu = Education.from_pdl(raw)
        assert edu.school == "MIT"
        assert edu.school_linkedin_url == "linkedin.com/school/mit"
        assert edu.degree == "Bachelor's Degree"
        assert edu.field_of_study == "Computer Science"
        assert edu.grade == "3.9"
        assert edu.starts_at_year == 2010
        assert edu.ends_at_year == 2014

    def test_missing_degrees_key_defaults_to_empty_string(self):
        raw = {"school": {"name": "MIT"}}
        edu = Education.from_pdl(raw)
        assert edu.degree == ""
        assert edu.field_of_study == ""

    def test_empty_degrees_list_defaults_to_empty_string(self):
        # [] is falsy, so `edu.get("degrees") or [""]` substitutes [""].
        raw = {"school": {"name": "MIT"}, "degrees": [], "majors": []}
        edu = Education.from_pdl(raw)
        assert edu.degree == ""
        assert edu.field_of_study == ""

    def test_missing_school_key_defaults_to_empty_string(self):
        raw = {"degrees": ["BSc"]}
        edu = Education.from_pdl(raw)
        assert edu.school == ""
        assert edu.school_linkedin_url == ""

    def test_missing_gpa_defaults_to_empty_string(self):
        raw = {"school": {"name": "MIT"}}
        edu = Education.from_pdl(raw)
        assert edu.grade == ""

    def test_non_digit_dates_yield_none_years(self):
        raw = {"school": {"name": "MIT"}, "start_date": "", "end_date": None}
        edu = Education.from_pdl(raw)
        assert edu.starts_at_year is None
        assert edu.ends_at_year is None

    def test_school_explicit_none_degrades_to_empty_string(self):
        """
        Regression test: same as WorkExperience - when "school" is present
        with value None (rather than omitted), from_pdl must degrade to ""
        rather than crashing with AttributeError.
        """
        raw = {"school": None, "degrees": ["BSc"]}
        edu = Education.from_pdl(raw)
        assert edu.school == ""
        assert edu.school_linkedin_url == ""
        assert edu.degree == "BSc"


# ---------------------------------------------------------------------------
# LinkedInProfile
# ---------------------------------------------------------------------------

def _sample_profile() -> LinkedInProfile:
    return LinkedInProfile(
        linkedin_url="https://www.linkedin.com/in/janedoe",
        full_name="Jane Doe",
        first_name="Jane",
        last_name="Doe",
        headline="Software Engineer",
        summary="Builds things.",
        location="San Francisco, CA",
        country="United States",
        city="San Francisco",
        profile_pic_url="https://example.com/pic.jpg",
        connections=500,
        follower_count=1200,
        experiences=[
            WorkExperience(company="Acme", title="Engineer", starts_at_year=2020, is_current=True),
            WorkExperience(company="OldCo", title="Junior Dev", starts_at_year=2015, ends_at_year=2019),
        ],
        education=[
            Education(school="MIT", degree="BSc", starts_at_year=2010, ends_at_year=2014),
        ],
        skills=["Python", "SQL"],
        languages=["English", "French"],
        source_api="pdl",
        fetch_status="success",
        error_message="",
    )


class TestLinkedInProfileToJson:
    def test_round_trips_all_scalar_fields(self):
        profile = _sample_profile()
        parsed = json.loads(profile.to_json())

        assert parsed["linkedin_url"] == profile.linkedin_url
        assert parsed["full_name"] == profile.full_name
        assert parsed["first_name"] == profile.first_name
        assert parsed["last_name"] == profile.last_name
        assert parsed["headline"] == profile.headline
        assert parsed["summary"] == profile.summary
        assert parsed["location"] == profile.location
        assert parsed["country"] == profile.country
        assert parsed["city"] == profile.city
        assert parsed["profile_pic_url"] == profile.profile_pic_url
        assert parsed["connections"] == 500
        assert parsed["follower_count"] == 1200
        assert parsed["skills"] == ["Python", "SQL"]
        assert parsed["languages"] == ["English", "French"]
        assert parsed["source_api"] == "pdl"
        assert parsed["fetch_status"] == "success"
        assert parsed["error_message"] == ""

    def test_round_trips_nested_experiences_and_education(self):
        profile = _sample_profile()
        parsed = json.loads(profile.to_json())

        assert len(parsed["experiences"]) == 2
        assert parsed["experiences"] == [e.to_dict() for e in profile.experiences]
        assert parsed["experiences"][0]["company"] == "Acme"
        assert parsed["experiences"][0]["is_current"] is True

        assert len(parsed["education"]) == 1
        assert parsed["education"] == [e.to_dict() for e in profile.education]
        assert parsed["education"][0]["school"] == "MIT"

    def test_empty_profile_round_trip(self):
        profile = LinkedInProfile(linkedin_url="https://www.linkedin.com/in/nobody")
        parsed = json.loads(profile.to_json())
        assert parsed["experiences"] == []
        assert parsed["education"] == []
        assert parsed["skills"] == []
        assert parsed["languages"] == []
        assert parsed["connections"] is None
        assert parsed["fetch_status"] == "pending"

    def test_to_json_returns_valid_json_string(self):
        profile = _sample_profile()
        result = profile.to_json()
        assert isinstance(result, str)
        # Should not raise.
        json.loads(result)

    def test_to_json_preserves_non_ascii_characters(self):
        profile = LinkedInProfile(linkedin_url="x", full_name="José Ñandú")
        result = profile.to_json()
        # ensure_ascii=False means the literal characters appear, not \uXXXX escapes.
        assert "José Ñandú" in result
        assert json.loads(result)["full_name"] == "José Ñandú"


class TestLinkedInProfileToFlatRows:
    def test_empty_experiences_and_education_yields_single_base_row(self):
        profile = LinkedInProfile(
            linkedin_url="https://www.linkedin.com/in/empty",
            full_name="Empty Person",
            fetch_status="success",
        )
        rows = profile.to_flat_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["linkedin_url"] == "https://www.linkedin.com/in/empty"
        assert row["full_name"] == "Empty Person"
        assert row["fetch_status"] == "success"
        # record_type is always explicitly set ("profile" here) so the CSV
        # column never silently blanks out for zero-experience/education rows
        # (which includes every failed/not_found profile).
        assert row["record_type"] == "profile"
        assert row["exp_company"] == ""
        assert row["edu_school"] == ""

    def test_row_count_is_n_plus_m(self):
        profile = _sample_profile()  # 2 experiences, 1 education
        rows = profile.to_flat_rows()
        assert len(rows) == 3

    def test_experience_rows_have_correct_record_type_and_populated_fields(self):
        profile = _sample_profile()
        rows = profile.to_flat_rows()
        exp_rows = [r for r in rows if r["record_type"] == "experience"]
        assert len(exp_rows) == 2

        row0 = exp_rows[0]
        assert row0["exp_index"] == 1
        assert row0["exp_company"] == "Acme"
        assert row0["exp_title"] == "Engineer"
        assert row0["exp_start"] == "2020"
        assert row0["exp_end"] == "Present"
        assert row0["exp_is_current"] is True

        row1 = exp_rows[1]
        assert row1["exp_index"] == 2
        assert row1["exp_company"] == "OldCo"
        assert row1["exp_end"] == "2019"
        assert row1["exp_is_current"] is False

    def test_experience_rows_have_blanked_education_fields(self):
        profile = _sample_profile()
        rows = profile.to_flat_rows()
        exp_rows = [r for r in rows if r["record_type"] == "experience"]
        for row in exp_rows:
            assert row["edu_index"] == ""
            assert row["edu_school"] == ""
            assert row["edu_school_linkedin_url"] == ""
            assert row["edu_degree"] == ""
            assert row["edu_field_of_study"] == ""
            assert row["edu_grade"] == ""
            assert row["edu_start_year"] == ""
            assert row["edu_end_year"] == ""

    def test_education_rows_have_correct_record_type_and_populated_fields(self):
        profile = _sample_profile()
        rows = profile.to_flat_rows()
        edu_rows = [r for r in rows if r["record_type"] == "education"]
        assert len(edu_rows) == 1

        row = edu_rows[0]
        assert row["edu_index"] == 1
        assert row["edu_school"] == "MIT"
        assert row["edu_degree"] == "BSc"
        assert row["edu_start_year"] == 2010
        assert row["edu_end_year"] == 2014

    def test_education_rows_have_blanked_experience_fields(self):
        profile = _sample_profile()
        rows = profile.to_flat_rows()
        edu_rows = [r for r in rows if r["record_type"] == "education"]
        for row in edu_rows:
            assert row["exp_index"] == ""
            assert row["exp_company"] == ""
            assert row["exp_company_linkedin_url"] == ""
            assert row["exp_title"] == ""
            assert row["exp_description"] == ""
            assert row["exp_location"] == ""
            assert row["exp_start"] == ""
            assert row["exp_end"] == ""
            assert row["exp_is_current"] == ""

    def test_all_rows_share_the_same_base_fields(self):
        profile = _sample_profile()
        rows = profile.to_flat_rows()
        for row in rows:
            assert row["linkedin_url"] == profile.linkedin_url
            assert row["full_name"] == "Jane Doe"
            assert row["connections"] == 500
            assert row["source_api"] == "pdl"
            assert row["fetch_status"] == "success"

    def test_education_only_still_produces_rows_with_no_experience_rows(self):
        profile = LinkedInProfile(
            linkedin_url="x",
            education=[Education(school="MIT"), Education(school="Harvard")],
        )
        rows = profile.to_flat_rows()
        assert len(rows) == 2
        assert all(r["record_type"] == "education" for r in rows)
        assert [r["edu_index"] for r in rows] == [1, 2]

    def test_experience_only_zero_edu_still_produces_rows_with_no_education_rows(self):
        profile = LinkedInProfile(
            linkedin_url="x",
            experiences=[WorkExperience(company="Acme")],
        )
        rows = profile.to_flat_rows()
        assert len(rows) == 1
        assert rows[0]["record_type"] == "experience"

    def test_ends_at_year_of_zero_is_blanked_by_falsy_or(self):
        # `edu.starts_at_year or ""` means a real year of 0 would also blank out,
        # but 0 is not a realistic year value so this documents intended behavior
        # for the only falsy int case (None).
        profile = LinkedInProfile(
            linkedin_url="x",
            education=[Education(school="MIT", starts_at_year=None, ends_at_year=None)],
        )
        row = profile.to_flat_rows()[0]
        assert row["edu_start_year"] == ""
        assert row["edu_end_year"] == ""


class TestLinkedInProfileFromPdl:
    def _raw_pdl_response(self) -> dict:
        # Realistic nested PDL-shaped "person" payload, as consumed by
        # LinkedInProfile.from_pdl / WorkExperience.from_pdl / Education.from_pdl.
        return {
            "full_name": "Jane Doe",
            "first_name": "Jane",
            "last_name": "Doe",
            "job_title": "Senior Software Engineer",
            "summary": "Experienced backend engineer.",
            "location_name": "San Francisco, California, United States",
            "location_country": "United States",
            "location_locality": "San Francisco",
            "profile_pic_url": "https://example.com/pic.jpg",
            "skills": ["Python", "Distributed Systems"],
            "languages": [{"name": "English"}, {"name": "French"}, "German"],
            "experience": [
                {
                    "company": {"name": "Acme Corp", "linkedin_url": "linkedin.com/company/acme"},
                    "title": {"name": "Senior Software Engineer"},
                    "location_names": ["San Francisco, California, United States"],
                    "start_date": "2020-03-01",
                    "end_date": None,
                    "is_primary": True,
                },
                {
                    "company": {"name": "OldCo", "linkedin_url": "linkedin.com/company/oldco"},
                    "title": {"name": "Software Engineer"},
                    "location_names": ["Austin, Texas, United States"],
                    "start_date": "2016-06-01",
                    "end_date": "2020-02-15",
                    "is_primary": False,
                },
            ],
            "education": [
                {
                    "school": {"name": "MIT", "linkedin_url": "linkedin.com/school/mit"},
                    "degrees": ["Bachelor's Degree"],
                    "majors": ["Computer Science"],
                    "gpa": "3.9",
                    "start_date": "2012-09-01",
                    "end_date": "2016-06-01",
                }
            ],
        }

    def test_top_level_fields_wrapped_in_data_key(self):
        raw = {"data": self._raw_pdl_response()}
        profile = LinkedInProfile.from_pdl("https://www.linkedin.com/in/janedoe", raw)

        assert profile.linkedin_url == "https://www.linkedin.com/in/janedoe"
        assert profile.full_name == "Jane Doe"
        assert profile.first_name == "Jane"
        assert profile.last_name == "Doe"
        assert profile.headline == "Senior Software Engineer"
        assert profile.summary == "Experienced backend engineer."
        assert profile.location == "San Francisco, California, United States"
        assert profile.country == "United States"
        assert profile.city == "San Francisco"
        assert profile.profile_pic_url == "https://example.com/pic.jpg"
        assert profile.skills == ["Python", "Distributed Systems"]
        assert profile.source_api == "pdl"
        assert profile.fetch_status == "success"

    def test_top_level_fields_without_data_wrapper(self):
        # from_pdl falls back to using `data` directly when there is no "data" key.
        raw = self._raw_pdl_response()
        profile = LinkedInProfile.from_pdl("https://www.linkedin.com/in/janedoe", raw)
        assert profile.full_name == "Jane Doe"
        assert len(profile.experiences) == 2

    def test_nested_experiences_parsed_correctly(self):
        raw = {"data": self._raw_pdl_response()}
        profile = LinkedInProfile.from_pdl("url", raw)

        assert len(profile.experiences) == 2
        first, second = profile.experiences
        assert isinstance(first, WorkExperience)
        assert first.company == "Acme Corp"
        assert first.title == "Senior Software Engineer"
        assert first.is_current is True
        assert first.starts_at_year == 2020

        assert second.company == "OldCo"
        assert second.is_current is False
        assert second.starts_at_year == 2016
        assert second.ends_at_year == 2020

    def test_nested_education_parsed_correctly(self):
        raw = {"data": self._raw_pdl_response()}
        profile = LinkedInProfile.from_pdl("url", raw)

        assert len(profile.education) == 1
        edu = profile.education[0]
        assert isinstance(edu, Education)
        assert edu.school == "MIT"
        assert edu.degree == "Bachelor's Degree"
        assert edu.field_of_study == "Computer Science"
        assert edu.starts_at_year == 2012
        assert edu.ends_at_year == 2016

    def test_languages_mixed_dicts_and_strings_are_normalized(self):
        raw = {"data": self._raw_pdl_response()}
        profile = LinkedInProfile.from_pdl("url", raw)
        assert profile.languages == ["English", "French", "German"]

    def test_languages_with_falsy_entries_are_filtered(self):
        person = self._raw_pdl_response()
        person["languages"] = [{"name": "English"}, {"name": None}, "", None]
        raw = {"data": person}
        profile = LinkedInProfile.from_pdl("url", raw)
        assert profile.languages == ["English"]

    def test_missing_experience_and_education_keys_yield_empty_lists(self):
        person = {"full_name": "No History"}
        profile = LinkedInProfile.from_pdl("url", {"data": person})
        assert profile.experiences == []
        assert profile.education == []
        assert profile.skills == []
        assert profile.languages == []

    def test_missing_top_level_string_fields_default_to_empty_string(self):
        profile = LinkedInProfile.from_pdl("url", {"data": {}})
        assert profile.full_name == ""
        assert profile.first_name == ""
        assert profile.last_name == ""
        assert profile.headline == ""
        assert profile.summary == ""
        assert profile.location == ""
        assert profile.country == ""
        assert profile.city == ""
        assert profile.profile_pic_url == ""

    def test_fetch_status_and_source_api_always_set_on_success(self):
        profile = LinkedInProfile.from_pdl("url", {"data": {}})
        assert profile.fetch_status == "success"
        assert profile.source_api == "pdl"

    def test_round_trip_through_to_json_after_from_pdl(self):
        raw = {"data": self._raw_pdl_response()}
        profile = LinkedInProfile.from_pdl("https://www.linkedin.com/in/janedoe", raw)
        parsed = json.loads(profile.to_json())
        assert parsed["full_name"] == "Jane Doe"
        assert len(parsed["experiences"]) == 2
        assert len(parsed["education"]) == 1
        assert parsed["experiences"][0]["company"] == "Acme Corp"
        assert parsed["education"][0]["school"] == "MIT"

    def test_round_trip_through_to_flat_rows_after_from_pdl(self):
        raw = {"data": self._raw_pdl_response()}
        profile = LinkedInProfile.from_pdl("https://www.linkedin.com/in/janedoe", raw)
        rows = profile.to_flat_rows()
        # 2 experiences + 1 education = 3 rows.
        assert len(rows) == 3
        assert sum(1 for r in rows if r["record_type"] == "experience") == 2
        assert sum(1 for r in rows if r["record_type"] == "education") == 1
