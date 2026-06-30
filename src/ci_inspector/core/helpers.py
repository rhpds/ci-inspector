from __future__ import annotations

import re

from jinja2 import BaseLoader, Environment, Undefined

_jinja_env = Environment(loader=BaseLoader(), undefined=Undefined)


def resolve_jinja_var(template, context: dict) -> str | int | float:
    if not isinstance(template, str) or "{{" not in template:
        return template

    simple = re.match(r"^\s*\{\{\s*(\w+)\s*\}\}\s*$", template)
    if simple:
        var_name = simple.group(1)
        val = context.get(var_name, "")
        if isinstance(val, str) and "{{" in val:
            return resolve_jinja_var(val, context)
        return str(val) if val else ""

    return eval_jinja_str(template, **{k: v for k, v in context.items() if isinstance(v, (str, int, float, bool))})


def eval_jinja_str(template: str, **kwargs) -> str:
    try:
        rendered = _jinja_env.from_string(template).render(**kwargs)
        return rendered.strip()
    except Exception:
        return ""


def extract_jinja_default(template: str) -> str:
    m = re.search(r"default\(\s*['\"]([^'\"]*)['\"]", template)
    return m.group(1) if m else ""


def resolve_image_to_os(image_str: str | None) -> tuple[str, str]:
    if not image_str or not isinstance(image_str, str):
        return "Linux", "Linux"

    img = image_str.strip()
    is_gold = bool(re.search(r"GOLD|gold|BYOS|byos", img))

    if re.search(r"[Ww]indows|win-", img):
        return "Windows", "Windows"
    if re.search(r"RHEL-10|rhel-10|rhel10", img):
        return "RHEL10", "Linux" if is_gold else "RHEL"
    if re.search(r"RHEL-9|rhel-9|rhel9", img):
        return "RHEL9", "Linux" if is_gold else "RHEL"
    if re.search(r"RHEL-8|rhel-8|rhel8", img):
        return "RHEL8", "Linux" if is_gold else "RHEL"
    if re.search(r"RHEL-7|rhel-7|rhel7", img):
        return "RHEL7", "Linux" if is_gold else "RHEL"
    if re.search(r"RHEL|rhel", img):
        return "RHEL", "Linux" if is_gold else "RHEL"
    if re.search(r"[Cc]ore[Oo][Ss]|[Rr][Hh][Cc][Oo][Ss]|rhcos|RHCOS|FCOS|fcos", img):
        return "RHCOS", "Linux"
    if re.search(r"[Ff]edora", img):
        return "Fedora", "Linux"
    if re.search(r"[Cc]ent[Oo][Ss]", img):
        return "CentOS", "Linux"
    if re.search(r"[Uu]buntu", img):
        return "Ubuntu", "Linux"

    return "Linux", "Linux"


def parse_k8s_size(value: str) -> int:
    if not value or not isinstance(value, str):
        return 0
    value = value.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(Ti|Gi|Mi|Ki|T|G|M|K)?$", value, re.IGNORECASE)
    if not m:
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0

    num = float(m.group(1))
    suffix = (m.group(2) or "").lower()

    multipliers = {"ti": 1024, "gi": 1, "mi": 1 / 1024, "ki": 1 / (1024 * 1024),
                   "t": 1024, "g": 1, "m": 1 / 1024, "k": 1 / (1024 * 1024)}
    return int(num * multipliers.get(suffix, 1))


def parse_memory_to_gi(value: str) -> int:
    if not value or not isinstance(value, str):
        return 0
    value = value.strip()

    m = re.match(r"^(\d+(?:\.\d+)?)\s*(Gi|G|Mi|M|Ti|T)?$", value, re.IGNORECASE)
    if not m:
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0

    num = float(m.group(1))
    suffix = (m.group(2) or "").lower()

    if suffix in ("gi", "g"):
        return int(num)
    if suffix in ("mi", "m"):
        return int(num / 1024)
    if suffix in ("ti", "t"):
        return int(num * 1024)

    # No suffix: if > 1024 treat as Mi, else as Gi
    if num > 1024:
        return int(num / 1024)
    return int(num)


# Cloud provider name normalization map
CLOUD_MAP: dict[str, str] = {
    "ec2": "AWS", "aws": "AWS",
    "azure": "Azure",
    "gcp": "GCP",
    "osp": "CNV", "cnv": "CNV", "openstack": "CNV",
    "none": "CNV",
}
