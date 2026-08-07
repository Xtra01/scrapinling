"""
Input validation and URL normalization utilities.
"""
import csv
import logging
import re
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

LINKEDIN_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?",
    re.IGNORECASE,
)


def normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn URL to canonical form."""
    url = url.strip().rstrip("/")
    url = re.sub(r"^http://", "https://", url)
    url = re.sub(r"https://linkedin\.com/", "https://www.linkedin.com/", url)
    url = url.split("?")[0].split("#")[0]
    return url


def is_valid_linkedin_url(url: str) -> bool:
    return bool(LINKEDIN_URL_PATTERN.match(url))


def load_urls_from_file(path: str) -> list[str]:
    """
    Load LinkedIn URLs from a CSV or TXT file.

    CSV: looks for a column named one of:
         linkedin_url, linkedin, url, profile_url, link
    TXT: treats each non-empty line as a URL.

    Returns a deduplicated, normalized list of valid LinkedIn profile URLs.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        urls = list(_load_from_csv(path))
    elif suffix in (".txt", ".tsv", ""):
        urls = list(_load_from_txt(path))
    else:
        try:
            urls = list(_load_from_csv(path))
        except Exception:
            urls = list(_load_from_txt(path))

    seen = set()
    valid_urls = []
    skipped = 0
    for raw_url in urls:
        if not raw_url:
            continue
        normalized = normalize_linkedin_url(raw_url)
        if not is_valid_linkedin_url(normalized):
            skipped += 1
            logger.debug("Skipping invalid URL: %s", raw_url)
            continue
        if normalized not in seen:
            seen.add(normalized)
            valid_urls.append(normalized)

    logger.info(
        "Loaded %d unique valid URLs from %s (skipped %d invalid)",
        len(valid_urls), path, skipped,
    )
    return valid_urls


def _load_from_csv(path: str) -> Generator[str, None, None]:
    candidate_cols = {"linkedin_url", "linkedin", "url", "profile_url", "link", "profileurl", "profile"}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = [h.lower().strip() for h in (reader.fieldnames or [])]
        col = next((h for h in headers if h in candidate_cols), None)

        if col is None:
            for row in reader:
                for val in row.values():
                    if val and "linkedin.com/in/" in val.lower():
                        yield val.strip()
            return

        for row in reader:
            for orig_header in (reader.fieldnames or []):
                if orig_header.lower().strip() == col:
                    val = row.get(orig_header, "").strip()
                    if val:
                        yield val
                    break


def _load_from_txt(path: str) -> Generator[str, None, None]:
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line
