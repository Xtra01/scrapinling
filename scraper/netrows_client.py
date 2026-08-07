"""
Netrows LinkedIn API client - FALLBACK #1 (cheapest at scale).

API Docs: https://www.netrows.com/docs
Endpoint: GET https://api.netrows.com/v1/linkedin/person
Cost:     €0.005/request; Growth plan €299/mo = 100k credits
          15,000 profiles = €75 total
Rate limit: Contact vendor for high-volume details.

Returns: 48+ LinkedIn endpoints including full work history,
         education, skills, recommendations, posts, companies.
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

NETROWS_BASE_URL = "https://api.netrows.com/v1/linkedin/person"


class NetrowsRateLimitError(Exception):
    pass


class NetrowsAuthError(Exception):
    pass


class NetrowsClient:
    """Async Netrows LinkedIn API client."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self.api_key = api_key
        self.session = session
        self.semaphore = semaphore

    async def fetch_profile(self, linkedin_url: str) -> LinkedInProfile:
        async with self.semaphore:
            try:
                return await self._fetch_with_retry(linkedin_url)
            except NetrowsAuthError as e:
                logger.error("Netrows auth error: %s", e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="netrows")
                profile.fetch_status = "failed"
                profile.error_message = f"AuthError: {e}"
                return profile
            except Exception as e:
                logger.warning("Netrows error for %s: %s", linkedin_url, e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="netrows")
                profile.fetch_status = "failed"
                profile.error_message = str(e)
                return profile

    @retry(
        retry=retry_if_exception_type(NetrowsRateLimitError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=5, max=120),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_with_retry(self, linkedin_url: str) -> LinkedInProfile:
        params = {"url": linkedin_url}
        headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

        async with self.session.get(
            NETROWS_BASE_URL, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                data = await response.json()
                if not data or data.get("error"):
                    profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="netrows")
                    profile.fetch_status = "not_found"
                    profile.error_message = data.get("error", "No data") if data else "Empty response"
                    return profile
                return self._parse_response(linkedin_url, data)

            elif response.status in (401, 403):
                raise NetrowsAuthError(f"HTTP {response.status}: Invalid API key")

            elif response.status == 404:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="netrows")
                profile.fetch_status = "not_found"
                profile.error_message = "Profile not found (404)"
                return profile

            elif response.status == 429:
                retry_after = int(response.headers.get("Retry-After", 30))
                logger.warning("Netrows rate limited, waiting %ds...", retry_after)
                await asyncio.sleep(retry_after)
                raise NetrowsRateLimitError("Rate limited (429)")

            elif response.status == 402:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="netrows")
                profile.fetch_status = "failed"
                profile.error_message = "Netrows: No credits remaining (402)"
                return profile

            elif response.status >= 500:
                raise NetrowsRateLimitError(f"Netrows server error {response.status}")

            else:
                error_text = await response.text()
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="netrows")
                profile.fetch_status = "failed"
                profile.error_message = f"HTTP {response.status}: {error_text[:200]}"
                return profile

    def _parse_response(self, linkedin_url: str, data: dict) -> LinkedInProfile:
        experiences = []
        for exp in (data.get("experience") or data.get("positions") or []):
            starts_year = exp.get("start_year") or exp.get("startYear")
            starts_month = exp.get("start_month") or exp.get("startMonth")
            ends_year = exp.get("end_year") or exp.get("endYear")
            ends_month = exp.get("end_month") or exp.get("endMonth")
            is_current = exp.get("is_current") or exp.get("isCurrent") or not ends_year

            we = WorkExperience(
                company=exp.get("company_name") or exp.get("companyName") or exp.get("company") or "",
                company_linkedin_url=exp.get("company_linkedin_url") or "",
                title=exp.get("title") or exp.get("role") or "",
                description=exp.get("description") or "",
                location=exp.get("location") or "",
                starts_at_year=starts_year,
                starts_at_month=starts_month,
                ends_at_year=ends_year,
                ends_at_month=ends_month,
                is_current=bool(is_current),
            )
            experiences.append(we)

        education = []
        for edu in (data.get("education") or []):
            ed = Education(
                school=edu.get("school_name") or edu.get("schoolName") or edu.get("school") or "",
                school_linkedin_url=edu.get("school_linkedin_url") or "",
                degree=edu.get("degree") or edu.get("degree_name") or "",
                field_of_study=edu.get("field_of_study") or edu.get("fieldOfStudy") or edu.get("major") or "",
                description=edu.get("description") or "",
                grade=edu.get("grade") or "",
                activities=edu.get("activities") or "",
                starts_at_year=edu.get("start_year") or edu.get("startYear"),
                ends_at_year=edu.get("end_year") or edu.get("endYear"),
            )
            education.append(ed)

        skills = [
            s.get("name", s) if isinstance(s, dict) else s
            for s in (data.get("skills") or [])
        ]

        return LinkedInProfile(
            linkedin_url=linkedin_url,
            full_name=data.get("full_name") or data.get("fullName") or "",
            first_name=data.get("first_name") or data.get("firstName") or "",
            last_name=data.get("last_name") or data.get("lastName") or "",
            headline=data.get("headline") or data.get("occupation") or "",
            summary=data.get("summary") or data.get("about") or "",
            location=data.get("location") or "",
            country=data.get("country") or "",
            city=data.get("city") or "",
            profile_pic_url=data.get("profile_picture") or data.get("profilePicture") or "",
            connections=data.get("connections_count") or data.get("connectionsCount"),
            follower_count=data.get("follower_count") or data.get("followerCount"),
            experiences=experiences,
            education=education,
            skills=[s for s in skills if s],
            languages=[],
            source_api="netrows",
            fetch_status="success",
        )
