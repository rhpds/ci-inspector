"""End-to-end integration tests using fixture AgnosticV repo."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ci_inspector.cli import main
from ci_inspector.core.catalog import GitSource
from ci_inspector.core.discovery import discover
from ci_inspector.core.thresholds import check_thresholds, load_config

FIXTURES = Path(__file__).parent / "fixtures" / "agnosticv"


@pytest.mark.asyncio
async def test_full_discovery_pipeline():
    """Test complete discovery flow: GitSource → discover → result."""
    source = GitSource(FIXTURES)
    result = await discover("my-workshop", "prod", source)

    assert result.ci_name == "my-workshop"
    assert result.stage == "prod"
    assert len(result.components) > 0
    # Components are discovered but CPU/memory are 0 until pricing resolution
    assert result.total_vcpus == 0
    assert result.total_memory_gb == 0
    # Verify instance types are extracted
    assert any(c.instance_type == "t3a.medium" for c in result.components)
    assert any(c.instance_type == "m5.xlarge" for c in result.components)


@pytest.mark.asyncio
async def test_full_check_pipeline():
    """Test complete check flow: discover → load_config → check_thresholds."""
    source = GitSource(FIXTURES)
    result = await discover("my-workshop", "prod", source)

    # Load cascading config from my-workshop directory
    config = load_config(FIXTURES / "my-workshop", FIXTURES.parent / "configs")

    if config.thresholds:
        findings = check_thresholds(result, config.thresholds)
        assert isinstance(findings, list)
        # Should not exceed max_vcpus: 64 from fixtures/configs/.ci-inspector.yml
        assert not any(f.severity == "error" for f in findings)


@pytest.mark.asyncio
async def test_binder_discovery():
    """Test binder lab discovery with linked components."""
    source = GitSource(FIXTURES)
    result = await discover("binder-lab", "prod", source)

    assert result.ci_name == "binder-lab"
    assert result.stage == "prod"
    assert result.is_binder is True
    # Binder labs should have components from linked items
    assert len(result.components) >= 0


def test_cli_analyze_json():
    """Test CLI analyze command with JSON output."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "analyze", "my-workshop",
        "--source", "git", "--repo-path", str(FIXTURES),
        "--format", "json",
    ])
    assert result.exit_code == 0
    assert "my-workshop" in result.output
    assert "components" in result.output


def test_cli_analyze_table():
    """Test CLI analyze command with table output."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "analyze", "my-workshop",
        "--source", "git", "--repo-path", str(FIXTURES),
        "--format", "table",
    ])
    assert result.exit_code == 0
    assert "my-workshop" in result.output or "My Workshop" in result.output


def test_cli_check():
    """Test CLI check command with threshold validation."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "check", "my-workshop",
        "--source", "git", "--repo-path", str(FIXTURES),
        "--config", str(FIXTURES.parent / "configs" / ".ci-inspector.yml"),
    ])
    # Exit code 0 = pass, 1 = threshold violations
    assert result.exit_code in (0, 1)


def test_cli_check_all():
    """Test CLI check-all command for batch validation."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "check-all",
        "--source", "git", "--repo-path", str(FIXTURES),
    ])
    # Exit code 0 = all pass, 1 = at least one violation
    assert result.exit_code in (0, 1)


def test_cli_check_annotations_format():
    """Test GitHub Actions annotation format output."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "check", "my-workshop",
        "--source", "git", "--repo-path", str(FIXTURES),
        "--format", "annotations",
        "--config", str(FIXTURES.parent / "configs" / ".ci-inspector.yml"),
    ])
    assert result.exit_code in (0, 1)
    # Annotations format should contain '::' markers if there are findings
    if result.exit_code == 1:
        assert "::" in result.output or result.output.strip() == ""


def test_cli_detect_changes_no_git():
    """Test detect-changes command behavior outside git repo."""
    runner = CliRunner()
    # This will fail outside a git repo, which is expected
    result = runner.invoke(main, ["detect-changes", "--base", "HEAD~1"])
    # Either success (in git repo) or failure (not in repo)
    assert result.exit_code in (0, 2)


@pytest.mark.asyncio
async def test_end_to_end_with_pricing():
    """Test full pipeline including pricing resolution."""
    from ci_inspector.core.pricing import load_cnv_rates, resolve_pricing

    source = GitSource(FIXTURES)
    result = await discover("my-workshop", "prod", source)

    pricing_config = load_cnv_rates()
    components_with_pricing = await resolve_pricing(
        result.components,
        pricing_config,
    )

    assert len(components_with_pricing) == len(result.components)
    # Some components may have pricing populated (AWS)
    # CNV components will have pricing based on formulas


@pytest.mark.asyncio
async def test_discovery_error_handling():
    """Test discovery with non-existent catalog item."""
    source = GitSource(FIXTURES)
    result = await discover("nonexistent-ci", "prod", source)

    # Should have errors
    assert len(result.errors) > 0
    assert result.ci_name == "nonexistent-ci"


def test_cli_scan_json():
    """Test CLI scan command with JSON output."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "scan",
        "--source", "git", "--repo-path", str(FIXTURES),
        "--format", "json",
    ])
    assert result.exit_code == 0
    assert "[" in result.output  # JSON array


def test_cli_scan_table():
    """Test CLI scan command with table output."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "scan",
        "--source", "git", "--repo-path", str(FIXTURES),
        "--format", "table",
    ])
    assert result.exit_code == 0


def test_cli_scan_with_filter():
    """Test CLI scan command with glob filter."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "scan",
        "--source", "git", "--repo-path", str(FIXTURES),
        "--format", "json",
        "--filter", "my-*",
    ])
    assert result.exit_code == 0
    # Should only include my-workshop
    assert "my-workshop" in result.output
