"""
Tests for utils/validators.py: normalize_linkedin_url, is_valid_linkedin_url,
and load_urls_from_file (CSV + TXT loading, dedup, normalization, filtering).
"""
import csv

import pytest

from utils.validators import (
    is_valid_linkedin_url,
    load_urls_from_file,
    normalize_linkedin_url,
)


# ---------------------------------------------------------------------------
# normalize_linkedin_url
# ---------------------------------------------------------------------------

class TestNormalizeLinkedinUrl:
    def test_http_to_https(self):
        assert (
            normalize_linkedin_url("http://www.linkedin.com/in/john-doe")
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_bare_domain_gets_www(self):
        assert (
            normalize_linkedin_url("https://linkedin.com/in/john-doe")
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_http_and_bare_domain_combined(self):
        # http:// -> https:// AND bare linkedin.com -> www.linkedin.com together
        assert (
            normalize_linkedin_url("http://linkedin.com/in/john-doe")
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_trailing_slash_removed(self):
        assert (
            normalize_linkedin_url("https://www.linkedin.com/in/john-doe/")
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_query_string_stripped(self):
        assert (
            normalize_linkedin_url("https://www.linkedin.com/in/john-doe?trk=public-profile")
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_fragment_stripped(self):
        assert (
            normalize_linkedin_url("https://www.linkedin.com/in/john-doe#experience")
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_query_and_fragment_both_stripped(self):
        assert (
            normalize_linkedin_url(
                "https://www.linkedin.com/in/john-doe?trk=abc#experience"
            )
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_whitespace_stripped(self):
        assert (
            normalize_linkedin_url("   https://www.linkedin.com/in/john-doe   ")
            == "https://www.linkedin.com/in/john-doe"
        )

    def test_mixed_case_input(self):
        # normalize_linkedin_url case-insensitively rewrites scheme+host to
        # a single canonical "https://www.linkedin.com/" prefix regardless of
        # input casing, so mixed-case duplicates collapse together for dedup.
        # Only the slug's own casing (path segment) is left untouched.
        result = normalize_linkedin_url("http://LinkedIn.COM/in/John-Doe/")
        assert result == "https://www.linkedin.com/in/John-Doe"
        assert is_valid_linkedin_url(result) is True

    def test_dedup_case_insensitive_scheme_and_host(self):
        # Two inputs differing only in scheme/host case now normalize to the
        # exact same canonical string (this used to defeat dedup).
        a = normalize_linkedin_url("https://www.linkedin.com/in/johndoe")
        b = normalize_linkedin_url("HTTPS://WWW.LINKEDIN.COM/in/johndoe")
        assert a == b == "https://www.linkedin.com/in/johndoe"

    def test_already_canonical_url_is_unchanged(self):
        url = "https://www.linkedin.com/in/john-doe"
        assert normalize_linkedin_url(url) == url

    def test_trailing_slash_before_query_string_is_stripped_consistently(self):
        # The query/fragment split now happens BEFORE the trailing-slash
        # strip, so a profile URL with a trailing slash before its query
        # string normalizes identically to the same URL without one.
        with_query = normalize_linkedin_url(
            "https://www.linkedin.com/in/john-doe/?trk=public-profile"
        )
        without_query = normalize_linkedin_url(
            "https://www.linkedin.com/in/john-doe?trk=public-profile"
        )
        assert with_query == without_query == "https://www.linkedin.com/in/john-doe"


# ---------------------------------------------------------------------------
# is_valid_linkedin_url
# ---------------------------------------------------------------------------

class TestIsValidLinkedinUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/in/john-doe",
            "https://www.linkedin.com/in/john-doe/",
            "http://www.linkedin.com/in/john-doe",
            "https://linkedin.com/in/john-doe",
            "https://www.linkedin.com/in/john-doe-123",
            "https://www.linkedin.com/in/john_doe",
            "https://www.linkedin.com/in/50%20cent",
        ],
    )
    def test_valid_in_urls_accepted(self, url):
        assert is_valid_linkedin_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/company/acme-corp",
            "https://www.linkedin.com/company/acme-corp/",
            "https://www.linkedin.com/school/some-university",
            "https://www.linkedin.com/jobs/view/12345",
        ],
    )
    def test_company_and_other_non_profile_urls_rejected(self, url):
        assert is_valid_linkedin_url(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not a url",
            "https://www.facebook.com/john-doe",
            "https://www.linkedin.com/in/",
            "ftp://www.linkedin.com/in/john-doe",
            "linkedin.com/in/john-doe",  # missing scheme entirely
            "just some garbage text",
        ],
    )
    def test_garbage_rejected(self, url):
        assert is_valid_linkedin_url(url) is False

    def test_trailing_garbage_after_valid_prefix_is_rejected(self):
        # is_valid_linkedin_url uses fullmatch, so a string that merely
        # *starts* with a valid /in/<slug> URL but has trailing junk is
        # correctly rejected instead of accepted.
        bogus = "https://www.linkedin.com/in/john-doe/this-should-not-be-here"
        assert is_valid_linkedin_url(bogus) is False
        bogus_with_space = "https://www.linkedin.com/in/john-doe garbage appended"
        assert is_valid_linkedin_url(bogus_with_space) is False


# ---------------------------------------------------------------------------
# load_urls_from_file - CSV
# ---------------------------------------------------------------------------

class TestLoadUrlsFromFileCsv:
    def test_csv_with_linkedin_url_column_dedups_and_filters_invalid(self, tmp_path):
        csv_path = tmp_path / "profiles.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "linkedin_url"])
            writer.writerow(["John Doe", "https://www.linkedin.com/in/john-doe"])
            # duplicate of the row above, different form (http, bare domain, trailing slash)
            writer.writerow(["John Doe (dup)", "http://linkedin.com/in/john-doe/"])
            writer.writerow(["Jane Smith", "https://www.linkedin.com/in/jane-smith/"])
            # invalid: a company URL, not a profile URL
            writer.writerow(["Acme Corp", "https://www.linkedin.com/company/acme-corp"])

        result = load_urls_from_file(str(csv_path))

        assert result == [
            "https://www.linkedin.com/in/john-doe",
            "https://www.linkedin.com/in/jane-smith",
        ]
        # dedup: only 2 unique valid urls out of 3 non-invalid rows
        assert len(result) == 2

    def test_csv_with_alternate_recognized_column_name(self, tmp_path):
        # "profile_url" is a recognized column name, distinct from "linkedin_url"
        csv_path = tmp_path / "profiles_alt_col.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "profile_url"])
            writer.writerow(["1", "https://www.linkedin.com/in/alice-w"])
            writer.writerow(["2", "https://www.linkedin.com/in/bob-j"])

        result = load_urls_from_file(str(csv_path))

        assert result == [
            "https://www.linkedin.com/in/alice-w",
            "https://www.linkedin.com/in/bob-j",
        ]

    def test_csv_with_unrecognized_column_falls_back_to_scanning_all_values(self, tmp_path):
        # No header matches the recognized set (linkedin_url, linkedin, url,
        # profile_url, link, profileurl, profile) -- loader must fall back to
        # scanning every cell in every row for a "linkedin.com/in/" substring.
        csv_path = tmp_path / "profiles_no_recognized_col.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["full_name", "website"])
            writer.writerow(["Carl King", "https://www.linkedin.com/in/carl-king"])
            writer.writerow(["No Link Here", "https://example.com/about"])

        result = load_urls_from_file(str(csv_path))

        assert result == ["https://www.linkedin.com/in/carl-king"]

    def test_csv_column_matching_is_case_insensitive(self, tmp_path):
        csv_path = tmp_path / "profiles_upper_header.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Name", "LinkedIn_URL"])
            writer.writerow(["Dana", "https://www.linkedin.com/in/dana-lee"])

        result = load_urls_from_file(str(csv_path))
        assert result == ["https://www.linkedin.com/in/dana-lee"]

    def test_csv_empty_cells_in_recognized_column_are_skipped(self, tmp_path):
        csv_path = tmp_path / "profiles_with_blanks.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "linkedin_url"])
            writer.writerow(["Has URL", "https://www.linkedin.com/in/has-url"])
            writer.writerow(["No URL", ""])

        result = load_urls_from_file(str(csv_path))
        assert result == ["https://www.linkedin.com/in/has-url"]


