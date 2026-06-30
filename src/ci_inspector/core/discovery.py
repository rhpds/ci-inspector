"""Instance extraction engine for CI Inspector.

Extracts cloud instance components from merged AgnosticD + CRD variables.
Ported from Labagator's resource_discovery.py with database dependencies
removed, returning Pydantic Component models instead of raw dicts.

Handles five extraction patterns:
  A) ``instances`` list with ``flavor`` dict (cloud labs via AgnosticD)
  B) Individual ``{role}_instance_type`` vars (no instances list)
  C) CNV ``instances`` with ``cores``/``memory`` (via extract_linked_instances)
  D) OCP cluster pool vars (ai_control_plane_cores, etc.)
  E) ROSA cluster vars (rosa_deploy, rosa_compute_replicas)

Plus:
  - CNV workload VM extraction from cnv_instances
  - Parent worker injection for binder sub-components
"""

from __future__ import annotations

from ci_inspector.core.helpers import (
    CLOUD_MAP,
    eval_jinja_str,
    extract_jinja_default,
    parse_memory_to_gi,
    resolve_image_to_os,
    resolve_jinja_var,
)
from ci_inspector.core.models import Component


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
    """Pattern E: extract ROSA cluster nodes.

    ROSA clusters are AWS-managed OCP -- workers are provisioned by the
    ROSA service, not via instances list or role vars.
    """
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
        # Classic ROSA also has 3 control-plane nodes (managed by AWS)
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


def extract_cnv_workload_vms(definition: dict) -> list[Component]:
    """Extract CNV workload VMs from cnv_instances list.

    These are VMs provisioned via OpenShift Virtualization on a cluster.
    They consume CPU/memory from cluster workers and should be counted
    in capacity planning.

    Args:
        definition: Merged variable dict containing cnv_instances.

    Returns:
        List of Component models representing CNV VMs.
    """
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


def extract_linked_instances(
    definition: dict, cluster_size: str
) -> list[Component]:
    """Extract components from a linked AVC definition.

    Handles two patterns:
      1. instances list (standard AgnosticD VMs with name/cores/memory/count)
      2. OCP cluster pool vars (ai_control_plane_cores, worker_instance_count, etc.)

    Args:
        definition: Merged variable dict from the linked AVC.
        cluster_size: Cluster size hint (e.g. "standard", "large", "sno").

    Returns:
        List of Component models representing cluster nodes.
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


def inject_parent_workers(
    parent_vars: dict,
    sub_components: list[dict],
    sub_cloud: str,
) -> None:
    """Add worker components from parent lab/binder vars to pool sub-components.

    Mutates sub_components in-place. Does NOT inject if the pool already defines
    its own workers.

    Handles two patterns:
      - Cloud-native: ``worker_instance_type`` (e.g. ``m5a.4xlarge``)
      - CNV: ``ai_workers_cores`` / ``ai_workers_memory``

    ``worker_instance_count`` may be a Jinja formula (resolved with num_users=1
    here; the capacity engine re-evaluates with real num_users at runtime).

    Args:
        parent_vars: Variable dict from the parent lab/binder.
        sub_components: List of component dicts (mutated in-place).
        sub_cloud: Cloud display name of the sub-component.
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
                    "os": "RHCOS",
                    "cloud": "CNV",
                    "count_formula": count_formula,
                }
            )
