"""Published wrappers must select and resolve their parameterized children."""
import copy
import json

import pytest
from click.testing import CliRunner

from ci_inspector.cli import main
from ci_inspector.core.catalog import K8sSource
from ci_inspector.core.discovery import discover
from ci_inspector.core.models import CatalogItemData


class PublishedSource:
    def __init__(self, default="cnv", missing_child=False):
        self.missing_child = missing_child
        self.parameters = [
            {"name": "provider", "openAPIV3Schema": {"default": default}},
            {"name": "worker_instance_type", "openAPIV3Schema": {"default": "m5a.2xlarge"},
             "components": [{"name": "aws"}]},
            {"name": "cores", "openAPIV3Schema": {"default": 4},
             "components": [{"name": "all"}]},
        ]

    async def get_catalog_item(self, name, stage):
        if name == "published.cluster":
            return K8sSource("")._parse_catalog_item({
                "metadata": {"name": f"{name}.{stage}"},
                "spec": {"parameters": copy.deepcopy(self.parameters)},
            })
        if self.missing_child:
            return None
        return CatalogItemData(ci_name=name, stage=stage, definition={
            "cloud_provider": "openshift_cnv", "env_type": "test",
            "instances": [{"name": name, "count": 1, "cores": "{{ cores }}", "memory": "8Gi"}],
        })

    async def get_component_definition(self, name, stage):
        return {"__meta__": {
            "catalog": {"parameters": copy.deepcopy(self.parameters)},
            "components": [
                {"name": p, "item": f"components/{p}", "when": f"provider | default('') == '{p}'"}
                for p in ("cnv", "aws")
            ],
        }}

    async def lookup_pool_by_ci(self, *args):
        return None

    async def lookup_pool_name(self, *args):
        return None


@pytest.mark.asyncio
async def test_live_style_defaults_select_child_and_route_size():
    result = await discover("published.cluster", "prod", PublishedSource())
    assert [c.group_ci_name for c in result.components] == ["components.cnv"]
    assert result.total_vcpus == 4


@pytest.mark.asyncio
async def test_missing_child_error_reaches_parent():
    result = await discover("published.cluster", "prod", PublishedSource(missing_child=True),
                            _parameter_overrides={"provider": "cnv"})
    assert result.errors
    assert "components.cnv" in " ".join(result.errors)


@pytest.mark.asyncio
async def test_unselected_binder_is_not_silent_zero():
    result = await discover("published.cluster", "prod", PublishedSource(default="unsupported"))
    assert not result.components
    assert result.warnings


def test_cli_parameters_select_branch_and_override_numeric_size(monkeypatch):
    monkeypatch.setattr("ci_inspector.cli._get_source", lambda *args: PublishedSource(default="aws"))
    result = CliRunner().invoke(main, ["analyze", "published.cluster", "--format", "json",
                                      "--parameter", "provider=cnv", "--parameter", "cores=12"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total_vcpus"] == 12
    assert data["components"][0]["group_ci_name"] == "components.cnv"


def test_cli_rejects_malformed_parameter():
    result = CliRunner().invoke(main, ["analyze", "published.cluster", "--parameter", "provider"])
    assert result.exit_code == 2
    assert "NAME=VALUE" in result.output


def test_cli_table_surfaces_unresolved_binder_warning(monkeypatch):
    monkeypatch.setattr("ci_inspector.cli._get_source", lambda *args: PublishedSource(default="unsupported"))
    result = CliRunner().invoke(main, ["analyze", "published.cluster"])
    assert result.exit_code == 0
    assert "Warning:" in result.stderr
    assert "no infrastructure" in result.stderr