# ---------------------------------------------------------------------------
# load_urls_from_file - TXT
# ---------------------------------------------------------------------------

class TestLoadUrlsFromFileTxt:
    def test_txt_one_url_per_line_skips_comments_and_blanks(self, tmp_path):
        txt_path = tmp_path / "urls.txt"
        txt_path.write_text(
            "https://www.linkedin.com/in/alice-wong\n"
            "\n"
            "# this is a comment and should be skipped\n"
            "https://www.linkedin.com/in/bob-jones/\n",
            encoding="utf-8",
        )

        result = load_urls_from_file(str(txt_path))

        assert result == [
            "https://www.linkedin.com/in/alice-wong",
            "https://www.linkedin.com/in/bob-jones",
        ]

    def test_txt_dedups_and_filters_invalid_lines(self, tmp_path):
        txt_path = tmp_path / "urls_with_dupes.txt"
        txt_path.write_text(
            "https://www.linkedin.com/in/carl-king\n"
            "http://linkedin.com/in/carl-king/\n"  # duplicate, different form
            "not-a-linkedin-url\n"  # invalid, should be filtered
            "https://www.linkedin.com/company/acme\n"  # invalid: company url
            "https://www.linkedin.com/in/dana-lee\n",
            encoding="utf-8",
        )

        result = load_urls_from_file(str(txt_path))

        assert result == [
            "https://www.linkedin.com/in/carl-king",
            "https://www.linkedin.com/in/dana-lee",
        ]

    def test_txt_whitespace_only_lines_are_skipped(self, tmp_path):
        txt_path = tmp_path / "urls_whitespace.txt"
        txt_path.write_text(
            "https://www.linkedin.com/in/eve-adams\n"
            "   \n"
            "\t\n"
            "https://www.linkedin.com/in/frank-oz\n",
            encoding="utf-8",
        )

        result = load_urls_from_file(str(txt_path))

        assert result == [
            "https://www.linkedin.com/in/eve-adams",
            "https://www.linkedin.com/in/frank-oz",
        ]


# ---------------------------------------------------------------------------
# load_urls_from_file - misc / errors
# ---------------------------------------------------------------------------

class TestLoadUrlsFromFileMisc:
    def test_missing_file_raises_file_not_found_error(self, tmp_path):
        missing_path = tmp_path / "does_not_exist.csv"
        with pytest.raises(FileNotFoundError):
            load_urls_from_file(str(missing_path))

    def test_returns_empty_list_when_no_valid_urls(self, tmp_path):
        txt_path = tmp_path / "all_invalid.txt"
        txt_path.write_text(
            "not a url\nhttps://www.linkedin.com/company/acme\n",
            encoding="utf-8",
        )
        result = load_urls_from_file(str(txt_path))
        assert result == []
