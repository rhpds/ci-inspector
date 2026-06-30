from __future__ import annotations

import json

from ci_inspector.core.models import DiscoveryResult, Finding


def format_json(result: DiscoveryResult, findings: list[Finding] | None = None) -> str:
    """Format discovery result and findings as JSON.

    Args:
        result: Discovery result with components and totals
        findings: Optional list of validation findings

    Returns:
        JSON string with discovery data and findings
    """
    data = result.model_dump(mode="json")
    if findings:
        data["findings"] = [f.model_dump() for f in findings]
    return json.dumps(data, indent=2, default=str)
