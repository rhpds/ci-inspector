from decimal import Decimal

from ci_inspector.core.models import (
    CatalogItemData,
    Component,
    DiscoveryResult,
    Finding,
    ThresholdConfig,
    ThresholdRule,
)


def test_component_defaults():
    c = Component(
        purpose="bastion",
        instance_type="t3a.medium",
        count=1,
        cpu=2,
        memory_gb=4,
        storage_gb=0,
        cloud="AWS",
        os="Linux",
    )
    assert c.cost_per_hour is None
    assert c.group is None
    assert c.count_formula is None
    assert c.warnings == []


def test_discovery_result_computed_totals():
    r = DiscoveryResult(
        ci_name="my-lab",
        stage="prod",
        components=[
            Component(
                purpose="bastion", instance_type="t3a.medium",
                count=1, cpu=2, memory_gb=4, storage_gb=0,
                cloud="AWS", os="Linux", cost_per_hour=Decimal("0.038"),
            ),
            Component(
                purpose="worker", instance_type="m5.xlarge",
                count=3, cpu=4, memory_gb=16, storage_gb=0,
                cloud="AWS", os="RHCOS", cost_per_hour=Decimal("0.192"),
            ),
        ],
        is_binder=False,
    )
    assert r.total_vcpus == 14  # 2*1 + 4*3
    assert r.total_memory_gb == 52  # 4*1 + 16*3
    assert r.total_cost_per_hour == Decimal("0.614")  # 0.038 + 0.192*3


def test_discovery_result_no_pricing():
    r = DiscoveryResult(
        ci_name="my-lab",
        stage="prod",
        components=[
            Component(
                purpose="worker", instance_type="m5.xlarge",
                count=2, cpu=4, memory_gb=16, storage_gb=0,
                cloud="AWS", os="RHCOS",
            ),
        ],
        is_binder=False,
    )
    assert r.total_vcpus == 8
    assert r.total_cost_per_hour is None


def test_finding_model():
    f = Finding(
        severity="warning",
        rule="max_vcpus",
        message="96 vCPUs exceeds threshold of 64",
        actual_value="96 vCPUs",
        threshold_value="64 vCPUs",
    )
    assert f.severity == "warning"


def test_threshold_config_defaults():
    cfg = ThresholdConfig()
    assert cfg.max_vcpus == 64
    assert cfg.max_memory_gb == 256
    assert cfg.max_instances == 10
    assert cfg.max_cost_per_hour == Decimal("10.00")
    assert cfg.max_storage_gb == 2000
    assert cfg.clouds == {}
    assert cfg.flagged_instance_types == []


def test_catalog_item_data():
    ci = CatalogItemData(
        ci_name="my-workshop",
        stage="prod",
        display_name="My Workshop",
    )
    assert ci.definition == {}
    assert ci.linked_components == []
    assert ci.parameters == {}
    assert ci.meta == {}


def test_threshold_rule():
    rule = ThresholdRule(
        pattern="p3.*",
        severity="warning",
        message="GPU instance",
    )
    assert rule.matches("p3.2xlarge")
    assert rule.matches("p3.8xlarge")
    assert not rule.matches("m5.xlarge")
    assert not rule.matches("p38xlarge")


def test_threshold_rule_wildcard_prefix():
    rule = ThresholdRule(pattern="*.metal", severity="error", message="No bare metal")
    assert rule.matches("i3.metal")
    assert rule.matches("c5.metal")
    assert not rule.matches("m5.xlarge")
