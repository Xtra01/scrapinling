"""
Bright Data LinkedIn Profile Scraper client.

API Docs: https://docs.brightdata.com/api-reference/web-scraper-api/social-media-apis/linkedin
Dataset ID: gd_l1viktl72bvl7bjuj0 (LinkedIn People Profiles)

ARCHITECTURE: Bright Data uses a batch-first async model:
  1. POST /datasets/v3/trigger  → returns snapshot_id immediately
  2. GET  /datasets/v3/snapshot/<id> → poll (202=running, 200=ready with data)
     GET  /datasets/v3/progress/<id> → early failure detection (collecting/digesting/ready/failed)
  3. Downloaded data comes back in the 200 response directly.

This is fundamentally different from other clients (which are per-URL sync).
For 15,000 profiles: trigger batches of 500 URLs (well under the 1GB input
limit), a few batches in flight at a time. No official SLA — large batches
can take from minutes to hours depending on LinkedIn-side friction.

PRICING: ~$0.05/profile (pay-per-result, not charged for failed/empty).
15,000 profiles ≈ $750. Most expensive option among those in this project, but:
  - Most legally defensible (won Meta v. Bright Data + X Corp v. Bright Data, both 2024)
  - Richest data: experience, education/educations_details, certifications,
    recommendations (full text), publications, patents, projects, honors,
    volunteer_experience, courses, languages
  - No fake LinkedIn accounts — scrapes as a logged-out visitor, residential
    proxy rotation only (this is the basis of their legal defense)

CONFIRMED LIMITATIONS (see PROJECT_NOTES.md for full writeup):
  - ~80% success rate per third-party benchmarks → budget retries for ~20%
  - No email/phone (public LinkedIn data only)
  - Private/restricted profiles return partial or empty data
  - Avatar/banner image URLs expire 24h after snapshot creation
  - Snapshots deleted after 30 days
  - No official Python SDK — this is a hand-written REST wrapper

FIELDS RETURNED (more than any other provider in this project):
  Profile: name, city, country_code, position, about, current_company,
           followers, connections, avatar
  experience[]:         title, company, duration (string), start_date,
                         end_date (ISO YYYY-MM-DD when present), description
  educations_details[]: richer structured education (preferred over education[])
  education[]:          simpler fallback list
  certifications[], languages[], recommendations[], volunteer_experience[],
  courses[], publications[], patents[], projects[], honors_and_awards[]

REAL-TIME vs BATCH:
  Sync:  POST /datasets/v3/scrape  — for ≤20 URLs; 60s timeout; used for retries
  Batch: POST /datasets/v3/trigger — for any scale; async with polling
"""
import asyncio
import logging
import time
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

# ─── API Config ──────────────────────────────────────────────────────────────────────
BASE_URL = "https://api.brightdata.com/datasets/v3"
LINKEDIN_PROFILE_DATASET_ID = "gd_l1viktl72bvl7bjuj0"

TRIGGER_URL  = f"{BASE_URL}/trigger"
SCRAPE_URL   = f"{BASE_URL}/scrape"
PROGRESS_URL = f"{BASE_URL}/progress"
SNAPSHOT_URL = f"{BASE_URL}/snapshot"

BATCH_SIZE          = 500   # Well under the 1GB input limit; reliable in practice
BATCH_CONCURRENCY   = 3     # Max parallel batches in flight at once
POLL_INTERVAL_SEC   = 30    # Docs recommend 15-60s
POLL_TIMEOUT_SEC    = 7200  # 2 hours max; large batches can take hours

_FAILED_STATUSES = {"failed", "error", "terminated"}


class BrightDataRateLimitError(Exception):
    pass


class BrightDataAuthError(Exception):
    pass


