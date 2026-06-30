from ci_inspector.core.helpers import (
    eval_jinja_str,
    extract_jinja_default,
    parse_k8s_size,
    parse_memory_to_gi,
    resolve_image_to_os,
    resolve_jinja_var,
)


class TestResolveJinjaVar:
    def test_simple_lookup(self):
        assert resolve_jinja_var("{{ cloud_provider }}", {"cloud_provider": "ec2"}) == "ec2"

    def test_no_jinja(self):
        assert resolve_jinja_var("m5.xlarge", {}) == "m5.xlarge"

    def test_recursive_resolution(self):
        ctx = {"a": "{{ b }}", "b": "resolved"}
        assert resolve_jinja_var("{{ a }}", ctx) == "resolved"

    def test_complex_jinja(self):
        result = resolve_jinja_var(
            "{{ [(num_users / 3)|round|int, 3]|max }}",
            {"num_users": 12},
        )
        assert result == "4"

    def test_missing_var(self):
        assert resolve_jinja_var("{{ missing }}", {}) == ""

    def test_non_string(self):
        assert resolve_jinja_var(42, {}) == 42


class TestEvalJinjaStr:
    def test_basic_expression(self):
        assert eval_jinja_str("{{ x + 1 }}", x=5) == "6"

    def test_filter(self):
        assert eval_jinja_str("{{ name | upper }}", name="hello") == "HELLO"

    def test_default_filter(self):
        assert eval_jinja_str("{{ missing | default('fallback') }}") == "fallback"


class TestResolveImageToOs:
    def test_rhel9(self):
        assert resolve_image_to_os("RHEL-9.2") == ("RHEL9", "RHEL")

    def test_rhel9_gold(self):
        assert resolve_image_to_os("RHEL-9.2-GOLD") == ("RHEL9", "Linux")

    def test_windows(self):
        assert resolve_image_to_os("Windows-Server-2022") == ("Windows", "Windows")

    def test_coreos(self):
        assert resolve_image_to_os("rhcos-4.14") == ("RHCOS", "Linux")

    def test_none(self):
        assert resolve_image_to_os(None) == ("Linux", "Linux")

    def test_empty(self):
        assert resolve_image_to_os("") == ("Linux", "Linux")

    def test_fedora(self):
        assert resolve_image_to_os("Fedora-38") == ("Fedora", "Linux")


class TestParseK8sSize:
    def test_gi(self):
        assert parse_k8s_size("100Gi") == 100

    def test_mi(self):
        assert parse_k8s_size("2048Mi") == 2

    def test_plain_number(self):
        assert parse_k8s_size("50") == 50

    def test_zero(self):
        assert parse_k8s_size("0") == 0

    def test_ti(self):
        assert parse_k8s_size("1Ti") == 1024


class TestParseMemoryToGi:
    def test_gi_suffix(self):
        assert parse_memory_to_gi("16Gi") == 16

    def test_g_suffix(self):
        assert parse_memory_to_gi("32G") == 32

    def test_mi_suffix(self):
        assert parse_memory_to_gi("8192Mi") == 8

    def test_plain_number_large(self):
        # Plain number > 1024 treated as Mi
        assert parse_memory_to_gi("16384") == 16

    def test_plain_number_small(self):
        # Plain number <= 1024 treated as Gi
        assert parse_memory_to_gi("32") == 32


class TestExtractJinjaDefault:
    def test_with_default(self):
        assert extract_jinja_default("{{ cluster_size | default('small') }}") == "small"

    def test_no_default(self):
        assert extract_jinja_default("{{ cluster_size }}") == ""
