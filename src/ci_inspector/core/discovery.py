"""Instance extraction engine for CI Inspector.

Extracts cloud instance components from merged AgnosticD + CRD variables.
Ported from Labagator's resource_discovery.py with database dependencies
removed, returning Pydantic Component models instead of raw dicts.

Handles six extraction patterns:
  A) ``instances`` list with ``flavor`` dict (cloud labs via AgnosticD)
  B) Individual ``{role}_instance_type`` vars (no instances list)
  C) CNV ``instances`` with ``cores``/``memory`` (via extract_linked_instances)
  D) OCP cluster pool vars (ai_control_plane_cores, etc.)
  E) ROSA cluster vars (rosa_deploy, rosa_compute_replicas)
  F) MachineSet groups (ocp4_workload_machinesets_machineset_groups)

Plus:
  - CNV workload VM extraction from cnv_instances
  - Parent worker injection for binder sub-components
  - URL-based instance lookups (showroom git repos)
  - Sandbox cluster resolution (OcpSandbox)
  - Linked component resolution (CatalogItem.spec.linkedComponents)
  - Meta-component resolution (labs with own infra + __meta__.components)
  - When condition evaluation for binder sub-components
  - Parameter routing from parent to sub-components
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import httpx
import yaml

from ci_inspector.core.agnosticd import read_agnosticd_defaults
from ci_inspector.core.helpers import (
    CLOUD_MAP,
    eval_jinja_str,
    extract_jinja_default,
    parse_memory_to_gi,
    resolve_image_to_os,
    resolve_jinja_var,
)
from ci_inspector.core.models import Component, DiscoveryResult

if TYPE_CHECKING:
    from ci_inspector.core.catalog import CatalogSource

logger = logging.getLogger(__name__)

_MAX_BINDER_DEPTH = 5

# Cache for GitHub URL fetches (instances.yaml)
_git_url_cache: dict[str, tuple[dict, float]] = {}
_GIT_URL_CACHE_TTL = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Secret stripping
# ---------------------------------------------------------------------------


def _strip_secrets(definition: dict) -> dict:
    """Remove sensitive and internal keys from a definition dict."""
    excluded_prefixes = ("secret_", "password", "token", "api_key", "aws_access", "ssh_")
    excluded_keys = {"__spec__", "agnosticv_meta"}

    cleaned = {}
    for key, value in definition.items():
        lower_key = key.lower()
        if key in excluded_keys:
            continue
        if any(lower_key.startswith(p) for p in excluded_prefixes):
            continue
        if isinstance(value, dict):
            cleaned[key] = _strip_secrets(value)
        else:
            cleaned[key] = value
    return cleaned


# ---------------------------------------------------------------------------
# Binder CRD name resolution
# ---------------------------------------------------------------------------


def _resolve_binder_crd_name(component_path: str, default_stage: str = "prod") -> tuple[str, str]:
    """Convert a binder component path to CRD name and stage.

    E.g., ``agd_v2/ocp-cluster-cnv-pools/event`` ->
    (``agd-v2.ocp-cluster-cnv-pools``, ``event``)

    When the path has no explicit stage suffix (only 2 parts),
    ``default_stage`` is used -- typically the parent CI's stage.
    """
    parts = component_path.replace("_", "-").lower().split("/")
    if len(parts) >= 2:
        stage = parts[-1] if len(parts) >= 3 else default_stage
        ci_name = ".".join(parts[:-1]) if len(parts) >= 3 else ".".join(parts)
        return ci_name, stage
    return component_path.lower(), default_stage


# ---------------------------------------------------------------------------
# Showroom URL extraction
# ---------------------------------------------------------------------------


def _extract_showroom_url(merged: dict) -> str | None:
    """Extract the showroom git repo URL from merged variables."""
    for key, val in merged.items():
        if not isinstance(val, str) or "ref" in key:
            continue
        if "showroom_content_git_repo" in key or key == "showroom_git_repo":
            return val
    return None


def _resolve_git_config_url(merged: dict) -> str | None:
    """Build a raw GitHub URL from git_config_directory and related variables."""
    repo = _extract_showroom_url(merged)
    ref = "main"
    for key, val in merged.items():
        if isinstance(val, str) and "showroom_content_git_repo_ref" in key:
            ref = val

    if not repo:
        return None

    url = repo.replace(".git", f"/{ref}/config/").replace(
        "github.com", "raw.githubusercontent.com"
    )
    return url


# ---------------------------------------------------------------------------
# URL-based instance lookups
# ---------------------------------------------------------------------------


async def _resolve_url_lookups(merged: dict) -> None:
    """Resolve Jinja URL lookup expressions for instances and containers.

    When instances or containers are Jinja expressions like:
      {{ (lookup('url', git_config_directory + 'instances.yaml') | from_yaml).virtualmachines }}
    we resolve the URL and fetch the actual data.
    """
    for field in ("instances", "containers"):
        val = merged.get(field)
        if not isinstance(val, str) or "lookup" not in val:
            continue

        base_url = _resolve_git_config_url(merged)
        if not base_url:
            logger.debug("  Cannot resolve git_config_directory for %s lookup", field)
            continue

        url = base_url + "instances.yaml"

        # Check cache
        now = time.time()
        cached = _git_url_cache.get(url)
        if cached and (now - cached[1]) < _GIT_URL_CACHE_TTL:
            content = cached[0]
            logger.debug("  Using cached %s from %s", field, url)
        else:
            logger.info("  Fetching %s from %s", field, url)
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url, headers={"User-Agent": "ci-inspector"})
                    if resp.status_code != 200:
                        logger.warning("  HTTP %d fetching %s", resp.status_code, url)
                        continue
                    content = yaml.safe_load(resp.text)
                    _git_url_cache[url] = (content, now)
            except Exception as e:
                logger.warning("  Failed to fetch %s from %s: %s", field, url, e)
                continue

        if isinstance(content, dict):
            if field == "instances":
                fetched_vms = content.get("virtualmachines", [])
                # Preserve AgnosticD instances that the URL fetch doesn't include
                agd_instances = merged.get("_agnosticd_instances", [])
                if isinstance(agd_instances, list) and agd_instances and isinstance(fetched_vms, list):
                    fetched_names = {vm.get("name") for vm in fetched_vms if isinstance(vm, dict)}
                    preserved = [
                        inst for inst in agd_instances
                        if isinstance(inst, dict) and inst.get("name") not in fetched_names
                    ]
                    merged["instances"] = preserved + fetched_vms
                else:
                    merged["instances"] = fetched_vms
                if content.get("containers") and not merged.get("containers"):
                    merged["containers"] = content["containers"]
            elif field == "containers":
                merged["containers"] = content.get("containers", [])
            logger.info(
                "  Resolved %s: %d entries from git repo",
                field,
                len(merged.get(field, [])) if isinstance(merged.get(field), list) else 0,
            )
        else:
            logger.warning("  Unexpected format in %s from %s", field, url)


# ---------------------------------------------------------------------------
# When condition evaluation
# ---------------------------------------------------------------------------


def _evaluate_when_condition(condition: str, variables: dict) -> bool:
    """Evaluate a Jinja-style 'when' condition against variables.

    Handles patterns like:
      "ocp_cloud_provider | default('') == 'cnv'"
      "ocp_cloud_provider == 'aws'"
      "ocp_cloud_provider != 'aws'"

    Returns True if the condition is met, False otherwise.
    Falls back to True (include component) on parse failure.
    """
    # Pattern: var_name | default('fallback') == 'value'
    m = re.match(
        r"(\w+)\s*\|\s*default\(['\"]([^'\"]*?)['\"]\)\s*(==|!=)\s*['\"]([^'\"]*?)['\"]",
        condition.strip(),
    )
    if m:
        var_name, fallback, op, expected = m.groups()
        actual = str(variables.get(var_name, fallback) or fallback)
        return (actual == expected) if op == "==" else (actual != expected)

    # Pattern: var_name == 'value' (no default filter)
    m = re.match(r"(\w+)\s*(==|!=)\s*['\"]([^'\"]*?)['\"]", condition.strip())
    if m:
        var_name, op, expected = m.groups()
        actual = str(variables.get(var_name, "") or "")
        return (actual == expected) if op == "==" else (actual != expected)

    # Can't parse -- include by default
    logger.debug("Cannot parse when condition: %s", condition)
    return True


# ---------------------------------------------------------------------------
# Parameter routing
# ---------------------------------------------------------------------------


def _build_routed_param_overrides(parent_definition: dict, comp_name: str) -> dict:
    """Extract parent catalog parameter values routed to a sub-component.

    AgnosticV catalog parameters define routing via
    ``__meta__.catalog.parameters[].components[].name``.  "all" routes to
    every sub-component; "current" routes only to the parent CI itself
    (skipped here).
    """
    catalog_params = (
        parent_definition.get("__meta__", {})
        .get("catalog", {})
        .get("parameters", [])
    )
    if not isinstance(catalog_params, list):
        return {}

    overrides: dict = {}
    for param in catalog_params:
        pname = param.get("name", "")
        if not pname:
            continue

        targets = param.get("components", [])
        routed = any(
            (t.get("name") if isinstance(t, dict) else str(t)) in ("all", comp_name)
            for t in targets
        )
        if not routed:
            continue

        value = parent_definition.get(pname)
        if value is None:
            value = (param.get("openAPIV3Schema") or {}).get("default")
        if value is None:
            continue

        overrides[pname] = value
        var_name = param.get("variable")
        if var_name and var_name != pname:
            overrides[var_name] = value

    return overrides


# ---------------------------------------------------------------------------
# Instance extraction (Patterns A-F)
# ---------------------------------------------------------------------------


def extract_instances(merged: dict, cloud_provider: str) -> list[Component]:
    """Extract cloud instances from merged AgnosticD + CRD variables.

    Dispatches to pattern-specific extractors based on the data shape.

    Args:
        merged: Merged variable dict (AgnosticD defaults + CRD overrides).
        cloud_provider: Raw cloud provider string (e.g. "ec2", "azure", "none").

    Returns:
        List of Component models representing discovered instances.
    """
    cloud_display = CLOUD_MAP.get(cloud_provider, cloud_provider or "unknown")
    components: list[Component] = []

    instances = merged.get("instances", [])
    if isinstance(instances, list) and instances:
        # Distinguish Pattern A (flavor dict) from Pattern C (cores/memory)
        first_inst = next((i for i in instances if isinstance(i, dict)), None)
        if first_inst and (
            "flavor" in first_inst
            or "instance_type" in first_inst
            or "__instance_type" in first_inst
        ):
            # Pattern A: cloud instances with flavor dict
            components = _extract_cloud_instances(
                merged, instances, cloud_provider, cloud_display
            )
            # Also check for role vars not covered by the instances list
            instance_names = {c.purpose for c in components}
            role_components = _extract_role_vars(merged, cloud_provider, cloud_display)
            for rc in role_components:
                if rc.purpose not in instance_names:
                    components.append(rc)
                    instance_names.add(rc.purpose)
            # Also check for ROSA cluster nodes
            rosa_components = _extract_rosa_cluster(merged, cloud_display)
            for rc in rosa_components:
                if rc.purpose not in instance_names:
                    components.append(rc)
            return components
        elif first_inst and ("cores" in first_inst or "memory" in first_inst):
            # Pattern C: CNV instances -- delegate to linked handler
            cluster_size = merged.get("cluster_size", "")
            if isinstance(cluster_size, str) and "{{" in cluster_size:
                cluster_size = extract_jinja_default(cluster_size)
            return extract_linked_instances(
                merged, str(cluster_size) if cluster_size else ""
            )

    # Pattern E: ROSA cluster vars
    rosa_components = _extract_rosa_cluster(merged, cloud_display)
    if rosa_components:
        return rosa_components

    # Pattern D: OCP cluster pool vars (ai_control_plane_cores, etc.)
    if merged.get("ai_control_plane_cores"):
        cluster_size = merged.get("cluster_size", "")
        if isinstance(cluster_size, str) and "{{" in cluster_size:
            cluster_size = extract_jinja_default(cluster_size)
        return extract_linked_instances(
            merged, str(cluster_size) if cluster_size else ""
        )

    # Pattern B: individual {role}_instance_type vars
    return _extract_role_vars(merged, cloud_provider, cloud_display)


def _extract_cloud_instances(
    merged: dict,
    instances: list,
    cloud_provider: str,
    cloud_display: str,
) -> list[Component]:
    """Pattern A: extract from ``instances`` list with ``flavor`` dicts."""
    # Map cloud_provider to flavor sub-key
    flavor_key_map = {
        "aws": "ec2",
        "ec2": "ec2",
        "azure": "azure",
        "gcp": "gcp",
        "openshift_cnv": "osp",
    }
    flavor_key = flavor_key_map.get(cloud_provider, "ec2")

    components: list[Component] = []

    for inst in instances:
        if not isinstance(inst, dict):
            continue

        name = inst.get("name", "instance")

        # Resolve count
        count_raw = inst.get("count", 1)
        count_formula = None
        if inst.get("unique"):
            count = 1
        elif isinstance(count_raw, str) and "{{" in count_raw:
            count_formula = count_raw
            try:
                resolved = resolve_jinja_var(count_raw, {**merged, "num_users": 1})
                count = int(float(resolved))
            except (ValueError, TypeError):
                count = 1
        else:
            try:
                count = int(count_raw)
            except (ValueError, TypeError):
                count = 1

        # Resolve instance type from flavor dict or direct var
        instance_type = ""
        instance_type_formula = None
        flavor = inst.get("flavor", {})
        if isinstance(flavor, dict):
            itype_raw = flavor.get(flavor_key, "")
        else:
            itype_raw = str(flavor) if flavor else ""

        if not itype_raw:
            itype_raw = inst.get("instance_type", "") or inst.get(
                "__instance_type", ""
            )

        if isinstance(itype_raw, str) and "{{" in itype_raw:
            instance_type_formula = itype_raw
            resolved = resolve_jinja_var(itype_raw, {**merged, "num_users": 1})
            instance_type = resolved if resolved and "{{" not in str(resolved) else ""
        else:
            instance_type = str(itype_raw) if itype_raw else ""

        # If instance_type is still empty, try common fallback vars
        if not instance_type:
            role_var = f"{name}_instance_type"
            if role_var in merged:
                val = merged[role_var]
                if isinstance(val, str) and "{{" in val:
                    instance_type = str(
                        resolve_jinja_var(val, {**merged, "num_users": 1})
                    )
                else:
                    instance_type = str(val)
            elif "default_instance_type" in merged:
                instance_type = str(merged["default_instance_type"])

        # Resolve image -> OS
        image_raw = inst.get("image", "")
        if isinstance(image_raw, str) and "{{" in image_raw:
            image_raw = str(resolve_jinja_var(image_raw, merged))
        os_name, os_billing = resolve_image_to_os(image_raw)

        # Extract storage from volumes
        storage_gb = 0
        volumes = inst.get("volumes", [])
        if isinstance(volumes, list):
            for vol in volumes:
                if isinstance(vol, dict):
                    vol_size = vol.get("size", 0)
                    try:
                        storage_gb += int(vol_size)
                    except (ValueError, TypeError):
                        pass

        components.append(
            Component(
                purpose=name,
                instance_type=instance_type,
                count=count,
                cpu=0,
                memory_gb=0,
                os=os_name,
                os_billing=os_billing,
                storage_gb=storage_gb,
                cloud=cloud_display,
                count_formula=count_formula,
                instance_type_formula=instance_type_formula,
            )
        )

    return components


def _extract_role_vars(
    merged: dict, cloud_provider: str, cloud_display: str
) -> list[Component]:
    """Pattern B: extract from individual ``{role}_instance_type`` vars."""
    components: list[Component] = []
    role_prefixes = [
        "bastion",
        "control_plane",
        "master",
        "worker",
        "infra",
        "student",
        "utility",
        "support",
    ]

    for prefix in role_prefixes:
        itype_var = f"{prefix}_instance_type"
        count_var = f"{prefix}_instance_count"
        alt_count_var = (
            f"num_{prefix}s" if not prefix.endswith("s") else f"num_{prefix}"
        )

        itype_val = merged.get(itype_var, "")
        if not itype_val:
            continue

        # Resolve instance type
        instance_type_formula = None
        if isinstance(itype_val, str) and "{{" in itype_val:
            instance_type_formula = itype_val
            itype_val = resolve_jinja_var(itype_val, {**merged, "num_users": 1})

        # Resolve count
        count_raw = merged.get(count_var, merged.get(alt_count_var, 1))
        count_formula = None
        if isinstance(count_raw, str) and "{{" in count_raw:
            count_formula = count_raw
            try:
                resolved = resolve_jinja_var(count_raw, {**merged, "num_users": 1})
                count = int(float(resolved))
            except (ValueError, TypeError):
                count = 1
        else:
            try:
                count = int(count_raw)
            except (ValueError, TypeError):
                count = 1

        # Skip disabled roles
        if count == 0 and not count_formula:
            continue

        # Resolve image for OS
        image_var = f"{prefix}_instance_image"
        image_val = merged.get(image_var, "")
        if isinstance(image_val, str) and "{{" in image_val:
            image_val = str(resolve_jinja_var(image_val, merged))
        os_name, os_billing = resolve_image_to_os(image_val)

        purpose = prefix.replace("_", "-")

        components.append(
            Component(
                purpose=purpose,
                instance_type=str(itype_val) if itype_val else "",
                count=count,
                cpu=0,
                memory_gb=0,
                os=os_name,
                os_billing=os_billing,
                storage_gb=0,
                cloud=cloud_display,
                count_formula=count_formula,
                instance_type_formula=instance_type_formula,
            )
        )

    return components


def _extract_rosa_cluster(merged: dict, cloud_display: str) -> list[Component]:
    """Pattern E: extract ROSA cluster nodes."""
    rosa_deploy = merged.get("rosa_deploy")
    if not rosa_deploy or str(rosa_deploy).lower() in ("false", "0", ""):
        return []

    components: list[Component] = []
    rosa_replicas = merged.get("rosa_compute_replicas")
    if rosa_replicas is None:
        rosa_replicas = merged.get("rosa_num_compute_nodes", 2)
    try:
        rosa_replicas = int(rosa_replicas)
    except (ValueError, TypeError):
        rosa_replicas = 2

    rosa_machine_type = merged.get("rosa_compute_machine_type", "") or ""
    if isinstance(rosa_machine_type, str) and "{{" in rosa_machine_type:
        rosa_machine_type = str(resolve_jinja_var(rosa_machine_type, merged))
    if not rosa_machine_type or "{{" in rosa_machine_type:
        rosa_machine_type = "m5.xlarge"

    is_hcp = str(merged.get("rosa_deploy_hcp", "")).lower() in ("true", "1")

    components.append(
        Component(
            purpose="worker",
            instance_type=rosa_machine_type,
            count=rosa_replicas,
            cpu=0,
            memory_gb=0,
            os="RHCOS",
            os_billing="Linux",
            storage_gb=0,
            cloud=cloud_display,
        )
    )

    if not is_hcp:
        components.append(
            Component(
                purpose="control-plane",
                instance_type="m5.xlarge",
                count=3,
                cpu=0,
                memory_gb=0,
                os="RHCOS",
                os_billing="Linux",
                storage_gb=0,
                cloud=cloud_display,
            )
        )

    return components


def _extract_machineset_groups(merged: dict, cloud_provider: str) -> list[Component]:
    """Pattern F: extract extra MachineSet node groups (GPU, metal workers).

    Each entry in ocp4_workload_machinesets_machineset_groups has
    instance_type, total_replicas, and a name/role.
    """
    ms_groups = merged.get("ocp4_workload_machinesets_machineset_groups", [])
    if not isinstance(ms_groups, list) or not ms_groups:
        return []

    cloud_display = CLOUD_MAP.get(cloud_provider, cloud_provider or "unknown")
    components: list[Component] = []

    for group in ms_groups:
        if not isinstance(group, dict):
            continue

        itype_raw = group.get("instance_type", "")
        if isinstance(itype_raw, str) and "{{" in itype_raw:
            itype_raw = str(resolve_jinja_var(itype_raw, merged))
        if not itype_raw or "{{" in str(itype_raw):
            continue

        replicas_raw = group.get("total_replicas", 1)
        if isinstance(replicas_raw, str) and "{{" in replicas_raw:
            replicas_raw = str(resolve_jinja_var(replicas_raw, merged))
        try:
            replicas = int(replicas_raw)
        except (ValueError, TypeError):
            replicas = 1

        if replicas <= 0:
            continue

        name = group.get("name", "machineset")
        components.append(
            Component(
                purpose=name,
                instance_type=str(itype_raw),
                count=replicas,
                cpu=0,
                memory_gb=0,
                os="RHCOS",
                os_billing="Linux",
                storage_gb=0,
                cloud=cloud_display,
            )
        )

    return components


# ---------------------------------------------------------------------------
# CNV workload VM extraction
# ---------------------------------------------------------------------------


def extract_cnv_workload_vms(definition: dict) -> list[Component]:
    """Extract CNV workload VMs from cnv_instances list."""
    cnv_vms = definition.get("cnv_instances")
    if not isinstance(cnv_vms, list) or not cnv_vms:
        return []

    components: list[Component] = []
    for vm in cnv_vms:
        if not isinstance(vm, dict):
            continue
        vm_name = vm.get("name", "vm")
        vm_cores = vm.get("cores", 0) or 0
        vm_memory_gb = parse_memory_to_gi(str(vm.get("memory", "0")))
        vm_storage_gb = parse_memory_to_gi(str(vm.get("image_size", "0")))

        components.append(
            Component(
                purpose=f"vm:{vm_name}",
                instance_type=f"{vm_cores}_core_{vm_memory_gb}Gi",
                count=vm.get("count", 1) or 1,
                os=vm.get("image", "Linux"),
                os_billing="Linux",
                cpu=vm_cores,
                memory_gb=vm_memory_gb,
                storage_gb=vm_storage_gb,
                cloud="CNV",
                cost_per_hour=None,
                count_formula=vm.get("count_formula"),
            )
        )
    return components


# ---------------------------------------------------------------------------
# Linked instance extraction (Pattern C + D)
# ---------------------------------------------------------------------------


def extract_linked_instances(
    definition: dict, cluster_size: str
) -> list[Component]:
    """Extract components from a linked AVC definition.

    Handles two patterns:
      1. instances list (standard AgnosticD VMs with name/cores/memory/count)
      2. OCP cluster pool vars (ai_control_plane_cores, worker_instance_count, etc.)
    """
    components: list[Component] = []

    # Detect cloud from definition
    cloud_provider = definition.get("cloud_provider", "") or ""
    config = definition.get("config", "") or ""
    if cloud_provider:
        cloud = CLOUD_MAP.get(cloud_provider, cloud_provider)
    elif "cnv" in config.lower() or "openshift-cluster" in config.lower():
        cloud = "CNV"
    else:
        cloud = "CNV"  # linked components are typically CNV

    # Build Jinja context from all scalar values
    jinja_ctx = {
        k: v
        for k, v in definition.items()
        if isinstance(v, (str, int, float, bool))
    }
    jinja_ctx["cluster_size"] = cluster_size
    jinja_ctx["num_users"] = 1

    # Pattern 1: instances list
    instances = definition.get("instances", [])
    if isinstance(instances, list) and instances:
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            name = inst.get("name", "instance")
            if isinstance(name, str) and "{{" in name:
                name = eval_jinja_str(name, **jinja_ctx)

            cores = inst.get("cores", 0)
            memory_str = str(inst.get("memory", "0"))
            count_raw = inst.get("count", 1)

            # Resolve Jinja in count
            if isinstance(count_raw, str) and "{{" in count_raw:
                try:
                    count = int(eval_jinja_str(count_raw, **jinja_ctx))
                except (ValueError, TypeError):
                    count = 1
            else:
                try:
                    count = int(count_raw)
                except (ValueError, TypeError):
                    count = 1

            # Resolve Jinja in cores
            if isinstance(cores, str) and "{{" in cores:
                try:
                    cores = int(eval_jinja_str(str(cores), **jinja_ctx))
                except (ValueError, TypeError):
                    cores = 0
            else:
                try:
                    cores = int(cores)
                except (ValueError, TypeError):
                    cores = 0

            # Resolve Jinja in memory
            if isinstance(memory_str, str) and "{{" in memory_str:
                memory_str = eval_jinja_str(memory_str, **jinja_ctx)
            memory_gi = parse_memory_to_gi(memory_str)

            instance_type = (
                f"{cores}_core_{memory_gi}Gi" if cores and memory_gi else ""
            )
            os_type = inst.get("os_type", "RHEL")

            components.append(
                Component(
                    purpose=name,
                    instance_type=instance_type,
                    count=count,
                    cpu=cores,
                    memory_gb=memory_gi,
                    os=os_type,
                    cloud=cloud,
                    storage_gb=0,
                )
            )
        # Don't return yet -- also check for pool vars below

    # Pattern 2: OCP cluster pool vars (ai_control_plane_cores, etc.)
    cp_cores_raw = definition.get("ai_control_plane_cores", "")
    if not cp_cores_raw:
        return components

    cp_memory_raw = definition.get("ai_control_plane_memory", "")
    cp_count_raw = (
        definition.get("control_plane_instance_count")
        or definition.get("master_instance_count")
        or definition.get("ai_control_plane_count")
        or "1"
    )
    worker_count_raw = definition.get("worker_instance_count", "0")
    worker_cores_raw = (
        definition.get("ai_workers_cores")
        or definition.get("ai_worker_cores")
        or ""
    )
    worker_memory_raw = (
        definition.get("ai_workers_memory")
        or definition.get("ai_worker_memory")
        or ""
    )

    # Resolve control plane
    cp_cores_str = (
        eval_jinja_str(str(cp_cores_raw), **jinja_ctx)
        if isinstance(cp_cores_raw, str) and "{{" in cp_cores_raw
        else str(cp_cores_raw)
    )
    cp_memory_str = (
        eval_jinja_str(str(cp_memory_raw), **jinja_ctx)
        if isinstance(cp_memory_raw, str) and "{{" in cp_memory_raw
        else str(cp_memory_raw)
    )
    cp_count_str = (
        eval_jinja_str(str(cp_count_raw), **jinja_ctx)
        if isinstance(cp_count_raw, str) and "{{" in cp_count_raw
        else str(cp_count_raw)
    )

    try:
        cp_cores = int(float(cp_cores_str))
    except (ValueError, TypeError):
        cp_cores = 0
    cp_memory = parse_memory_to_gi(cp_memory_str)
    try:
        cp_count = int(float(cp_count_str))
    except (ValueError, TypeError):
        cp_count = 1

    if cp_cores and cp_memory:
        components.append(
            Component(
                purpose="control-plane",
                instance_type=f"{cp_cores}_core_{cp_memory}Gi",
                count=cp_count,
                cpu=cp_cores,
                memory_gb=cp_memory,
                os="RHCOS",
                cloud=cloud,
                storage_gb=0,
            )
        )

    # Resolve workers
    worker_count_str = (
        eval_jinja_str(str(worker_count_raw), **jinja_ctx)
        if isinstance(worker_count_raw, str) and "{{" in worker_count_raw
        else str(worker_count_raw)
    )
    try:
        worker_count = int(float(worker_count_str))
    except (ValueError, TypeError):
        worker_count = 0

    if worker_count > 0 and worker_cores_raw:
        worker_cores_str = (
            eval_jinja_str(str(worker_cores_raw), **jinja_ctx)
            if isinstance(worker_cores_raw, str) and "{{" in worker_cores_raw
            else str(worker_cores_raw)
        )
        worker_memory_str = (
            eval_jinja_str(str(worker_memory_raw), **jinja_ctx)
            if isinstance(worker_memory_raw, str) and "{{" in worker_memory_raw
            else str(worker_memory_raw)
        )

        try:
            worker_cores = int(float(worker_cores_str))
        except (ValueError, TypeError):
            worker_cores = 0
        worker_memory = parse_memory_to_gi(worker_memory_str)

        if worker_cores and worker_memory:
            components.append(
                Component(
                    purpose="worker",
                    instance_type=f"{worker_cores}_core_{worker_memory}Gi",
                    count=worker_count,
                    cpu=worker_cores,
                    memory_gb=worker_memory,
                    os="RHCOS",
                    cloud=cloud,
                    storage_gb=0,
                )
            )

    return components


# ---------------------------------------------------------------------------
# Parent worker injection
# ---------------------------------------------------------------------------


def inject_parent_workers(
    parent_vars: dict,
    sub_components: list[dict],
    sub_cloud: str,
) -> None:
    """Add worker components from parent lab/binder vars to pool sub-components.

    Mutates sub_components in-place. Does NOT inject if the pool already defines
    its own workers.
    """
    pool_has_workers = any(c.get("purpose") == "worker" for c in sub_components)
    if pool_has_workers:
        return

    wc_raw = parent_vars.get("worker_instance_count", 0)
    # Resolve Jinja formulas
    if isinstance(wc_raw, str) and "{{" in wc_raw:
        jinja_ctx = {
            k: v
            for k, v in parent_vars.items()
            if isinstance(v, (str, int, float, bool))
        }
        jinja_ctx.setdefault("num_users", 1)
        jinja_ctx.setdefault("cluster_size", "")
        resolved = eval_jinja_str(wc_raw, **jinja_ctx)
        try:
            worker_count = int(float(resolved))
        except (ValueError, TypeError):
            worker_count = 0
        count_formula = wc_raw
    else:
        try:
            worker_count = int(wc_raw)
        except (ValueError, TypeError):
            worker_count = 0
        count_formula = None

    if worker_count <= 0 and not count_formula:
        return

    # Pattern 1: cloud-native worker_instance_type (AWS, Azure, GCP)
    worker_itype = parent_vars.get("worker_instance_type", "")
    if isinstance(worker_itype, str) and "{{" in worker_itype:
        worker_itype = str(resolve_jinja_var(worker_itype, parent_vars))
    if worker_itype and not str(worker_itype).startswith("{{"):
        cloud = sub_cloud
        cloud_provider = parent_vars.get("cloud_provider", "")
        if cloud_provider and str(cloud_provider).lower() != "none":
            cloud = CLOUD_MAP.get(cloud_provider, cloud_provider)
        sub_components.append(
            {
                "purpose": "worker",
                "instance_type": worker_itype,
                "count": worker_count,
                "cpu": 0,
                "memory_gb": 0,
                "storage_gb": 0,
                "os": "RHCOS",
                "os_billing": "Linux",
                "cloud": cloud,
                "count_formula": count_formula,
                "instance_type_formula": None,
            }
        )
        return

    # Pattern 2: CNV cores/memory
    worker_cores_raw = (
        parent_vars.get("ai_workers_cores")
        or parent_vars.get("ai_worker_cores")
        or ""
    )
    worker_memory_raw = (
        parent_vars.get("ai_workers_memory")
        or parent_vars.get("ai_worker_memory")
        or ""
    )
    if worker_cores_raw and worker_memory_raw:
        try:
            worker_cores = int(float(str(worker_cores_raw)))
        except (ValueError, TypeError):
            worker_cores = 0
        worker_memory = parse_memory_to_gi(str(worker_memory_raw))
        if worker_cores and worker_memory:
            sub_components.append(
                {
                    "purpose": "worker",
                    "instance_type": f"{worker_cores}_core_{worker_memory}Gi",
                    "count": worker_count,
                    "cpu": worker_cores,
                    "memory_gb": worker_memory,
                    "storage_gb": 0,
                    "os": "RHCOS",
                    "cloud": "CNV",
                    "count_formula": count_formula,
                }
            )


# ---------------------------------------------------------------------------
# Sandbox cluster resolution
# ---------------------------------------------------------------------------


async def _resolve_sandbox_clusters(
    ci_name: str,
    stage: str,
    sandboxes: list,
    definition: dict,
    source: "CatalogSource",
) -> list[Component]:
    """Resolve OCP cluster resources from __meta__.sandboxes entries.

    Labs with OcpSandbox entries get a pre-provisioned OCP cluster from a
    shared pool.
    """
    if not sandboxes or not isinstance(sandboxes, list):
        return []

    all_components: list[Component] = []

    for sandbox in sandboxes:
        if not isinstance(sandbox, dict):
            continue
        kind = sandbox.get("kind", "")
        if kind != "OcpSandbox":
            continue

        cloud_selector = sandbox.get("cloud_selector", {})
        cloud = cloud_selector.get("cloud", "cnv")

        cluster_size = definition.get("cluster_size", "")
        if isinstance(cluster_size, str) and "{{" in cluster_size:
            cluster_size = extract_jinja_default(cluster_size)
        cluster_size = str(cluster_size) if cluster_size else ""

        # Determine pool AVC based on cloud
        if cloud == "cnv":
            pool_avc = "agd-v2.ocp-cluster-cnv-pools"
            pool_stage = stage if stage in ("event", "dev") else "event"
        elif cloud == "aws":
            pool_avc = "agd-v2.ocp-cluster-aws-pools"
            pool_stage = stage if stage in ("event", "dev") else "event"
        else:
            continue

        logger.info(
            "  Resolving OcpSandbox cluster -> %s.%s (cluster_size=%s)",
            pool_avc, pool_stage, cluster_size or "default",
        )

        # Fetch the pool AVC definition
        pool_def = await source.get_component_definition(pool_avc, pool_stage)
        if not pool_def:
            logger.warning("  Failed to fetch sandbox pool AVC %s.%s", pool_avc, pool_stage)
            continue

        # Resolve URL lookups (some pool AVCs fetch instances from git)
        await _resolve_url_lookups(pool_def)

        # Extract cluster instances
        cluster_instances = extract_linked_instances(pool_def, cluster_size)
        if not cluster_instances:
            logger.debug("  No instances extracted from sandbox pool AVC %s.%s", pool_avc, pool_stage)
            continue

        # Look up the pool name
        pool_name = await source.lookup_pool_by_ci(f"{ci_name}.{stage}")
        if not pool_name:
            pool_name = await source.lookup_pool_name(f"{ci_name}.{stage}", cluster_size)
        if not pool_name:
            pool_name = await source.lookup_pool_name(f"{pool_avc}.{pool_stage}", cluster_size)

        # Tag components with group metadata
        group_name = sandbox.get("namespace_suffix", "openshift")
        for comp in cluster_instances:
            all_components.append(
                comp.model_copy(
                    update={
                        "group": group_name,
                        "group_display_name": "OpenShift Cluster",
                        "group_ci_name": pool_avc,
                    }
                )
            )

        logger.info(
            "  Extracted %d sandbox cluster instances from %s (cluster_size=%s)",
            len(cluster_instances), pool_avc, cluster_size or "default",
        )

    return all_components


# ---------------------------------------------------------------------------
# Linked component resolution
# ---------------------------------------------------------------------------


async def _resolve_linked_components(
    ci_name: str,
    stage: str,
    item_data: "CatalogItemData | None",
    source: "CatalogSource",
) -> list[Component]:
    """Resolve linked components from CatalogItem spec.linkedComponents.

    Fetches linked AVCs, extracts instances, returns tagged Components.
    """
    from ci_inspector.core.models import CatalogItemData

    if item_data is None:
        return []

    linked_components = item_data.linked_components
    if not linked_components:
        return []

    # Build resource lookup from item_data.resources (full spec.resources list)
    # and fall back to meta.components for git-sourced items
    resource_by_name: dict[str, dict] = {}
    for res in item_data.resources or item_data.meta.get("components", []):
        rname = res.get("name", "")
        if rname:
            resource_by_name[rname] = res

    all_linked: list[Component] = []

    for lc in linked_components:
        if isinstance(lc, str):
            lc = {"name": lc}
        lc_name = lc.get("name", "")
        lc_display_name = lc.get("displayName", lc_name)

        # Find matching resource
        resource = resource_by_name.get(lc_name)
        if not resource:
            # Try direct provider name from linked component
            provider_name = lc_name
        else:
            provider = resource.get("provider", {})
            provider_name = provider.get("name", "")

        if not provider_name:
            logger.debug("  No provider name for linkedComponent %s", lc_name)
            continue

        # Parse provider.name to ci_name + stage
        parts = provider_name.split(".")
        if len(parts) >= 3:
            linked_stage = parts[-1]
            linked_ci_name = ".".join(parts[:-1])
        elif len(parts) == 2:
            linked_ci_name = provider_name
            linked_stage = "prod"
        else:
            linked_ci_name = provider_name
            linked_stage = stage

        logger.info("  Resolving linked component %s -> AVC %s.%s", lc_name, linked_ci_name, linked_stage)

        # Fetch the linked AVC definition
        linked_def = await source.get_component_definition(linked_ci_name, linked_stage)
        if not linked_def:
            logger.warning("  Failed to fetch linked AVC %s.%s", linked_ci_name, linked_stage)
            continue

        # Read AgnosticD defaults for the linked AVC and merge
        linked_spec = linked_def.pop("__spec__", {})
        linked_scm_url = linked_spec.get("agnosticvRepo", "")
        linked_env_type = linked_def.get("env_type", "") or ""
        if linked_env_type.startswith("{{"):
            stripped = linked_env_type.strip()[2:-2].strip()
            linked_env_type = linked_def.get(stripped, "") or ""
        linked_config = linked_def.get("config", "") or ""
        if not linked_env_type and linked_config:
            linked_env_type = linked_config
        linked_cloud = linked_def.get("cloud_provider", "") or ""
        linked_ref = linked_def.get("scm_ref", "") or "main"

        agd_defaults = await read_agnosticd_defaults(
            linked_scm_url, linked_ref, linked_env_type, linked_cloud
        )
        merged_linked = {**agd_defaults, **linked_def}

        # Resolve URL lookups
        await _resolve_url_lookups(merged_linked)

        # Determine cluster_size
        cluster_size = ""
        if resource:
            resource_params = (
                resource.get("template", {})
                .get("spec", {})
                .get("vars", {})
                .get("job_vars", {})
            )
            cluster_size = resource_params.get("cluster_size", "")
        if not cluster_size:
            cs_raw = merged_linked.get("cluster_size", "")
            if isinstance(cs_raw, str) and "{{" in cs_raw:
                cluster_size = extract_jinja_default(cs_raw)
            elif cs_raw:
                cluster_size = str(cs_raw)

        # Extract instances from merged defaults + AVC definition
        linked_instances = extract_linked_instances(merged_linked, cluster_size)
        if not linked_instances:
            linked_instances = extract_instances(merged_linked, linked_cloud)
        else:
            # Also extract non-pool instances (e.g., bastion from AgnosticD defaults)
            extra = extract_instances(merged_linked, linked_cloud)
            existing_purposes = {c.purpose for c in linked_instances}
            for c in extra:
                if c.purpose not in existing_purposes:
                    linked_instances.append(c)
                    existing_purposes.add(c.purpose)
        if not linked_instances:
            logger.debug("  No instances extracted from linked AVC %s.%s", linked_ci_name, linked_stage)
            continue

        # Look up pool name
        pool_name = await source.lookup_pool_by_ci(f"{ci_name}.{stage}")
        if not pool_name:
            pool_name = await source.lookup_pool_name(provider_name, cluster_size)

        # Tag each component with group metadata
        for comp in linked_instances:
            all_linked.append(
                comp.model_copy(
                    update={
                        "group": lc_name,
                        "group_display_name": lc_display_name,
                        "group_ci_name": linked_ci_name,
                    }
                )
            )

        logger.info(
            "  Extracted %d linked instances from %s (cluster_size=%s)",
            len(linked_instances), linked_ci_name, cluster_size or "none",
        )

    return all_linked


# ---------------------------------------------------------------------------
# Meta-component resolution
# ---------------------------------------------------------------------------


async def _resolve_meta_components(
    ci_name: str,
    stage: str,
    merged: dict,
    binder_components: list | dict,
    parent_definition: dict,
    source: "CatalogSource",
    _depth: int = 0,
) -> list[Component]:
    """Resolve __meta__.components for labs that have their own infra.

    Unlike _discover_binder (which handles pure binders), this resolves
    pool cluster components alongside the lab's own instances.
    """
    all_components: list[Component] = []

    # Normalize to list of (name, config) pairs
    if isinstance(binder_components, dict):
        items = list(binder_components.items())
    else:
        items = [(c.get("name", f"component-{i}"), c) for i, c in enumerate(binder_components)]

    for comp_name, comp_config in items:
        if not isinstance(comp_config, dict):
            continue

        comp_path = comp_config.get("item", "") or comp_config.get("path", "") or ""
        if not comp_path:
            continue

        sub_ci_name, sub_stage = _resolve_binder_crd_name(comp_path, default_stage=stage)
        parameter_values = comp_config.get("parameter_values", {}) or {}

        # Forward parent catalog parameters routed to this sub-component
        routed = _build_routed_param_overrides(parent_definition, comp_name)
        if routed:
            parameter_values = {**routed, **parameter_values}
        display_name = comp_config.get("display_name", comp_name)

        logger.info("  Resolving meta component %s -> %s.%s", comp_name, sub_ci_name, sub_stage)

        # Recurse into the sub-component
        sub_result = await discover(
            sub_ci_name,
            sub_stage,
            source,
            _depth=_depth + 1,
            _parameter_overrides=parameter_values if parameter_values else None,
        )

        # Tag components with group info + inject parent workers
        cluster_size = parameter_values.get("cluster_size", "")
        sub_dicts = [c.model_dump() for c in sub_result.components]
        sub_cloud = sub_result.cloud_provider or "CNV"

        # Determine if we should skip worker injection
        sub_cp = (sub_result.cloud_provider or "").lower()
        parent_defines_workers = "worker_instance_count" in merged
        parent_worker_only = merged.get("worker_only") in (True, "true", "True")
        skip_inject = (
            sub_cp in ("aws", "azure", "gcp")
            or (cluster_size == "sno" and not parent_defines_workers)
            or parent_worker_only
        )
        if not skip_inject:
            inject_parent_workers(merged, sub_dicts, sub_cloud)

        # Look up pool name
        pool_name = await source.lookup_pool_by_ci(f"{ci_name}.{stage}")
        if not pool_name:
            provider_name = f"{sub_ci_name}.{sub_stage}"
            pool_name = await source.lookup_pool_name(provider_name, cluster_size)

        for cd in sub_dicts:
            cd["group"] = comp_name
            cd["group_display_name"] = display_name
            cd["group_ci_name"] = sub_ci_name
            all_components.append(Component(**cd))

    return all_components


# ---------------------------------------------------------------------------
# Renamed item detection
# ---------------------------------------------------------------------------


async def _find_renamed_item(
    ci_name: str, stage: str, source: "CatalogSource"
) -> tuple[str | None, str | None]:
    """Search the catalog for an item that was renamed from ci_name.

    Uses asset_uuid as a stable identifier.
    Returns (new_ci_name, new_stage) or (None, None).
    """
    catalog = await source.list_catalog_items()

    # Find the asset_uuid for the old ci_name
    old_uuid = ""
    for item in catalog:
        if item.ci_name == ci_name and item.stage == stage:
            old_uuid = item.definition.get("asset_uuid", "")
            break

    if not old_uuid:
        return None, None

    for item in catalog:
        if item.definition.get("asset_uuid") == old_uuid and item.ci_name != ci_name:
            return item.ci_name, item.stage

    return None, None


# ---------------------------------------------------------------------------
# Cloud normalization from instance type patterns
# ---------------------------------------------------------------------------


def _normalize_cloud_from_instances(components: list[Component]) -> str:
    """Detect cloud from instance type patterns when cloud_provider is unset."""
    itypes = [c.instance_type or "" for c in components]
    if any("_core_" in it for it in itypes):
        return "CNV"
    if any(re.match(r"^[a-z]\d[a-z]*\.", it) for it in itypes):
        return "AWS"
    if any(it.startswith("Standard_") for it in itypes):
        return "Azure"
    if any(re.match(r"^[a-z]\d-", it) for it in itypes):
        return "GCP"
    return ""


# ---------------------------------------------------------------------------
# Metadata extraction helpers
# ---------------------------------------------------------------------------


def _extract_metadata(merged: dict, meta: dict) -> dict:
    """Extract lab-level metadata fields from merged vars and __meta__."""
    # showroom_url
    showroom_url = (_extract_showroom_url(merged) or "").removesuffix(".git") or None

    # showroom_ref
    showroom_ref = "main"
    for key, val in merged.items():
        if isinstance(val, str) and "showroom_content_git_repo_ref" in key:
            showroom_ref = val

    # persistent
    persistent = merged.get("persistent")
    if isinstance(persistent, str):
        persistent = persistent.lower() in ("true", "yes", "1")
    elif persistent is not None:
        persistent = bool(persistent)

    # can_be_stopped
    action_schedule = meta.get("lifecycle", {}).get("action_schedule") if meta.get("lifecycle") else None
    can_be_stopped = bool(action_schedule) if action_schedule else None

    # uses_maas / maas_models
    maas_models = merged.get("ocp4_workload_litellm_virtual_keys_models")
    if isinstance(maas_models, list):
        maas_models = [str(m) for m in maas_models if not (isinstance(m, str) and "{{" in str(m))]
        if not maas_models:
            maas_models = None
    else:
        maas_models = None
    uses_maas = bool(maas_models)

    return {
        "showroom_url": showroom_url,
        "showroom_ref": showroom_ref,
        "persistent": persistent,
        "can_be_stopped": can_be_stopped,
        "uses_maas": uses_maas,
        "maas_models": maas_models,
    }


# ---------------------------------------------------------------------------
# Bastion fallback
# ---------------------------------------------------------------------------

_BASTION_DEFAULTS = {
    "ec2": ("t3a.medium", "AWS"),
    "aws": ("t3a.medium", "AWS"),
    "azure": ("Standard_D4s_v3", "Azure"),
    "gcp": ("e2-medium", "GCP"),
}


def _add_bastion_fallback(
    components: list[Component],
    env_type: str,
    cloud_provider: str,
    merged: dict,
) -> None:
    """Add a default bastion if expected but not found.

    OCP cluster configs normally provision a bastion. If one is missing,
    add a cloud-appropriate default.
    """
    has_bastion = any("bastion" in (c.purpose or "").lower() for c in components)
    if has_bastion:
        return
    if env_type not in ("ocp4-cluster", "openshift-cluster"):
        return
    if not (merged.get("install_bastion", True) or merged.get("dns_bastion", False)):
        return

    cp = cloud_provider.lower() if cloud_provider else ""
    if cp in _BASTION_DEFAULTS:
        itype, cloud = _BASTION_DEFAULTS[cp]
        components.append(
            Component(
                purpose="bastion",
                instance_type=itype,
                count=1,
                cpu=0,
                memory_gb=0,
                os="RHEL9",
                os_billing="RHEL",
                storage_gb=0,
                cloud=cloud,
            )
        )
        logger.info("  Added default bastion fallback (%s) for %s/%s", itype, cloud_provider, env_type)


# ---------------------------------------------------------------------------
# Main discovery orchestrator
# ---------------------------------------------------------------------------


async def discover(
    ci_name: str,
    stage: str,
    source: "CatalogSource",
    _depth: int = 0,
    _parameter_overrides: dict | None = None,
) -> DiscoveryResult:
    """Main discovery entry point.

    Pipeline:
    1. Fetch catalog item from source
    2. Fetch AVC definition if needed
    3. Apply parameter overrides and catalog parameter defaults
    4. Read AgnosticD defaults and merge with CRD overrides
    5. Resolve URL lookups for instances/containers
    6. Check for binder pattern (__meta__.components without own instances)
    7. Extract instances from merged vars
    7b. Extract MachineSet groups (Pattern F)
    7c. Extract CNV workload VMs
    7d. Bastion fallback
    8. Resolve linked components
    9. Resolve meta components
    10. Cloud normalization
    11. Extract metadata
    12. Return DiscoveryResult
    """
    if _depth > _MAX_BINDER_DEPTH:
        return DiscoveryResult(
            ci_name=ci_name,
            stage=stage,
            components=[],
            is_binder=False,
            errors=[f"Max binder depth ({_MAX_BINDER_DEPTH}) exceeded"],
        )

    item = await source.get_catalog_item(ci_name, stage)
    if item is None:
        # Check for renamed item
        if _depth == 0:
            renamed_ci, renamed_stage = await _find_renamed_item(ci_name, stage, source)
            if renamed_ci and renamed_stage:
                logger.info("  %s.%s appears renamed to %s.%s", ci_name, stage, renamed_ci, renamed_stage)
                return await discover(renamed_ci, renamed_stage, source, _depth, _parameter_overrides)
        return DiscoveryResult(
            ci_name=ci_name,
            stage=stage,
            components=[],
            is_binder=False,
            errors=[f"Catalog item '{ci_name}.{stage}' not found"],
        )

    definition = item.definition
    meta = item.meta
    warnings: list[str] = []

    # CatalogItems don't contain the full definition -- fetch the AVC
    if not definition.get("cloud_provider") and not definition.get("env_type"):
        avc_defn = await source.get_component_definition(ci_name, stage)
        if avc_defn:
            avc_spec = avc_defn.pop("__spec__", {})
            definition = {**definition, **avc_defn}
            if not item.agnosticv_repo and avc_spec.get("agnosticvRepo"):
                item = item.model_copy(update={"agnosticv_repo": avc_spec["agnosticvRepo"]})

    # Apply parameter overrides
    if _parameter_overrides:
        definition = {**definition, **_parameter_overrides}

    # Apply catalog parameter defaults (openAPIV3Schema.default)
    meta_params = meta.get("catalog", {}).get("parameters", [])
    if not isinstance(meta_params, list):
        # Try from definition __meta__
        meta_params = definition.get("__meta__", {}).get("catalog", {}).get("parameters", [])
    if isinstance(meta_params, list):
        for param in meta_params:
            pname = param.get("name", "")
            if pname and pname not in definition:
                pdefault = (param.get("openAPIV3Schema") or {}).get("default")
                if pdefault is not None:
                    definition[pname] = pdefault

    # Extract key fields
    cloud_provider = definition.get("cloud_provider", "") or ""
    env_type = definition.get("env_type", "") or ""
    # Resolve env_type if it's a Jinja template reference
    if env_type.startswith("{{"):
        stripped = env_type.strip()[2:-2].strip()
        env_type = definition.get(stripped, "") or ""
    config = definition.get("config", "") or ""
    if not env_type and config:
        env_type = config

    deployer = definition.get("__meta__", {}).get("deployer", {})
    # Deployer scm_url points to AgnosticD (infra defaults), agnosticv_repo points
    # to AgnosticV (catalog configs). Prefer deployer for AgnosticD lookups.
    scm_url = (
        deployer.get("scm_url", "")
        or definition.get("scm_url", "")
        or item.agnosticv_repo
        or definition.get("agnosticv_repo", "")
    )
    scm_ref = deployer.get("scm_ref", "") or definition.get("scm_ref", "") or "main"

    # Read AgnosticD defaults and merge
    agnosticd_defaults = await read_agnosticd_defaults(
        scm_url, scm_ref, env_type, cloud_provider
    )
    merged = {**agnosticd_defaults, **_strip_secrets(definition)}
    merged.pop("__meta__", None)

    # Stash AgnosticD instances before URL lookup overwrites them
    agd_instances = agnosticd_defaults.get("instances")
    if isinstance(agd_instances, list) and agd_instances:
        merged["_agnosticd_instances"] = agd_instances

    # Resolve URL lookups (instances/containers from git repos)
    await _resolve_url_lookups(merged)

    # Check for binder pattern — look in BOTH meta (from CatalogItem) and
    # definition.__meta__ (from the AVC, which is the authoritative source)
    binder_components = meta.get("components", [])
    avc_meta = definition.get("__meta__", {})
    avc_meta_components = avc_meta.get("components", {})

    # Prefer AVC __meta__.components over CatalogItem meta.components
    # AVC components have parameter_values, when conditions, etc.
    if avc_meta_components and isinstance(avc_meta_components, (list, dict)):
        binder_components = avc_meta_components

    has_own_infra = bool(merged.get("instances")) or bool(merged.get("ai_control_plane_cores"))
    if not has_own_infra:
        has_own_infra = any(k.endswith("_instance_type") for k in merged if merged.get(k))

    if binder_components and isinstance(binder_components, (list, dict)) and not has_own_infra:
        # Check if these are __meta__-style entries (with path/item keys)
        _entries = binder_components if isinstance(binder_components, list) else (
            [{"name": k, **v} for k, v in binder_components.items()] if isinstance(binder_components, dict) else []
        )
        _is_meta_style = any(
            isinstance(c, dict) and (c.get("path") or c.get("item"))
            for c in _entries
        )
        if _is_meta_style:
            return await _discover_binder(
                ci_name, stage, definition, binder_components, source, _depth
            )

    # Linked-component binder pattern (K8s source):
    # CatalogItem has linkedComponents + resources but no own infra → binder
    if (
        not has_own_infra
        and _depth == 0
        and item.linked_components
        and item.resources
    ):
        linked = await _resolve_linked_components(ci_name, stage, item, source)
        if linked:
            # This is a binder — extract metadata and return
            parent_meta = definition.get("__meta__", {})
            metadata = _extract_metadata(definition, parent_meta)
            return DiscoveryResult(
                ci_name=ci_name,
                stage=stage,
                components=linked,
                is_binder=True,
                env_type=env_type or None,
                cloud_provider=_normalize_cloud_from_instances(linked) or None,
                showroom_url=metadata["showroom_url"],
                showroom_ref=metadata["showroom_ref"],
                persistent=metadata["persistent"],
                can_be_stopped=metadata["can_be_stopped"],
                uses_maas=metadata["uses_maas"],
                maas_models=metadata["maas_models"],
                warnings=warnings,
            )

    # Extract instances
    components = extract_instances(merged, cloud_provider)

    # Pattern F: MachineSet groups (GPU, metal workers)
    ms_components = _extract_machineset_groups(merged, cloud_provider)
    if ms_components:
        components.extend(ms_components)

    # Extract CNV workload VMs
    cnv_vms = extract_cnv_workload_vms(merged)
    components.extend(cnv_vms)

    # Bastion fallback
    _add_bastion_fallback(components, env_type, cloud_provider, merged)

    # Resolve linked components (only at top level)
    linked_ci_names: set[str] = set()
    if _depth == 0 and item.linked_components:
        linked = await _resolve_linked_components(ci_name, stage, item, source)
        if linked:
            components.extend(linked)
            linked_ci_names = {c.group_ci_name or "" for c in linked if c.group_ci_name}

    # Resolve meta components (labs with own infra + __meta__.components)
    if _depth == 0 and binder_components and isinstance(binder_components, (list, dict)):
        meta_results = await _resolve_meta_components(
            ci_name, stage, merged, binder_components, definition, source, _depth
        )
        if meta_results:
            # Deduplicate against linked components
            if linked_ci_names:
                meta_results = [c for c in meta_results if (c.group_ci_name or "") not in linked_ci_names]
            if meta_results:
                components.extend(meta_results)

    # Resolve sandbox clusters
    sandboxes = meta.get("sandboxes", [])
    if sandboxes and _depth == 0:
        sandbox_components = await _resolve_sandbox_clusters(
            ci_name, stage, sandboxes, definition, source
        )
        if sandbox_components:
            components.extend(sandbox_components)

    # Cloud normalization
    cloud_display = CLOUD_MAP.get(cloud_provider, cloud_provider)
    if not cloud_display or cloud_display.lower() == "none":
        cloud_display = _normalize_cloud_from_instances(components)
    if cloud_display and cloud_display.lower() == "none":
        cloud_display = ""
    # Fill in missing cloud on components
    for i, comp in enumerate(components):
        comp_cloud = comp.cloud or ""
        if not comp_cloud or comp_cloud.lower() == "none":
            components[i] = comp.model_copy(update={"cloud": cloud_display})

    # Extract metadata
    metadata = _extract_metadata(merged, meta)

    return DiscoveryResult(
        ci_name=ci_name,
        stage=stage,
        components=components,
        is_binder=False,
        env_type=env_type or None,
        cloud_provider=CLOUD_MAP.get(cloud_provider, cloud_provider) or cloud_display or None,
        showroom_url=metadata["showroom_url"],
        showroom_ref=metadata["showroom_ref"],
        persistent=metadata["persistent"],
        can_be_stopped=metadata["can_be_stopped"],
        uses_maas=metadata["uses_maas"],
        maas_models=metadata["maas_models"],
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Merged YAML discovery (for CI pipelines using agnosticv --merge)
# ---------------------------------------------------------------------------


async def discover_from_merged(
    ci_name: str,
    stage: str,
    merged_yaml: dict,
) -> DiscoveryResult:
    """Discover infrastructure from a pre-merged YAML dict.

    Use this when the caller already has the fully merged catalog item
    definition (e.g., output of ``agnosticv --git=false --merge``).
    Skips CatalogSource-dependent steps (linked components, sandbox
    resolution, renamed item lookup).
    """
    definition = dict(merged_yaml)
    meta = definition.pop("__meta__", {})
    warnings: list[str] = []

    cloud_provider = definition.get("cloud_provider", "") or ""
    env_type = definition.get("env_type", "") or ""
    if env_type.startswith("{{"):
        stripped = env_type.strip()[2:-2].strip()
        env_type = definition.get(stripped, "") or ""
    config = definition.get("config", "") or ""
    if not env_type and config:
        env_type = config

    deployer = meta.get("deployer", {})
    scm_url = deployer.get("scm_url", "") or definition.get("scm_url", "")
    scm_ref = deployer.get("scm_ref", "") or definition.get("scm_ref", "") or "main"

    agnosticd_defaults = await read_agnosticd_defaults(
        scm_url, scm_ref, env_type, cloud_provider
    )
    merged = {**agnosticd_defaults, **_strip_secrets(definition)}

    agd_instances = agnosticd_defaults.get("instances")
    if isinstance(agd_instances, list) and agd_instances:
        merged["_agnosticd_instances"] = agd_instances

    await _resolve_url_lookups(merged)

    # Check for binder pattern
    binder_components = meta.get("components", {})
    has_own_infra = bool(merged.get("instances")) or bool(merged.get("ai_control_plane_cores"))
    if not has_own_infra:
        has_own_infra = any(k.endswith("_instance_type") for k in merged if merged.get(k))

    if binder_components and not has_own_infra:
        warnings.append("Binder CI — sub-components not resolved in merged mode. "
                         "Use --source k8s for full binder resolution.")

    # Extract instances
    components = extract_instances(merged, cloud_provider)

    ms_components = _extract_machineset_groups(merged, cloud_provider)
    if ms_components:
        components.extend(ms_components)

    cnv_vms = extract_cnv_workload_vms(merged)
    components.extend(cnv_vms)

    _add_bastion_fallback(components, env_type, cloud_provider, merged)

    # Cloud normalization
    cloud_display = CLOUD_MAP.get(cloud_provider, cloud_provider)
    if not cloud_display or cloud_display.lower() == "none":
        cloud_display = _normalize_cloud_from_instances(components)
    if cloud_display and cloud_display.lower() == "none":
        cloud_display = ""
    for i, comp in enumerate(components):
        comp_cloud = comp.cloud or ""
        if not comp_cloud or comp_cloud.lower() == "none":
            components[i] = comp.model_copy(update={"cloud": cloud_display})

    metadata = _extract_metadata(merged, meta)

    return DiscoveryResult(
        ci_name=ci_name,
        stage=stage,
        components=components,
        is_binder=bool(binder_components and not has_own_infra),
        env_type=env_type or None,
        cloud_provider=CLOUD_MAP.get(cloud_provider, cloud_provider) or cloud_display or None,
        showroom_url=metadata["showroom_url"],
        showroom_ref=metadata["showroom_ref"],
        persistent=metadata["persistent"],
        can_be_stopped=metadata["can_be_stopped"],
        uses_maas=metadata["uses_maas"],
        maas_models=metadata["maas_models"],
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Binder discovery
# ---------------------------------------------------------------------------


async def _discover_binder(
    ci_name: str,
    stage: str,
    parent_definition: dict,
    binder_components: list | dict,
    source: "CatalogSource",
    depth: int,
) -> DiscoveryResult:
    """Resolve a binder catalog item by recursing into sub-components.

    Handles:
    - when conditions (conditional inclusion)
    - parameter routing (parent params routed to sub-components)
    - pool name resolution
    - parent worker injection
    - MachineSet groups and CNV VMs from parent
    """
    all_components: list[Component] = []
    warnings: list[str] = []

    # Normalize to list of (name, config) pairs
    if isinstance(binder_components, dict):
        items = list(binder_components.items())
    else:
        items = [(c.get("name", f"component-{i}"), c) for i, c in enumerate(binder_components)]

    for comp_name, comp_config in items:
        if not isinstance(comp_config, dict):
            continue

        # Evaluate "when" condition -- skip components that don't match
        when_cond = comp_config.get("when", "")
        if when_cond and isinstance(when_cond, str):
            if not _evaluate_when_condition(when_cond, parent_definition):
                logger.info("  Skipping component %s: when condition not met (%s)", comp_name, when_cond)
                continue

        # Get the referenced catalog item
        comp_path = comp_config.get("item", "") or comp_config.get("path", "") or ""
        sub_name = comp_config.get("name", "")

        # Resolve sub-component CI name and stage from path
        if comp_path:
            sub_ci, sub_stage = _resolve_binder_crd_name(comp_path, default_stage=stage)
        elif sub_name:
            sub_ci = sub_name
            sub_stage = stage
        else:
            warnings.append(f"Binder component missing path and name: {comp_config}")
            continue

        # Build parameter overrides: routed params + explicit parameter_values
        parameter_values = comp_config.get("parameter_values", {}) or {}
        routed = _build_routed_param_overrides(parent_definition, comp_name)
        if routed:
            merged_overrides = {**routed, **parameter_values}
        else:
            merged_overrides = parameter_values

        display_name = comp_config.get("display_name", comp_name)

        sub_result = await discover(
            sub_ci, sub_stage, source, _depth=depth + 1,
            _parameter_overrides=merged_overrides if merged_overrides else None,
        )

        # Inject parent workers into sub-components if needed
        sub_dicts = [c.model_dump() for c in sub_result.components]
        sub_cloud = sub_result.cloud_provider or "CNV"

        # Skip injection for cloud-native, SNO without parent workers, or worker_only
        sub_cp = (sub_result.cloud_provider or "").lower()
        parent_defines_workers = "worker_instance_count" in parent_definition
        parent_worker_only = parent_definition.get("worker_only") in (True, "true", "True")
        cluster_size = merged_overrides.get("cluster_size", "")
        skip_inject = (
            sub_cp in ("aws", "azure", "gcp")
            or (cluster_size == "sno" and not parent_defines_workers)
            or parent_worker_only
        )
        if not skip_inject:
            inject_parent_workers(parent_definition, sub_dicts, sub_cloud)

        # Look up pool name
        pool_name = await source.lookup_pool_by_ci(f"{ci_name}.{stage}")
        if not pool_name:
            provider_name = f"{sub_ci}.{sub_stage}"
            pool_name = await source.lookup_pool_name(provider_name, cluster_size)

        for cd in sub_dicts:
            cd["group"] = comp_name if comp_name != sub_name else (sub_name or sub_ci)
            cd["group_ci_name"] = sub_ci
            cd["group_display_name"] = display_name
            all_components.append(Component(**cd))

        warnings.extend(sub_result.warnings)

    # Extract MachineSet groups from parent definition
    parent_cloud = (parent_definition.get("cloud_provider") or "none").lower()
    if parent_cloud in ("none", ""):
        parent_cloud = next(
            (c.cloud.lower() for c in all_components if c.cloud and c.cloud.lower() not in ("unknown", "")),
            "none",
        )
        parent_cloud = {"aws": "aws", "azure": "azure", "gcp": "gcp", "cnv": "openshift_cnv"}.get(
            parent_cloud, parent_cloud
        )
    ms_components = _extract_machineset_groups(parent_definition, parent_cloud)
    if ms_components:
        all_components.extend(ms_components)

    # Extract CNV workload VMs from parent definition
    vm_components = extract_cnv_workload_vms(parent_definition)
    if vm_components:
        all_components.extend(vm_components)

    # Extract metadata from parent definition
    parent_meta = parent_definition.get("__meta__", {})
    metadata = _extract_metadata(parent_definition, parent_meta)

    # Resolve env_type from parent
    env_type = parent_definition.get("env_type", "") or ""
    if env_type.startswith("{{"):
        stripped = env_type.strip()[2:-2].strip()
        env_type = parent_definition.get(stripped, "") or ""
    config_val = parent_definition.get("config", "") or ""
    if not env_type and config_val:
        env_type = config_val

    return DiscoveryResult(
        ci_name=ci_name,
        stage=stage,
        components=all_components,
        is_binder=True,
        env_type=env_type or None,
        showroom_url=metadata["showroom_url"],
        showroom_ref=metadata["showroom_ref"],
        persistent=metadata["persistent"],
        can_be_stopped=metadata["can_be_stopped"],
        uses_maas=metadata["uses_maas"],
        maas_models=metadata["maas_models"],
        warnings=warnings,
    )
