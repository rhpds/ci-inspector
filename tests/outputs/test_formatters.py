import json
from decimal import Decimal

from ci_inspector.core.models import Component, DiscoveryResult, Finding
from ci_inspector.outputs.json_fmt import format_json
from ci_inspector.outputs.table_fmt import format_table
from ci_inspector.outputs.annotations_fmt import format_annotations


def _sample_result() -> DiscoveryResult:
    return DiscoveryResult(
        ci_name="my-workshop", stage="prod",
        components=[
            Component(purpose="bastion", instance_type="t3a.medium",
                      count=1, cpu=2, memory_gb=4, storage_gb=0,
                      cloud="AWS", os="Linux", cost_per_hour=Decimal("0.038")),
            Component(purpose="worker", instance_type="m5.xlarge",
                      count=3, cpu=4, memory_gb=16, storage_gb=0,
                      cloud="AWS", os="RHCOS", cost_per_hour=Decimal("0.192")),
        ],
        is_binder=False, env_type="ocp4-cluster", cloud_provider="AWS",
    )


def _sample_findings() -> list[Finding]:
    return [
        Finding(severity="warning", rule="max_vcpus",
                message="96 vCPUs exceeds threshold of 64",
                actual_value="96 vCPUs", threshold_value="64 vCPUs"),
    ]


class TestJsonFormatter:
    def test_valid_json(self):
        output = format_json(_sample_result(), [])
        data = json.loads(output)
        assert data["ci_name"] == "my-workshop"
        assert len(data["components"]) == 2
        assert data["total_vcpus"] == 14

    def test_with_findings(self):
        output = format_json(_sample_result(), _sample_findings())
        data = json.loads(output)
        assert len(data["findings"]) == 1


class TestTableFormatter:
    def test_contains_ci_name(self):
        output = format_table(_sample_result())
        assert "my-workshop" in output

    def test_contains_instance_types(self):
        output = format_table(_sample_result())
        assert "t3a.medium" in output
        assert "m5.xlarge" in output

    def test_contains_totals(self):
        output = format_table(_sample_result())
        assert "14" in output  # total vCPUs


class TestAnnotationsFormatter:
    def test_github_format(self):
        output = format_annotations(_sample_findings(), "summit/my-workshop/prod.yml")
        assert "::warning file=summit/my-workshop/prod.yml" in output
        assert "96 vCPUs exceeds threshold of 64" in output

    def test_error_severity(self):
        findings = [Finding(severity="error", rule="max_vcpus",
                            message="Too many", actual_value="100",
                            threshold_value="64")]
        output = format_annotations(findings, "test.yml")
        assert "::error file=test.yml" in output
