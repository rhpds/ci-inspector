from pathlib import Path

import pytest

from ci_inspector.core.catalog import GitSource, resolve_ci_from_path

FIXTURES = Path(__file__).parent.parent / "fixtures" / "agnosticv"


class TestResolveCiFromPath:
    def test_stage_file(self):
        ci, stage = resolve_ci_from_path(FIXTURES / "my-workshop" / "prod.yml", FIXTURES)
        assert ci == "my-workshop"
        assert stage == "prod"

    def test_nested_dir(self):
        ci, stage = resolve_ci_from_path(
            FIXTURES / "summit" / "binder-lab" / "prod.yml", FIXTURES
        )
        assert ci == "binder-lab"
        assert stage == "prod"


class TestGitSource:
    @pytest.mark.asyncio
    async def test_get_catalog_item(self):
        source = GitSource(FIXTURES)
        item = await source.get_catalog_item("my-workshop", "prod")
        assert item.ci_name == "my-workshop"
        assert item.stage == "prod"
        assert item.display_name == "My Workshop"
        assert item.definition["cloud_provider"] == "ec2"
        assert item.definition["env_type"] == "ocp4-cluster"

    @pytest.mark.asyncio
    async def test_get_catalog_item_not_found(self):
        source = GitSource(FIXTURES)
        item = await source.get_catalog_item("nonexistent", "prod")
        assert item is None

    @pytest.mark.asyncio
    async def test_list_catalog_items(self):
        source = GitSource(FIXTURES)
        items = await source.list_catalog_items()
        names = {i.ci_name for i in items}
        assert "my-workshop" in names
        assert "binder-lab" in names

    @pytest.mark.asyncio
    async def test_get_component_definition(self):
        source = GitSource(FIXTURES)
        defn = await source.get_component_definition("my-workshop", "prod")
        assert defn["cloud_provider"] == "ec2"
