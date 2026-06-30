from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ci_inspector.core.catalog import GitSource
from ci_inspector.core.discovery import discover
from ci_inspector.core.models import CatalogItemData, DiscoveryResult

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


@pytest.mark.asyncio
async def test_discover_k8s_binder_via_linked_components():
    """Test binder detection via linkedComponents + resources (K8s source pattern).

    Simulates a CatalogItem with linkedComponents pointing to a pool cluster
    AVC via spec.resources[].provider.name. The AVC has CNV cluster specs.
    """
    # Build the CatalogItemData as K8sSource would return it
    catalog_item = CatalogItemData(
        ci_name="summit-2026.lb1758-binder",
        stage="event",
        display_name="Binder Lab",
        definition={},  # No own infra
        linked_components=[
            {"name": "openshift", "displayName": "OpenShift Cluster"},
        ],
        resources=[
            {
                "name": "openshift",
                "provider": {
                    "apiVersion": "poolboy.gpte.redhat.com/v1",
                    "kind": "ResourceProvider",
                    "name": "agd-v2.ocp-virt-labs-pool.event",
                    "namespace": "poolboy",
                },
            },
            {
                "name": "lb1758-binder",
                "provider": {
                    "name": "summit-2026.lb1758-binder.event",
                    "namespace": "poolboy",
                },
            },
        ],
        meta={
            "catalog": {"display_name": "Binder Lab"},
            "deployer": {},
            "components": [
                {
                    "name": "openshift",
                    "provider": {
                        "name": "agd-v2.ocp-virt-labs-pool.event",
                        "namespace": "poolboy",
                    },
                },
                {
                    "name": "lb1758-binder",
                    "provider": {
                        "name": "summit-2026.lb1758-binder.event",
                        "namespace": "poolboy",
                    },
                },
            ],
            "sandboxes": [],
        },
    )

    # The main CI's AVC definition (binder — no own instances)
    main_avc_def = {
        "env_type": "ocp4-cluster",
        "__spec__": {
            "agnosticvRepo": "",
            "path": "",
        },
    }

    # The linked AVC definition (pool cluster with CNV instances)
    linked_avc_def = {
        "cloud_provider": "openshift_cnv",
        "env_type": "ocp4-cluster",
        "instances": [
            {
                "name": "control-plane",
                "count": 3,
                "cores": 8,
                "memory": "32Gi",
                "rootVolumeSize": "120Gi",
            },
            {
                "name": "worker",
                "count": 10,
                "cores": 16,
                "memory": "64Gi",
                "rootVolumeSize": "120Gi",
            },
        ],
        "bastion_cores": 2,
        "bastion_memory": "8Gi",
        "__spec__": {
            "agnosticvRepo": "",
            "path": "",
        },
    }

    async def mock_get_component_definition(ci_name, stage):
        if ci_name == "agd-v2.ocp-virt-labs-pool":
            return linked_avc_def
        # Main CI has no own instances
        return main_avc_def

    # Build a mock source
    source = MagicMock()
    source.get_catalog_item = AsyncMock(return_value=catalog_item)
    source.get_component_definition = AsyncMock(side_effect=mock_get_component_definition)
    source.lookup_pool_by_ci = AsyncMock(return_value=None)
    source.lookup_pool_name = AsyncMock(return_value=None)

    result = await discover("summit-2026.lb1758-binder", "event", source)

    assert result.is_binder is True
    assert len(result.components) > 0
    # Should have bastion, control-plane, worker components
    purposes = sorted(c.purpose for c in result.components)
    assert "control-plane" in purposes
    assert "worker" in purposes
    # Cloud should be CNV
    assert result.cloud_provider == "CNV"
