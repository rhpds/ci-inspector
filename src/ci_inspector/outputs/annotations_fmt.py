from __future__ import annotations

from ci_inspector.core.models import Finding


def format_annotations(findings: list[Finding], file_path: str) -> str:
    """Format findings as GitHub Actions annotations.

    Args:
        findings: List of validation findings
        file_path: Path to the catalog file (for annotation context)

    Returns:
        GitHub Actions annotation string
    """
    lines: list[str] = []
    for f in findings:
        severity = "error" if f.severity == "error" else "warning"
        lines.append(f"::{severity} file={file_path},line=1::CI Inspector: {f.message}")
    return "\n".join(lines)
