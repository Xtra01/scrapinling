"""
People Data Labs (PDL) API client - FALLBACK #3.

API Docs: https://docs.peopledatalabs.com/docs/linkedin-enrichment
Endpoint: GET https://api.peopledatalabs.com/v5/person/enrich
Cost:     ~$0.04/record; 15k profiles ≈ ~$600
Rate limit: 100 req/min (free), up to 1500 req/min (enterprise)

PDL enriches a person record from a LinkedIn URL. Deepest career-history
data of any provider (per third-party 2026 comparisons), but most
expensive per-profile among the mainstream options.
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

from scraper.models import LinkedInProfile

logger = logging.getLogger(__name__)

PDL_BASE_URL = "https://api.peopledatalabs.com/v5/person/enrich"


class PDLRateLimitError(Exception):
    pass


class PDLAuthError(Exception):
    pass


class PDLClient:
    """Async People Data Labs API client."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self.api_key = api_key
        self.session = session
        self.semaphore = semaphore

    async def fetch_profile(self, linkedin_url: str) -> LinkedInProfile:
        async with self.semaphore:
            try:
                return await self._fetch_with_retry(linkedin_url)
            except PDLAuthError as e:
                logger.error("PDL auth error: %s", e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="pdl")
                profile.fetch_status = "failed"
                profile.error_message = f"AuthError: {e}"
                return profile
            except Exception as e:
                logger.warning("PDL unexpected error for %s: %s", linkedin_url, e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="pdl")
                profile.fetch_status = "failed"
                profile.error_message = str(e)
                return profile

    @retry(
        retry=retry_if_exception_type(PDLRateLimitError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_with_retry(self, linkedin_url: str) -> LinkedInProfile:
        params = {
            "profile": linkedin_url,
            "pretty": "false",
            "titlecase": "true",
            "include_if_matched": "true",
        }
        headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

        async with self.session.get(
            PDL_BASE_URL, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("status") == 200 and data.get("data"):
                    return LinkedInProfile.from_pdl(linkedin_url, data)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="pdl")
                profile.fetch_status = "not_found"
                profile.error_message = "No match found in PDL database"
                return profile

            elif response.status in (401, 403):
                raise PDLAuthError(f"HTTP {response.status}: Invalid API key")

            elif response.status == 402:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="pdl")
                profile.fetch_status = "failed"
                profile.error_message = "PDL: No credits remaining (402)"
                return profile

            elif response.status == 404:
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="pdl")
                profile.fetch_status = "not_found"
                profile.error_message = "Profile not found (404)"
                return profile

            elif response.status == 429:
                try:
                    retry_after = int(response.headers.get("Retry-After", 30))
                except (TypeError, ValueError):
                    retry_after = 30
                logger.warning("PDL rate limited, waiting %ds...", retry_after)
                await asyncio.sleep(retry_after)
                raise PDLRateLimitError("Rate limited (429)")

            elif response.status >= 500:
                raise PDLRateLimitError(f"PDL server error {response.status}")

            else:
                error_text = await response.text()
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="pdl")
                profile.fetch_status = "failed"
                profile.error_message = f"HTTP {response.status}: {error_text[:200]}"
                return profile
