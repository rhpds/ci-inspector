from __future__ import annotations

import logging
import time

import httpx
import yaml

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600  # 10 minutes

_REPO_MAP = {
    "agnosticd": "redhat-cop/agnosticd",
    "redhat-cop/agnosticd": "redhat-cop/agnosticd",
    "agnosticd-v2": "agnosticd/agnosticd-v2",
    "agnosticd/agnosticd-v2": "agnosticd/agnosticd-v2",
}


def _resolve_github_repo(scm_url: str) -> str | None:
    if not scm_url:
        return None
    # Check longer keys first to avoid substring false matches
    for key in sorted(_REPO_MAP.keys(), key=len, reverse=True):
        if key in scm_url:
            return _REPO_MAP[key]
    return None


async def _fetch_yaml_from_github(repo: str, ref: str, path: str) -> dict:
    cache_key = f"{repo}/{ref}/{path}"
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {}
            data = yaml.safe_load(resp.text) or {}
            _cache[cache_key] = (now, data)
            return data
    except Exception as e:
        logger.debug("Failed to fetch %s: %s", url, e)
        return {}


async def read_agnosticd_defaults(
    scm_url: str,
    scm_ref: str,
    env_type: str,
    cloud_provider: str,
) -> dict:
    repo = _resolve_github_repo(scm_url)
    if not repo:
        return {}
    if not env_type:
        return {}

    ref = scm_ref or "main"

    base_path = f"ansible/configs/{env_type}/default_vars.yml"
    base_vars = await _fetch_yaml_from_github(repo, ref, base_path)
    if not base_vars:
        base_vars = await _fetch_yaml_from_github(
            repo, ref, f"ansible/configs/{env_type}/default_vars.yaml"
        )

    cloud_vars: dict = {}
    if cloud_provider:
        cloud_path = f"ansible/configs/{env_type}/{cloud_provider}/default_vars.yml"
        cloud_vars = await _fetch_yaml_from_github(repo, ref, cloud_path)
        if not cloud_vars:
            cloud_vars = await _fetch_yaml_from_github(
                repo, ref, f"ansible/configs/{env_type}/{cloud_provider}/default_vars.yaml"
            )
        if not cloud_vars:
            cloud_vars = await _fetch_yaml_from_github(
                repo, ref, f"ansible/configs/{env_type}/default_vars_{cloud_provider}.yml"
            )

    return {**base_vars, **cloud_vars}
