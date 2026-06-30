from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from ci_inspector.core.models import CatalogItemData

logger = logging.getLogger(__name__)

_STAGE_FILES = {"prod.yml", "dev.yml", "event.yml", "prod.yaml", "dev.yaml", "event.yaml"}


def resolve_ci_from_path(file_path: Path, repo_root: Path) -> tuple[str, str]:
    rel = file_path.relative_to(repo_root)
    stage = rel.stem  # "prod" from "prod.yml"
    ci_name = rel.parent.name  # directory name = CI name
    return ci_name, stage


@runtime_checkable
class CatalogSource(Protocol):
    async def get_catalog_item(self, ci_name: str, stage: str) -> CatalogItemData | None: ...
    async def get_component_definition(self, ci_name: str, stage: str) -> dict | None: ...
    async def list_catalog_items(self) -> list[CatalogItemData]: ...


class GitSource:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def _find_ci_dir(self, ci_name: str) -> Path | None:
        for path in self.repo_path.rglob("prod.yml"):
            if path.parent.name == ci_name:
                return path.parent
        for path in self.repo_path.rglob("prod.yaml"):
            if path.parent.name == ci_name:
                return path.parent
        return None

    def _parse_stage_file(self, ci_name: str, stage_file: Path) -> CatalogItemData | None:
        try:
            data = yaml.safe_load(stage_file.read_text()) or {}
        except Exception:
            return None

        meta = data.pop("__meta__", {})
        catalog_meta = meta.get("catalog", {})
        deployer = meta.get("deployer", {})
        components = meta.get("components", [])
        sandboxes = meta.get("sandboxes", [])

        display_name = catalog_meta.get("display_name", ci_name)
        stage = stage_file.stem

        linked_components = data.pop("linkedComponents", [])

        return CatalogItemData(
            ci_name=ci_name,
            stage=stage,
            display_name=display_name,
            definition=data,
            linked_components=linked_components,
            parameters=data.get("parameters", {}),
            meta={
                "catalog": catalog_meta,
                "deployer": deployer,
                "components": components,
                "sandboxes": sandboxes,
            },
            agnosticv_repo=deployer.get("scm_url", ""),
            agnosticv_dir=deployer.get("entry_point", ""),
        )

    async def get_catalog_item(self, ci_name: str, stage: str) -> CatalogItemData | None:
        ci_dir = self._find_ci_dir(ci_name)
        if ci_dir is None:
            return None

        for ext in (".yml", ".yaml"):
            stage_file = ci_dir / f"{stage}{ext}"
            if stage_file.exists():
                return self._parse_stage_file(ci_name, stage_file)
        return None

    async def get_component_definition(self, ci_name: str, stage: str) -> dict | None:
        item = await self.get_catalog_item(ci_name, stage)
        if item is None:
            return None
        return item.definition

    async def list_catalog_items(self) -> list[CatalogItemData]:
        items: list[CatalogItemData] = []
        seen: set[tuple[str, str]] = set()

        for pattern in ("**/prod.yml", "**/prod.yaml"):
            for stage_file in self.repo_path.glob(pattern):
                ci_name = stage_file.parent.name
                key = (ci_name, "prod")
                if key in seen:
                    continue
                seen.add(key)

                item = self._parse_stage_file(ci_name, stage_file)
                if item:
                    items.append(item)

        return items


class K8sSource:
    def __init__(self, kubeconfig: str):
        self.kubeconfig = kubeconfig
        self._api = None

    async def get_catalog_item(self, ci_name: str, stage: str) -> CatalogItemData | None:
        raise NotImplementedError("K8s source — Task 11")

    async def get_component_definition(self, ci_name: str, stage: str) -> dict | None:
        raise NotImplementedError("K8s source — Task 11")

    async def list_catalog_items(self) -> list[CatalogItemData]:
        raise NotImplementedError("K8s source — Task 11")
