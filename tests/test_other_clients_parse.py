"""
Unit tests for the pure `_parse_response(self, url, data)` method on the six
per-URL API clients (Scrapingdog, Netrows, LinkdAPI, ScrapIn, RocketReach),
plus the PDL client's parse path (LinkedInProfile.from_pdl, since PDLClient
has no _parse_response of its own - it calls the model classmethod directly).

_parse_response never touches self.session/self.semaphore, so every client
is constructed as ClientClass(api_key="test", session=None, semaphore=None)
and exercised with hand-built dicts shaped exactly like the keys each client
reads via .get() - no network, no mocking.

Where a test's docstring says BUG, the assertion locks in the *actual*
current behavior of the source (crash or logic quirk) rather than hiding it -
same convention already used in tests/test_models.py for the PDL from_pdl()
None-crash bug. These are reported, not silently avoided.
"""
from __future__ import annotations

import pytest

from scraper.models import LinkedInProfile
from scraper.linkdapi_client import LinkdAPIClient
from scraper.netrows_client import NetrowsClient
from scraper.pdl_client import PDLClient
from scraper.rocketreach_client import RocketReachClient
from scraper.scrapin_client import ScrapInClient
from scraper.scrapingdog_client import ScrapingdogClient, _parse_date

URL = "https://www.linkedin.com/in/testuser"


def _client(cls):
    """Every client's _parse_response is pure - session/semaphore are never touched."""
    return cls(api_key="test", session=None, semaphore=None)


# ===========================================================================
# Scrapingdog
# ===========================================================================

class TestScrapingdogParseDate:
    def test_month_year(self):
        assert _parse_date("Jan 2020") == (2020, 1)

    def test_full_month_name_year(self):
        assert _parse_date("January 2020") == (2020, 1)

    def test_year_only(self):
        assert _parse_date("2020") == (2020, None)

    def test_month_only_no_year(self):
        assert _parse_date("Dec") == (None, 12)

    def test_present_returns_none_none(self):
        assert _parse_date("Present") == (None, None)
        assert _parse_date("present") == (None, None)

    def test_none_input_returns_none_none(self):
        assert _parse_date(None) == (None, None)

    def test_empty_string_returns_none_none(self):
        assert _parse_date("") == (None, None)

    def test_unrecognized_format_returns_none_none(self):
        # Hyphenated ISO-ish dates aren't in scope per the function's docstring
        # ("Jan 2020", "2020", "January 2020") - documents, not a bug.
        assert _parse_date("2020-05") == (None, None)


