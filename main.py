#!/usr/bin/env python3
"""
LinkedIn Profile Scraper v2.0

Aktif API'ler (Proxycurl Temmuz 2025'te kapandı):
  BRIGHT DATA (opsiyonel Phase 1) brightdata.com  (~$750 for 15k, en yasal + en zengin)
  PRIMARY:    Scrapingdog       scrapingdog.com     (~$135 for 15k)
  FALLBACK 1: Netrows           netrows.com         (~€75 for 15k)
  FALLBACK 2: LinkdAPI          linkdapi.com        (credit-based, Proxycurl replacement)
  FALLBACK 3: People Data Labs  peopledatalabs.com  (~$600 for 15k, deepest data)
  FALLBACK 4: ScrapIn           scrapin.io          (real-time, $1k+/mo)
  FALLBACK 5: RocketReach       rocketreach.co      ($53+/mo)

Usage:
    python main.py scrape --input links.csv
    python main.py scrape --input links.txt --concurrency 10
    python main.py status
    python main.py export --format csv
"""
import asyncio
import csv
import logging
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()
console = Console()


def _setup_logging(level: str):
    Path("logs").mkdir(exist_ok=True)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/scraper.log"),
        ],
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _clean_key(key):
    """Return None if key is missing or still has placeholder value."""
    return key if key and not key.startswith("your_") else None


@click.group()
@click.version_option("2.0.0")
def cli():
    """LinkedIn Profile Scraper - iş deneyimi & eğitim verisi, 15k+ profil."""
    pass


@cli.command()
@click.option("--input", "-i", "input_file", required=True,
              help="CSV veya TXT dosyası (LinkedIn URL listesi).")
@click.option("--output-dir", "-o",
              default=os.getenv("OUTPUT_DIR", "data/output"), show_default=True)
@click.option("--db", "db_path",
              default=os.getenv("DB_PATH", "data/linkedin_profiles.db"), show_default=True)
@click.option("--concurrency", "-c",
              default=int(os.getenv("MAX_CONCURRENCY", "5")), show_default=True)
@click.option("--delay", default=float(os.getenv("REQUEST_DELAY", "0.2")), show_default=True)
@click.option("--brightdata-key", default=os.getenv("BRIGHTDATA_API_TOKEN"),
              help="Bright Data API token (batch mode, ~$0.05/profile, most legal).")
@click.option("--scrapingdog-key", default=os.getenv("SCRAPINGDOG_API_KEY"),
              help="Scrapingdog API key (~$0.009/profile).")
@click.option("--netrows-key", default=os.getenv("NETROWS_API_KEY"),
              help="Netrows API key (~€0.005/profile).")
@click.option("--linkdapi-key", default=os.getenv("LINKDAPI_API_KEY"),
              help="LinkdAPI key (Proxycurl replacement).")
@click.option("--pdl-key", default=os.getenv("PDL_API_KEY"),
              help="People Data Labs API key (~$0.04/profile).")
@click.option("--scrapin-key", default=os.getenv("SCRAPIN_API_KEY"),
              help="ScrapIn API key ($1k+/mo plan).")
@click.option("--rocketreach-key", default=os.getenv("ROCKETREACH_API_KEY"),
              help="RocketReach API key ($53+/mo).")
@click.option("--resume/--no-resume", default=True,
              help="Kaldığı yerden devam et.")
@click.option("--log-level",
              default=os.getenv("LOG_LEVEL", "INFO"),
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
              show_default=True)
