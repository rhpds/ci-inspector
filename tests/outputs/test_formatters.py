import json
from decimal import Decimal

from ci_inspector.core.models import Component, DiscoveryResult, Finding
from ci_inspector.outputs.json_fmt import format_json
from ci_inspector.outputs.table_fmt import format_table, format_summary, format_validation, group_results
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


class TestFormatSummary:
    def _make_result(self, **kwargs) -> DiscoveryResult:
        defaults = dict(
            ci_name="test-lab", stage="prod", components=[], is_binder=False,
        )
        defaults.update(kwargs)
        return DiscoveryResult(**defaults)

    def test_basic_summary(self):
        result = self._make_result(components=[
            Component(purpose="bastion", instance_type="t3a.medium",
                      count=1, cpu=2, memory_gb=4, storage_gb=0,
                      cloud="AWS", os="Linux"),
            Component(purpose="worker", instance_type="m5.xlarge",
                      count=3, cpu=4, memory_gb=16, storage_gb=0,
                      cloud="AWS", os="RHCOS"),
        ])
        summary = format_summary(result)
        assert "test-lab (prod)" in summary
        assert "14 vCPUs" in summary
        assert "52 GB RAM" in summary
        assert "4 instances" in summary

    def test_summary_with_cost(self):
        result = self._make_result(components=[
            Component(purpose="worker", instance_type="m5.xlarge",
                      count=2, cpu=4, memory_gb=16, storage_gb=0,
                      cloud="AWS", os="RHCOS",
                      cost_per_hour=Decimal("0.192")),
        ])
        summary = format_summary(result)
        assert "$0.38/hr" in summary

    def test_summary_without_cost(self):
        result = self._make_result(components=[
            Component(purpose="worker", instance_type="m5.xlarge",
                      count=1, cpu=4, memory_gb=16, storage_gb=0,
                      cloud="AWS", os="RHCOS"),
        ])
        summary = format_summary(result)
        assert "/hr" not in summary

    def test_summary_empty_components(self):
        result = self._make_result(components=[])
        summary = format_summary(result)
        assert "0 vCPUs" in summary
        assert "0 instances" in summary


class TestFormatValidation:
    def test_no_findings(self):
        output = format_validation([])
        assert "All checks passed" in output

    def test_warning_findings(self):
        findings = [
            Finding(severity="warning", rule="max_vcpus",
                    message="80 vCPUs (AWS) exceeds threshold of 64",
                    actual_value="80 vCPUs", threshold_value="64 vCPUs"),
        ]
        output = format_validation(findings)
        assert "[!]" in output
        assert "80 vCPUs" in output

    def test_info_findings(self):
        findings = [
            Finding(severity="info", rule="flagged_instance_type",
                    message="p3.2xlarge: GPU instance",
                    actual_value="p3.2xlarge", threshold_value="p3.*"),
        ]
        output = format_validation(findings)
        assert "[i]" in output

    def test_mixed_findings(self):
        findings = [
            Finding(severity="warning", rule="max_vcpus",
                    message="80 vCPUs exceeds 64",
                    actual_value="80", threshold_value="64"),
            Finding(severity="info", rule="flagged_instance_type",
                    message="GPU instance",
                    actual_value="p3.2xlarge", threshold_value="p3.*"),
        ]
        output = format_validation(findings)
        assert "[!]" in output
        assert "[i]" in output


class TestGroupResults:
    def _make_result(self, stage, components=None) -> DiscoveryResult:
        if components is None:
            components = [
                Component(purpose="bastion", instance_type="t3a.medium",
                          count=1, cpu=2, memory_gb=4, storage_gb=0,
                          cloud="AWS", os="Linux"),
            ]
        return DiscoveryResult(
            ci_name="test-lab", stage=stage, components=components,
            is_binder=False, env_type="ocp4-cluster", cloud_provider="AWS",
        )

    def test_all_identical(self):
        results = [self._make_result(s) for s in ("dev", "event", "prod")]
        groups = group_results(results)
        assert len(groups) == 1
        stages, _ = groups[0]
        assert stages == ["dev", "event", "prod"]

    def test_all_different(self):
        r1 = self._make_result("dev", [
            Component(purpose="bastion", instance_type="t3a.medium",
                      count=1, cpu=2, memory_gb=4, storage_gb=0,
                      cloud="AWS", os="Linux"),
        ])
        r2 = self._make_result("prod", [
            Component(purpose="bastion", instance_type="m5.xlarge",
                      count=1, cpu=4, memory_gb=16, storage_gb=0,
                      cloud="AWS", os="Linux"),
        ])
        groups = group_results([r1, r2])
        assert len(groups) == 2
        assert groups[0][0] == ["dev"]
        assert groups[1][0] == ["prod"]

    def test_partial_match(self):
        small = [
            Component(purpose="bastion", instance_type="t3a.medium",
                      count=1, cpu=2, memory_gb=4, storage_gb=0,
                      cloud="AWS", os="Linux"),
        ]
        big = [
            Component(purpose="bastion", instance_type="m5.xlarge",
                      count=1, cpu=4, memory_gb=16, storage_gb=0,
                      cloud="AWS", os="Linux"),
        ]
        results = [
            self._make_result("dev", small),
            self._make_result("event", small),
            self._make_result("prod", big),
        ]
        groups = group_results(results)
        assert len(groups) == 2
        assert groups[0][0] == ["dev", "event"]
        assert groups[1][0] == ["prod"]

    def test_single_result(self):
        groups = group_results([self._make_result("prod")])
        assert len(groups) == 1
        assert groups[0][0] == ["prod"]


class TestGroupedFormatting:
    def _make_result(self, stage) -> DiscoveryResult:
        return DiscoveryResult(
            ci_name="test-lab", stage=stage,
            components=[
                Component(purpose="bastion", instance_type="t3a.medium",
                          count=1, cpu=2, memory_gb=4, storage_gb=0,
                          cloud="AWS", os="Linux"),
            ],
            is_binder=False, env_type="ocp4-cluster", cloud_provider="AWS",
        )

    def test_summary_with_multiple_stages(self):
        result = self._make_result("dev")
        summary = format_summary(result, stages=["dev", "event", "prod"])
        assert "test-lab (dev, event, prod)" in summary

    def test_summary_single_stage_unchanged(self):
        result = self._make_result("prod")
        summary = format_summary(result)
        assert "test-lab (prod)" in summary

    def test_table_with_multiple_stages(self):
        result = self._make_result("dev")
        output = format_table(result, stages=["dev", "event"])
        assert "dev, event" in output

    def test_table_single_stage_unchanged(self):
        result = self._make_result("prod")
        output = format_table(result)
        assert "(prod)" in output
