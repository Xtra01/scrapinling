"""
Core scraping engine: orchestrates LinkedIn profile fetching.

API priority (cheapest-first for 15k profiles):
  PRIMARY     Scrapingdog  ~$135 for 15k  | real-time REST
  FALLBACK 1  Netrows      ~€75  for 15k  | real-time REST
  FALLBACK 2  LinkdAPI     credit-based   | Proxycurl replacement
  FALLBACK 3  PDL          ~$600 for 15k  | bulk API
  FALLBACK 4  ScrapIn      $1k+/mo        | real-time
  FALLBACK 5  RocketReach  $53+/mo        | B2B enrichment

  BRIGHT DATA ~$750 for 15k | BATCH mode (trigger/poll/download)
    ↳ Most legally defensible option (won Meta + X Corp lawsuits in 2024).
      Uses its own bulk_fetch() pipeline — see brightdata_client.py.

Architecture:
  - Standard clients (Scrapingdog, Netrows, etc.): per-URL async requests
    via fetch_profile(), wrapped in a semaphore for rate-limiting.
  - Bright Data client: batch-first via bulk_fetch() — triggers batches
    of 500 URLs, polls for completion, downloads in bulk.
  - Both pipelines write to the same SQLite + CSV + JSON Lines outputs.
  - Resume: pending_urls are loaded from SQLite at startup; already-
    successful profiles are skipped automatically.
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

from scraper.models import LinkedInProfile
from scraper.brightdata_client import BrightDataClient
from scraper.scrapingdog_client import ScrapingdogClient
from scraper.netrows_client import NetrowsClient
from scraper.linkdapi_client import LinkdAPIClient
from scraper.pdl_client import PDLClient
from scraper.scrapin_client import ScrapInClient
from scraper.rocketreach_client import RocketReachClient
from utils.storage import Database, CSVWriter, JSONLinesWriter
from utils.progress import ScraperStats, console, make_progress

logger = logging.getLogger(__name__)


class ScraperEngine:
    """
    Orchestrates concurrent LinkedIn profile fetching across multiple APIs.

    Two execution paths:
      1. Bright Data batch pipeline  (if brightdata_key configured)
      2. Standard per-URL pipeline   (Scrapingdog, Netrows, LinkdAPI, PDL, …)

    These can be combined: Bright Data handles the initial pass,
    standard clients retry failures.
    """

    def __init__(
        self,
        brightdata_key: Optional[str] = None,
        scrapingdog_key: Optional[str] = None,
        netrows_key: Optional[str] = None,
        linkdapi_key: Optional[str] = None,
        pdl_key: Optional[str] = None,
        scrapin_key: Optional[str] = None,
        rocketreach_key: Optional[str] = None,
        db: Database = None,
        csv_writer: CSVWriter = None,
        jsonl_writer: JSONLinesWriter = None,
        concurrency: int = 5,
        request_delay: float = 0.2,
    ):
        self.brightdata_key = brightdata_key
        self.scrapingdog_key = scrapingdog_key
        self.netrows_key = netrows_key
        self.linkdapi_key = linkdapi_key
        self.pdl_key = pdl_key
        self.scrapin_key = scrapin_key
        self.rocketreach_key = rocketreach_key
        self.db = db
        self.csv_writer = csv_writer
        self.jsonl_writer = jsonl_writer
        self.concurrency = concurrency
        self.request_delay = request_delay
        self.stats = ScraperStats()

    async def run(self, urls: list[str]):
        """Main entry point: fetch all URLs and save results."""
        self.stats.total = len(urls)
        self.stats.start_time = time.time()

        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=max(self.concurrency * 2, 20), ssl=True),
            headers={"User-Agent": "LinkedInScraper/2.0"},
        ) as session:

            # ── Phase 1: Bright Data bulk pass ────────────────────────
            remaining_urls = list(urls)
            if self.brightdata_key:
                remaining_urls = await self._run_brightdata_pass(urls, session)
                if not remaining_urls:
                    console.print(self.stats.summary_table())
                    return

            # ── Phase 2: Standard per-URL pipeline for remaining / all ────
            if remaining_urls:
                await self._run_standard_pass(remaining_urls, session)

        console.print(self.stats.summary_table())

    # ── Bright Data batch pipeline ──────────────────────────

    async def _run_brightdata_pass(
        self, urls: list[str], session: aiohttp.ClientSession
    ) -> list[str]:
        """
        Run Bright Data batch fetching on all URLs.
        Returns list of URLs that failed (or never reported a result) and
        need retry via the standard pipeline.
        """
        semaphore = asyncio.Semaphore(self.concurrency)
        bd_client = BrightDataClient(self.brightdata_key, session, semaphore)

        progress = make_progress()
        task_id = progress.add_task("Bright Data (batch)", total=len(urls), rate=0.0)

        failed_urls = []
        seen_urls = set()

        def on_result(profile: LinkedInProfile):
            seen_urls.add(profile.linkedin_url)
            try:
                self._persist(profile)
            except Exception as e:
                logger.error("Failed to persist %s: %s", profile.linkedin_url, e)
                profile.fetch_status = "failed"
                profile.error_message = f"Persist error: {e}"
            self.stats.update(profile.fetch_status)
            progress.update(task_id, advance=1, rate=self.stats.rate)
            if profile.fetch_status != "success":
                failed_urls.append(profile.linkedin_url)

        with progress:
            await bd_client.bulk_fetch(urls, on_result=on_result)

        # bulk_fetch/on_result should cover every URL, but if an unexpected
        # exception anywhere in the batch pipeline dropped one silently,
        # don't let it vanish — route it through Phase 2 as a retry too.
        missing = [u for u in urls if u not in seen_urls]
        if missing:
            logger.warning(
                "%d URL(s) never reported a result from Bright Data — queuing for retry", len(missing)
            )
            failed_urls.extend(missing)

        console.print(
            f"[cyan]Bright Data complete:[/cyan] "
            f"{self.stats.success} success, {len(failed_urls)} to retry"
        )
        return failed_urls

    # ── Standard per-URL pipeline ──────────────────────────

    async def _run_standard_pass(self, urls: list[str], session: aiohttp.ClientSession):
        """
        Process URLs with standard per-URL clients (Scrapingdog, Netrows, …).
        Tries clients in priority order until one succeeds.
        """
        semaphore = asyncio.Semaphore(self.concurrency)

        clients = []
        if self.scrapingdog_key:
            clients.append(ScrapingdogClient(self.scrapingdog_key, session, semaphore))
        if self.netrows_key:
            clients.append(NetrowsClient(self.netrows_key, session, asyncio.Semaphore(self.concurrency)))
        if self.linkdapi_key:
            clients.append(LinkdAPIClient(self.linkdapi_key, session, asyncio.Semaphore(self.concurrency)))
        if self.pdl_key:
            clients.append(PDLClient(self.pdl_key, session, asyncio.Semaphore(self.concurrency)))
        if self.scrapin_key:
            clients.append(ScrapInClient(self.scrapin_key, session, asyncio.Semaphore(self.concurrency)))
        if self.rocketreach_key:
            clients.append(RocketReachClient(self.rocketreach_key, session, asyncio.Semaphore(min(self.concurrency, 3))))

        if not clients:
            logger.warning("No standard API clients configured — skipping %d URLs", len(urls))
            return

        label = "Fetching profiles" if not self.brightdata_key else "Retrying failures"
        progress = make_progress()
        task_id = progress.add_task(label, total=len(urls), rate=0.0)

        with progress:
            tasks = [self._process_url(url, clients, progress, task_id) for url in urls]
            batch_size = 500
            for i in range(0, len(tasks), batch_size):
                await asyncio.gather(*tasks[i: i + batch_size], return_exceptions=True)

    async def _process_url(self, url: str, clients: list, progress, task_id):
        """
        Fetch, persist, and account for one URL. The whole body is guarded so
        that a failure anywhere (DB error, disk-full on CSV flush, an unhandled
        client exception) still results in exactly one stats/progress update
        instead of silently vanishing from asyncio.gather(return_exceptions=True).
        """
        profile: Optional[LinkedInProfile] = None
        try:
            self.db.mark_in_progress(url)

            for client in clients:
                try:
                    profile = await client.fetch_profile(url)
                    await asyncio.sleep(self.request_delay)
                    if profile.fetch_status == "success":
                        break
                except Exception as e:
                    logger.warning("%s failed for %s: %s", type(client).__name__, url, e)
                    continue

            if profile is None:
                profile = LinkedInProfile(linkedin_url=url, source_api="none")
                profile.fetch_status = "failed"
                profile.error_message = "No API client configured or all failed"

            self._persist(profile)

            if profile.fetch_status == "success":
                logger.debug("[OK] %s → %s | %d exp, %d edu | %s",
                             url, profile.full_name,
                             len(profile.experiences), len(profile.education),
                             profile.source_api)
            else:
                logger.debug("[%s] %s → %s",
                             profile.fetch_status.upper(), url, profile.error_message)
        except Exception as e:
            logger.error("Unhandled error processing %s: %s", url, e)
            profile = LinkedInProfile(linkedin_url=url, source_api="none")
            profile.fetch_status = "failed"
            profile.error_message = f"Unhandled error: {e}"
        finally:
            self.stats.update(profile.fetch_status if profile else "failed")
            progress.update(task_id, advance=1, rate=self.stats.rate)

    def _persist(self, profile: LinkedInProfile):
        """Write to SQLite, CSV, and JSON Lines."""
        self.db.save_profile(profile)
        self.csv_writer.write(profile)
        self.jsonl_writer.write(profile)
