from unittest.mock import MagicMock, patch

import pytest

from ci_inspector.core.catalog import K8sSource


@pytest.mark.asyncio
async def test_k8s_source_get_catalog_item():
    """Test fetching a CatalogItem from the Babylon cluster."""
    mock_api = MagicMock()

    def mock_list(group, version, namespace, plural):
        if namespace == "babylon-catalog-prod":
            return {
                "items": [
                    {
                        "metadata": {"name": "my-workshop.prod", "namespace": "babylon-catalog-prod"},
                        "spec": {
                            "displayName": "My Workshop",
                            "keywords": ["openshift", "workshop"],
                            "agnosticvRepo": {
                                "git": {
                                    "url": "https://github.com/rhpds/agnosticv.git",
                                    "ref": "master",
                                },
                                "contextDir": "myaccount/my-workshop",
                            },
                            "parameters": [
                                {
                                    "name": "num_users",
                                    "openAPIV3Schema": {"type": "integer", "maximum": 100},
                                }
                            ],
                            "linkedComponents": ["pool.prod"],
                        },
                    }
                ]
            }
        return {"items": []}

    mock_api.list_namespaced_custom_object.side_effect = mock_list

    with patch("ci_inspector.core.catalog._load_k8s_api", return_value=mock_api):
        source = K8sSource("/fake/kubeconfig")
        item = await source.get_catalog_item("my-workshop", "prod")

    assert item is not None
    assert item.ci_name == "my-workshop"
    assert item.stage == "prod"
    assert item.display_name == "My Workshop"
    assert "openshift" in item.definition.get("keywords", [])
    assert item.agnosticv_repo == "https://github.com/rhpds/agnosticv.git"
    assert item.agnosticv_dir == "myaccount/my-workshop"
    assert item.linked_components == [{"name": "pool.prod"}]


@pytest.mark.asyncio
async def test_k8s_source_get_component_definition():
    """Test fetching an AgnosticVComponent definition from the Babylon cluster."""
    mock_api = MagicMock()
    mock_api.get_namespaced_custom_object.return_value = {
        "metadata": {"name": "my-workshop.prod", "namespace": "babylon-config"},
        "spec": {
            "definition": {
                "cloud_provider": "ec2",
                "env_type": "ocp4-cluster",
                "bastion_instance_type": "t3a.medium",
                "worker_instance_count": 3,
            },
            "agnosticvRepo": "https://github.com/redhat-cop/agnosticd.git",
            "path": "ansible/configs/ocp4-cluster",
        },
    }

    with patch("ci_inspector.core.catalog._load_k8s_api", return_value=mock_api):
        source = K8sSource("/fake/kubeconfig")
        definition = await source.get_component_definition("my-workshop", "prod")

    assert definition is not None
    assert definition["cloud_provider"] == "ec2"
    assert definition["env_type"] == "ocp4-cluster"
    assert definition["bastion_instance_type"] == "t3a.medium"
    assert definition["worker_instance_count"] == 3
    # Check that __spec__ was added
    assert definition["__spec__"]["agnosticvRepo"] == "https://github.com/redhat-cop/agnosticd.git"
    assert definition["__spec__"]["path"] == "ansible/configs/ocp4-cluster"


@pytest.mark.asyncio
async def test_k8s_source_list_catalog_items():
    """Test listing all CatalogItems from all catalog namespaces."""
    mock_api = MagicMock()

    def mock_list(group, version, namespace, plural):
        if namespace == "babylon-catalog-prod":
            return {
                "items": [
                    {
                        "metadata": {"name": "item1.prod", "namespace": "babylon-catalog-prod"},
                        "spec": {"displayName": "Item 1"},
                    },
                    {
                        "metadata": {"name": "item2.prod", "namespace": "babylon-catalog-prod"},
                        "spec": {"displayName": "Item 2"},
                    },
                ]
            }
        elif namespace == "babylon-catalog-dev":
            return {
                "items": [
                    {
                        "metadata": {"name": "item3.dev", "namespace": "babylon-catalog-dev"},
                        "spec": {"displayName": "Item 3"},
                    }
                ]
            }
        elif namespace == "babylon-catalog-event":
            return {"items": []}
        return {"items": []}

    mock_api.list_namespaced_custom_object.side_effect = mock_list

    with patch("ci_inspector.core.catalog._load_k8s_api", return_value=mock_api):
        source = K8sSource("/fake/kubeconfig")
        items = await source.list_catalog_items()

    assert len(items) == 3
    assert items[0].ci_name == "item1"
    assert items[0].stage == "prod"
    assert items[1].ci_name == "item2"
    assert items[1].stage == "prod"
    assert items[2].ci_name == "item3"
    assert items[2].stage == "dev"


@pytest.mark.asyncio
async def test_k8s_source_get_catalog_item_not_found():
    """Test that get_catalog_item returns None when item not found."""
    mock_api = MagicMock()
    mock_api.list_namespaced_custom_object.return_value = {"items": []}

    with patch("ci_inspector.core.catalog._load_k8s_api", return_value=mock_api):
        source = K8sSource("/fake/kubeconfig")
        item = await source.get_catalog_item("nonexistent", "prod")

    assert item is None


@pytest.mark.asyncio
async def test_k8s_source_get_component_definition_not_found():
    """Test that get_component_definition returns None when AVC not found."""
    mock_api = MagicMock()
    mock_api.get_namespaced_custom_object.side_effect = Exception("Not found")

    with patch("ci_inspector.core.catalog._load_k8s_api", return_value=mock_api):
        source = K8sSource("/fake/kubeconfig")
        definition = await source.get_component_definition("nonexistent", "prod")

    assert definition is None


@pytest.mark.asyncio
async def test_k8s_source_kubernetes_not_installed():
    """Test that K8sSource gracefully handles missing kubernetes package."""
    with patch("ci_inspector.core.catalog._load_k8s_api", return_value=None):
        source = K8sSource("/fake/kubeconfig")
        item = await source.get_catalog_item("my-workshop", "prod")

    assert item is None
