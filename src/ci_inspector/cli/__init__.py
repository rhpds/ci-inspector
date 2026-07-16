from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import click

from ci_inspector.core.catalog import GitSource
from ci_inspector.core.discovery import discover, discover_from_merged
from ci_inspector.core.models import DiscoveryResult
from ci_inspector.core.thresholds import check_thresholds, load_config
from ci_inspector.outputs.annotations_fmt import format_annotations
from ci_inspector.outputs.json_fmt import format_json
from ci_inspector.outputs.table_fmt import format_summary, format_table, format_validation


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


def _load_merged_yaml(file_path: str) -> tuple[dict, str]:
    """Load YAML from a file path or stdin. Returns (data, source_path)."""
    if file_path == "-":
        import yaml
        data = yaml.safe_load(sys.stdin.read()) or {}
        return data, "<stdin>"
    path = Path(file_path)
    if not path.is_file():
        click.echo(f"Error: file not found: {file_path}", err=True)
        sys.exit(2)
    import yaml
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return data, str(path)


def _infer_ci_name_stage(file_path: str, ci_name: str | None, stage: str | None) -> tuple[str, str]:
    """Infer ci_name and stage from file path if not provided."""
    if ci_name and stage:
        return ci_name, stage
    path = Path(file_path)
    if not stage:
        stage = path.stem if path.stem in ("prod", "dev", "event", "test", "common") else "prod"
    if not ci_name:
        ci_name = path.parent.name if path.parent.name != "." else path.stem
    return ci_name, stage


@main.command("analyze-merged")
@click.argument("file_path")
@click.option("--ci-name", default=None, help="CI name (inferred from path if omitted)")
@click.option("--stage", default=None, help="Stage (inferred from filename if omitted)")
@click.option("--format", "fmt", default="table", type=click.Choice(["json", "table"]))
@click.option("--pricing/--no-pricing", default=False, help="Include cost estimates")
@click.option("--pricing-json", default=None, help="Path to pricing JSON")
def analyze_merged(file_path, ci_name, stage, fmt, pricing, pricing_json):
    """Analyze infrastructure from a pre-merged YAML file.

    FILE_PATH is the path to a merged YAML (output of agnosticv --merge), or - for stdin.
    """
    data, source_path = _load_merged_yaml(file_path)
    ci_name, stage = _infer_ci_name_stage(file_path, ci_name, stage)

    result = asyncio.run(discover_from_merged(ci_name, stage, data))

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


@main.command("check-merged")
@click.argument("file_path")
@click.option("--ci-name", default=None, help="CI name (inferred from path if omitted)")
@click.option("--stage", default=None, help="Stage (inferred from filename if omitted)")
@click.option("--config", "config_path", default=None, help="Override config path")
@click.option("--format", "fmt", default="table",
              type=click.Choice(["json", "table", "annotations"]))
@click.option("--fail-on", default="error", type=click.Choice(["warning", "error"]))
def check_merged(file_path, ci_name, stage, config_path, fmt, fail_on):
    """Check a pre-merged YAML file against threshold policies.

    FILE_PATH is the path to a merged YAML (output of agnosticv --merge), or - for stdin.
    """
    data, source_path = _load_merged_yaml(file_path)
    ci_name, stage = _infer_ci_name_stage(file_path, ci_name, stage)

    result = asyncio.run(discover_from_merged(ci_name, stage, data))

    if result.errors:
        for err in result.errors:
            click.echo(f"Error: {err}", err=True)
        sys.exit(2)

    if config_path:
        cfg = load_config(Path(config_path), Path(config_path).parent)
    else:
        cfg = load_config(Path(source_path).parent, Path("."))

    if cfg.thresholds is None:
        click.echo("Thresholds disabled for this catalog item.")
        sys.exit(0)

    findings = check_thresholds(result, cfg.thresholds)

    if fmt == "json":
        click.echo(format_json(result, findings))
    elif fmt == "annotations":
        click.echo(format_annotations(findings, f"{ci_name}/{stage}.yaml"))
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


@main.command("validate-merged")
@click.argument("file_path")
@click.option("--ci-name", default=None, help="CI name (inferred from path if omitted)")
@click.option("--stage", default=None, help="Stage (inferred from filename if omitted)")
@click.option("--config", "config_path", default=None, help="Override config path")
@click.option("--format", "fmt", default="table",
              type=click.Choice(["json", "table", "annotations"]))
@click.option("--pricing/--no-pricing", default=False, help="Include cost estimates")
@click.option("--pricing-json", default=None, help="Path to pricing JSON")
def validate_merged(file_path, ci_name, stage, config_path, fmt, pricing, pricing_json):
    """Validate a pre-merged YAML file (informational only, never fails).

    Shows infrastructure size summary and threshold warnings without
    blocking the pipeline. FILE_PATH is the path to a merged YAML
    (output of agnosticv --merge), or - for stdin.
    """
    data, source_path = _load_merged_yaml(file_path)
    ci_name, stage = _infer_ci_name_stage(file_path, ci_name, stage)

    result = asyncio.run(discover_from_merged(ci_name, stage, data))

    if result.errors:
        for err in result.errors:
            click.echo(f"Warning: {err}", err=True)

    if pricing:
        from ci_inspector.core.pricing import load_cnv_rates, resolve_pricing
        config = load_cnv_rates(pricing_json)
        updated = asyncio.run(resolve_pricing(result.components, config, pricing_json))
        result = result.model_copy(update={"components": updated})

    if config_path:
        cfg = load_config(Path(config_path), Path(config_path).parent)
    else:
        cfg = load_config(Path(source_path).parent, Path("."))

    findings = []
    if cfg.thresholds is not None:
        findings = check_thresholds(result, cfg.thresholds)
        findings = [
            f.model_copy(update={"severity": "warning"}) if f.severity == "error" else f
            for f in findings
        ]

    if fmt == "json":
        import json
        json_data = result.model_dump(mode="json")
        total_instances = sum(c.count for c in result.components)
        json_data["summary"] = {
            "total_vcpus": result.total_vcpus,
            "total_memory_gb": result.total_memory_gb,
            "total_instances": total_instances,
            "total_storage_gb": sum(c.storage_gb * c.count for c in result.components),
            "total_cost_per_hour": str(result.total_cost_per_hour) if result.total_cost_per_hour else None,
        }
        json_data["findings"] = [f.model_dump() for f in findings]
        click.echo(json.dumps(json_data, indent=2, default=str))
    elif fmt == "annotations":
        for f in findings:
            severity = "warning" if f.severity == "warning" else "notice"
            click.echo(f"::{severity} file={ci_name}/{stage}.yaml,line=1::CI Inspector: {f.message}")
    else:
        click.echo(format_summary(result))
        click.echo()
        click.echo(format_table(result))
        click.echo(format_validation(findings))


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
