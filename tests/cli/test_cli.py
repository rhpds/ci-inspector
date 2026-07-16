from pathlib import Path

from click.testing import CliRunner

from ci_inspector.cli import main

FIXTURES = Path(__file__).parent.parent / "fixtures" / "agnosticv"
MERGED_FIXTURES = Path(__file__).parent.parent / "fixtures" / "merged"


class TestAnalyze:
    def test_json_output(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "analyze", "my-workshop", "--source", "git",
            "--repo-path", str(FIXTURES), "--format", "json",
        ])
        assert result.exit_code == 0
        assert '"ci_name": "my-workshop"' in result.output

    def test_table_output(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "analyze", "my-workshop", "--source", "git",
            "--repo-path", str(FIXTURES), "--format", "table",
        ])
        assert result.exit_code == 0
        assert "my-workshop" in result.output

    def test_not_found(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "analyze", "nonexistent", "--source", "git",
            "--repo-path", str(FIXTURES),
        ])
        assert result.exit_code == 2


class TestCheck:
    def test_clean_check(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "check", "my-workshop", "--source", "git",
            "--repo-path", str(FIXTURES),
        ])
        # Should pass (small lab, under defaults)
        assert result.exit_code == 0

    def test_annotations_format(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "check", "my-workshop", "--source", "git",
            "--repo-path", str(FIXTURES),
            "--format", "annotations",
        ])
        assert result.exit_code == 0


class TestDetectChanges:
    def test_detect_changes_requires_base(self):
        runner = CliRunner()
        result = runner.invoke(main, ["detect-changes"])
        assert result.exit_code != 0  # missing --base


class TestValidateMerged:
    def test_table_output_always_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "validate-merged",
            str(MERGED_FIXTURES / "my-workshop-prod.yml"),
            "--ci-name", "my-workshop",
            "--stage", "prod",
        ])
        assert result.exit_code == 0
        assert "Summary" in result.output
        assert "Validation:" in result.output

    def test_exits_zero_even_with_violations(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "validate-merged",
            str(MERGED_FIXTURES / "large-lab-prod.yml"),
            "--ci-name", "large-lab",
            "--stage", "prod",
        ])
        assert result.exit_code == 0
        assert "[!]" in result.output

    def test_no_error_severity_in_output(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "validate-merged",
            str(MERGED_FIXTURES / "large-lab-prod.yml"),
            "--ci-name", "large-lab",
            "--stage", "prod",
        ])
        assert result.exit_code == 0
        assert "[X]" not in result.output

    def test_json_format(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "validate-merged",
            str(MERGED_FIXTURES / "my-workshop-prod.yml"),
            "--ci-name", "my-workshop",
            "--format", "json",
        ])
        assert result.exit_code == 0
        import json
        data = json.loads(result.output)
        assert "summary" in data
        assert "total_vcpus" in data["summary"]
        assert "findings" in data

    def test_annotations_format_no_errors(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "validate-merged",
            str(MERGED_FIXTURES / "large-lab-prod.yml"),
            "--ci-name", "large-lab",
            "--stage", "prod",
            "--format", "annotations",
        ])
        assert result.exit_code == 0
        assert "::error" not in result.output
        assert "::warning" in result.output or "::notice" in result.output or result.output.strip() == ""

    def test_stdin_input(self):
        fixture = (MERGED_FIXTURES / "my-workshop-prod.yml").read_text()
        runner = CliRunner()
        result = runner.invoke(main, [
            "validate-merged", "-",
            "--ci-name", "my-workshop",
            "--stage", "prod",
        ], input=fixture)
        assert result.exit_code == 0
        assert "Summary" in result.output

    def test_infers_ci_name_from_path(self):
        runner = CliRunner()
        result = runner.invoke(main, [
            "validate-merged",
            str(MERGED_FIXTURES / "my-workshop-prod.yml"),
        ])
        assert result.exit_code == 0
