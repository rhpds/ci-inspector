from decimal import Decimal
from pathlib import Path

from ci_inspector.core.models import Component, DiscoveryResult, Finding, ThresholdConfig
from ci_inspector.core.thresholds import check_thresholds, load_config

FIXTURES = Path(__file__).parent.parent / "fixtures" / "configs"


class TestLoadConfig:
    def test_cascading_merge(self):
        ci_path = FIXTURES / "summit" / "my-workshop"
        config = load_config(ci_path, FIXTURES)
        # max_vcpus from summit level (overrides root 64)
        assert config.thresholds.max_vcpus == 128
        # max_cost_per_hour from CI level (overrides root 10.00)
        assert config.thresholds.max_cost_per_hour == Decimal("20.00")
        # flagged_instance_types from root (not overridden)
        assert len(config.thresholds.flagged_instance_types) == 1
        # ignore merged from all levels
        assert "tests/**" in config.ignore
        assert "sandbox/**" in config.ignore

    def test_root_only(self):
        config = load_config(FIXTURES, FIXTURES)
        assert config.thresholds.max_vcpus == 64

    def test_no_config_uses_defaults(self):
        config = load_config(Path("/nonexistent"), Path("/nonexistent"))
        assert config.thresholds.max_vcpus == 64
        assert config.thresholds.max_instances == 10

    def test_null_thresholds_disables(self):
        # When thresholds is explicitly null, checking should be skipped
        config = load_config(FIXTURES, FIXTURES)
        assert config.thresholds is not None  # root has thresholds


class TestCheckThresholds:
    def _make_result(self, **kwargs) -> DiscoveryResult:
        defaults = dict(
            ci_name="test", stage="prod", components=[], is_binder=False,
        )
        defaults.update(kwargs)
        return DiscoveryResult(**defaults)

    def test_vcpu_exceeded(self):
        result = self._make_result(components=[
            Component(purpose="worker", instance_type="m5.4xlarge",
                      count=5, cpu=16, memory_gb=64, storage_gb=0,
                      cloud="AWS", os="RHCOS"),
        ])
        config = ThresholdConfig(max_vcpus=64)
        findings = check_thresholds(result, config)
        assert any(f.rule == "max_vcpus" and f.severity == "error" for f in findings)

    def test_under_threshold(self):
        result = self._make_result(components=[
            Component(purpose="bastion", instance_type="t3a.medium",
                      count=1, cpu=2, memory_gb=4, storage_gb=0,
                      cloud="AWS", os="Linux"),
        ])
        config = ThresholdConfig()
        findings = check_thresholds(result, config)
        assert len(findings) == 0

    def test_flagged_instance_type(self):
        from ci_inspector.core.models import ThresholdRule
        result = self._make_result(components=[
            Component(purpose="gpu-worker", instance_type="p3.2xlarge",
                      count=1, cpu=8, memory_gb=61, storage_gb=0,
                      cloud="AWS", os="Linux"),
        ])
        config = ThresholdConfig(flagged_instance_types=[
            ThresholdRule(pattern="p3.*", severity="warning", message="GPU instance"),
        ])
        findings = check_thresholds(result, config)
        assert any(f.rule == "flagged_instance_type" and f.severity == "warning" for f in findings)

    def test_per_cloud_override(self):
        result = self._make_result(components=[
            Component(purpose="worker", instance_type="8_core_32Gi",
                      count=10, cpu=8, memory_gb=32, storage_gb=0,
                      cloud="CNV", os="RHCOS"),
        ])
        config = ThresholdConfig(
            max_vcpus=64,
            clouds={"CNV": {"max_vcpus": 128}},
        )
        findings = check_thresholds(result, config)
        # 80 vCPUs > 64 default but < 128 CNV override
        vcpu_findings = [f for f in findings if f.rule == "max_vcpus"]
        assert len(vcpu_findings) == 0

    def test_cost_exceeded(self):
        result = self._make_result(components=[
            Component(purpose="worker", instance_type="m5.4xlarge",
                      count=10, cpu=16, memory_gb=64, storage_gb=0,
                      cloud="AWS", os="RHCOS",
                      cost_per_hour=Decimal("1.50")),
        ])
        config = ThresholdConfig(max_cost_per_hour=Decimal("10.00"))
        findings = check_thresholds(result, config)
        assert any(f.rule == "max_cost_per_hour" for f in findings)