class BrightDataClient:
    """
    Async Bright Data LinkedIn scraper.

    Supports two modes:
      - bulk_fetch(urls)     : efficient batch processing (trigger/poll/download) — USE THIS
      - fetch_profile(url)   : sync real-time for a single URL (fallback / retry use)
    """

    def __init__(self, api_token: str, session: aiohttp.ClientSession,
                 semaphore: asyncio.Semaphore):
        self.token = api_token
        self.session = session
        self.semaphore = semaphore
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    # ── Bulk batch mode (primary path for 15k profiles) ─────────────────

    async def bulk_fetch(
        self,
        urls: list[str],
        on_result: callable = None,
    ) -> dict[str, LinkedInProfile]:
        """
        Process all URLs in batches of BATCH_SIZE.
        Returns dict of {linkedin_url: LinkedInProfile}.

        on_result(profile): optional callback invoked as each result is parsed.
        """
        results: dict[str, LinkedInProfile] = {}
        batch_semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

        batches = [urls[i:i + BATCH_SIZE] for i in range(0, len(urls), BATCH_SIZE)]
        logger.info("BrightData: %d URLs → %d batches of %d",
                    len(urls), len(batches), BATCH_SIZE)

        async def process_batch(batch: list[str]):
            async with batch_semaphore:
                try:
                    batch_results = await self._trigger_and_collect(batch)
                except Exception as e:
                    logger.error("BrightData: batch of %d failed entirely: %s", len(batch), e)
                    batch_results = self._make_error_results(batch, str(e))
                for url, profile in batch_results.items():
                    results[url] = profile
                    if on_result:
                        on_result(profile)

        gather_results = await asyncio.gather(
            *[process_batch(b) for b in batches], return_exceptions=True
        )
        for exc in gather_results:
            if isinstance(exc, Exception):
                logger.error("BrightData: unexpected batch-task exception: %s", exc)
        return results

    async def _trigger_and_collect(self, urls: list[str]) -> dict[str, LinkedInProfile]:
        """Trigger one batch, poll until ready, parse, return results."""
        payload = [{"url": u} for u in urls]
        params = {"dataset_id": LINKEDIN_PROFILE_DATASET_ID, "format": "json"}

        # Step 1: Trigger
        try:
            async with self.session.post(
                TRIGGER_URL, params=params, headers=self._headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401:
                    raise BrightDataAuthError("Invalid API token")
                if resp.status == 403:
                    raise BrightDataAuthError("Insufficient permissions or credits")
                if resp.status == 429:
                    raise BrightDataRateLimitError("Rate limited on trigger")
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"Trigger failed HTTP {resp.status}: {text[:200]}")

                trigger_resp = await resp.json()
                snapshot_id = trigger_resp.get("snapshot_id")
                if not snapshot_id:
                    raise RuntimeError(f"No snapshot_id in trigger response: {trigger_resp}")
                logger.debug("BrightData: triggered batch of %d → snapshot_id=%s",
                             len(urls), snapshot_id)
        except BrightDataAuthError:
            raise
        except Exception as e:
            logger.warning("BrightData trigger error: %s — marking batch as failed", e)
            return self._make_error_results(urls, str(e))

        # Step 2: Poll snapshot endpoint. Per Bright Data docs:
        # /snapshot/{id} returns 202 while in progress, 200 with data when ready.
        # Status values (via /progress/{id}): collecting → digesting → ready/failed/error/terminated.
        download_url = f"{SNAPSHOT_URL}/{snapshot_id}?format=json"
        progress_url = f"{PROGRESS_URL}/{snapshot_id}"
        deadline = time.time() + POLL_TIMEOUT_SEC
        raw = None
        consecutive_not_found = 0

        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SEC)

            try:
                async with self.session.get(
                    progress_url, headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as presp:
                    if presp.status == 200:
                        consecutive_not_found = 0
                        prog = await presp.json()
                        status = prog.get("status", "")
                        logger.debug("BrightData: %s status=%s", snapshot_id, status)
                        if status in _FAILED_STATUSES:
                            logger.warning("BrightData: batch %s → %s", snapshot_id, status)
                            return self._make_error_results(urls, f"Batch {status}")
                    elif presp.status in (401, 403):
                        # Terminal, cheaply detectable — don't poll for 2 hours
                        # to eventually report a generic timeout instead.
                        logger.error("BrightData: auth/credits failure while polling %s (HTTP %d)",
                                     snapshot_id, presp.status)
                        return self._make_error_results(
                            urls, f"Auth/credits failure while polling (HTTP {presp.status})"
                        )
                    elif presp.status == 404:
                        consecutive_not_found += 1
                        if consecutive_not_found >= 3:
                            logger.error("BrightData: snapshot %s not found after %d checks",
                                         snapshot_id, consecutive_not_found)
                            return self._make_error_results(urls, "Snapshot not found (404)")
                    else:
                        logger.debug("BrightData: progress HTTP %d for %s", presp.status, snapshot_id)
            except Exception as e:
                logger.debug("BrightData: progress check error: %s", e)

            try:
                async with self.session.get(
                    download_url, headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as dresp:
                    if dresp.status == 200:
                        raw = await dresp.json()
                        break
                    elif dresp.status == 202:
                        continue  # Still in progress
                    elif dresp.status in (401, 403):
                        logger.error("BrightData: auth/credits failure downloading %s (HTTP %d)",
                                     snapshot_id, dresp.status)
                        return self._make_error_results(
                            urls, f"Auth/credits failure on download (HTTP {dresp.status})"
                        )
                    else:
                        text = await dresp.text()
                        logger.warning("BrightData: download HTTP %d: %s", dresp.status, text[:200])
            except asyncio.TimeoutError:
                logger.debug("BrightData: download timeout for %s, will retry", snapshot_id)
            except Exception as e:
                logger.warning("BrightData: download error for %s: %s", snapshot_id, e)

        if raw is None:
            logger.warning("BrightData: batch %s timed out after %ds", snapshot_id, POLL_TIMEOUT_SEC)
            return self._make_error_results(urls, "Batch timed out")

        if not isinstance(raw, list):
            raw = [raw]

        # Parse each result item and match back to input URLs.
        # Exact match first (the common case). Only fall back to substring
        # matching for a normalization mismatch, and even then: only consider
        # URLs not already claimed by an exact match, and prefer the longest
        # (most specific) candidate — this avoids one URL being a substring
        # of another (e.g. .../in/john vs .../in/john-smith) silently
        # colliding onto the shorter one.
        url_set = set(urls)
        results: dict[str, LinkedInProfile] = {}
        for item in raw:
            input_url = item.get("input_url") or item.get("url") or ""
            if input_url in url_set:
                matched_url = input_url
            else:
                candidates = [
                    u for u in urls
                    if u not in results and (input_url in u or u in input_url)
                ]
                matched_url = max(candidates, key=len) if candidates else (input_url or None)
            if not matched_url:
                continue
            if item.get("error"):
                profile = LinkedInProfile(linkedin_url=matched_url, source_api="brightdata")
                profile.fetch_status = "not_found"
                profile.error_message = str(item["error"])
            else:
                try:
                    profile = self._parse_response(matched_url, item)
                except Exception as e:
                    logger.warning("BrightData: failed to parse item for %s: %s", matched_url, e)
                    profile = LinkedInProfile(linkedin_url=matched_url, source_api="brightdata")
                    profile.fetch_status = "failed"
                    profile.error_message = f"Parse error: {e}"
            results[matched_url] = profile

        for url in urls:
            if url not in results:
                profile = LinkedInProfile(linkedin_url=url, source_api="brightdata")
                profile.fetch_status = "not_found"
                profile.error_message = "No data returned in batch"
                results[url] = profile

        logger.debug("BrightData: batch %s complete: %d/%d successful",
                     snapshot_id,
                     sum(1 for p in results.values() if p.fetch_status == "success"),
                     len(urls))
        return results

    # ── Single URL sync mode (for retries / small sets) ────────────────

    async def fetch_profile(self, linkedin_url: str) -> LinkedInProfile:
        """Fetch a single profile via sync endpoint (60s timeout)."""
        async with self.semaphore:
            try:
                return await self._fetch_single_with_retry(linkedin_url)
            except BrightDataAuthError as e:
                logger.error("BrightData auth error: %s", e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="brightdata")
                profile.fetch_status = "failed"
                profile.error_message = f"AuthError: {e}"
                return profile
            except Exception as e:
                logger.warning("BrightData error for %s: %s", linkedin_url, e)
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="brightdata")
                profile.fetch_status = "failed"
                profile.error_message = str(e)
                return profile

    @retry(
        retry=retry_if_exception_type(BrightDataRateLimitError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_single_with_retry(self, linkedin_url: str) -> LinkedInProfile:
        params = {"dataset_id": LINKEDIN_PROFILE_DATASET_ID, "format": "json"}
        payload = [{"url": linkedin_url}]

        async with self.session.post(
            SCRAPE_URL, params=params, headers=self._headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=65),
        ) as resp:
            if resp.status in (401, 403):
                raise BrightDataAuthError(f"HTTP {resp.status}")
            if resp.status == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                await asyncio.sleep(retry_after)
                raise BrightDataRateLimitError("Rate limited (429)")
            if resp.status >= 500:
                raise BrightDataRateLimitError(f"Server error {resp.status}")
            if resp.status != 200:
                text = await resp.text()
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="brightdata")
                profile.fetch_status = "failed"
                profile.error_message = f"HTTP {resp.status}: {text[:200]}"
                return profile

            raw = await resp.json()
            if isinstance(raw, list):
                raw = raw[0] if raw else {}
            if not raw or raw.get("error"):
                profile = LinkedInProfile(linkedin_url=linkedin_url, source_api="brightdata")
                profile.fetch_status = "not_found"
                profile.error_message = raw.get("error", "Empty response") if raw else "Empty response"
                return profile

            return self._parse_response(linkedin_url, raw)

    # ── Response parser ────────────────────────────────────────────

    def _parse_response(self, linkedin_url: str, data: dict) -> LinkedInProfile:
        experiences = []
        for exp in (data.get("experience") or []):
            company = exp.get("company") or ""
            if isinstance(company, dict):
                company = company.get("name") or ""
            title = exp.get("title") or ""
            duration = exp.get("duration") or ""
            description = exp.get("description") or ""

            if exp.get("start_date"):
                starts_year, starts_month = _parse_iso_date(exp["start_date"])
                ends_year, ends_month     = _parse_iso_date(exp.get("end_date"))
                is_current = not bool(exp.get("end_date"))
            else:
                starts_year, starts_month, ends_year, ends_month, is_current = _parse_duration(duration)

            we = WorkExperience(
                company=company,
                company_linkedin_url=exp.get("company_linkedin_url") or "",
                title=title,
                description=description,
                location=exp.get("location") or "",
                starts_at_year=starts_year,
                starts_at_month=starts_month,
                ends_at_year=ends_year,
                ends_at_month=ends_month,
                is_current=is_current,
            )
            experiences.append(we)

        # Prefer educations_details[] (more structured) over education[]
        education = []
        edu_source = data.get("educations_details") or data.get("education") or []
        for edu in edu_source:
            if isinstance(edu, str):
                education.append(Education(school=edu))
                continue
            ed = Education(
                school=edu.get("school") or edu.get("school_name") or "",
                school_linkedin_url=edu.get("school_linkedin_url") or "",
                degree=edu.get("degree") or edu.get("degree_name") or "",
                field_of_study=edu.get("field_of_study") or edu.get("major") or "",
                description=edu.get("description") or "",
                grade=edu.get("grade") or str(edu.get("gpa", "")) or "",
                activities=edu.get("activities") or edu.get("activities_and_societies") or "",
                starts_at_year=_coerce_year(edu.get("start_year") or edu.get("from_year")),
                ends_at_year=_coerce_year(edu.get("end_year") or edu.get("to_year")),
            )
            education.append(ed)

        skills = [
            s.get("name", s) if isinstance(s, dict) else s
            for s in (data.get("skills") or [])
            if s
        ]

        languages = [
            (lang.get("name") or lang.get("language") or "")
            if isinstance(lang, dict) else lang
            for lang in (data.get("languages") or [])
        ]

        return LinkedInProfile(
            linkedin_url=linkedin_url,
            full_name=data.get("name") or "",
            first_name=(data.get("name") or "").split(" ")[0] if data.get("name") else "",
            last_name=" ".join((data.get("name") or "").split(" ")[1:]) if data.get("name") else "",
            headline=data.get("position") or "",
            summary=data.get("about") or "",
            location=data.get("city") or data.get("location") or "",
            country=data.get("country_code") or "",
            city=data.get("city") or "",
            profile_pic_url=data.get("avatar") or "",
            connections=_parse_int(data.get("connections")),
            follower_count=_parse_int(data.get("followers")),
            experiences=experiences,
            education=education,
            skills=[s for s in skills if s],
            languages=[lang for lang in languages if lang],
            source_api="brightdata",
            fetch_status="success",
        )

    def _make_error_results(self, urls: list[str], message: str) -> dict[str, LinkedInProfile]:
        results = {}
        for url in urls:
            profile = LinkedInProfile(linkedin_url=url, source_api="brightdata")
            profile.fetch_status = "failed"
            profile.error_message = message
            results[url] = profile
        return results


# ── Date/duration parsing helpers ────────────────────────────────

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_iso_date(date_str: Optional[str]):
    """Parse ISO-style date 'YYYY-MM-DD' or 'YYYY-MM' into (year, month)."""
    if not date_str:
        return None, None
    parts = str(date_str).split("-")
    year  = int(parts[0]) if parts[0].isdigit() else None
    month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return year, month


def _parse_duration(duration: str):
    """
    Parse Bright Data duration strings like:
      "Jan 2020 – Mar 2023 · 3 yrs 2 mos"
      "2020 – Present"
    Returns: (start_year, start_month, end_year, end_month, is_current)
    """
    if not duration:
        return None, None, None, None, False

    if duration.strip().lower() == "present":
        return None, None, None, None, True

    duration = duration.replace("–", "-").replace("—", "-")
    parts = [p.strip() for p in duration.split("-")]
    if len(parts) < 2:
        return None, None, None, None, False

    start_part = parts[0].strip()
    end_part = parts[1].split("·")[0].strip()

    def parse_date_part(s):
        tokens = s.strip().lower().split()
        year, month = None, None
        for t in tokens:
            if t.isdigit() and len(t) == 4:
                year = int(t)
            elif t[:3] in _MONTHS:
                month = _MONTHS[t[:3]]
        return year, month

    start_year, start_month = parse_date_part(start_part)
    is_current = "present" in end_part.lower()
    end_year, end_month = (None, None) if is_current else parse_date_part(end_part)

    return start_year, start_month, end_year, end_month, is_current


def _coerce_year(v) -> Optional[int]:
    """Accept an int, float, or numeric string year; reject bool/garbage."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _parse_int(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, str):
        cleaned = val.replace(",", "").replace("+", "").strip()
        multiplier = 1
        if cleaned[-1:].upper() == "K":
            multiplier, cleaned = 1_000, cleaned[:-1]
        elif cleaned[-1:].upper() == "M":
            multiplier, cleaned = 1_000_000, cleaned[:-1]
        try:
            return int(float(cleaned) * multiplier)
        except (ValueError, TypeError):
            return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
