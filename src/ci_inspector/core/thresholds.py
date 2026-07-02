from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from ci_inspector.core.models import (
    DiscoveryResult,
    Finding,
    InspectorConfig,
    ThresholdConfig,
    ThresholdRule,
)

_CONFIG_FILENAME = ".ci-inspector.yml"


def load_config(ci_path: Path, repo_root: Path) -> InspectorConfig:
    configs: list[dict] = []

    current = ci_path if ci_path.is_dir() else ci_path.parent
    while True:
        cfg_file = current / _CONFIG_FILENAME
        if cfg_file.is_file():
            try:
                data = yaml.safe_load(cfg_file.read_text()) or {}
                configs.append(data)
            except Exception:
                pass

        if current == repo_root or current == current.parent:
            break
        current = current.parent

    # Reverse so root is first, most specific is last
    configs.reverse()

    if not configs:
        return InspectorConfig()

    merged_thresholds: dict = {}
    merged_ignore: list[str] = []
    merged_pricing: dict = {}

    for cfg in configs:
        raw_thresholds = cfg.get("thresholds")
        if raw_thresholds is None and "thresholds" in cfg:
            # Explicit null — disable thresholds
            return InspectorConfig(thresholds=None, ignore=merged_ignore)

        if isinstance(raw_thresholds, dict):
            for k, v in raw_thresholds.items():
                if k == "flagged_instance_types" and isinstance(v, list):
                    # Lists: child replaces parent entirely
                    merged_thresholds[k] = v
                elif k == "clouds" and isinstance(v, dict):
                    if "clouds" not in merged_thresholds:
                        merged_thresholds["clouds"] = {}
                    for cloud, overrides in v.items():
                        if cloud not in merged_thresholds["clouds"]:
                            merged_thresholds["clouds"][cloud] = {}
                        merged_thresholds["clouds"][cloud].update(overrides)
                else:
                    merged_thresholds[k] = v

        ignore = cfg.get("ignore", [])
        if isinstance(ignore, list):
            merged_ignore.extend(ignore)

        pricing = cfg.get("pricing", {})
        if isinstance(pricing, dict):
            merged_pricing.update(pricing)

    # Build ThresholdConfig
    flagged = [ThresholdRule(**r) for r in merged_thresholds.pop("flagged_instance_types", [])]
    clouds_raw = merged_thresholds.pop("clouds", {})

    threshold_kwargs = {}
    for field in ("max_vcpus", "max_memory_gb", "max_instances", "max_storage_gb"):
        if field in merged_thresholds:
            threshold_kwargs[field] = int(merged_thresholds[field])
    if "max_cost_per_hour" in merged_thresholds:
        threshold_kwargs["max_cost_per_hour"] = Decimal(str(merged_thresholds["max_cost_per_hour"]))

    tc = ThresholdConfig(
        **threshold_kwargs,
        clouds=clouds_raw,
        flagged_instance_types=flagged,
    )

    return InspectorConfig(
        thresholds=tc,
        ignore=list(set(merged_ignore)),
    )


def check_thresholds(result: DiscoveryResult, config: ThresholdConfig) -> list[Finding]:
    findings: list[Finding] = []

    # Group components by cloud for per-cloud checks
    by_cloud: dict[str, list] = {}
    for c in result.components:
        by_cloud.setdefault(c.cloud, []).append(c)

    # Check global thresholds (using per-cloud override if available)
    for cloud, components in by_cloud.items():
        cloud_overrides = config.clouds.get(cloud, {})

        total_vcpus = sum(c.cpu * c.count for c in components)
        max_vcpus = cloud_overrides.get("max_vcpus", config.max_vcpus)
        if total_vcpus > max_vcpus:
            findings.append(Finding(
                severity=config.severity, rule="max_vcpus",
                message=f"{total_vcpus} vCPUs ({cloud}) exceeds threshold of {max_vcpus}",
                actual_value=f"{total_vcpus} vCPUs",
                threshold_value=f"{max_vcpus} vCPUs",
            ))

        total_mem = sum(c.memory_gb * c.count for c in components)
        max_mem = cloud_overrides.get("max_memory_gb", config.max_memory_gb)
        if total_mem > max_mem:
            findings.append(Finding(
                severity=config.severity, rule="max_memory_gb",
                message=f"{total_mem} GB memory ({cloud}) exceeds threshold of {max_mem}",
                actual_value=f"{total_mem} GB",
                threshold_value=f"{max_mem} GB",
            ))

        total_instances = sum(c.count for c in components)
        max_inst = cloud_overrides.get("max_instances", config.max_instances)
        if total_instances > max_inst:
            findings.append(Finding(
                severity=config.severity, rule="max_instances",
                message=f"{total_instances} instances ({cloud}) exceeds threshold of {max_inst}",
                actual_value=str(total_instances),
                threshold_value=str(max_inst),
            ))

        total_storage = sum(c.storage_gb * c.count for c in components)
        max_stor = cloud_overrides.get("max_storage_gb", config.max_storage_gb)
        if total_storage > max_stor:
            findings.append(Finding(
                severity=config.severity, rule="max_storage_gb",
                message=f"{total_storage} GB storage ({cloud}) exceeds threshold of {max_stor}",
                actual_value=f"{total_storage} GB",
                threshold_value=f"{max_stor} GB",
            ))

        # Cost check
        costs = [c.cost_per_hour * c.count for c in components if c.cost_per_hour is not None]
        if costs:
            total_cost = sum(costs, Decimal("0"))
            max_cost = Decimal(str(cloud_overrides.get("max_cost_per_hour", config.max_cost_per_hour)))
            if total_cost > max_cost:
                findings.append(Finding(
                    severity=config.severity, rule="max_cost_per_hour",
                    message=f"${total_cost}/hr ({cloud}) exceeds threshold of ${max_cost}/hr",
                    actual_value=f"${total_cost}/hr",
                    threshold_value=f"${max_cost}/hr",
                ))

    # Flagged instance types (across all clouds)
    for c in result.components:
        for rule in config.flagged_instance_types:
            if rule.matches(c.instance_type):
                findings.append(Finding(
                    severity=rule.severity,
                    rule="flagged_instance_type",
                    message=f"{c.instance_type}: {rule.message}",
                    actual_value=c.instance_type,
                    threshold_value=rule.pattern,
                ))

    return findings
