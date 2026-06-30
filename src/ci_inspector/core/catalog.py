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
    async def lookup_pool_by_ci(self, ci_name_stage: str) -> str | None: ...
    async def lookup_pool_name(self, provider_name: str, cluster_size: str) -> str | None: ...


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

    async def lookup_pool_by_ci(self, ci_name_stage: str) -> str | None:
        """Pool name lookup not available from git source."""
        return None

    async def lookup_pool_name(self, provider_name: str, cluster_size: str) -> str | None:
        """Pool name lookup not available from git source."""
        return None


def _load_k8s_api(kubeconfig: str):
    """Initialize and return the Kubernetes CustomObjectsApi client.

    Args:
        kubeconfig: Path to the kubeconfig file

    Returns:
        CustomObjectsApi client, or None if kubernetes is not installed
    """
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        from kubernetes import client, config

        config.load_kube_config(config_file=kubeconfig)
        return client.CustomObjectsApi()
    except ImportError:
        logger.warning("kubernetes package not installed — K8sSource unavailable")
        return None
    except Exception as e:
        logger.error("Failed to initialize K8s client: %s", e)
        return None


_CATALOG_NAMESPACES = [
    "babylon-catalog-event",
    "babylon-catalog-prod",
    "babylon-catalog-dev",
]


