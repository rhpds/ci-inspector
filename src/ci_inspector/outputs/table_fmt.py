from __future__ import annotations

from collections import defaultdict

from rich.console import Console
from rich.table import Table

from ci_inspector.core.models import DiscoveryResult, Finding


def _result_fingerprint(result: DiscoveryResult) -> tuple:
    """Create a hashable fingerprint from a result's infrastructure shape."""
    components = tuple(
        (c.purpose, c.instance_type, c.count, c.cpu, c.memory_gb,
         c.storage_gb, c.cloud, str(c.cost_per_hour), c.group)
        for c in sorted(result.components, key=lambda c: (c.group or "", c.purpose))
    )
    return (result.ci_name, result.env_type, result.cloud_provider, components)


def group_results(
    results: list[DiscoveryResult],
) -> list[tuple[list[str], DiscoveryResult]]:
    """Group results with identical infrastructure, preserving stage order.

    Returns a list of (stages, representative_result) tuples.
    """
    groups: dict[tuple, list[str]] = defaultdict(list)
    representatives: dict[tuple, DiscoveryResult] = {}
    order: list[tuple] = []

    for r in results:
        fp = _result_fingerprint(r)
        if fp not in representatives:
            order.append(fp)
            representatives[fp] = r
        groups[fp].append(r.stage)

    return [(groups[fp], representatives[fp]) for fp in order]


def _stage_label(result: DiscoveryResult, stages: list[str] | None = None) -> str:
    if stages and len(stages) > 1:
        return ", ".join(stages)
    return result.stage


def format_table(result: DiscoveryResult, *, stages: list[str] | None = None) -> str:
    """Format discovery result as a Rich table.

    Args:
        result: Discovery result with components and totals
        stages: Optional list of stage names to display (for grouped output)

    Returns:
        Rich table string showing components and totals
    """
    label = _stage_label(result, stages)
    table = Table(
        title=f"{result.ci_name} ({label}) — {result.env_type or 'unknown'} / {result.cloud_provider or 'unknown'}",
        show_lines=True,
    )
    table.add_column("Purpose", style="cyan")
    table.add_column("Instance Type", style="green")
    table.add_column("Count", justify="right")
    table.add_column("vCPU", justify="right")
    table.add_column("Mem GB", justify="right")
    table.add_column("Cost/hr", justify="right", style="yellow")

    for c in result.components:
        cost = f"${c.cost_per_hour:.2f}" if c.cost_per_hour else "—"
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
    total_cost = f"${result.total_cost_per_hour:.2f}/hr" if result.total_cost_per_hour else "—"
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


def format_summary(result: DiscoveryResult, *, stages: list[str] | None = None) -> str:
    label = _stage_label(result, stages)
    total_instances = sum(c.count for c in result.components)
    parts = [
        f"{result.total_vcpus} vCPUs",
        f"{result.total_memory_gb} GB RAM",
        f"{total_instances} instances",
    ]
    if result.total_cost_per_hour is not None:
        parts.append(f"${result.total_cost_per_hour:.2f}/hr")

    return f"{result.ci_name} ({label}) — Summary\n  {' · '.join(parts)}"


def format_validation(findings: list[Finding]) -> str:
    if not findings:
        return "Validation:\n  All checks passed."
    lines = ["Validation:"]
    for f in findings:
        icon = "[!]" if f.severity == "warning" else "[i]"
        lines.append(f"  {icon} {f.message}")
    return "\n".join(lines)