class TestScrapingdogParseResponse:
    def test_rich_response(self):
        data = {
            "fullName": "Jane Doe",
            "firstName": "Jane",
            "lastName": "Doe",
            "headline": "Senior Engineer",
            "about": "Bio text",
            "location": "San Francisco, CA",
            "country": "United States",
            "city": "San Francisco",
            "profilePicture": "https://example.com/pic.jpg",
            "connectionsCount": 500,
            "followerCount": 1000,
            "experience": [
                {
                    "company_name": "Acme Corp",
                    "company_url": "https://linkedin.com/company/acme",
                    "title": "Senior Engineer",
                    "description": "Built things",
                    "location": "SF",
                    "duration_start": "Jan 2020",
                    "duration_end": "Present",
                },
                {
                    "company_name": "OldCo",
                    "title": "Engineer",
                    "duration_start": "Jun 2015",
                    "duration_end": "Dec 2019",
                },
            ],
            "education": [
                {
                    "school_name": "MIT",
                    "school_url": "https://linkedin.com/school/mit",
                    "degree": "BSc",
                    "field_of_study": "CS",
                    "description": "",
                    "grade": "3.9",
                    "activities": "Chess",
                    "start_year": "2010",
                    "end_year": "2014",
                }
            ],
            "skills": [{"name": "Python"}, "SQL", {"skill": "Go"}],
        }
        client = _client(ScrapingdogClient)
        profile = client._parse_response(URL, data)

        assert isinstance(profile, LinkedInProfile)
        assert profile.full_name == "Jane Doe"
        assert profile.first_name == "Jane"
        assert profile.last_name == "Doe"
        assert profile.headline == "Senior Engineer"
        assert profile.summary == "Bio text"
        assert profile.location == "San Francisco, CA"
        assert profile.country == "United States"
        assert profile.city == "San Francisco"
        assert profile.profile_pic_url == "https://example.com/pic.jpg"
        assert profile.connections == 500
        assert profile.follower_count == 1000
        assert profile.source_api == "scrapingdog"
        assert profile.fetch_status == "success"

        assert len(profile.experiences) == 2
        e0, e1 = profile.experiences
        assert e0.company == "Acme Corp"
        assert e0.company_linkedin_url == "https://linkedin.com/company/acme"
        assert e0.starts_at_year == 2020
        assert e0.starts_at_month == 1
        assert e0.ends_at_year is None  # "Present" parses to (None, None)
        assert e0.is_current is True

        assert e1.company == "OldCo"
        assert e1.starts_at_year == 2015
        assert e1.starts_at_month == 6
        assert e1.ends_at_year == 2019
        assert e1.ends_at_month == 12
        assert e1.is_current is False

        assert len(profile.education) == 1
        edu = profile.education[0]
        assert edu.school == "MIT"
        assert edu.field_of_study == "CS"
        assert edu.starts_at_year == 2010
        assert edu.ends_at_year == 2014

        assert profile.skills == ["Python", "SQL", "Go"]

    def test_sparse_response_no_crash_and_sensible_defaults(self):
        # Only firstName present - no fullName, no lastName, no experience/
        # education/skills keys at all.
        data = {"firstName": "Jane"}
        client = _client(ScrapingdogClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == "Jane"  # falls back to first+last, stripped
        assert profile.first_name == "Jane"
        assert profile.last_name == ""
        assert profile.headline == ""
        assert profile.summary == ""
        assert profile.location == ""
        assert profile.connections is None
        assert profile.follower_count is None
        assert profile.experiences == []
        assert profile.education == []
        assert profile.skills == []
        assert profile.fetch_status == "success"

    def test_experience_and_education_entries_missing_all_optional_keys(self):
        # Empty-dict entries must not raise (all fields go through `or ""`/None).
        data = {"experience": [{}], "education": [{}]}
        client = _client(ScrapingdogClient)
        profile = client._parse_response(URL, data)

        assert len(profile.experiences) == 1
        assert profile.experiences[0].company == ""
        assert profile.experiences[0].starts_at_year is None
        assert len(profile.education) == 1
        assert profile.education[0].school == ""

    def test_is_current_correctly_false_when_only_end_date_key_present(self):
        """
        Regression test: is_current's absence-check now inspects both
        "duration_end" and "end_date" before defaulting to current, so a
        response that uses "end_date" (not "duration_end") for a clearly
        finished role is no longer misreported as still current.
        """
        data = {
            "experience": [
                {"company_name": "OldCo", "title": "Engineer", "end_date": "Dec 2019"}
            ]
        }
        client = _client(ScrapingdogClient)
        profile = client._parse_response(URL, data)
        exp = profile.experiences[0]

        assert exp.ends_at_year == 2019
        assert exp.is_current is False


# ===========================================================================
# Netrows
# ===========================================================================

class TestNetrowsParseResponse:
    def test_rich_response(self):
        data = {
            "full_name": "John Smith",
            "first_name": "John",
            "last_name": "Smith",
            "headline": "Product Manager",
            "summary": "Bio",
            "location": "NYC",
            "country": "USA",
            "city": "New York",
            "profile_picture": "https://example.com/pic2.jpg",
            "connections_count": 800,
            "follower_count": 300,
            "experience": [
                {
                    "company_name": "BigCo",
                    "company_linkedin_url": "https://linkedin.com/company/bigco",
                    "title": "PM",
                    "description": "Led stuff",
                    "location": "NYC",
                    "start_year": 2018,
                    "start_month": 3,
                    "end_year": None,
                    "is_current": True,
                },
                {
                    # camelCase fallback keys, no snake_case equivalents present
                    "companyName": "SmallCo",
                    "role": "Associate PM",
                    "startYear": 2015,
                    "startMonth": 6,
                    "endYear": 2018,
                    "endMonth": 2,
                },
            ],
            "education": [
                {
                    "school_name": "NYU",
                    "degree": "MBA",
                    "field_of_study": "Business",
                    "start_year": 2013,
                    "end_year": 2015,
                }
            ],
            "skills": [{"name": "Leadership"}, "Strategy"],
        }
        client = _client(NetrowsClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == "John Smith"
        assert profile.headline == "Product Manager"
        assert profile.profile_pic_url == "https://example.com/pic2.jpg"
        assert profile.connections == 800
        assert profile.follower_count == 300
        assert profile.source_api == "netrows"
        assert profile.fetch_status == "success"

        assert len(profile.experiences) == 2
        e0, e1 = profile.experiences
        assert e0.company == "BigCo"
        assert e0.starts_at_year == 2018
        assert e0.starts_at_month == 3
        assert e0.is_current is True

        # camelCase-only entry must resolve via the fallback chain
        assert e1.company == "SmallCo"
        assert e1.title == "Associate PM"
        assert e1.starts_at_year == 2015
        assert e1.ends_at_year == 2018
        assert e1.is_current is False

        assert len(profile.education) == 1
        assert profile.education[0].school == "NYU"
        assert profile.education[0].starts_at_year == 2013
        assert profile.education[0].ends_at_year == 2015

        assert profile.skills == ["Leadership", "Strategy"]

    def test_sparse_response_no_crash_and_sensible_defaults(self):
        data = {}
        client = _client(NetrowsClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == ""
        assert profile.first_name == ""
        assert profile.headline == ""
        assert profile.connections is None
        assert profile.follower_count is None
        assert profile.experiences == []
        assert profile.education == []
        assert profile.skills == []
        assert profile.fetch_status == "success"

    def test_experience_falls_back_to_positions_key(self):
        # data.get("experience") or data.get("positions") or [] - exercise the
        # "positions" fallback branch when "experience" is absent entirely.
        data = {"positions": [{"company": "X", "title": "Y"}]}
        client = _client(NetrowsClient)
        profile = client._parse_response(URL, data)

        assert len(profile.experiences) == 1
        assert profile.experiences[0].company == "X"
        assert profile.experiences[0].title == "Y"
        # No end year anywhere -> not-ends_year fallback makes this current.
        assert profile.experiences[0].is_current is True

    def test_empty_dict_experience_and_education_entries_do_not_crash(self):
        data = {"experience": [{}], "education": [{}]}
        client = _client(NetrowsClient)
        profile = client._parse_response(URL, data)

        assert len(profile.experiences) == 1
        assert profile.experiences[0].company == ""
        assert len(profile.education) == 1
        assert profile.education[0].school == ""


# ===========================================================================
# LinkdAPI
# ===========================================================================

class TestLinkdAPIParseResponse:
    def test_rich_response(self):
        data = {
            "full_name": "Alice Wong",
            "first_name": "Alice",
            "last_name": "Wong",
            "headline": "CTO",
            "summary": "Executive bio",
            "city": "Seattle",
            "country_full_name": "United States",
            "profile_pic_url": "https://example.com/pic3.jpg",
            "connections": 1200,
            "follower_count": 5000,
            "experiences": [
                {
                    "company": "TechCo",
                    "company_linkedin_profile_url": "https://linkedin.com/company/techco",
                    "title": "CTO",
                    "description": "Runs engineering",
                    "location": "Seattle, WA",
                    "starts_at": {"year": 2019, "month": 4, "day": 1},
                    "ends_at": None,
                },
                {
                    "company": "StartUpCo",
                    "title": "VP Eng",
                    "starts_at": {"year": 2014, "month": 1, "day": 15},
                    "ends_at": {"year": 2019, "month": 3, "day": 31},
                },
            ],
            "education": [
                {
                    "school": "Stanford",
                    "school_linkedin_profile_url": "https://linkedin.com/school/stanford",
                    "degree_name": "MS Computer Science",
                    "field_of_study": "Computer Science",
                    "activities_and_societies": "ACM",
                    "starts_at": {"year": 2010},
                    "ends_at": {"year": 2012},
                }
            ],
            "skills": [{"name": "Leadership"}, "Go", {"skill": "Bad"}],
            "languages": [{"name": "English"}, "Spanish"],
        }
        client = _client(LinkdAPIClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == "Alice Wong"
        assert profile.location == "Seattle"  # data.get("city") takes priority
        assert profile.country == "United States"
        assert profile.city == "Seattle"
        assert profile.connections == 1200
        assert profile.follower_count == 5000
        assert profile.source_api == "linkdapi"
        assert profile.fetch_status == "success"

        assert len(profile.experiences) == 2
        e0, e1 = profile.experiences
        assert e0.company == "TechCo"
        assert e0.starts_at_year == 2019
        assert e0.starts_at_month == 4
        assert e0.starts_at_day == 1
        assert e0.ends_at_year is None
        assert e0.is_current is True  # ends_at was None -> {} -> falsy

        assert e1.company == "StartUpCo"
        assert e1.ends_at_year == 2019
        assert e1.ends_at_month == 3
        assert e1.ends_at_day == 31
        assert e1.is_current is False

        assert len(profile.education) == 1
        edu = profile.education[0]
        assert edu.school == "Stanford"
        assert edu.degree == "MS Computer Science"
        assert edu.field_of_study == "Computer Science"
        assert edu.activities == "ACM"
        assert edu.starts_at_year == 2010
        assert edu.ends_at_year == 2012

        # A skill dict without a "name" key degrades to "" and is filtered out
        # (unlike Netrows/ScrapIn's `s.get("name", s)` pattern - see below).
        assert profile.skills == ["Leadership", "Go"]
        assert profile.languages == ["English", "Spanish"]

    def test_sparse_response_no_crash_and_sensible_defaults(self):
        data = {}
        client = _client(LinkdAPIClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == ""
        assert profile.location == ""
        assert profile.experiences == []
        assert profile.education == []
        assert profile.skills == []
        assert profile.languages == []
        assert profile.fetch_status == "success"

    def test_empty_dict_experience_and_education_entries_do_not_crash(self):
        # starts_at/ends_at absent entirely - the `or {}` pattern (not a
        # default-arg .get()) protects against both missing keys AND explicit
        # None values here, unlike scrapin_client.py's positions/schools bug.
        data = {
            "experiences": [{"starts_at": None, "ends_at": None}],
            "education": [{"starts_at": None, "ends_at": None}],
        }
        client = _client(LinkdAPIClient)
        profile = client._parse_response(URL, data)

        assert len(profile.experiences) == 1
        assert profile.experiences[0].company == ""
        assert profile.experiences[0].starts_at_year is None
        assert profile.experiences[0].is_current is True
        assert len(profile.education) == 1
        assert profile.education[0].starts_at_year is None

    def test_skills_and_languages_junk_entries_do_not_crash(self):
        data = {"skills": [None, {}, "Real"], "languages": [None, {}]}
        client = _client(LinkdAPIClient)
        profile = client._parse_response(URL, data)
        assert profile.skills == ["Real"]


# ===========================================================================
# PDL - has no _parse_response of its own; delegates to LinkedInProfile.from_pdl.
# WorkExperience/Education.from_pdl edge cases are already covered exhaustively
# in tests/test_models.py; here we exercise the exact envelope shape
# pdl_client.py actually passes: the *full* JSON response dict (with a
# top-level "status" and "data" key), not just the inner person object.
# ===========================================================================

class TestPDLParsePath:
    def test_pdl_client_has_no_parse_response_and_delegates_to_from_pdl(self):
        # Confirms the documented architecture difference from the other five
        # clients: no _parse_response method exists on PDLClient at all.
        assert not hasattr(PDLClient, "_parse_response")

    def test_rich_response_via_full_envelope(self):
        # Shape exactly as pdl_client.py builds it: `data = await response.json()`
        # then `LinkedInProfile.from_pdl(linkedin_url, data)` where data still
        # has the top-level "status"/"data" wrapper.
        envelope = {
            "status": 200,
            "data": {
                "full_name": "Diana Prince",
                "first_name": "Diana",
                "last_name": "Prince",
                "job_title": "Director of Ops",
                "summary": "Ops leader",
                "location_name": "Washington, DC",
                "location_country": "United States",
                "location_locality": "Washington",
                "profile_pic_url": "https://example.com/pic6.jpg",
                "skills": ["Operations", "Logistics"],
                "languages": [{"name": "English"}, "French"],
                "experience": [
                    {
                        "company": {"name": "OpsCo", "linkedin_url": "linkedin.com/company/opsco"},
                        "title": {"name": "Director of Ops"},
                        "location_names": ["Washington, DC"],
                        "start_date": "2019-01-01",
                        "end_date": None,
                        "is_primary": True,
                    }
                ],
                "education": [
                    {
                        "school": {"name": "Georgetown", "linkedin_url": "linkedin.com/school/georgetown"},
                        "degrees": ["BA"],
                        "majors": ["Political Science"],
                        "start_date": "2010-09-01",
                        "end_date": "2014-06-01",
                    }
                ],
            },
        }

        # Constructed for parity with the other five clients even though the
        # parse path itself is a bare classmethod call, not an instance method.
        _client(PDLClient)
        profile = LinkedInProfile.from_pdl(URL, envelope)

        assert profile.full_name == "Diana Prince"
        assert profile.headline == "Director of Ops"
        assert profile.location == "Washington, DC"
        assert profile.city == "Washington"
        assert profile.skills == ["Operations", "Logistics"]
        assert profile.languages == ["English", "French"]
        assert profile.source_api == "pdl"
        assert profile.fetch_status == "success"

        assert len(profile.experiences) == 1
        assert profile.experiences[0].company == "OpsCo"
        assert profile.experiences[0].starts_at_year == 2019
        assert profile.experiences[0].is_current is True

        assert len(profile.education) == 1
        assert profile.education[0].school == "Georgetown"
        assert profile.education[0].degree == "BA"
        assert profile.education[0].starts_at_year == 2010
        assert profile.education[0].ends_at_year == 2014

    def test_sparse_response_no_crash_and_sensible_defaults(self):
        envelope = {"status": 200, "data": {"full_name": "Nobody Yet"}}
        profile = LinkedInProfile.from_pdl(URL, envelope)

        assert profile.full_name == "Nobody Yet"
        assert profile.headline == ""
        assert profile.experiences == []
        assert profile.education == []
        assert profile.skills == []
        assert profile.languages == []
        assert profile.fetch_status == "success"

    def test_company_explicit_none_degrades_to_empty_string(self):
        """
        Regression test - same fix exhaustively covered in
        tests/test_models.py::TestWorkExperienceFromPdl. Confirming it holds
        through the exact envelope shape pdl_client.py passes:
        WorkExperience.from_pdl must degrade to company="" rather than
        crashing when "company" is present with an explicit JSON null.
        """
        envelope = {
            "status": 200,
            "data": {"full_name": "X", "experience": [{"company": None, "title": {"name": "Eng"}}]},
        }
        profile = LinkedInProfile.from_pdl(URL, envelope)
        assert profile.experiences[0].company == ""
        assert profile.experiences[0].title == "Eng"


# ===========================================================================
# ScrapIn
# ===========================================================================

class TestScrapInParseResponse:
    def test_rich_response(self):
        data = {
            "person": {
                "firstName": "Bob",
                "lastName": "Lee",
                "headline": "Data Scientist",
                "summary": "ML bio",
                "location": "Boston, MA",
                "country": "USA",
                "city": "Boston",
                "photoUrl": "https://example.com/pic4.jpg",
                "connectionsCount": 400,
                "followersCount": 900,
                "positions": {
                    "positionHistory": [
                        {
                            "companyName": "DataCo",
                            "linkedInUrl": "https://linkedin.com/company/dataco",
                            "title": "Data Scientist",
                            "description": "Built models",
                            "location": "Boston, MA",
                            "startedOn": {"year": 2021, "month": 5},
                            "finishedOn": None,
                        },
                        {
                            "companyName": "OldDataCo",
                            "title": "Analyst",
                            "startedOn": {"year": 2018, "month": 1},
                            "finishedOn": {"year": 2021, "month": 4},
                        },
                    ]
                },
                "schools": {
                    "educationHistory": [
                        {
                            "schoolName": "Harvard",
                            "linkedInUrl": "https://linkedin.com/school/harvard",
                            "degreeName": "PhD Statistics",
                            "fieldOfStudy": "Statistics",
                            "startedOn": {"year": 2014},
                            "finishedOn": {"year": 2018},
                        }
                    ]
                },
                "skills": [{"name": "Machine Learning"}, "Python"],
            }
        }
        client = _client(ScrapInClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == "Bob Lee"
        assert profile.first_name == "Bob"
        assert profile.last_name == "Lee"
        assert profile.headline == "Data Scientist"
        assert profile.profile_pic_url == "https://example.com/pic4.jpg"
        assert profile.connections == 400
        assert profile.follower_count == 900
        assert profile.source_api == "scrapin"
        assert profile.fetch_status == "success"

        assert len(profile.experiences) == 2
        e0, e1 = profile.experiences
        assert e0.company == "DataCo"
        assert e0.starts_at_year == 2021
        assert e0.starts_at_month == 5
        assert e0.is_current is True  # finishedOn None -> {} -> falsy

        assert e1.company == "OldDataCo"
        assert e1.ends_at_year == 2021
        assert e1.ends_at_month == 4
        assert e1.is_current is False

        assert len(profile.education) == 1
        edu = profile.education[0]
        assert edu.school == "Harvard"
        assert edu.degree == "PhD Statistics"
        assert edu.field_of_study == "Statistics"
        assert edu.starts_at_year == 2014
        assert edu.ends_at_year == 2018

        assert profile.skills == ["Machine Learning", "Python"]

    def test_sparse_response_no_crash_when_keys_absent(self):
        # "positions"/"schools"/"skills" keys absent entirely (not None) -
        # the .get(key, {}) default correctly kicks in when the key is
        # missing; the crash only happens when the key is explicitly None
        # (see the BUG test below).
        data = {"person": {}}
        client = _client(ScrapInClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == ""
        assert profile.experiences == []
        assert profile.education == []
        assert profile.skills == []
        assert profile.fetch_status == "success"

    def test_person_key_absent_falls_back_to_top_level_data(self):
        # `person = data.get("person") or data` - exercise the fallback when
        # the response has no "person" wrapper at all.
        data = {"firstName": "Top", "lastName": "Level"}
        client = _client(ScrapInClient)
        profile = client._parse_response(URL, data)
        assert profile.full_name == "Top Level"

    def test_positions_explicit_none_degrades_to_empty_experiences(self):
        """
        Regression test. scrapin_client.py now does:

            positions = person.get("positions") or {}
            for exp in (positions.get("positionHistory") or []): ...

        so an explicit "positions": null (a plausible way for ScrapIn to
        represent "no position data") degrades to an empty experience list
        instead of crashing with AttributeError. Same for "schools".
        """
        data = {"person": {"firstName": "X", "positions": None, "schools": {"educationHistory": []}}}
        client = _client(ScrapInClient)
        profile = client._parse_response(URL, data)
        assert profile.experiences == []

    def test_schools_explicit_none_degrades_to_empty_education(self):
        data = {"person": {"firstName": "X", "positions": {"positionHistory": []}, "schools": None}}
        client = _client(ScrapInClient)
        profile = client._parse_response(URL, data)
        assert profile.education == []

    def test_first_name_explicit_none_degrades_to_empty_name(self):
        """
        Regression test. scrapin_client.py now builds full_name as:

            ((person.get("firstName") or "") + " " + (person.get("lastName") or "")).strip()

        so an explicit JSON null for "firstName"/"lastName" degrades to ""
        instead of raising TypeError on `None + " "`.
        """
        data = {
            "person": {
                "firstName": None,
                "lastName": "Lee",
                "positions": {"positionHistory": []},
                "schools": {"educationHistory": []},
            }
        }
        client = _client(ScrapInClient)
        profile = client._parse_response(URL, data)
        assert profile.full_name == "Lee"


# ===========================================================================
# RocketReach
# ===========================================================================

class TestRocketReachParseResponse:
    def test_rich_response(self):
        data = {
            "name": "Carol King",
            "first_name": "Carol",
            "last_name": "King",
            "current_title": "VP Sales",
            "location": "Chicago, IL",
            "country": "USA",
            "city": "Chicago",
            "profile_pic": "https://example.com/pic5.jpg",
            "current_employer": {
                "employer": "SalesCo",
                "title": "VP Sales",
                "start": "2020-01",
                "end": None,
                "current": True,
            },
            "past_employers": [
                {
                    "employer": "OldSalesCo",
                    "title": "Sales Manager",
                    "start": "2015-06",
                    "end": "2019-12",
                    "current": False,
                },
                None,  # a stray null entry must not crash the loop
            ],
            "education": [
                {"school": "Northwestern", "degree": "MBA", "major": "Marketing", "start": 2011, "end": 2013},
                None,
            ],
        }
        client = _client(RocketReachClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == "Carol King"  # from "name", not "full_name"
        assert profile.headline == "VP Sales"  # from "current_title"
        assert profile.location == "Chicago, IL"
        assert profile.profile_pic_url == "https://example.com/pic5.jpg"
        assert profile.source_api == "rocketreach"
        assert profile.fetch_status == "success"
        # RocketReach's parser never populates these - defaults are expected.
        assert profile.summary == ""
        assert profile.connections is None
        assert profile.skills == []

        assert len(profile.experiences) == 2  # None entry skipped
        e0, e1 = profile.experiences
        assert e0.company == "SalesCo"
        assert e0.starts_at_year == 2020
        assert e0.ends_at_year is None
        assert e0.is_current is True

        assert e1.company == "OldSalesCo"
        assert e1.starts_at_year == 2015
        assert e1.ends_at_year == 2019
        assert e1.is_current is False

        assert len(profile.education) == 1  # None entry skipped
        edu = profile.education[0]
        assert edu.school == "Northwestern"
        assert edu.degree == "MBA"
        assert edu.field_of_study == "Marketing"
        assert edu.starts_at_year == 2011
        assert edu.ends_at_year == 2013

    def test_sparse_response_no_crash_and_sensible_defaults(self):
        data = {}
        client = _client(RocketReachClient)
        profile = client._parse_response(URL, data)

        assert profile.full_name == ""
        assert profile.headline == ""
        assert profile.experiences == []
        assert profile.education == []
        assert profile.fetch_status == "success"

    def test_current_employer_explicit_none_is_skipped_not_crashed(self):
        data = {"current_employer": None, "past_employers": [{"employer": "OnlyPast", "start": "2020"}]}
        client = _client(RocketReachClient)
        profile = client._parse_response(URL, data)
        assert len(profile.experiences) == 1
        assert profile.experiences[0].company == "OnlyPast"

    def test_non_digit_start_end_strings_yield_none_years(self):
        data = {"current_employer": {"employer": "X", "start": "Unknown", "end": ""}}
        client = _client(RocketReachClient)
        profile = client._parse_response(URL, data)
        assert profile.experiences[0].starts_at_year is None
        assert profile.experiences[0].ends_at_year is None

    def test_education_years_are_coerced_to_int_like_experience(self):
        """
        Regression test: education start/end years now go through the same
        `int(s[:4]) if s[:4].isdigit() else None` coercion as experience
        years, so Education.starts_at_year/ends_at_year are consistently
        Optional[int] rather than leaking a raw str.
        """
        data = {"education": [{"school": "X", "start": "2011", "end": "2013"}]}
        client = _client(RocketReachClient)
        profile = client._parse_response(URL, data)
        edu = profile.education[0]
        assert edu.starts_at_year == 2011
        assert isinstance(edu.starts_at_year, int)
        assert edu.ends_at_year == 2013

    def test_education_years_as_int_input_still_coerced(self):
        data = {"education": [{"school": "X", "start": 2011, "end": 2013}]}
        client = _client(RocketReachClient)
        profile = client._parse_response(URL, data)
        edu = profile.education[0]
        assert edu.starts_at_year == 2011
        assert edu.ends_at_year == 2013
