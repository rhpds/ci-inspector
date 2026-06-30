from pathlib import Path

import yaml

from ci_inspector.core.discovery import (
    _add_bastion_fallback,
    _build_routed_param_overrides,
    _evaluate_when_condition,
    _extract_machineset_groups,
    _extract_metadata,
    _extract_showroom_url,
    _normalize_cloud_from_instances,
    _resolve_binder_crd_name,
    _resolve_git_config_url,
    _strip_secrets,
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


class TestExtractMachinesetGroups:
    def test_extracts_groups(self):
        merged = _load("pattern_f_machineset.yml")
        components = _extract_machineset_groups(merged, "ec2")
        assert len(components) == 2  # disabled-group (count=0) is skipped
        gpu = next(c for c in components if c.purpose == "gpu-workers")
        assert gpu.instance_type == "p3.2xlarge"
        assert gpu.count == 2
        assert gpu.cloud == "AWS"
        metal = next(c for c in components if c.purpose == "metal-workers")
        assert metal.instance_type == "m5.metal"
        assert metal.count == 1

    def test_skips_zero_replicas(self):
        merged = _load("pattern_f_machineset.yml")
        components = _extract_machineset_groups(merged, "ec2")
        purposes = {c.purpose for c in components}
        assert "disabled-group" not in purposes

    def test_empty_groups(self):
        assert _extract_machineset_groups({}, "ec2") == []
        assert _extract_machineset_groups({"ocp4_workload_machinesets_machineset_groups": []}, "ec2") == []

    def test_jinja_instance_type(self):
        merged = {
            "ocp4_workload_machinesets_machineset_groups": [
                {"name": "gpu", "instance_type": "{{ gpu_type }}", "total_replicas": 1}
            ],
            "gpu_type": "p4d.24xlarge",
        }
        components = _extract_machineset_groups(merged, "ec2")
        assert len(components) == 1
        assert components[0].instance_type == "p4d.24xlarge"

    def test_returns_component_models(self):
        merged = _load("pattern_f_machineset.yml")
        components = _extract_machineset_groups(merged, "ec2")
        assert all(isinstance(c, Component) for c in components)


class TestEvaluateWhenCondition:
    def test_simple_equality(self):
        assert _evaluate_when_condition("ocp_cloud_provider == 'aws'", {"ocp_cloud_provider": "aws"})
        assert not _evaluate_when_condition("ocp_cloud_provider == 'aws'", {"ocp_cloud_provider": "cnv"})

    def test_simple_inequality(self):
        assert _evaluate_when_condition("ocp_cloud_provider != 'aws'", {"ocp_cloud_provider": "cnv"})
        assert not _evaluate_when_condition("ocp_cloud_provider != 'aws'", {"ocp_cloud_provider": "aws"})

    def test_with_default_filter(self):
        assert _evaluate_when_condition(
            "ocp_cloud_provider | default('') == 'cnv'",
            {"ocp_cloud_provider": "cnv"},
        )
        # Missing var uses default
        assert _evaluate_when_condition(
            "ocp_cloud_provider | default('cnv') == 'cnv'",
            {},
        )
        assert not _evaluate_when_condition(
            "ocp_cloud_provider | default('aws') == 'cnv'",
            {},
        )

    def test_default_inequality(self):
        assert _evaluate_when_condition(
            "ocp_cloud_provider | default('') != 'aws'",
            {"ocp_cloud_provider": "cnv"},
        )

    def test_unparseable_returns_true(self):
        assert _evaluate_when_condition("some_complex_expression", {})

    def test_missing_var_empty_string(self):
        assert _evaluate_when_condition("ocp_cloud_provider == ''", {})


class TestBuildRoutedParamOverrides:
    def test_routes_to_named_component(self):
        parent = {
            "__meta__": {
                "catalog": {
                    "parameters": [
                        {
                            "name": "cluster_size",
                            "components": [{"name": "my-cluster"}],
                        }
                    ]
                }
            },
            "cluster_size": "large",
        }
        result = _build_routed_param_overrides(parent, "my-cluster")
        assert result == {"cluster_size": "large"}

    def test_routes_to_all(self):
        parent = {
            "__meta__": {
                "catalog": {
                    "parameters": [
                        {
                            "name": "ocp_cloud_provider",
                            "components": [{"name": "all"}],
                        }
                    ]
                }
            },
            "ocp_cloud_provider": "cnv",
        }
        result = _build_routed_param_overrides(parent, "any-component")
        assert result == {"ocp_cloud_provider": "cnv"}

    def test_skips_non_matching(self):
        parent = {
            "__meta__": {
                "catalog": {
                    "parameters": [
                        {
                            "name": "cluster_size",
                            "components": [{"name": "other-cluster"}],
                        }
                    ]
                }
            },
            "cluster_size": "large",
        }
        result = _build_routed_param_overrides(parent, "my-cluster")
        assert result == {}

    def test_uses_default_when_value_missing(self):
        parent = {
            "__meta__": {
                "catalog": {
                    "parameters": [
                        {
                            "name": "cluster_size",
                            "components": [{"name": "my-cluster"}],
                            "openAPIV3Schema": {"default": "standard"},
                        }
                    ]
                }
            },
        }
        result = _build_routed_param_overrides(parent, "my-cluster")
        assert result == {"cluster_size": "standard"}

    def test_variable_alias(self):
        parent = {
            "__meta__": {
                "catalog": {
                    "parameters": [
                        {
                            "name": "size",
                            "variable": "cluster_size",
                            "components": [{"name": "my-cluster"}],
                        }
                    ]
                }
            },
            "size": "large",
        }
        result = _build_routed_param_overrides(parent, "my-cluster")
        assert result["size"] == "large"
        assert result["cluster_size"] == "large"

    def test_empty_params(self):
        assert _build_routed_param_overrides({}, "any") == {}
        assert _build_routed_param_overrides({"__meta__": {}}, "any") == {}


class TestResolvBinderCrdName:
    def test_three_part_path(self):
        ci, stage = _resolve_binder_crd_name("agd_v2/ocp-cluster-cnv-pools/event")
        assert ci == "agd-v2.ocp-cluster-cnv-pools"
        assert stage == "event"

    def test_two_part_path_inherits_stage(self):
        ci, stage = _resolve_binder_crd_name("agd_v2/ocp-cluster-cnv-pools", default_stage="prod")
        assert ci == "agd-v2.ocp-cluster-cnv-pools"
        assert stage == "prod"

    def test_single_part_path(self):
        ci, stage = _resolve_binder_crd_name("my-component", default_stage="dev")
        assert ci == "my-component"
        assert stage == "dev"

    def test_underscores_to_hyphens(self):
        ci, stage = _resolve_binder_crd_name("agd_v2/my_component/event")
        assert ci == "agd-v2.my-component"

    def test_lowercased(self):
        ci, stage = _resolve_binder_crd_name("AgD_V2/My-Component/Event")
        assert ci == "agd-v2.my-component"
        assert stage == "event"


class TestStripSecrets:
    def test_removes_secret_keys(self):
        d = {"name": "test", "secret_key": "hidden", "password": "hidden", "data": "visible"}
        result = _strip_secrets(d)
        assert "name" in result
        assert "data" in result
        assert "secret_key" not in result
        assert "password" not in result

    def test_removes_internal_keys(self):
        d = {"__spec__": {"internal": True}, "name": "test"}
        result = _strip_secrets(d)
        assert "__spec__" not in result
        assert "name" in result

    def test_nested_dicts(self):
        d = {"config": {"password": "hidden", "host": "localhost"}}
        result = _strip_secrets(d)
        assert "host" in result["config"]
        assert "password" not in result["config"]


class TestExtractShowroomUrl:
    def test_extracts_url(self):
        merged = _load("showroom_vars.yml")
        url = _extract_showroom_url(merged)
        assert url == "https://github.com/rhpds/my-showroom.git"

    def test_skips_ref_keys(self):
        merged = {"showroom_content_git_repo_ref": "main", "ocp4_workroom": "x"}
        url = _extract_showroom_url(merged)
        assert url is None

    def test_empty(self):
        assert _extract_showroom_url({}) is None


class TestResolveGitConfigUrl:
    def test_builds_url(self):
        merged = {
            "ocp4_workload_showroom_content_git_repo": "https://github.com/rhpds/my-showroom.git",
            "ocp4_workload_showroom_content_git_repo_ref": "develop",
        }
        url = _resolve_git_config_url(merged)
        assert url is not None
        assert "raw.githubusercontent.com" in url
        assert "/develop/config/" in url

    def test_no_repo(self):
        assert _resolve_git_config_url({}) is None


class TestNormalizeCloudFromInstances:
    def test_cnv(self):
        components = [Component(purpose="cp", instance_type="8_core_32Gi", count=1, cpu=8, memory_gb=32, storage_gb=0, cloud="", os="RHCOS")]
        assert _normalize_cloud_from_instances(components) == "CNV"

    def test_aws(self):
        components = [Component(purpose="w", instance_type="m5.xlarge", count=1, cpu=0, memory_gb=0, storage_gb=0, cloud="", os="RHCOS")]
        assert _normalize_cloud_from_instances(components) == "AWS"

    def test_azure(self):
        components = [Component(purpose="w", instance_type="Standard_D4s_v3", count=1, cpu=0, memory_gb=0, storage_gb=0, cloud="", os="RHCOS")]
        assert _normalize_cloud_from_instances(components) == "Azure"

    def test_gcp(self):
        components = [Component(purpose="w", instance_type="e2-medium", count=1, cpu=0, memory_gb=0, storage_gb=0, cloud="", os="RHCOS")]
        assert _normalize_cloud_from_instances(components) == "GCP"

    def test_unknown(self):
        components = [Component(purpose="w", instance_type="custom", count=1, cpu=0, memory_gb=0, storage_gb=0, cloud="", os="RHCOS")]
        assert _normalize_cloud_from_instances(components) == ""

    def test_empty(self):
        assert _normalize_cloud_from_instances([]) == ""


class TestExtractMetadata:
    def test_full_metadata(self):
        merged = _load("showroom_vars.yml")
        meta = {"lifecycle": {"action_schedule": {"start": "0 9 * * *"}}}
        result = _extract_metadata(merged, meta)
        assert result["showroom_url"] == "https://github.com/rhpds/my-showroom"
        assert result["showroom_ref"] == "develop"
        assert result["persistent"] is True
        assert result["can_be_stopped"] is True
        assert result["uses_maas"] is True
        assert result["maas_models"] == ["gpt-4o", "claude-3-sonnet"]

    def test_minimal_metadata(self):
        result = _extract_metadata({}, {})
        assert result["showroom_url"] is None
        assert result["persistent"] is None
        assert result["can_be_stopped"] is None
        assert result["uses_maas"] is False
        assert result["maas_models"] is None

    def test_persistent_string_values(self):
        assert _extract_metadata({"persistent": "false"}, {})["persistent"] is False
        assert _extract_metadata({"persistent": "yes"}, {})["persistent"] is True
        assert _extract_metadata({"persistent": "1"}, {})["persistent"] is True

    def test_maas_filters_jinja(self):
        merged = {"ocp4_workload_litellm_virtual_keys_models": ["gpt-4o", "{{ some_var }}"]}
        result = _extract_metadata(merged, {})
        assert result["maas_models"] == ["gpt-4o"]


class TestBastionFallback:
    def test_adds_aws_bastion(self):
        components: list[Component] = []
        _add_bastion_fallback(components, "ocp4-cluster", "ec2", {"install_bastion": True})
        assert len(components) == 1
        assert components[0].purpose == "bastion"
        assert components[0].instance_type == "t3a.medium"
        assert components[0].cloud == "AWS"

    def test_adds_azure_bastion(self):
        components: list[Component] = []
        _add_bastion_fallback(components, "ocp4-cluster", "azure", {"install_bastion": True})
        assert components[0].instance_type == "Standard_D4s_v3"
        assert components[0].cloud == "Azure"

    def test_adds_gcp_bastion(self):
        components: list[Component] = []
        _add_bastion_fallback(components, "ocp4-cluster", "gcp", {"install_bastion": True})
        assert components[0].instance_type == "e2-medium"
        assert components[0].cloud == "GCP"

    def test_skips_when_bastion_exists(self):
        components = [Component(purpose="bastion", instance_type="m5.large", count=1, cpu=0, memory_gb=0, storage_gb=0, cloud="AWS", os="RHEL9")]
        _add_bastion_fallback(components, "ocp4-cluster", "ec2", {"install_bastion": True})
        assert len(components) == 1  # no duplicate added

    def test_skips_non_ocp_env_type(self):
        components: list[Component] = []
        _add_bastion_fallback(components, "ansible-tower", "ec2", {"install_bastion": True})
        assert len(components) == 0

    def test_skips_when_bastion_disabled(self):
        components: list[Component] = []
        _add_bastion_fallback(components, "ocp4-cluster", "ec2", {"install_bastion": False, "dns_bastion": False})
        assert len(components) == 0
