from __future__ import annotations

import fnmatch
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, computed_field


class Component(BaseModel):
    purpose: str
    instance_type: str
    count: int
    cpu: int
    memory_gb: int
    storage_gb: int
    cloud: str
    os: str
    cost_per_hour: Decimal | None = None
    group: str | None = None
    group_display_name: str | None = None
    group_ci_name: str | None = None
    count_formula: str | None = None
    instance_type_formula: str | None = None
    os_billing: str = "Linux"
    warnings: list[str] = []


class DiscoveryResult(BaseModel):
    ci_name: str
    stage: str
    components: list[Component]
    is_binder: bool
    env_type: str | None = None
    cloud_provider: str | None = None
    showroom_url: str | None = None
    showroom_ref: str | None = None
    persistent: bool | None = None
    can_be_stopped: bool | None = None
    uses_maas: bool | None = None
    maas_models: list[str] | None = None
    warnings: list[str] = []
    errors: list[str] = []

    @computed_field
    @property
    def total_vcpus(self) -> int:
        return sum(c.cpu * c.count for c in self.components)

    @computed_field
    @property
    def total_memory_gb(self) -> int:
        return sum(c.memory_gb * c.count for c in self.components)

    @computed_field
    @property
    def total_cost_per_hour(self) -> Decimal | None:
        costs = [c.cost_per_hour * c.count for c in self.components if c.cost_per_hour is not None]
        return sum(costs, Decimal("0")) if costs else None


class Finding(BaseModel):
    severity: Literal["error", "warning", "info"]
    rule: str
    message: str
    actual_value: str
    threshold_value: str


class ThresholdRule(BaseModel):
    pattern: str
    severity: Literal["error", "warning", "info"] = "warning"
    message: str = ""

    def matches(self, instance_type: str) -> bool:
        return fnmatch.fnmatch(instance_type, self.pattern)


class ThresholdConfig(BaseModel):
    max_vcpus: int = 64
    max_memory_gb: int = 256
    max_instances: int = 10
    max_cost_per_hour: Decimal = Decimal("10.00")
    max_storage_gb: int = 2000
    clouds: dict[str, dict] = {}
    flagged_instance_types: list[ThresholdRule] = []
    ignore: list[str] = []


class PricingConfig(BaseModel):
    cnv_cpu_per_core_per_hour: Decimal = Decimal("0.0969377605")
    cnv_memory_per_gb_per_hour: Decimal = Decimal("0.0036485604")
    cnv_storage_per_gb_per_hour: Decimal = Decimal("0.0000925493")
    aws_region: str = "us-east-1"
    azure_region: str = "eastus"
    gcp_region: str = "us-east1"


class InspectorConfig(BaseModel):
    thresholds: ThresholdConfig | None = ThresholdConfig()
    pricing: PricingConfig = PricingConfig()
    ignore: list[str] = []


class CatalogItemData(BaseModel):
    ci_name: str
    stage: str
    display_name: str = ""
    definition: dict = {}
    linked_components: list[dict] = []
    resources: list[dict] = []
    parameters: dict = {}
    meta: dict = {}
    agnosticv_repo: str = ""
    agnosticv_dir: str = ""
    namespace: str = ""
