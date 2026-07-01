from decimal import Decimal

import pytest

from ci_inspector.core.models import Component, PricingConfig
from ci_inspector.core.pricing import (
    calculate_cnv_hourly_rate,
    load_cnv_rates,
    parse_cnv_instance_type,
    resolve_pricing,
)


class TestParseCnvInstanceType:
    def test_standard_format(self):
        result = parse_cnv_instance_type("4_core_14Gi")
        assert result == (4, 14)

    def test_large(self):
        result = parse_cnv_instance_type("16_core_64Gi")
        assert result == (16, 64)

    def test_not_cnv(self):
        assert parse_cnv_instance_type("m5.xlarge") is None

    def test_empty(self):
        assert parse_cnv_instance_type("") is None


class TestLoadCnvRates:
    def test_loads_from_bundled_json(self):
        config = load_cnv_rates()
        assert config.cnv_cpu_per_core_per_hour == Decimal("0.0969377605")
        assert config.cnv_memory_per_gb_per_hour == Decimal("0.0036485604")
        assert config.cnv_storage_per_gb_per_hour == Decimal("0.0000925493")

    def test_nonexistent_file_uses_model_defaults(self):
        # When a custom path doesn't exist, load_cnv_rates returns model defaults (zero)
        config = load_cnv_rates("/nonexistent/path.json")
        assert config.cnv_cpu_per_core_per_hour == Decimal("0")
        assert config.cnv_memory_per_gb_per_hour == Decimal("0")


class TestCalculateCnvHourlyRate:
    def test_with_loaded_rates(self):
        config = load_cnv_rates()
        rate = calculate_cnv_hourly_rate(4, 16, 0, config)
        expected = Decimal("4") * Decimal("0.0969377605") + Decimal("16") * Decimal("0.0036485604")
        assert rate == expected

    def test_with_storage(self):
        config = load_cnv_rates()
        rate = calculate_cnv_hourly_rate(4, 16, 100, config)
        expected = (
            Decimal("4") * Decimal("0.0969377605")
            + Decimal("16") * Decimal("0.0036485604")
            + Decimal("100") * Decimal("0.0000925493")
        )
        assert rate == expected

    def test_custom_rates(self):
        config = PricingConfig(
            cnv_cpu_per_core_per_hour=Decimal("0.10"),
            cnv_memory_per_gb_per_hour=Decimal("0.01"),
        )
        rate = calculate_cnv_hourly_rate(4, 16, 0, config)
        expected = Decimal("4") * Decimal("0.10") + Decimal("16") * Decimal("0.01")
        assert rate == expected


class TestResolvePricing:
    @pytest.mark.asyncio
    async def test_cnv_pricing_from_instance_type(self):
        config = load_cnv_rates()
        components = [
            Component(
                purpose="bastion", instance_type="4_core_16Gi",
                count=1, cpu=4, memory_gb=16, storage_gb=50,
                cloud="CNV", os="Linux",
            ),
        ]
        result = await resolve_pricing(components, config)
        assert len(result) == 1
        assert result[0].cost_per_hour is not None
        assert result[0].cost_per_hour > Decimal("0")

    @pytest.mark.asyncio
    async def test_cnv_pricing_from_cpu_memory(self):
        config = load_cnv_rates()
        components = [
            Component(
                purpose="worker", instance_type="custom",
                count=1, cpu=8, memory_gb=32, storage_gb=0,
                cloud="CNV", os="Linux",
            ),
        ]
        result = await resolve_pricing(components, config)
        assert result[0].cost_per_hour is not None

    @pytest.mark.asyncio
    async def test_skip_already_priced_components(self):
        config = load_cnv_rates()
        existing_price = Decimal("1.23")
        components = [
            Component(
                purpose="bastion", instance_type="m5.xlarge",
                count=1, cpu=4, memory_gb=16, storage_gb=0,
                cloud="AWS", os="Linux", cost_per_hour=existing_price,
            ),
        ]
        result = await resolve_pricing(components, config)
        assert result[0].cost_per_hour == existing_price

    @pytest.mark.asyncio
    async def test_aws_pricing_from_json(self):
        config = load_cnv_rates()
        components = [
            Component(
                purpose="bastion", instance_type="t3a.medium",
                count=1, cpu=0, memory_gb=0, storage_gb=0,
                cloud="AWS", os="Linux",
            ),
        ]
        result = await resolve_pricing(components, config)
        assert result[0].cost_per_hour is not None
        assert result[0].cpu == 2
        assert result[0].memory_gb == 4

    @pytest.mark.asyncio
    async def test_aws_specs_populated(self):
        config = load_cnv_rates()
        components = [
            Component(
                purpose="worker", instance_type="m5.xlarge",
                count=3, cpu=0, memory_gb=0, storage_gb=0,
                cloud="AWS", os="Linux",
            ),
        ]
        result = await resolve_pricing(components, config)
        assert result[0].cpu == 4
        assert result[0].memory_gb == 16

    @pytest.mark.asyncio
    async def test_mixed_clouds(self):
        config = load_cnv_rates()
        components = [
            Component(
                purpose="bastion", instance_type="4_core_16Gi",
                count=1, cpu=4, memory_gb=16, storage_gb=0,
                cloud="CNV", os="Linux",
            ),
            Component(
                purpose="master", instance_type="m5.xlarge",
                count=1, cpu=0, memory_gb=0, storage_gb=0,
                cloud="AWS", os="Linux",
            ),
        ]
        result = await resolve_pricing(components, config)
        assert len(result) == 2
        assert result[0].cost_per_hour is not None  # CNV
        assert result[1].cost_per_hour is not None  # AWS from JSON
        assert result[1].cpu == 4  # specs populated

    @pytest.mark.asyncio
    async def test_missing_pricing(self):
        config = load_cnv_rates()
        components = [
            Component(
                purpose="bastion", instance_type="nonexistent.99xlarge",
                count=1, cpu=0, memory_gb=0, storage_gb=0,
                cloud="AWS", os="Linux",
            ),
        ]
        result = await resolve_pricing(components, config)
        assert result[0].cost_per_hour is None
        assert result[0].cpu == 0

    @pytest.mark.asyncio
    async def test_openshift_cnv_cloud_matched(self):
        config = load_cnv_rates()
        components = [
            Component(
                purpose="bastion", instance_type="2_core_4Gi",
                count=1, cpu=2, memory_gb=4, storage_gb=0,
                cloud="openshift_cnv", os="Linux",
            ),
        ]
        result = await resolve_pricing(components, config)
        assert result[0].cost_per_hour is not None
