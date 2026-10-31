"""
The app manifest: the rules of the format, applied to its text once rendered.
"""

import pytest
from ansible.errors import AnsibleError

from ansible_collections.kriansa.quadlet.plugins.plugin_utils.manifest import Manifest

PATH = "/apps/web/application.yml"


def parse(text):
    return Manifest.parse(text, PATH)


def error(text) -> str:
    with pytest.raises(AnsibleError) as excinfo:
        parse(text)
    return str(excinfo.value)


def test_the_smallest_manifest_has_the_defaults():
    assert parse('version: "v1"\n') == Manifest(
        version="v1", systemctl_timeout=300, main_container="main", resource_claims={}
    )


def test_an_absent_manifest_has_the_defaults_and_no_version():
    assert Manifest() == Manifest(
        version=None, systemctl_timeout=300, main_container="main", resource_claims={}
    )


def test_every_key():
    manifest = parse(
        'version: "v1"\n'
        "systemctl_timeout: 900\n"
        "main_container: web\n"
        "resource_claims:\n"
        "  kriansa.commons.quadlet_provider.example_dns:\n"
        '    - name: "app.example"\n'
        '      value: "10.0.0.1"\n'
        "  caddy-ingress:\n"
        "    - domain: app.example\n"
        "      upstream: main:8080\n"
        "    - plain string claim\n"
    )
    assert manifest == Manifest(
        version="v1",
        systemctl_timeout=900,
        main_container="web",
        resource_claims={
            "kriansa.commons.quadlet_provider.example_dns": [
                {"name": "app.example", "value": "10.0.0.1"}
            ],
            # What a claim holds is the provider's business, never checked here
            "caddy-ingress": [
                {"domain": "app.example", "upstream": "main:8080"},
                "plain string claim",
            ],
        },
    )
    assert manifest.warnings == ()


class TestVersion:
    def test_required(self):
        assert (
            error("systemctl_timeout: 5\n")
            == f'{PATH} has no version. Start it with: version: "v1"'
        )

    @pytest.mark.parametrize(
        "line, kind",
        [
            ("version: 1", "a number 1"),
            ("version: 1.0", "a fraction 1.0"),
            ("version:", "nothing None"),
        ],
    )
    def test_a_string(self, line, kind):
        assert error(f"{line}\n") == (
            f'The version in {PATH} must be a string, such as "v1", not {kind}. '
            "Quote it: YAML reads 1 as a number and 1.0 as a fraction."
        )

    @pytest.mark.parametrize("version", ["v2", "1", "V1", "", "/v1", "v1.0"])
    def test_unknown(self, version):
        assert error(f'version: "{version}"\n') == (
            f"Unknown version in {PATH}: {version!r}. This collection reads v1, optionally "
            'followed by "/" and a variant, such as "v1/testing".'
        )

    @pytest.mark.parametrize("version", ["v1/testing-new-thing", "v1/", "v1/a/b"])
    def test_a_variant_reads_as_its_version(self, version):
        assert parse(f'version: "{version}"\n').version == version


class TestSystemctlTimeout:
    @pytest.mark.parametrize(
        "value, kind",
        [
            ("0", "a number 0"),
            ("-5", "a number -5"),
            ("1.5", "a fraction 1.5"),
            ("yes", "a boolean True"),
            ("", "nothing None"),
        ],
    )
    def test_whole_seconds_above_zero(self, value, kind):
        assert error(f'version: "v1"\nsystemctl_timeout: {value}\n') == (
            f"systemctl_timeout in {PATH} must be a whole number of seconds above 0, not {kind}"
        )

    def test_a_number_templated_inside_quotes(self):
        # What systemctl_timeout: "{{ t }}" renders to; the message says how to template it
        assert error('version: "v1"\nsystemctl_timeout: "300"\n') == (
            f"systemctl_timeout in {PATH} must be a whole number of seconds above 0, not a "
            "string '300'. Template values as {{ var | to_json }}, unquoted: it writes any value "
            "- a number, a string with quotes or backslashes in it, a list - as YAML that reads "
            "back as that same value."
        )


