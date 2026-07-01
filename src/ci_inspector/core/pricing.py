from __future__ import annotations

import logging
import re
from decimal import Decimal

import httpx

from ci_inspector.core.models import Component, PricingConfig

logger = logging.getLogger(__name__)

_CNV_TYPE_RE = re.compile(r"^(\d+)_core_(\d+)Gi$")


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


async def fetch_aws_pricing(
    instance_types: list[str], region: str, os_billing: str = "Linux",
) -> dict[str, Decimal]:
    url = (
        f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/"
        f"{region}/index.json"
    )
    results: dict[str, Decimal] = {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return results
            data = resp.json()
            products = data.get("products", {})
            terms = data.get("terms", {}).get("OnDemand", {})
            wanted = set(instance_types)

            for sku, product in products.items():
                attrs = product.get("attributes", {})
                itype = attrs.get("instanceType", "")
                if (
                    itype in wanted
                    and attrs.get("operatingSystem") == os_billing
                    and attrs.get("tenancy") == "Shared"
                    and attrs.get("preInstalledSw") == "NA"
                    and attrs.get("capacitystatus") == "Used"
                ):
                    for term in terms.get(sku, {}).values():
                        for dim in term.get("priceDimensions", {}).values():
                            price = dim.get("pricePerUnit", {}).get("USD")
                            if price and itype not in results:
                                results[itype] = Decimal(price)
                                wanted.discard(itype)
    except Exception as e:
        logger.error("AWS pricing error: %s", e)
    return results


async def fetch_azure_pricing(
    instance_types: list[str], region: str, os_billing: str = "Linux",
) -> dict[str, Decimal]:
    results: dict[str, Decimal] = {}
    os_name = "Windows" if os_billing == "Windows" else "Linux"
    for itype in instance_types:
        url = (
            f"https://prices.azure.com/api/retail/prices?"
            f"$filter=armSkuName eq '{itype}' and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption' and contains(meterName, 'Spot') eq false "
            f"and contains(productName, '{os_name}')"
        )
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    items = resp.json().get("Items", [])
                    for item in items:
                        price = item.get("retailPrice", 0)
                        if price > 0:
                            results[itype] = Decimal(str(price))
                            break
        except Exception:
            pass
    return results


async def resolve_pricing(
    components: list[Component], config: PricingConfig,
) -> list[Component]:
    updated: list[Component] = []

    def _is_cloud(cloud: str, *names: str) -> bool:
        norm = cloud.lower() if cloud else ""
        return norm in names or cloud in names

    aws_types = [c.instance_type for c in components
                 if _is_cloud(c.cloud, "aws", "ec2", "AWS") and not c.cost_per_hour]
    azure_types = [c.instance_type for c in components
                   if _is_cloud(c.cloud, "azure", "Azure") and not c.cost_per_hour]

    aws_prices = await fetch_aws_pricing(aws_types, config.aws_region) if aws_types else {}
    azure_prices = await fetch_azure_pricing(azure_types, config.azure_region) if azure_types else {}

    for c in components:
        if c.cost_per_hour is not None:
            updated.append(c)
            continue

        price = None
        is_cnv = _is_cloud(c.cloud, "cnv", "openshift_cnv", "osp", "none") or "_core_" in (c.instance_type or "")

        if is_cnv:
            cnv = parse_cnv_instance_type(c.instance_type)
            if cnv:
                price = calculate_cnv_hourly_rate(cnv[0], cnv[1], c.storage_gb, config)
            elif c.cpu and c.memory_gb:
                price = calculate_cnv_hourly_rate(c.cpu, c.memory_gb, c.storage_gb, config)
        elif _is_cloud(c.cloud, "aws", "ec2", "AWS"):
            price = aws_prices.get(c.instance_type)
        elif _is_cloud(c.cloud, "azure", "Azure"):
            price = azure_prices.get(c.instance_type)

        updated.append(c.model_copy(update={"cost_per_hour": price}))

    return updated