def scrape(
    input_file, output_dir, db_path, concurrency, delay,
    brightdata_key, scrapingdog_key, netrows_key, linkdapi_key,
    pdl_key, scrapin_key, rocketreach_key,
    resume, log_level,
):
    """LinkedIn profillerini URL listesinden çek."""
    _setup_logging(log_level)

    brightdata_key  = _clean_key(brightdata_key)
    scrapingdog_key = _clean_key(scrapingdog_key)
    netrows_key     = _clean_key(netrows_key)
    linkdapi_key    = _clean_key(linkdapi_key)
    pdl_key         = _clean_key(pdl_key)
    scrapin_key     = _clean_key(scrapin_key)
    rocketreach_key = _clean_key(rocketreach_key)

    configured = [k for k in [brightdata_key, scrapingdog_key, netrows_key,
                               linkdapi_key, pdl_key, scrapin_key, rocketreach_key] if k]
    if not configured:
        console.print("[bold red]Hata:[/bold red] Hiç API anahtarı yapılandırılmadı.")
        console.print("  .env.example dosyasına bakarak en az bir API anahtarı gir.")
        console.print("\n  Önerilen başlangıç:")
        console.print("    SCRAPINGDOG_API_KEY  → scrapingdog.com  (~$135 for 15k)")
        console.print("    NETROWS_API_KEY      → netrows.com      (~€75 for 15k)")
        console.print("    BRIGHTDATA_API_TOKEN → brightdata.com   (en yasal, ~$750)")
        sys.exit(1)

    from utils.validators import load_urls_from_file
    from utils.storage import Database, CSVWriter, JSONLinesWriter
    from scraper.engine import ScraperEngine

    console.print(f"[bold]URL dosyası:[/bold] {input_file}")
    all_urls = load_urls_from_file(input_file)

    if not all_urls:
        console.print("[bold red]Geçerli LinkedIn URL bulunamadı.[/bold red]")
        sys.exit(1)

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    db.init_job_queue(all_urls)

    if resume:
        pending_urls = db.get_pending_urls()
        skipped = len(all_urls) - len(pending_urls)
        if skipped > 0:
            console.print(f"[cyan]Resume modu:[/cyan] {skipped} profil atlanıyor (zaten işlendi).")
        urls_to_process = pending_urls
    else:
        urls_to_process = all_urls

    if not urls_to_process:
        console.print("[bold green]Tüm URL'ler işlendi![/bold green]")
        _print_db_stats(db)
        sys.exit(0)

    console.print(f"\n[bold]İşlenecek profil:[/bold]  {len(urls_to_process):,}")
    console.print(f"[bold]Eş zamanlı istek:[/bold] {concurrency}")
    console.print(f"\n[bold]API Konfigürasyonu:[/bold]")
    _print_api_config(brightdata_key, scrapingdog_key, netrows_key, linkdapi_key, pdl_key, scrapin_key, rocketreach_key)
    console.print()

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    csv_path = os.path.join(output_dir, "profiles.csv")
    jsonl_path = os.path.join(output_dir, "profiles.jsonl")

    with CSVWriter(csv_path) as csv_writer, JSONLinesWriter(jsonl_path) as jsonl_writer:
        engine = ScraperEngine(
            brightdata_key=brightdata_key,
            scrapingdog_key=scrapingdog_key,
            netrows_key=netrows_key,
            linkdapi_key=linkdapi_key,
            pdl_key=pdl_key,
            scrapin_key=scrapin_key,
            rocketreach_key=rocketreach_key,
            db=db,
            csv_writer=csv_writer,
            jsonl_writer=jsonl_writer,
            concurrency=concurrency,
            request_delay=delay,
        )
        asyncio.run(engine.run(urls_to_process))

    console.print(f"\n[bold green]Çıktı dosyaları:[/bold green]")
    console.print(f"  CSV:    {csv_path}")
    console.print(f"  JSON:   {jsonl_path}")
    console.print(f"  SQLite: {db_path}")


@cli.command()
@click.option("--db", "db_path", default="data/linkedin_profiles.db", show_default=True)
def status(db_path):
    """İşlem ilerlemesini göster."""
    from utils.storage import Database
    if not Path(db_path).exists():
        console.print(f"[red]Veritabanı bulunamadı: {db_path}[/red]")
        sys.exit(1)
    db = Database(db_path)
    _print_db_stats(db)


@cli.command()
@click.option("--db", "db_path", default="data/linkedin_profiles.db", show_default=True)
@click.option("--format", "-f", "fmt",
              type=click.Choice(["csv", "json", "both"], case_sensitive=False),
              default="both", show_default=True)
@click.option("--output-dir", "-o", default="data/output", show_default=True)
@click.option("--filter-status", default="success", show_default=True)
def export(db_path, fmt, output_dir, filter_status):
    """SQLite'tan profilleri CSV/JSON olarak dışa aktar."""
    import sqlite3
    if not Path(db_path).exists():
        console.print(f"[red]Veritabanı bulunamadı: {db_path}[/red]")
        sys.exit(1)

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    profiles = conn.execute(
        "SELECT * FROM profiles WHERE fetch_status=?", (filter_status,)
    ).fetchall()
    console.print(f"{len(profiles)} profil dışa aktarılıyor (status={filter_status})...")

    if fmt in ("csv", "both"):
        csv_path = os.path.join(output_dir, f"export_{filter_status}.csv")
        _export_csv(conn, profiles, csv_path)
        console.print(f"[green]CSV:[/green] {csv_path}")

    if fmt in ("json", "both"):
        json_path = os.path.join(output_dir, f"export_{filter_status}.jsonl")
        with open(json_path, "w", encoding="utf-8") as f:
            for p in profiles:
                if p["raw_json"]:
                    f.write(p["raw_json"] + "\n")
        console.print(f"[green]JSON:[/green] {json_path}")

    conn.close()