class K8sSource:
    def __init__(self, kubeconfig: str):
        self.kubeconfig = kubeconfig
        self._api = None

    def _get_api(self):
        """Get or initialize the K8s API client."""
        if self._api is None:
            self._api = _load_k8s_api(self.kubeconfig)
        return self._api

    def _parse_catalog_item(self, item: dict) -> CatalogItemData | None:
        """Parse one CatalogItem CRD into a CatalogItemData model."""
        try:
            metadata = item.get("metadata", {})
            name = metadata.get("name", "")
            namespace = metadata.get("namespace", "")
            spec = item.get("spec", {})

            # Parse name: account.item.stage (last segment is stage)
            parts = name.split(".")
            if len(parts) >= 2:
                stage = parts[-1]
                ci_name = ".".join(parts[:-1])
            else:
                # Fallback to labels if name doesn't follow pattern
                labels = metadata.get("labels", {})
                stage = labels.get("babylon.gpte.redhat.com/stage", "prod")
                ci_name = name

            display_name = spec.get("displayName", "") or ci_name

            # Extract AgnosticV repo info
            agnosticv_repo_obj = spec.get("agnosticvRepo", {}) or {}
            git_info = agnosticv_repo_obj.get("git", {}) or {}
            git_url = git_info.get("url", "") or ""
            agnosticv_dir = agnosticv_repo_obj.get("contextDir", "") or ""

            # Build agnosticv_repo as full URL
            agnosticv_repo = git_url

            # Extract parameters
            raw_parameters = spec.get("parameters", []) or []
            # Build parameters dict for the definition field
            parameters = {}
            for param in raw_parameters:
                param_name = param.get("name", "")
                if param_name:
                    # Store the full parameter spec
                    parameters[param_name] = param

            # Extract linked components (convert strings to dicts if needed)
            raw_linked = spec.get("linkedComponents", []) or []
            linked_components = []
            for lc_entry in raw_linked:
                if isinstance(lc_entry, dict):
                    linked_components.append(lc_entry)
                elif isinstance(lc_entry, str):
                    # Convert string reference to dict format
                    linked_components.append({"name": lc_entry})
                else:
                    linked_components.append(lc_entry)

            # Build definition dict from spec (everything except our extracted fields)
            definition = {
                "keywords": spec.get("keywords", []),
                "category": spec.get("category", ""),
                "multiuser": spec.get("multiuser"),
                "parameters": raw_parameters,
            }

            # Add meta info (components, sandboxes from resources)
            raw_resources = spec.get("resources", []) or []
            components = []
            sandboxes = []
            for resource in raw_resources:
                kind = resource.get("kind", "")
                if kind == "OcpSandbox":
                    sandboxes.append(resource)
                else:
                    # All non-sandbox resources (AnarchySubject and provider-only)
                    components.append(resource)

            meta = {
                "catalog": {
                    "display_name": display_name,
                    "namespace": namespace,
                },
                "deployer": {},
                "components": components,
                "sandboxes": sandboxes,
            }

            return CatalogItemData(
                ci_name=ci_name,
                stage=stage,
                display_name=display_name,
                definition=definition,
                linked_components=linked_components,
                resources=raw_resources,
                parameters=parameters,
                meta=meta,
                agnosticv_repo=agnosticv_repo,
                agnosticv_dir=agnosticv_dir,
            )
        except Exception as e:
            logger.debug("Failed to parse CatalogItem %s: %s", item.get("metadata", {}).get("name", "?"), e)
            return None

    async def get_catalog_item(self, ci_name: str, stage: str) -> CatalogItemData | None:
        """Fetch a specific CatalogItem by ci_name and stage.

        Args:
            ci_name: The catalog item name (e.g., "my-workshop")
            stage: The stage (e.g., "prod", "dev", "event")

        Returns:
            CatalogItemData if found, None otherwise
        """
        api = self._get_api()
        if api is None:
            return None

        # Normalize: "summit-2026/lb1404-foo" → "summit-2026.lb1404-foo" (CRD names use dots)
        normalized = ci_name.replace("/", ".") if "/" in ci_name else ci_name
        item_name = f"{normalized}.{stage}"

        # Try each catalog namespace
        for namespace in _CATALOG_NAMESPACES:
            try:
                result = api.list_namespaced_custom_object(
                    group="babylon.gpte.redhat.com",
                    version="v1",
                    namespace=namespace,
                    plural="catalogitems",
                )
                for item in result.get("items", []):
                    if item.get("metadata", {}).get("name") == item_name:
                        return self._parse_catalog_item(item)
            except Exception as e:
                logger.debug("Failed to query namespace %s: %s", namespace, e)

        return None

    async def get_component_definition(self, ci_name: str, stage: str) -> dict | None:
        """Fetch the AgnosticVComponent definition for a catalog item.

        Args:
            ci_name: The catalog item name (e.g., "my-workshop")
            stage: The stage (e.g., "prod", "dev", "event")

        Returns:
            The spec.definition dict from the AVC CRD, with __spec__ added for metadata
        """
        api = self._get_api()
        if api is None:
            return None

        normalized = ci_name.replace("/", ".") if "/" in ci_name else ci_name
        crd_name = f"{normalized}.{stage}"

        try:
            result = api.get_namespaced_custom_object(
                group="gpte.redhat.com",
                version="v1",
                namespace="babylon-config",
                plural="agnosticvcomponents",
                name=crd_name,
            )
            spec = result.get("spec", {})
            definition = spec.get("definition", {})

            # Add top-level spec fields needed for discovery
            definition["__spec__"] = {
                "agnosticvRepo": spec.get("agnosticvRepo", ""),
                "path": spec.get("path", ""),
            }
            return definition
        except Exception as e:
            logger.debug("Failed to fetch AVC %s: %s", crd_name, e)
            return None

    async def list_catalog_items(self) -> list[CatalogItemData]:
        """List all CatalogItems from all catalog namespaces.

        Returns:
            List of CatalogItemData objects
        """
        api = self._get_api()
        if api is None:
            return []

        items: list[CatalogItemData] = []

        for namespace in _CATALOG_NAMESPACES:
            try:
                result = api.list_namespaced_custom_object(
                    group="babylon.gpte.redhat.com",
                    version="v1",
                    namespace=namespace,
                    plural="catalogitems",
                )
                for item in result.get("items", []):
                    parsed = self._parse_catalog_item(item)
                    if parsed:
                        items.append(parsed)
                logger.debug("Fetched %d CatalogItems from %s", len(result.get("items", [])), namespace)
            except Exception as e:
                logger.debug("Failed to query namespace %s: %s", namespace, e)

        return items

    async def lookup_pool_by_ci(self, ci_name_stage: str) -> str | None:
        """Look up pool name by CI label on Poolboy ResourcePools.

        Searches for ResourcePool CRDs with label
        ``babylon.gpte.redhat.com/catalogItemName`` matching the given CI.
        """
        api = self._get_api()
        if api is None:
            return None
        try:
            result = api.list_namespaced_custom_object(
                group="poolboy.gpte.redhat.com",
                version="v1",
                namespace="poolboy",
                plural="resourcepools",
                label_selector=f"babylon.gpte.redhat.com/catalogItemName={ci_name_stage}",
            )
            items = result.get("items", [])
            if items:
                return items[0].get("metadata", {}).get("name")
        except Exception as e:
            logger.debug("Pool lookup by CI %s failed: %s", ci_name_stage, e)
        return None

    async def lookup_pool_name(self, provider_name: str, cluster_size: str) -> str | None:
        """Look up pool name by provider reference in ResourcePool spec.

        Searches for a ResourcePool whose spec.resources reference matches
        the given provider_name and optionally cluster_size.
        """
        api = self._get_api()
        if api is None:
            return None
        try:
            result = api.list_namespaced_custom_object(
                group="poolboy.gpte.redhat.com",
                version="v1",
                namespace="poolboy",
                plural="resourcepools",
            )
            for pool in result.get("items", []):
                spec = pool.get("spec", {})
                resources = spec.get("resources", [])
                for res in resources:
                    provider = res.get("provider", {})
                    pname = provider.get("name", "")
                    if pname == provider_name:
                        pool_name = pool.get("metadata", {}).get("name", "")
                        if cluster_size:
                            # Check if pool matches cluster_size in vars
                            job_vars = (
                                res.get("template", {})
                                .get("spec", {})
                                .get("vars", {})
                                .get("job_vars", {})
                            )
                            pool_cs = job_vars.get("cluster_size", "")
                            if pool_cs and pool_cs != cluster_size:
                                continue
                        return pool_name
        except Exception as e:
            logger.debug("Pool lookup by provider %s failed: %s", provider_name, e)
        return None
