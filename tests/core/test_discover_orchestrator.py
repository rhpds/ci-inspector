from pathlib import Path

import pytest

from ci_inspector.core.catalog import GitSource
from ci_inspector.core.discovery import discover
from ci_inspector.core.models import DiscoveryResult

FIXTURES = Path(__file__).parent.parent / "fixtures" / "agnosticv"


@pytest.mark.asyncio
async def test_discover_simple_lab():
    source = GitSource(FIXTURES)
    result = await discover("my-workshop", "prod", source)
    assert isinstance(result, DiscoveryResult)
    assert result.ci_name == "my-workshop"
    assert result.stage == "prod"
    assert len(result.components) > 0
    assert result.cloud_provider is not None


@pytest.mark.asyncio
async def test_discover_not_found():
    source = GitSource(FIXTURES)
    result = await discover("nonexistent", "prod", source)
    assert len(result.errors) > 0


@pytest.mark.asyncio
async def test_discover_binder():
    source = GitSource(FIXTURES)
    result = await discover("binder-lab", "prod", source)
    assert result.is_binder is True