class TestMainContainer:
    @pytest.mark.parametrize("value", ["web", "web-1", "web.v2", "Web_2"])
    def test_a_quadlet_name(self, value):
        assert parse(f'version: "v1"\nmain_container: "{value}"\n').main_container == value

    @pytest.mark.parametrize(
        "value, kind", [('""', "a string ''"), ("a/b", "a string 'a/b'"), ("1", "a number 1")]
    )
    def test_invalid(self, value, kind):
        assert error(f'version: "v1"\nmain_container: {value}\n') == (
            f"main_container in {PATH} must be the name of a quadlet in quadlets/, not {kind}"
        )

    @pytest.mark.parametrize("filename", ["web.container", "web.pod", "web.kube"])
    def test_not_a_filename(self, filename):
        assert error(f'version: "v1"\nmain_container: {filename}\n') == (
            f"main_container in {PATH} is the quadlet's name without its extension: "
            f"'web', not '{filename}'"
        )


class TestResourceClaims:
    def test_empty_means_none(self):
        assert parse('version: "v1"\nresource_claims:\n').resource_claims == {}
        assert parse('version: "v1"\nresource_claims: {}\n').resource_claims == {}

    @pytest.mark.parametrize("value, kind", [("[]", "a list"), ("x", "a string")])
    def test_a_mapping(self, value, kind):
        assert error(f'version: "v1"\nresource_claims: {value}\n') == (
            f"resource_claims in {PATH} must map role names to lists of claims, not {kind}"
        )

    @pytest.mark.parametrize("value, kind", [("", "nothing"), ("{a: 1}", "a mapping")])
    def test_each_a_list(self, value, kind):
        assert error(f'version: "v1"\nresource_claims:\n  dns: {value}\n') == (
            f"The claims for dns in {PATH} must be a list, not {kind}. "
            "Write each claim as an item starting with '- '."
        )

    @pytest.mark.parametrize(
        "role",
        [
            "dns",
            "caddy-ingress",
            "_private",
            "dns_2",
            "ns.coll.role",
            "kriansa.commons.quadlet_provider.example_dns",
            "ns.coll.sub.dir.role",
            # Roles installed from Galaxy, on the roles path under author.role
            "geerlingguy.docker",
            "dev-sec.os-hardening",
            # Not a collection's - those have no dashes - but a roles path directory can be
            "ns.coll.caddy-ingress",
        ],
    )
    def test_role_names(self, role):
        assert list(parse(f'version: "v1"\nresource_claims:\n  {role}: []\n').resource_claims) == [
            role
        ]

    @pytest.mark.parametrize(
        "role",
        [
            # An empty part between the dots
            "ns..role",
            ".ns.coll.role",
            "ns.coll.role.",
            # Not a directory name
            "-dns",
            "dns ingress",
            "roles/dns",
        ],
    )
    def test_not_role_names(self, role):
        message = error(f'version: "v1"\nresource_claims:\n  "{role}": []\n')
        assert message == (
            f"resource_claims in {PATH} names {role!r}, which is not a role name. Use a role on "
            "the roles path (letters, digits, '_', '-' and '.', as in geerlingguy.docker) or one "
            "in a collection (namespace.collection.role)."
        )

    def test_the_whole_name_has_to_match(self):
        # A YAML escape puts the newline in the key; $ alone would let it through
        assert "names 'dns\\n', which is not a role name" in error(
            'version: "v1"\nresource_claims:\n  "dns\\n": []\n'
        )

    def test_a_key_that_is_not_text(self):
        assert "names 1, which is not a role name" in error(
            'version: "v1"\nresource_claims:\n  1: []\n'
        )


class TestUnknownKeys:
    def test_warned_about(self):
        manifest = parse('version: "v1"\nsystemctl_timout: 5\n')
        assert manifest.systemctl_timeout == 300
        assert manifest.warnings == (
            (
                f"{PATH}: unknown key 'systemctl_timout' is ignored. The keys a manifest has are "
                "version, systemctl_timeout, main_container, resource_claims; a key of your own "
                "starting with x_ is ignored without this warning."
            ),
        )

    @pytest.mark.parametrize("key", ["x_owner", "_anchors", "x_", "_"])
    def test_the_authors_own_are_ignored_silently(self, key):
        assert parse(f'version: "v1"\n{key}: 1\n').warnings == ()

    def test_never_fatal_whatever_the_version(self):
        assert len(parse('version: "v1/new"\nnew_key: 1\nother: 2\n').warnings) == 2

    def test_not_text(self):
        assert "unknown key 1 is ignored" in parse('version: "v1"\n1: 2\n').warnings[0]


