from pathlib import Path

from click.testing import CliRunner

from ci_inspector.cli import main

FIXTURES = Path(__file__).parent.parent / "fixtures" / "agnosticv"


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
