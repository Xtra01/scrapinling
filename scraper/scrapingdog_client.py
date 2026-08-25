"""
Scrapingdog LinkedIn Scraper API - PRIMARY option (lowest cost).

API Docs: https://www.scrapingdog.com/linkedin-scraper-api
Endpoint: GET https://api.scrapingdog.com/linkedin
Cost:     ~$0.009/profile at Enterprise; Pro plan $200/mo = 3M credits
          (15,000 profiles × 50 credits = 750,000 credits)
Rate limit: Plan-based (no hard internal limit; LinkedIn anti-bot is the real constraint)

Returns: name, headline, work experience (company, title, dates), education,
         skills, location, connections count, profile picture.
"""
import asyncio
import logging
from typing import Optional

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

SCRAPINGDOG_URL = "https://api.scrapingdog.com/linkedin"


class ScrapingdogRateLimitError(Exception):
    pass


class ScrapingdogAuthError(Exception):
    pass


class ScrapingdogClient:
    """Async Scrapingdog LinkedIn API client."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self.api_key = api_key
        self.session = session
        self.semaphore = semaphore

    async def fetch_profile(self, linkedin_url: str) -> LinkedInProfile:
        async with self.semaphore:
            try:
                return await self._fetch_with_retry(linkedin_url)
            except ScrapingdogAuthError as e:
                logger.error("Scrapingdog auth error: %s", e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapingdog")
                profile.fetch_status = "failed"
                profile.error_message = f"AuthError: {e}"
                return profile
            except Exception as e:
                logger.warning("Scrapingdog error for %s: %s", linkedin_url, e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapingdog")
                profile.fetch_status = "failed"
                profile.error_message = str(e)
                return profile

    @retry(
        retry=retry_if_exception_type(ScrapingdogRateLimitError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=5, max=120),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_with_retry(self, linkedin_url: str) -> LinkedInProfile:
        params = {
            "api_key": self.api_key,
            "type": "profile",
            "linkId": linkedin_url,
        }

        async with self.session.get(
            SCRAPINGDOG_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status == 200:
                data = await response.json()
                if isinstance(data, list) and data:
                    return self._parse_response(linkedin_url, data[0])
                elif isinstance(data, dict):
                    return self._parse_response(linkedin_url, data)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapingdog")
                profile.fetch_status = "not_found"
                profile.error_message = "Empty response"
                return profile

            elif response.status in (401, 403):
                raise ScrapingdogAuthError(f"HTTP {response.status}: Invalid API key or out of credits")

            elif response.status == 404:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapingdog")
                profile.fetch_status = "not_found"
                profile.error_message = "Profile not found (404)"
                return profile

            elif response.status == 429:
                try:
                    retry_after = int(response.headers.get("Retry-After", 30))
                except (TypeError, ValueError):
                    retry_after = 30
                logger.warning("Scrapingdog rate limited, waiting %ds...", retry_after)
                await asyncio.sleep(retry_after)
                raise ScrapingdogRateLimitError("Rate limited (429)")

            elif response.status >= 500:
                raise ScrapingdogRateLimitError(f"Scrapingdog server error {response.status}")

            else:
                error_text = await response.text()
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="scrapingdog")
                profile.fetch_status = "failed"
                profile.error_message = f"HTTP {response.status}: {error_text[:200]}"
                return profile

    def _parse_response(self, linkedin_url: str, data: dict) -> LinkedInProfile:
        experiences = []
        for exp in (data.get("experience") or []):
            starts_year, starts_month = _parse_date(exp.get("duration_start") or exp.get("start_date"))
            ends_year, ends_month = _parse_date(exp.get("duration_end") or exp.get("end_date"))
            is_current = (
                "present" in str(exp.get("duration_end", "")).lower()
                or "present" in str(exp.get("end_date", "")).lower()
                or (not exp.get("duration_end") and not exp.get("end_date"))
            )
            company = exp.get("company_name") or exp.get("company") or ""
            we = WorkExperience(
                company=company,
                company_linkedin_url=exp.get("company_url") or "",
                title=exp.get("title") or "",
                description=exp.get("description") or "",
                location=exp.get("location") or "",
                starts_at_year=starts_year,
                starts_at_month=starts_month,
                ends_at_year=ends_year,
                ends_at_month=ends_month,
                is_current=is_current,
            )
            experiences.append(we)

        education = []
        for edu in (data.get("education") or []):
            starts_year, _ = _parse_date(edu.get("start_year") or edu.get("start_date"))
            ends_year, _ = _parse_date(edu.get("end_year") or edu.get("end_date"))
            ed = Education(
                school=edu.get("school_name") or edu.get("school") or "",
                school_linkedin_url=edu.get("school_url") or "",
                degree=edu.get("degree") or "",
                field_of_study=edu.get("field_of_study") or edu.get("major") or "",
                description=edu.get("description") or "",
                grade=edu.get("grade") or "",
                activities=edu.get("activities") or "",
                starts_at_year=starts_year,
                ends_at_year=ends_year,
            )
            education.append(ed)

        skills = []
        for s in (data.get("skills") or []):
            if isinstance(s, dict):
                skills.append(s.get("name") or s.get("skill") or "")
            elif isinstance(s, str):
                skills.append(s)

        full_name = data.get("fullName") or (
            (data.get("firstName") or "") + " " + (data.get("lastName") or "")
        ).strip()

        return LinkedInProfile(
            linkedin_url=linkedin_url,
            full_name=full_name,
            first_name=data.get("firstName") or "",
            last_name=data.get("lastName") or "",
            headline=data.get("headline") or data.get("occupation") or "",
            summary=data.get("about") or data.get("summary") or "",
            location=data.get("location") or "",
            country=data.get("country") or "",
            city=data.get("city") or "",
            profile_pic_url=data.get("profilePicture") or data.get("profile_picture") or "",
            connections=data.get("connectionsCount") or data.get("connections"),
            follower_count=data.get("followerCount"),
            experiences=experiences,
            education=education,
            skills=[s for s in skills if s],
            languages=[],
            source_api="scrapingdog",
            fetch_status="success",
        )


def _parse_date(date_str: Optional[str]):
    """Parse a date string like 'Jan 2020', '2020', 'January 2020' into (year, month)."""
    if not date_str or "present" in str(date_str).lower():
        return None, None
    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    parts = str(date_str).strip().split()
    year = None
    month = None
    for part in parts:
        if part.isdigit() and len(part) == 4:
            year = int(part)
        elif part[:3].lower() in month_map:
            month = month_map[part[:3].lower()]
    return year, month
