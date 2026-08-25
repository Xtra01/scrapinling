"""
LinkdAPI client - direct Proxycurl replacement (FALLBACK #2).

API Docs: https://linkdapi.com/docs
Endpoint: GET https://api.linkdapi.com/v1/linkedin/person/profile
Migration: https://linkdapi.com/blog/migrating-from-proxycurl-to-linkdapi

Key features:
  - Zero LinkedIn fake accounts - accesses LinkedIn's own mobile API
  - Explicit Proxycurl migration support
  - Official Python SDK: pip install linkdapi
  - 100 free trial credits; credits never expire
"""
import asyncio
import logging

import aiohttp
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from scraper.models import LinkedInProfile, WorkExperience, Education

logger = logging.getLogger(__name__)

LINKDAPI_BASE_URL = "https://api.linkdapi.com/v1/linkedin/person/profile"


class LinkdAPIRateLimitError(Exception):
    pass


class LinkdAPIAuthError(Exception):
    pass


class LinkdAPIClient:
    """Async LinkdAPI client - designed as a direct Proxycurl replacement."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self.api_key = api_key
        self.session = session
        self.semaphore = semaphore

    async def fetch_profile(self, linkedin_url: str) -> LinkedInProfile:
        async with self.semaphore:
            try:
                return await self._fetch_with_retry(linkedin_url)
            except LinkdAPIAuthError as e:
                logger.error("LinkdAPI auth error: %s", e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="linkdapi")
                profile.fetch_status = "failed"
                profile.error_message = f"AuthError: {e}"
                return profile
            except Exception as e:
                logger.warning("LinkdAPI error for %s: %s", linkedin_url, e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="linkdapi")
                profile.fetch_status = "failed"
                profile.error_message = str(e)
                return profile

    @retry(
        retry=retry_if_exception_type(LinkdAPIRateLimitError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=5, max=120),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_with_retry(self, linkedin_url: str) -> LinkedInProfile:
        params = {"url": linkedin_url}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        async with self.session.get(
            LINKDAPI_BASE_URL, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                data = await response.json()
                return self._parse_response(linkedin_url, data)

            elif response.status in (401, 403):
                raise LinkdAPIAuthError(f"HTTP {response.status}: Invalid API key or no credits")

            elif response.status == 404:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="linkdapi")
                profile.fetch_status = "not_found"
                profile.error_message = "Profile not found (404)"
                return profile

            elif response.status == 422:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="linkdapi")
                profile.fetch_status = "failed"
                profile.error_message = "Invalid LinkedIn URL (422)"
                return profile

            elif response.status == 429:
                try:
                    retry_after = int(response.headers.get("Retry-After", 30))
                except (TypeError, ValueError):
                    retry_after = 30
                logger.warning("LinkdAPI rate limited, waiting %ds...", retry_after)
                await asyncio.sleep(retry_after)
                raise LinkdAPIRateLimitError("Rate limited (429)")

            elif response.status >= 500:
                raise LinkdAPIRateLimitError(f"LinkdAPI server error {response.status}")

            else:
                error_text = await response.text()
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="linkdapi")
                profile.fetch_status = "failed"
                profile.error_message = f"HTTP {response.status}: {error_text[:200]}"
                return profile

    def _parse_response(self, linkedin_url: str, data: dict) -> LinkedInProfile:
        experiences = []
        for exp in (data.get("experiences") or []):
            starts = exp.get("starts_at") or {}
            ends = exp.get("ends_at") or {}
            we = WorkExperience(
                company=exp.get("company") or "",
                company_linkedin_url=exp.get("company_linkedin_profile_url") or "",
                title=exp.get("title") or "",
                description=exp.get("description") or "",
                location=exp.get("location") or "",
                starts_at_year=starts.get("year"),
                starts_at_month=starts.get("month"),
                starts_at_day=starts.get("day"),
                ends_at_year=ends.get("year"),
                ends_at_month=ends.get("month"),
                ends_at_day=ends.get("day"),
                is_current=not bool(ends),
            )
            experiences.append(we)

        education = []
        for edu in (data.get("education") or []):
            starts = edu.get("starts_at") or {}
            ends = edu.get("ends_at") or {}
            ed = Education(
                school=edu.get("school") or "",
                school_linkedin_url=edu.get("school_linkedin_profile_url") or "",
                degree=edu.get("degree_name") or "",
                field_of_study=edu.get("field_of_study") or "",
                description=edu.get("description") or "",
                grade=edu.get("grade") or "",
                activities=edu.get("activities_and_societies") or "",
                starts_at_year=starts.get("year"),
                ends_at_year=ends.get("year"),
            )
            education.append(ed)

        skills = []
        for s in (data.get("skills") or []):
            if isinstance(s, dict):
                skills.append(s.get("name") or "")
            elif isinstance(s, str):
                skills.append(s)

        languages = [
            lang.get("name", lang) if isinstance(lang, dict) else lang
            for lang in (data.get("languages") or [])
        ]

        return LinkedInProfile(
            linkedin_url=linkedin_url,
            full_name=data.get("full_name") or "",
            first_name=data.get("first_name") or "",
            last_name=data.get("last_name") or "",
            headline=data.get("headline") or "",
            summary=data.get("summary") or "",
            location=data.get("city") or data.get("location") or "",
            country=data.get("country_full_name") or data.get("country") or "",
            city=data.get("city") or "",
            profile_pic_url=data.get("profile_pic_url") or "",
            connections=data.get("connections"),
            follower_count=data.get("follower_count"),
            experiences=experiences,
            education=education,
            skills=[s for s in skills if s],
            languages=languages,
            source_api="linkdapi",
            fetch_status="success",
        )
