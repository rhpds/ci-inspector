from pathlib import Path

import pytest
import yaml

from ci_inspector.core.discovery import (
    extract_cnv_workload_vms,
    extract_instances,
    extract_linked_instances,
    inject_parent_workers,
)
from ci_inspector.core.models import Component

FIXTURES = Path(__file__).parent.parent / "fixtures" / "discovery"


def _load(name: str) -> dict:
    return yaml.safe_load((FIXTURES / name).read_text())


class TestExtractInstances:
    def test_pattern_a_cloud_instances(self):
        merged = _load("pattern_a_cloud_instances.yml")
        components = extract_instances(merged, "ec2")
        assert len(components) >= 2
        bastion = next(c for c in components if c.purpose == "bastion")
        assert bastion.instance_type == "t3a.medium"
        assert bastion.count == 1
        assert bastion.cloud == "AWS"
        assert bastion.os == "RHEL9"
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.instance_type == "m5.xlarge"
        assert worker.count == 3

    def test_pattern_a_resolves_jinja_count(self):
        merged = _load("pattern_a_cloud_instances.yml")
        components = extract_instances(merged, "ec2")
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.count == 3
        assert worker.count_formula == "{{ worker_instance_count }}"

    def test_pattern_a_resolves_jinja_instance_type(self):
        merged = _load("pattern_a_cloud_instances.yml")
        components = extract_instances(merged, "ec2")
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.instance_type == "m5.xlarge"
        assert worker.instance_type_formula == "{{ worker_instance_type }}"

    def test_pattern_a_storage_from_volumes(self):
        merged = _load("pattern_a_cloud_instances.yml")
        # Add volumes to bastion
        merged["instances"][0]["volumes"] = [{"size": 50}, {"size": 100}]
        components = extract_instances(merged, "ec2")
        bastion = next(c for c in components if c.purpose == "bastion")
        assert bastion.storage_gb == 150

    def test_pattern_b_role_vars(self):
        merged = _load("pattern_b_role_vars.yml")
        components = extract_instances(merged, "ec2")
        purposes = {c.purpose for c in components}
        assert "bastion" in purposes
        assert "worker" in purposes
        assert "master" in purposes
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.instance_type == "m5.2xlarge"
        assert worker.count == 3
        master = next(c for c in components if c.purpose == "master")
        assert master.instance_type == "m5.xlarge"
        assert master.count == 3

    def test_pattern_b_skips_zero_count(self):
        merged = _load("pattern_b_role_vars.yml")
        merged["worker_instance_count"] = 0
        components = extract_instances(merged, "ec2")
        purposes = {c.purpose for c in components}
        assert "worker" not in purposes

    def test_pattern_c_cnv_instances(self):
        merged = _load("pattern_c_cnv_instances.yml")
        components = extract_instances(merged, "none")
        assert len(components) >= 2
        cp = next(c for c in components if c.purpose == "control-plane")
        assert cp.cpu == 8
        assert cp.memory_gb == 32
        assert cp.cloud == "CNV"
        assert cp.instance_type == "8_core_32Gi"
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.cpu == 16
        assert worker.memory_gb == 64

    def test_pattern_d_ocp_pool(self):
        merged = _load("pattern_d_ocp_pool.yml")
        components = extract_instances(merged, "none")
        cp = next(c for c in components if c.purpose == "control-plane")
        assert cp.cpu == 8
        assert cp.memory_gb == 32
        assert cp.count == 3

    def test_pattern_e_rosa(self):
        merged = _load("pattern_e_rosa.yml")
        components = extract_instances(merged, "ec2")
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.instance_type == "m5.2xlarge"
        assert worker.count == 3
        # Classic ROSA has control-plane nodes
        cp = next(c for c in components if c.purpose == "control-plane")
        assert cp.instance_type == "m5.xlarge"
        assert cp.count == 3

    def test_pattern_e_rosa_hcp(self):
        merged = _load("pattern_e_rosa.yml")
        merged["rosa_deploy_hcp"] = True
        components = extract_instances(merged, "ec2")
        # HCP ROSA has no control-plane nodes (AWS-managed)
        purposes = {c.purpose for c in components}
        assert "worker" in purposes
        assert "control-plane" not in purposes

    def test_pattern_e_rosa_jinja_machine_type(self):
        merged = _load("pattern_e_rosa.yml")
        merged["rosa_compute_machine_type"] = "{{ compute_instance_type }}"
        merged["compute_instance_type"] = "m5.4xlarge"
        components = extract_instances(merged, "ec2")
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.instance_type == "m5.4xlarge"

    def test_returns_component_models(self):
        merged = _load("pattern_a_cloud_instances.yml")
        components = extract_instances(merged, "ec2")
        assert all(isinstance(c, Component) for c in components)

    def test_empty_instances(self):
        result = extract_instances({}, "ec2")
        assert result == []