class TestYaml:
    def test_not_a_mapping(self):
        assert error("- version: v1\n") == f"{PATH} must be a mapping of keys, not a list"

    def test_empty_has_no_version(self):
        assert "has no version" in error("")

    @pytest.mark.parametrize(
        "rendered",
        [
            # What "{{ var }}" leaves behind when it renders to text containing ": "
            'version: "v1"\nresource_claims:\n  dns:\n    - name: a: b\n',
            # A value starting with "*" reads as an alias
            'version: "v1"\nmain_container: *web\n',
            # A value starting with "%" reads as a directive
            '%web\nversion: "v1"\n',
        ],
    )
    def test_invalid_shows_the_rendered_text(self, rendered):
        message = error(rendered)
        assert message.startswith(f"{PATH} is not valid YAML once rendered: ")
        assert "The rendered content:\n" in message
        first_line = rendered.split("\n")[0]
        assert f"1 | {first_line}\n" in message
        assert message.endswith(
            "Template values as {{ var | to_json }}, unquoted: it writes any value - a number, "
            "a string with quotes or backslashes in it, a list - as YAML that reads back as that "
            "same value."
        )

    def test_braces_a_lookup_returned(self):
        # A manifest is rendered before it is parsed, so braces only reach YAML as text a lookup
        # returned: {{ ... }} reads as a mapping inside a mapping
        assert "is not valid YAML once rendered" in error('version: "v1"\nx: {{ var }}\n')

    @pytest.mark.parametrize(
        "rendered, values",
        [
            # What "{{ x }}" renders to: YAML decides the type, silently
            ("yes", [True]),
            ("1.0", [1.0]),
            # And inside double quotes, escapes are processed: C:\temp\new loses its backslashes
            ('"C:\\temp\\new"', ["C:\temp\new"]),
            # What "{{ x | to_json }}" renders to: the value, whatever it is
            ('"yes"', ["yes"]),
            ('"C:\\\\temp\\\\new"', ["C:\\temp\\new"]),
            ("[1, 2]", [[1, 2]]),
        ],
    )
    def test_values_are_read_as_yaml_reads_them(self, rendered, values):
        manifest = parse(f'version: "v1"\nresource_claims:\n  dns:\n    - {rendered}\n')
        assert manifest.resource_claims == {"dns": values}

    @pytest.mark.parametrize(
        "rendered, key",
        [
            # A role listed twice, as an {% if %} block can make it: its first claims would be lost
            ('version: "v1"\nresource_claims:\n  dns:\n    - a\n  dns:\n    - b\n', "dns"),
            ('version: "v1"\nsystemctl_timeout: 5\nsystemctl_timeout: 9\n', "systemctl_timeout"),
            # In a claim too
            ('version: "v1"\nresource_claims:\n  dns:\n    - {name: a, name: b}\n', "name"),
        ],
    )
    def test_a_repeated_key(self, rendered, key):
        message = error(rendered)
        assert message.startswith(
            f"{PATH} is not valid YAML once rendered: while reading a mapping\n"
        )
        assert f"found the key '{key}' more than once" in message
        # Nothing to do with how a value was templated: the message ends with the rendered text
        assert "to_json" not in message
        assert message.endswith(f"{len(rendered.splitlines()) + 1} |")

    def test_merge_keys(self):
        # The usual way to share a block between claims, kept under a key of the author's own
        manifest = parse(
            'version: "v1"\n'
            "x_base: &base\n"
            "  upstream: main:8080\n"
            "  tls: true\n"
            "resource_claims:\n"
            "  ingress:\n"
            "    - <<: *base\n"
            "      domain: a.example\n"
            "    - <<: *base\n"
            "      domain: b.example\n"
            # Overriding a merged key on purpose is not a repeat
            "      upstream: other:9090\n"
        )
        assert manifest.resource_claims == {
            "ingress": [
                {"upstream": "main:8080", "tls": True, "domain": "a.example"},
                {"upstream": "other:9090", "tls": True, "domain": "b.example"},
            ]
        }
        assert manifest.warnings == ()

    def test_a_key_repeated_beside_a_merge_key(self):
        message = error(
            'version: "v1"\nx_base: &base\n  a: 1\nx_other:\n  <<: *base\n  b: 1\n  b: 2\n'
        )
        assert "found the key 'b' more than once" in message
