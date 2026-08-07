"""
RocketReach API client - B2B enrichment API (FALLBACK #5).

API Docs: https://rocketreach.co/api
Endpoint: GET https://api.rocketreach.co/v2/api/lookupProfile
Pricing:  Plans from $53/mo; lookup credits per profile

RocketReach looks up a person by LinkedIn URL and returns
job history, education, and contact information.
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

RR_BASE_URL = "https://api.rocketreach.co/v2/api/lookupProfile"


class RocketReachRateLimitError(Exception):
    pass


class RocketReachAuthError(Exception):
    pass


class RocketReachClient:
    """Async RocketReach API client."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self.api_key = api_key
        self.session = session
        self.semaphore = semaphore

    async def fetch_profile(self, linkedin_url: str) -> LinkedInProfile:
        async with self.semaphore:
            try:
                return await self._fetch_with_retry(linkedin_url)
            except RocketReachAuthError as e:
                logger.error("RocketReach auth error: %s", e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="rocketreach")
                profile.fetch_status = "failed"
                profile.error_message = f"AuthError: {e}"
                return profile
            except Exception as e:
                logger.warning("RocketReach error for %s: %s", linkedin_url, e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="rocketreach")
                profile.fetch_status = "failed"
                profile.error_message = str(e)
                return profile

    @retry(
        retry=retry_if_exception_type(RocketReachRateLimitError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=10, max=120),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_with_retry(self, linkedin_url: str) -> LinkedInProfile:
        params = {"li_url": linkedin_url}
        headers = {"Api-Key": self.api_key, "Content-Type": "application/json"}

        async with self.session.get(
            RR_BASE_URL, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                data = await response.json()
                return self._parse_response(linkedin_url, data)

            elif response.status in (401, 403):
                raise RocketReachAuthError(f"HTTP {response.status}: Invalid API key")

            elif response.status == 404:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="rocketreach")
                profile.fetch_status = "not_found"
                profile.error_message = "Profile not found (404)"
                return profile

            elif response.status == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning("RocketReach rate limited, waiting %ds...", retry_after)
                await asyncio.sleep(retry_after)
                raise RocketReachRateLimitError("Rate limited (429)")

            elif response.status >= 500:
                raise RocketReachRateLimitError(f"RocketReach server error {response.status}")

            else:
                error_text = await response.text()
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="rocketreach")
                profile.fetch_status = "failed"
                profile.error_message = f"HTTP {response.status}: {error_text[:200]}"
                return profile

    def _parse_response(self, linkedin_url: str, data: dict) -> LinkedInProfile:
        experiences = []
        employer_records = (
            ([data.get("current_employer")] if data.get("current_employer") else [])
            + (data.get("past_employers") or [])
        )
        for exp in employer_records:
            if not exp:
                continue
            start = str(exp.get("start") or "")
            end = str(exp.get("end") or "")
            we = WorkExperience(
                company=exp.get("employer") or "",
                title=exp.get("title") or "",
                starts_at_year=int(start[:4]) if start[:4].isdigit() else None,
                ends_at_year=int(end[:4]) if end[:4].isdigit() else None,
                is_current=bool(exp.get("current", False)),
            )
            experiences.append(we)

        education = []
        for edu in (data.get("education") or []):
            if not edu:
                continue
            ed = Education(
                school=edu.get("school") or "",
                degree=edu.get("degree") or "",
                field_of_study=edu.get("major") or "",
                starts_at_year=edu.get("start"),
                ends_at_year=edu.get("end"),
            )
            education.append(ed)

        return LinkedInProfile(
            linkedin_url=linkedin_url,
            full_name=data.get("name") or "",
            first_name=data.get("first_name") or "",
            last_name=data.get("last_name") or "",
            headline=data.get("current_title") or "",
            location=data.get("location") or "",
            country=data.get("country") or "",
            city=data.get("city") or "",
            profile_pic_url=data.get("profile_pic") or "",
            experiences=experiences,
            education=education,
            source_api="rocketreach",
            fetch_status="success",
        )
