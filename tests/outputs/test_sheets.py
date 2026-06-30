from decimal import Decimal

from ci_inspector.core.models import Component, DiscoveryResult
from ci_inspector.outputs.sheets import build_row, TOOL_COLUMNS


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


def test_tool_columns():
    assert TOOL_COLUMNS == [
        "CI Name", "Stage", "Env Type", "Components",
        "vCPUs", "Memory (GB)", "Storage (GB)", "Cost/hr", "Last Updated",
    ]


def test_build_row():
    row = build_row(_sample_result())
    assert row[0] == "my-workshop"
    assert row[1] == "prod"
    assert row[4] == 14  # total vCPUs
    assert row[5] == 52  # total memory
    assert "1x bastion" in row[3]
    assert "3x worker" in row[3]


def test_build_row_components_format():
    row = build_row(_sample_result())
    assert "t3a.medium" in row[3]
    assert "m5.xlarge" in row[3]
