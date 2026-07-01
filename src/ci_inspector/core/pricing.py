from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path

from ci_inspector.core.models import Component, PricingConfig

logger = logging.getLogger(__name__)

_CNV_TYPE_RE = re.compile(r"^(\d+)_core_(\d+)Gi$")

_BUNDLED_PRICING_DB = Path(__file__).parent.parent.parent.parent / "data" / "pricing.json"

_pricing_db_cache: dict | None = None


def _load_pricing_db(pricing_json_path: str | None = None) -> dict:
    global _pricing_db_cache
    if _pricing_db_cache is not None and pricing_json_path is None:
        return _pricing_db_cache

    path = Path(pricing_json_path) if pricing_json_path else _BUNDLED_PRICING_DB
    if not path.is_file():
        logger.warning("Pricing database not found: %s", path)
        return {}

    with path.open() as f:
        db = json.load(f)

    if pricing_json_path is None:
        _pricing_db_cache = db
    return db


def load_cnv_rates(pricing_json_path: str | None = None) -> PricingConfig:
    db = _load_pricing_db(pricing_json_path)
    rates = db.get("cnv_rates", {})
    return PricingConfig(
        cnv_cpu_per_core_per_hour=Decimal(rates.get("cpu_per_core_per_hour", "0")),
        cnv_memory_per_gb_per_hour=Decimal(rates.get("memory_per_gb_per_hour", "0")),
        cnv_storage_per_gb_per_hour=Decimal(rates.get("storage_per_gb_per_hour", "0")),
    )


def parse_cnv_instance_type(name: str) -> tuple[int, int] | None:
    if not name:
        return None
    m = _CNV_TYPE_RE.match(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def calculate_cnv_hourly_rate(
    cpu: int, memory_gb: int, storage_gb: int, config: PricingConfig,
) -> Decimal:
    return (
        Decimal(cpu) * config.cnv_cpu_per_core_per_hour
        + Decimal(memory_gb) * config.cnv_memory_per_gb_per_hour
        + Decimal(storage_gb) * config.cnv_storage_per_gb_per_hour
    )


def _lookup_instance_spec(
    cloud: str, instance_type: str, region: str, pricing_json_path: str | None = None,
) -> tuple[int, int, Decimal | None]:
    """Look up (vcpu, memory_gb, cost_per_hour) from the pricing database."""
    db = _load_pricing_db(pricing_json_path)
    cloud_types = db.get("instance_types", {}).get(cloud, {})
    entry = cloud_types.get(instance_type)
    if not entry:
        return 0, 0, None

    vcpu = entry.get("vcpu", 0)
    memory_gb = entry.get("memory_gb", 0)

    regions = entry.get("regions", {})
    cost = regions.get(region) or regions.get(next(iter(regions), ""), None)
    cost_decimal = Decimal(cost) if cost else None

    return vcpu, memory_gb, cost_decimal


async def resolve_pricing(
    components: list[Component],
    config: PricingConfig,
    pricing_json_path: str | None = None,
) -> list[Component]:
    updated: list[Component] = []

    def _is_cloud(cloud: str, *names: str) -> bool:
        norm = cloud.lower() if cloud else ""
        return norm in names or cloud in names

    def _cloud_key(cloud: str) -> str:
        norm = cloud.lower() if cloud else ""
        if norm in ("aws", "ec2"):
            return "AWS"
        if norm in ("azure",):
            return "Azure"
        if norm in ("gcp",):
            return "GCP"
        if norm in ("equinix",):
            return "Equinix"
        if norm in ("ibm",):
            return "IBM"
        return cloud

    for c in components:
        if c.cost_per_hour is not None and c.cpu > 0:
            updated.append(c)
            continue

        is_cnv = _is_cloud(c.cloud, "cnv", "openshift_cnv", "osp", "none") or "_core_" in (c.instance_type or "")

        price = c.cost_per_hour
        vcpu = c.cpu
        mem = c.memory_gb

        if is_cnv:
            cnv = parse_cnv_instance_type(c.instance_type)
            if cnv:
                vcpu = vcpu or cnv[0]
                mem = mem or cnv[1]
                if not price:
                    price = calculate_cnv_hourly_rate(cnv[0], cnv[1], c.storage_gb, config)
            elif c.cpu and c.memory_gb and not price:
                price = calculate_cnv_hourly_rate(c.cpu, c.memory_gb, c.storage_gb, config)
        else:
            cloud_key = _cloud_key(c.cloud)
            region = config.aws_region if cloud_key == "AWS" else config.azure_region if cloud_key == "Azure" else config.gcp_region
            db_vcpu, db_mem, db_price = _lookup_instance_spec(
                cloud_key, c.instance_type, region, pricing_json_path
            )
            vcpu = vcpu or db_vcpu
            mem = mem or db_mem
            if not price:
                price = db_price

        updates: dict = {}
        if price != c.cost_per_hour:
            updates["cost_per_hour"] = price
        if vcpu != c.cpu:
            updates["cpu"] = vcpu
        if mem != c.memory_gb:
            updates["memory_gb"] = mem

        updated.append(c.model_copy(update=updates) if updates else c)

    return updated
