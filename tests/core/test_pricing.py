from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from ci_inspector.core.models import Component, PricingConfig
from ci_inspector.core.pricing import (
    calculate_cnv_hourly_rate,
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


class TestCalculateCnvHourlyRate:
    def test_default_rates(self):
        config = PricingConfig()
        rate = calculate_cnv_hourly_rate(4, 16, 0, config)
        expected = Decimal("4") * Decimal("0.0711") + Decimal("16") * Decimal("0.00175")
        assert rate == expected

    def test_with_storage(self):
        config = PricingConfig()
        rate = calculate_cnv_hourly_rate(4, 16, 100, config)
        expected = (
            Decimal("4") * Decimal("0.0711")
            + Decimal("16") * Decimal("0.00175")
            + Decimal("100") * Decimal("0.00012")
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
        """Test CNV pricing calculation from instance type name."""
        config = PricingConfig()
        components = [
            Component(
                purpose="bastion",
                instance_type="4_core_16Gi",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=50,
                cloud="CNV",
                os="Linux",
            ),
        ]

        result = await resolve_pricing(components, config)
        assert len(result) == 1
        assert result[0].cost_per_hour is not None
        expected = (
            Decimal("4") * Decimal("0.0711")
            + Decimal("16") * Decimal("0.00175")
            + Decimal("50") * Decimal("0.00012")
        )
        assert result[0].cost_per_hour == expected

    @pytest.mark.asyncio
    async def test_cnv_pricing_from_cpu_memory(self):
        """Test CNV pricing calculation from cpu/memory_gb fields."""
        config = PricingConfig()
        components = [
            Component(
                purpose="worker",
                instance_type="custom",
                count=1,
                cpu=8,
                memory_gb=32,
                storage_gb=0,
                cloud="CNV",
                os="Linux",
            ),
        ]

        result = await resolve_pricing(components, config)
        assert len(result) == 1
        assert result[0].cost_per_hour is not None
        expected = Decimal("8") * Decimal("0.0711") + Decimal("32") * Decimal("0.00175")
        assert result[0].cost_per_hour == expected

    @pytest.mark.asyncio
    async def test_skip_already_priced_components(self):
        """Test that components with existing pricing are not re-priced."""
        config = PricingConfig()
        existing_price = Decimal("1.23")
        components = [
            Component(
                purpose="bastion",
                instance_type="m5.xlarge",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=0,
                cloud="AWS",
                os="Linux",
                cost_per_hour=existing_price,
            ),
        ]

        result = await resolve_pricing(components, config)
        assert len(result) == 1
        assert result[0].cost_per_hour == existing_price

    @pytest.mark.asyncio
    async def test_aws_pricing_mocked(self):
        """Test AWS pricing with mocked HTTP response."""
        config = PricingConfig(aws_region="us-east-1")
        components = [
            Component(
                purpose="bastion",
                instance_type="m5.xlarge",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=0,
                cloud="AWS",
                os="Linux",
            ),
        ]

        mock_price = Decimal("0.192")
        with patch("ci_inspector.core.pricing.fetch_aws_pricing", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"m5.xlarge": mock_price}
            result = await resolve_pricing(components, config)

        assert len(result) == 1
        assert result[0].cost_per_hour == mock_price

    @pytest.mark.asyncio
    async def test_azure_pricing_mocked(self):
        """Test Azure pricing with mocked HTTP response."""
        config = PricingConfig(azure_region="eastus")
        components = [
            Component(
                purpose="bastion",
                instance_type="Standard_D4s_v3",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=0,
                cloud="Azure",
                os="Linux",
            ),
        ]

        mock_price = Decimal("0.192")
        with patch("ci_inspector.core.pricing.fetch_azure_pricing", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"Standard_D4s_v3": mock_price}
            result = await resolve_pricing(components, config)

        assert len(result) == 1
        assert result[0].cost_per_hour == mock_price

    @pytest.mark.asyncio
    async def test_mixed_clouds(self):
        """Test pricing resolution for multiple cloud providers."""
        config = PricingConfig()
        components = [
            Component(
                purpose="bastion",
                instance_type="4_core_16Gi",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=0,
                cloud="CNV",
                os="Linux",
            ),
            Component(
                purpose="master",
                instance_type="m5.xlarge",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=0,
                cloud="AWS",
                os="Linux",
            ),
            Component(
                purpose="worker",
                instance_type="Standard_D4s_v3",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=0,
                cloud="Azure",
                os="Linux",
            ),
        ]

        aws_price = Decimal("0.192")
        azure_price = Decimal("0.184")

        with patch("ci_inspector.core.pricing.fetch_aws_pricing", new_callable=AsyncMock) as mock_aws, \
             patch("ci_inspector.core.pricing.fetch_azure_pricing", new_callable=AsyncMock) as mock_azure:
            mock_aws.return_value = {"m5.xlarge": aws_price}
            mock_azure.return_value = {"Standard_D4s_v3": azure_price}
            result = await resolve_pricing(components, config)

        assert len(result) == 3
        # CNV component should have calculated price
        assert result[0].cost_per_hour is not None
        assert result[0].cost_per_hour == Decimal("4") * Decimal("0.0711") + Decimal("16") * Decimal("0.00175")
        # AWS component
        assert result[1].cost_per_hour == aws_price
        # Azure component
        assert result[2].cost_per_hour == azure_price

    @pytest.mark.asyncio
    async def test_missing_pricing(self):
        """Test that components without pricing get None."""
        config = PricingConfig()
        components = [
            Component(
                purpose="bastion",
                instance_type="unknown.type",
                count=1,
                cpu=4,
                memory_gb=16,
                storage_gb=0,
                cloud="AWS",
                os="Linux",
            ),
        ]

        with patch("ci_inspector.core.pricing.fetch_aws_pricing", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {}  # No pricing found
            result = await resolve_pricing(components, config)

        assert len(result) == 1
        assert result[0].cost_per_hour is None
