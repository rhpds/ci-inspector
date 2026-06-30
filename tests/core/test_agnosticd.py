from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ci_inspector.core.agnosticd import read_agnosticd_defaults, _resolve_github_repo

FIXTURES = Path(__file__).parent.parent / "fixtures" / "agnosticd"


def test_resolve_github_repo_agnosticd():
    assert _resolve_github_repo("https://github.com/redhat-cop/agnosticd.git") == "redhat-cop/agnosticd"


def test_resolve_github_repo_agnosticd_v2():
    assert _resolve_github_repo("https://github.com/agnosticd/agnosticd-v2.git") == "agnosticd/agnosticd-v2"


def test_resolve_github_repo_unknown():
    assert _resolve_github_repo("https://gitlab.com/other/repo") is None


def test_resolve_github_repo_empty():
    assert _resolve_github_repo("") is None


@pytest.mark.asyncio
async def test_read_defaults_merges_base_and_cloud():
    base = yaml.safe_load(FIXTURES.joinpath("default_vars.yml").read_text())
    cloud = yaml.safe_load(FIXTURES.joinpath("cloud_vars.yml").read_text())

    async def mock_fetch(repo, ref, path):
        if "ec2" in path or "aws" in path:
            return cloud
        return base

    with patch("ci_inspector.core.agnosticd._fetch_yaml_from_github", side_effect=mock_fetch):
        result = await read_agnosticd_defaults(
            scm_url="https://github.com/redhat-cop/agnosticd.git",
            scm_ref="main",
            env_type="ocp4-cluster",
            cloud_provider="ec2",
        )

    assert result["bastion_instance_type"] == "t3a.large"  # cloud overrides base
    assert result["aws_region"] == "us-east-1"  # base preserved
    assert result["worker_instance_type"] == "m5.xlarge"  # cloud-only


@pytest.mark.asyncio
async def test_read_defaults_no_env_type():
    result = await read_agnosticd_defaults(
        scm_url="https://github.com/redhat-cop/agnosticd.git",
        scm_ref="main",
        env_type="",
        cloud_provider="ec2",
    )
    assert result == {}


@pytest.mark.asyncio
async def test_read_defaults_no_scm_url():
    result = await read_agnosticd_defaults(
        scm_url="",
        scm_ref="main",
        env_type="ocp4-cluster",
        cloud_provider="ec2",
    )
    assert result == {}
