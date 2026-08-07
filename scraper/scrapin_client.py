"""
ScrapIn API client - LinkedIn-specific data API (FALLBACK #4).

API Docs: https://scrapin.io/docs
Endpoint: GET https://api.scrapin.io/enrichment/profile
Pricing:  Builders ~$1,000/mo | Production ~$2,500/mo

ScrapIn is a dedicated LinkedIn enrichment API that returns
complete work history, education, skills, and more, in real time
(not from a static database).
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

SCRAPIN_BASE_URL = "https://api.scrapin.io/enrichment/profile"


class ScrapInRateLimitError(Exception):
    pass


class ScrapInAuthError(Exception):
    pass


class ScrapInClient:
    """Async ScrapIn API client for LinkedIn profile data."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self.api_key = api_key
        self.session = session
        self.semaphore = semaphore

    async def fetch_profile(self, linkedin_url: str) -> LinkedInProfile:
        async with self.semaphore:
            try:
                return await self._fetch_with_retry(linkedin_url)
            except ScrapInAuthError as e:
                logger.error("ScrapIn auth error: %s", e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapin")
                profile.fetch_status = "failed"
                profile.error_message = f"AuthError: {e}"
                return profile
            except Exception as e:
                logger.warning("ScrapIn unexpected error for %s: %s", linkedin_url, e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapin")
                profile.fetch_status = "failed"
                profile.error_message = str(e)
                return profile

    @retry(
        retry=retry_if_exception_type(ScrapInRateLimitError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=5, max=120),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_with_retry(self, linkedin_url: str) -> LinkedInProfile:
        params = {"linkedInUrl": linkedin_url, "apikey": self.api_key}

        async with self.session.get(
            SCRAPIN_BASE_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("success") is False:
                    profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapin")
                    profile.fetch_status = "not_found"
                    profile.error_message = data.get("message", "No data returned")
                    return profile
                return self._parse_response(linkedin_url, data)

            elif response.status in (401, 403):
                raise ScrapInAuthError(f"HTTP {response.status}: Invalid API key")

            elif response.status == 404:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapin")
                profile.fetch_status = "not_found"
                profile.error_message = "Profile not found (404)"
                return profile

            elif response.status == 429:
                retry_after = int(response.headers.get("Retry-After", 30))
                logger.warning("ScrapIn rate limited, waiting %ds...", retry_after)
                await asyncio.sleep(retry_after)
                raise ScrapInRateLimitError("Rate limited (429)")

            elif response.status == 402:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapin")
                profile.fetch_status = "failed"
                profile.error_message = "ScrapIn: Credits exhausted (402)"
                return profile

            elif response.status >= 500:
                raise ScrapInRateLimitError(f"ScrapIn server error {response.status}")

            else:
                error_text = await response.text()
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapin")
                profile.fetch_status = "failed"
                profile.error_message = f"HTTP {response.status}: {error_text[:200]}"
                return profile

    def _parse_response(self, linkedin_url: str, data: dict) -> LinkedInProfile:
        person = data.get("person") or data

        experiences = []
        for exp in (person.get("positions", {}).get("positionHistory", []) or []):
            starts = exp.get("startedOn") or {}
            ends = exp.get("finishedOn") or {}
            we = WorkExperience(
                company=exp.get("companyName") or "",
                company_linkedin_url=exp.get("linkedInUrl") or "",
                title=exp.get("title") or "",
                description=exp.get("description") or "",
                location=exp.get("location") or "",
                starts_at_year=starts.get("year"),
                starts_at_month=starts.get("month"),
                ends_at_year=ends.get("year"),
                ends_at_month=ends.get("month"),
                is_current=not bool(ends),
            )
            experiences.append(we)

        education = []
        for edu in (person.get("schools", {}).get("educationHistory", []) or []):
            starts = edu.get("startedOn") or {}
            ends = edu.get("finishedOn") or {}
            ed = Education(
                school=edu.get("schoolName") or "",
                school_linkedin_url=edu.get("linkedInUrl") or "",
                degree=edu.get("degreeName") or "",
                field_of_study=edu.get("fieldOfStudy") or "",
                description=edu.get("description") or "",
                grade=edu.get("grade") or "",
                activities=edu.get("activitiesAndSocieties") or "",
                starts_at_year=starts.get("year"),
                ends_at_year=ends.get("year"),
            )
            education.append(ed)

        skills = [s.get("name", s) if isinstance(s, dict) else s
                  for s in (person.get("skills") or [])]

        return LinkedInProfile(
            linkedin_url=linkedin_url,
            full_name=(person.get("firstName", "") + " " + person.get("lastName", "")).strip(),
            first_name=person.get("firstName") or "",
            last_name=person.get("lastName") or "",
            headline=person.get("headline") or "",
            summary=person.get("summary") or "",
            location=person.get("location") or "",
            country=person.get("country") or "",
            city=person.get("city") or "",
            profile_pic_url=person.get("photoUrl") or "",
            connections=person.get("connectionsCount"),
            follower_count=person.get("followersCount"),
            experiences=experiences,
            education=education,
            skills=skills,
            languages=[],
            source_api="scrapin",
            fetch_status="success",
        )
