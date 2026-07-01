from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import click

from ci_inspector.core.catalog import GitSource
from ci_inspector.core.discovery import discover
from ci_inspector.core.models import DiscoveryResult
from ci_inspector.core.thresholds import check_thresholds, load_config
from ci_inspector.outputs.annotations_fmt import format_annotations
from ci_inspector.outputs.json_fmt import format_json
from ci_inspector.outputs.table_fmt import format_table


def _get_source(source: str, repo_path: str, kubeconfig: str | None):
    if source == "k8s":
        from ci_inspector.core.catalog import K8sSource
        if not kubeconfig:
            click.echo("Error: --kubeconfig required for k8s source", err=True)
            sys.exit(2)
        return K8sSource(kubeconfig)
    return GitSource(Path(repo_path))


@click.group()
@click.version_option()
def main():
    """ci-inspector: Catalog Item infrastructure inspector."""
    pass


@main.command()
@click.argument("ci_name")
@click.option("--stage", default="prod", help="Stage (prod, dev, event)")
@click.option("--source", default="git", type=click.Choice(["git", "k8s"]))
@click.option("--repo-path", default=".", help="Path to AgnosticV repo checkout")
@click.option("--kubeconfig", default=None, help="Kubeconfig path (required for k8s)")
@click.option("--format", "fmt", default="table", type=click.Choice(["json", "table"]))
@click.option("--pricing/--no-pricing", default=False, help="Include cost estimates")
@click.option("--pricing-json", default=None, help="Path to pricing JSON (default: bundled)")
def analyze(ci_name, stage, source, repo_path, kubeconfig, fmt, pricing, pricing_json):
    """Analyze infrastructure for a catalog item."""
    src = _get_source(source, repo_path, kubeconfig)
    result = asyncio.run(discover(ci_name, stage, src))

    if result.errors:
        for err in result.errors:
            click.echo(f"Error: {err}", err=True)
        sys.exit(2)

    if pricing:
        from ci_inspector.core.pricing import load_cnv_rates, resolve_pricing
        config = load_cnv_rates(pricing_json)
        updated = asyncio.run(resolve_pricing(result.components, config, pricing_json))
        result = result.model_copy(update={"components": updated})

    if fmt == "json":
        click.echo(format_json(result))
    else:
        click.echo(format_table(result))


@main.command()
@click.argument("ci_name")
@click.option("--stage", default="prod")
@click.option("--source", default="git", type=click.Choice(["git", "k8s"]))
@click.option("--repo-path", default=".")
@click.option("--kubeconfig", default=None)
@click.option("--config", "config_path", default=None, help="Override config path")
@click.option("--format", "fmt", default="table",
              type=click.Choice(["json", "table", "annotations"]))
@click.option("--fail-on", default="error", type=click.Choice(["warning", "error"]))
def check(ci_name, stage, source, repo_path, kubeconfig, config_path, fmt, fail_on):
    """Check a catalog item against threshold policies."""
    src = _get_source(source, repo_path, kubeconfig)
    result = asyncio.run(discover(ci_name, stage, src))

    if result.errors:
        for err in result.errors:
            click.echo(f"Error: {err}", err=True)
        sys.exit(2)

    repo_root = Path(repo_path)
    if config_path:
        cfg = load_config(Path(config_path), repo_root)
    else:
        ci_dir = repo_root
        for p in repo_root.rglob("prod.yml"):
            if p.parent.name == ci_name:
                ci_dir = p.parent
                break
        cfg = load_config(ci_dir, repo_root)

    if cfg.thresholds is None:
        click.echo("Thresholds disabled for this catalog item.")
        sys.exit(0)

    findings = check_thresholds(result, cfg.thresholds)

    if fmt == "json":
        click.echo(format_json(result, findings))
    elif fmt == "annotations":
        file_path = f"{ci_name}/{stage}.yml"
        click.echo(format_annotations(findings, file_path))
    else:
        if findings:
            for f in findings:
                icon = "X" if f.severity == "error" else "!"
                click.echo(f"  [{icon}] {f.message}")
        else:
            click.echo("All checks passed.")

    has_errors = any(f.severity == "error" for f in findings)
    has_warnings = any(f.severity == "warning" for f in findings)

    if has_errors:
        sys.exit(1)
    if fail_on == "warning" and has_warnings:
        sys.exit(1)