def _export_csv(conn, profiles, path):
    from utils.storage import CSV_FIELDNAMES
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for p in profiles:
            url = p["linkedin_url"]
            base = {
                "linkedin_url": url, "full_name": p["full_name"],
                "first_name": p["first_name"], "last_name": p["last_name"],
                "headline": p["headline"], "location": p["location"],
                "country": p["country"], "city": p["city"],
                "connections": p["connections"], "source_api": p["source_api"],
                "fetch_status": p["fetch_status"], "error_message": p["error_message"],
            }
            exps = conn.execute(
                "SELECT * FROM experiences WHERE linkedin_url=? ORDER BY exp_index", (url,)
            ).fetchall()
            edus = conn.execute(
                "SELECT * FROM education WHERE linkedin_url=? ORDER BY edu_index", (url,)
            ).fetchall()

            if not exps and not edus:
                writer.writerow(base)
                continue
            for exp in exps:
                row = dict(base)
                row.update({
                    "record_type": "experience", "exp_index": exp["exp_index"],
                    "exp_company": exp["company"], "exp_company_linkedin_url": exp["company_linkedin_url"],
                    "exp_title": exp["title"], "exp_description": exp["description"],
                    "exp_location": exp["location"], "exp_start": exp["start_date"],
                    "exp_end": exp["end_date"], "exp_is_current": exp["is_current"],
                })
                writer.writerow(row)
            for edu in edus:
                row = dict(base)
                row.update({
                    "record_type": "education", "edu_index": edu["edu_index"],
                    "edu_school": edu["school"], "edu_school_linkedin_url": edu["school_linkedin_url"],
                    "edu_degree": edu["degree"], "edu_field_of_study": edu["field_of_study"],
                    "edu_grade": edu["grade"], "edu_start_year": edu["start_year"],
                    "edu_end_year": edu["end_year"],
                })
                writer.writerow(row)


def _print_api_config(brightdata, scrapingdog, netrows, linkdapi, pdl, scrapin, rocketreach):
    apis = [
        ("Bright Data (BATCH, en yasal)", brightdata,   "~$750 for 15k | batch 500 URL/batch"),
        ("Scrapingdog",                   scrapingdog,  "~$135 for 15k"),
        ("Netrows",                       netrows,      "~€75 for 15k"),
        ("LinkdAPI",                      linkdapi,     "Proxycurl replacement"),
        ("People Data Labs",              pdl,          "~$600 for 15k"),
        ("ScrapIn",                       scrapin,      "$1k+/mo"),
        ("RocketReach",                   rocketreach,  "$53+/mo"),
    ]
    for name, key, cost in apis:
        if key:
            console.print(f"  [green]✓[/green] {name} ({cost})")
        else:
            console.print(f"  [dim]✗ {name}[/dim]")


def _print_db_stats(db):
    stats = db.get_stats()
    stuck = stats.get("in_progress", 0)
    table = Table(title="Scraping İlerlemesi", show_header=True, header_style="bold cyan")
    table.add_column("Durum", style="bold")
    table.add_column("Sayı", justify="right")
    table.add_row("[green]Başarılı[/green]",  f"[green]{stats.get('success', 0):,}[/green]")
    table.add_row("[red]Başarısız[/red]",     f"[red]{stats.get('failed', 0):,}[/red]")
    table.add_row("[yellow]Bulunamadı[/yellow]", f"[yellow]{stats.get('not_found', 0):,}[/yellow]")
    table.add_row("Bekliyor", str(stats.get("pending", 0) + stats.get("retry", 0) + stuck))
    if stuck:
        table.add_row(
            "[orange3]Yarıda kalmış[/orange3]",
            f"[orange3]{stuck:,}[/orange3] (bir sonraki --resume'da otomatik devam eder)",
        )
    table.add_row("[bold]Toplam[/bold]", f"[bold]{sum(stats.values()):,}[/bold]")
    console.print(table)


if __name__ == "__main__":
    cli()