class TestExtractCnvWorkloadVms:
    def test_extracts_vms(self):
        data = _load("cnv_workload_vms.yml")
        vms = extract_cnv_workload_vms(data)
        assert len(vms) == 2
        gpu = next(v for v in vms if v.purpose == "vm:gpu-vm")
        assert gpu.cpu == 8
        assert gpu.memory_gb == 32
        assert gpu.storage_gb == 100
        assert gpu.count == 2
        assert gpu.cloud == "CNV"
        assert gpu.instance_type == "8_core_32Gi"

    def test_storage_vm(self):
        data = _load("cnv_workload_vms.yml")
        vms = extract_cnv_workload_vms(data)
        storage = next(v for v in vms if v.purpose == "vm:storage-vm")
        assert storage.cpu == 4
        assert storage.memory_gb == 16
        assert storage.storage_gb == 0  # no image_size specified
        assert storage.count == 1

    def test_empty_definition(self):
        assert extract_cnv_workload_vms({}) == []

    def test_no_cnv_instances(self):
        assert extract_cnv_workload_vms({"other_key": "value"}) == []

    def test_returns_component_models(self):
        data = _load("cnv_workload_vms.yml")
        vms = extract_cnv_workload_vms(data)
        assert all(isinstance(v, Component) for v in vms)


class TestExtractLinkedInstances:
    def test_cnv_instances_pattern(self):
        data = _load("pattern_c_cnv_instances.yml")
        components = extract_linked_instances(data, "large")
        assert len(components) >= 2
        cp = next(c for c in components if c.purpose == "control-plane")
        assert cp.cpu == 8
        assert cp.memory_gb == 32
        assert cp.cloud == "CNV"

    def test_ocp_pool_pattern(self):
        data = _load("pattern_d_ocp_pool.yml")
        components = extract_linked_instances(data, "large")
        cp = next(c for c in components if c.purpose == "control-plane")
        assert cp.cpu == 8
        assert cp.memory_gb == 32
        assert cp.count == 3

    def test_ocp_pool_with_workers(self):
        data = {
            "ai_control_plane_cores": 8,
            "ai_control_plane_memory": "32Gi",
            "worker_instance_count": 2,
            "ai_workers_cores": 16,
            "ai_workers_memory": "64Gi",
        }
        components = extract_linked_instances(data, "standard")
        assert len(components) == 2
        cp = next(c for c in components if c.purpose == "control-plane")
        assert cp.cpu == 8
        worker = next(c for c in components if c.purpose == "worker")
        assert worker.cpu == 16
        assert worker.memory_gb == 64
        assert worker.count == 2

    def test_empty_definition(self):
        assert extract_linked_instances({}, "") == []

    def test_returns_component_models(self):
        data = _load("pattern_c_cnv_instances.yml")
        components = extract_linked_instances(data, "large")
        assert all(isinstance(c, Component) for c in components)


class TestInjectParentWorkers:
    def test_cloud_native_workers(self):
        parent = _load("binder_parent.yml")
        sub = [{"purpose": "control-plane", "instance_type": "8_core_32Gi",
                "count": 3, "cpu": 8, "memory_gb": 32, "cloud": "CNV", "os": "RHCOS"}]
        inject_parent_workers(parent, sub, "CNV")
        worker = next(c for c in sub if c["purpose"] == "worker")
        assert worker["instance_type"] == "m5a.4xlarge"
        assert worker["count"] == 3  # formula resolves with num_users=1

    def test_no_inject_when_workers_exist(self):
        parent = {"worker_instance_type": "m5.xlarge", "worker_instance_count": 3}
        sub = [{"purpose": "worker", "instance_type": "existing", "count": 2}]
        inject_parent_workers(parent, sub, "CNV")
        assert len([c for c in sub if c["purpose"] == "worker"]) == 1
        assert sub[0]["instance_type"] == "existing"

    def test_cnv_workers(self):
        parent = {"ai_workers_cores": 16, "ai_workers_memory": "64Gi",
                  "worker_instance_count": 2}
        sub = [{"purpose": "control-plane", "count": 3}]
        inject_parent_workers(parent, sub, "CNV")
        worker = next(c for c in sub if c["purpose"] == "worker")
        assert worker["cpu"] == 16
        assert worker["memory_gb"] == 64
        assert worker["count"] == 2
        assert worker["cloud"] == "CNV"

    def test_no_inject_when_count_zero(self):
        parent = {"worker_instance_type": "m5.xlarge", "worker_instance_count": 0}
        sub = [{"purpose": "control-plane", "count": 3}]
        inject_parent_workers(parent, sub, "CNV")
        purposes = [c["purpose"] for c in sub]
        assert "worker" not in purposes

    def test_jinja_count_formula_preserved(self):
        parent = _load("binder_parent.yml")
        sub = [{"purpose": "control-plane", "count": 3}]
        inject_parent_workers(parent, sub, "CNV")
        worker = next(c for c in sub if c["purpose"] == "worker")
        assert worker.get("count_formula") is not None
        assert "num_users" in worker["count_formula"]

    def test_cloud_override_from_parent(self):
        parent = {"worker_instance_type": "m5.xlarge",
                  "worker_instance_count": 2,
                  "cloud_provider": "ec2"}
        sub = [{"purpose": "control-plane", "count": 3}]
        inject_parent_workers(parent, sub, "CNV")
        worker = next(c for c in sub if c["purpose"] == "worker")
        assert worker["cloud"] == "AWS"

    def test_alt_worker_var_names(self):
        parent = {"ai_worker_cores": 8, "ai_worker_memory": "32Gi",
                  "worker_instance_count": 3}
        sub = [{"purpose": "control-plane", "count": 3}]
        inject_parent_workers(parent, sub, "CNV")
        worker = next(c for c in sub if c["purpose"] == "worker")
        assert worker["cpu"] == 8
        assert worker["memory_gb"] == 32