@main.command("check-all")
@click.option("--source", default="git", type=click.Choice(["git", "k8s"]))
@click.option("--repo-path", default=".")
@click.option("--kubeconfig", default=None)
@click.option("--format", "fmt", default="table",
              type=click.Choice(["json", "table", "annotations"]))
@click.option("--fail-on", default="error", type=click.Choice(["warning", "error"]))
def check_all(source, repo_path, kubeconfig, fmt, fail_on):
    """Check all catalog items against threshold policies."""
    src = _get_source(source, repo_path, kubeconfig)
    items = asyncio.run(src.list_catalog_items())

    any_failure = False
    for item in items:
        result = asyncio.run(discover(item.ci_name, item.stage, src))
        if result.errors:
            continue

        repo_root = Path(repo_path)
        ci_dir = repo_root
        for p in repo_root.rglob("prod.yml"):
            if p.parent.name == item.ci_name:
                ci_dir = p.parent
                break
        cfg = load_config(ci_dir, repo_root)
        if cfg.thresholds is None:
            continue

        findings = check_thresholds(result, cfg.thresholds)
        if findings:
            click.echo(f"\n{item.ci_name} ({item.stage}):")
            if fmt == "annotations":
                click.echo(format_annotations(findings, f"{item.ci_name}/{item.stage}.yml"))
            else:
                for f in findings:
                    icon = "X" if f.severity == "error" else "!"
                    click.echo(f"  [{icon}] {f.message}")

            has_errors = any(f.severity == "error" for f in findings)
            has_warnings = any(f.severity == "warning" for f in findings)
            if has_errors or (fail_on == "warning" and has_warnings):
                any_failure = True

    sys.exit(1 if any_failure else 0)


@main.command("detect-changes")
@click.option("--base", required=True, help="Base git ref to compare against")
def detect_changes(base):
    """Detect changed catalog items between git refs."""
    try:
        diff_output = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        click.echo(f"Error: git diff failed: {e}", err=True)
        sys.exit(2)

    seen: set[str] = set()
    for line in diff_output.strip().splitlines():
        path = Path(line)
        if path.suffix not in (".yml", ".yaml"):
            continue
        if path.stem in ("prod", "dev", "event"):
            ci_name = path.parent.name
            if ci_name not in seen:
                seen.add(ci_name)
                click.echo(ci_name)


@main.command()
@click.option("--source", default="git", type=click.Choice(["git", "k8s"]))
@click.option("--repo-path", default=".")
@click.option("--kubeconfig", default=None)
@click.option("--format", "fmt", default="json",
              type=click.Choice(["json", "table", "sheets"]))
@click.option("--sheet-id", default=None, help="Google Sheets spreadsheet ID")
@click.option("--credentials", default=None, help="Google SA key path")
@click.option("--pricing/--no-pricing", default=False)
@click.option("--filter", "filter_glob", default=None, help="Filter by CI name glob")
@click.option("--prune/--no-prune", default=False, help="Hard-delete removed CIs from sheet")
def scan(source, repo_path, kubeconfig, fmt, sheet_id, credentials, pricing, filter_glob, prune):
    """Scan all catalog items."""
    import fnmatch
    src = _get_source(source, repo_path, kubeconfig)
    items = asyncio.run(src.list_catalog_items())

    if filter_glob:
        items = [i for i in items if fnmatch.fnmatch(i.ci_name, filter_glob)]

    results: list[DiscoveryResult] = []
    for item in items:
        result = asyncio.run(discover(item.ci_name, item.stage, src))
        if not result.errors:
            if pricing:
                from ci_inspector.core.pricing import resolve_pricing
                result = result.model_copy(update={
                    "components": asyncio.run(resolve_pricing(
                        result.components,
                        load_config(Path(repo_path), Path(repo_path)).pricing,
                    ))
                })
            results.append(result)

    if fmt == "sheets":
        if not sheet_id or not credentials:
            click.echo("Error: --sheet-id and --credentials required for sheets format", err=True)
            sys.exit(2)
        from ci_inspector.outputs.sheets import sync_to_sheets
        sync_to_sheets(results, sheet_id, credentials, prune=prune)
        click.echo(f"Synced {len(results)} CIs to sheet.")
    elif fmt == "json":
        import json
        data = [r.model_dump(mode="json") for r in results]
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        for r in results:
            click.echo(format_table(r))
            click.echo()
