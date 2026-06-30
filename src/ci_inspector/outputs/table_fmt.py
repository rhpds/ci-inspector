from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ci_inspector.core.models import DiscoveryResult


def format_table(result: DiscoveryResult) -> str:
    """Format discovery result as a Rich table.

    Args:
        result: Discovery result with components and totals

    Returns:
        Rich table string showing components and totals
    """
    table = Table(
        title=f"{result.ci_name} ({result.stage}) — {result.env_type or 'unknown'} / {result.cloud_provider or 'unknown'}",
        show_lines=True,
    )
    table.add_column("Purpose", style="cyan")
    table.add_column("Instance Type", style="green")
    table.add_column("Count", justify="right")
    table.add_column("vCPU", justify="right")
    table.add_column("Mem GB", justify="right")
    table.add_column("Cost/hr", justify="right", style="yellow")

    for c in result.components:
        cost = f"${c.cost_per_hour}" if c.cost_per_hour else "—"
        group_prefix = f"[dim]{c.group}:[/dim] " if c.group else ""
        table.add_row(
            f"{group_prefix}{c.purpose}",
            c.instance_type,
            str(c.count),
            str(c.cpu * c.count),
            str(c.memory_gb * c.count),
            cost,
        )

    # Totals row
    total_cost = f"${result.total_cost_per_hour}/hr" if result.total_cost_per_hour else "—"
    table.add_row(
        "[bold]Totals[/bold]", "", "",
        f"[bold]{result.total_vcpus}[/bold]",
        f"[bold]{result.total_memory_gb}[/bold]",
        f"[bold]{total_cost}[/bold]",
        style="bold",
    )

    console = Console(file=None, force_terminal=False, width=120)
    with console.capture() as capture:
        console.print(table)
    return capture.get()
