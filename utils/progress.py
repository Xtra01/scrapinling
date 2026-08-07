"""
Rich progress display and statistics reporting.
"""
import logging
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class ScraperStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    not_found: int = 0
    rate_limited: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def processed(self) -> int:
        return self.success + self.failed + self.not_found

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def rate(self) -> float:
        """Profiles per minute."""
        elapsed = self.elapsed
        return (self.processed / elapsed * 60) if elapsed > 0 else 0.0

    def update(self, status: str):
        if status == "success":
            self.success += 1
        elif status == "failed":
            self.failed += 1
        elif status == "not_found":
            self.not_found += 1
        elif status == "rate_limited":
            self.rate_limited += 1

    def summary_table(self) -> Table:
        table = Table(title="LinkedIn Scraper Summary", show_header=True, header_style="bold cyan")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        table.add_row("Total URLs", str(self.total))
        table.add_row("Processed", str(self.processed))
        table.add_row("[green]Success[/green]", f"[green]{self.success}[/green]")
        table.add_row("[red]Failed[/red]", f"[red]{self.failed}[/red]")
        table.add_row("[yellow]Not Found[/yellow]", f"[yellow]{self.not_found}[/yellow]")
        table.add_row("Success Rate", f"{self.success / max(self.processed, 1) * 100:.1f}%")
        table.add_row("Avg Speed", f"{self.rate:.1f} profiles/min")
        table.add_row("Total Time", f"{self.elapsed:.0f}s")
        return table


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        TextColumn("•"),
        TextColumn("[green]{task.fields[rate]:.1f}/min"),
        console=console,
        transient=False,
    )
