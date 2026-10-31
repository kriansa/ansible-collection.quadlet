"""
The app manifest: the rules of the format, applied to the file read as YAML and then templated.
"""

import pytest
from ansible.errors import AnsibleError
from ansible.module_utils.common.yaml import HAS_LIBYAML
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, is_trusted_as_template

from ansible_collections.kriansa.quadlet.plugins.plugin_utils.manifest import Manifest

PATH = "/apps/web/application.yml"

# JSON indented with tabs is YAML to the C parser Ansible uses when it is installed, and not to
# PyYAML's own, which Ansible falls back to
NEEDS_LIBYAML = pytest.mark.skipif(not HAS_LIBYAML, reason="needs the C YAML parser")


def parse(text, **variables):
    return Manifest.parse(text, PATH, Templar(loader=DataLoader(), variables=variables))


def error(text, **variables) -> str:
    with pytest.raises(AnsibleError) as excinfo:
        parse(text, **variables)
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
            ("[300]", "a list [300]"),
            # Not the internal name of the class Ansible's reader returns a date as
            ("2024-01-01", "date datetime.date(2024, 1, 1)"),
        ],
    )
    def test_whole_seconds_above_zero(self, value, kind):
        assert error(f'version: "v1"\nsystemctl_timeout: {value}\n') == (
            f"systemctl_timeout in {PATH} must be a whole number of seconds above 0, not {kind}"
        )

    # A variable given with -e key=value is text, whatever it holds; and Jinja's / gives a fraction
    @pytest.mark.parametrize("t", [900, "900", 900.0])
    def test_templated(self, t):
        manifest = parse('version: "v1"\nsystemctl_timeout: "{{ t }}"\n', t=t)
        assert manifest.systemctl_timeout == 900
        # The exact type: isinstance would also accept the subclass Ansible's templating returns
        assert type(manifest.systemctl_timeout) is int  # pylint: disable=unidiomatic-typecheck

    # Read as Ansible's modules read a number option
    @pytest.mark.parametrize(
        "value", ['"300"', "'300'", '" 300 "', '"+300"', '"300.0"', "300.0", '"3e2"']
    )
    def test_a_whole_number_in_another_form(self, value):
        assert parse(f'version: "v1"\nsystemctl_timeout: {value}\n').systemctl_timeout == 300

    @pytest.mark.parametrize("text", ["0", "-5", "1.5", "", "yes", "3 00", "²", "inf", "nan"])
    def test_text_that_is_not_a_whole_number_above_zero(self, text):
        assert error('version: "v1"\nsystemctl_timeout: "{{ t }}"\n', t=text) == (
            f"systemctl_timeout in {PATH} must be a whole number of seconds above 0, not a "
            f"string {text!r}"
        )

    def test_omitted_keeps_the_default(self):
        text = 'version: "v1"\nsystemctl_timeout: "{{ t | default(omit) }}"\n'
        assert parse(text).systemctl_timeout == 300


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
        "text, line",
        [
            # A value starting with braces reads as a mapping: in a playbook too, it needs quotes
            ('version: "v1"\nmain_container: {{ name }}\n', 2),
            ('version: "v1"\nresource_claims:\n  dns:\n    - name: a: b\n', 4),
            # YAML reserves "%" as the first character of a plain value
            ('version: "v1"\nmain_container: %web\n', 2),
        ],
    )
    def test_invalid_says_where(self, text, line):
        # The task's message is all the user sees, so the line has to be in it
        assert error(text).startswith(f"Failed to read {PATH}, at line {line} column ")

    def test_unquoted_braces_get_ansibles_hint(self, tmp_path):
        # Ansible reads the line back from the file to recognize the mistake, so it has to exist
        path = tmp_path / "application.yml"
        text = 'version: "v1"\nmain_container: {{ name }}\n'
        path.write_text(text)
        with pytest.raises(AnsibleError) as excinfo:
            Manifest.parse(text, str(path), Templar(loader=DataLoader()))
        assert str(excinfo.value) == (
            f"Failed to read {path}, at line 2 column 17: YAML parsing failed: This may be an "
            "issue with missing quotes around a template block."
        )

    @pytest.mark.parametrize(
        "text",
        [
            '{"version": "v1", "main_container": "web"}',
            pytest.param(
                '{\n\t"version": "v1",\n\t"main_container": "web"\n}\n', marks=NEEDS_LIBYAML
            ),
        ],
    )
    def test_json_is_read_as_yaml(self, text):
        assert parse(text).main_container == "web"

    def test_json_that_is_not_yaml_is_refused(self):
        # Ansible's reader would read it as JSON, which goes unchecked for repeated keys. The
        # problem is worded by the YAML parser, which differs between the C one and PyYAML's own.
        message = error('\t{"version": "v1"}')
        assert message.startswith(f"Failed to read {PATH}, at line 1 column 1: found character ")
        assert message.endswith(
            "cannot start any token. It reads as JSON but not as YAML, and a manifest has to be YAML."
        )

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
            # Overriding a merged key on purpose is not a repeat here, though Ansible's reader
            # warns about it, as it does in a playbook
            "      upstream: other:9090\n"
        )
        assert manifest.resource_claims == {
            "ingress": [
                {"upstream": "main:8080", "tls": True, "domain": "a.example"},
                {"upstream": "other:9090", "tls": True, "domain": "b.example"},
            ]
        }
        assert manifest.warnings == ()


class TestRepeatedKeys:
    @pytest.mark.parametrize(
        "text, key, line, column",
        [
            # A role listed twice: its first claims would be lost
            ('version: "v1"\nresource_claims:\n  dns:\n    - a\n  dns:\n    - b\n', "dns", 5, 3),
            (
                'version: "v1"\nsystemctl_timeout: 5\nsystemctl_timeout: 9\n',
                "systemctl_timeout",
                3,
                1,
            ),
            # In a claim too
            ('version: "v1"\nresource_claims:\n  dns:\n    - {name: a, name: b}\n', "name", 4, 17),
            # Ansible's reader takes a file that is JSON as JSON, which keeps the last value silently
            (
                '{"version": "v1", "resource_claims": {"dns": ["a"], "dns": ["b"]}}',
                "dns",
                1,
                53,
            ),
            pytest.param(
                '{\n\t"version": "v1",\n\t"resource_claims": {\n'
                '\t\t"dns": ["a"],\n\t\t"dns": ["b"]\n\t}\n}\n',
                "dns",
                5,
                3,
                marks=NEEDS_LIBYAML,
            ),
            # Beside a tag only Ansible knows
            ('version: "v1"\nx_raw:\n  a: !unsafe "{{ x }}"\n  a: 1\n', "a", 4, 3),
        ],
    )
    def test_refused(self, text, key, line, column):
        assert error(text) == (
            f"Failed to read {PATH}, at line {line} column {column}: the key {key!r} is written "
            "more than once in the same mapping. Only its last value would be kept."
        )

    def test_an_anchor_used_twice_is_not_a_repeat(self):
        manifest = parse(
            'version: "v1"\n'
            "x_dns: &dns\n"
            "  - name: a\n"
            "resource_claims:\n"
            "  dns: *dns\n"
            "  other-dns: *dns\n"
        )
        assert manifest.resource_claims == {"dns": [{"name": "a"}], "other-dns": [{"name": "a"}]}

    def test_the_same_text_as_different_types_is_not_a_repeat(self):
        manifest = parse('version: "v1"\nresource_claims:\n  dns:\n    - {1: a, "1": b}\n')
        assert manifest.resource_claims == {"dns": [{1: "a", "1": "b"}]}


class TestTemplating:
    @pytest.mark.parametrize(
        "value",
        [
            # Each one would read back as something else if YAML saw it after rendering
            "yes",
            "1.0",
            "C:\\temp\\new",
            "a: b",
            "\"quoted\" and 'single'",
            "*web",
            "line one\nline two",
            # And those that are not text stay what they are
            900,
            True,
            None,
            [1, "a"],
            {"k": [1]},
        ],
    )
    def test_an_expression_keeps_the_type_of_its_value(self, value):
        manifest = parse('version: "v1"\nresource_claims:\n  dns:\n    - "{{ v }}"\n', v=value)
        assert manifest.resource_claims == {"dns": [value]}

    def test_text_around_expressions(self):
        manifest = parse(
            'version: "v1"\nresource_claims:\n  dns:\n    - name: "{{ a }}.{{ b }}"\n',
            a="web",
            b="example",
        )
        assert manifest.resource_claims == {"dns": [{"name": "web.example"}]}

    def test_a_whole_list_of_claims(self):
        claims = [{"name": "a"}, {"name": "b"}]
        manifest = parse('version: "v1"\nresource_claims:\n  dns: "{{ claims }}"\n', claims=claims)
        assert manifest.resource_claims == {"dns": claims}

    def test_control_flow_inside_a_value(self):
        text = 'version: "v1"\nmain_container: "{% if public %}edge{% else %}internal{% endif %}"\n'
        assert parse(text, public=True).main_container == "edge"

    def test_main_container(self):
        manifest = parse('version: "v1"\nmain_container: "{{ name }}"\n', name="web")
        assert manifest.main_container == "web"

    def test_merged_values_are_templated(self):
        # A block kept under a key of the author's own is templated where it is merged in
        manifest = parse(
            'version: "v1"\n'
            "x_base: &base\n"
            '  upstream: "{{ upstream }}"\n'
            "resource_claims:\n"
            "  ingress:\n"
            "    - <<: *base\n"
            "      domain: a.example\n",
            upstream="main:8080",
        )
        assert manifest.resource_claims == {
            "ingress": [{"upstream": "main:8080", "domain": "a.example"}]
        }

    def test_unsafe_is_kept_as_written(self):
        manifest = parse('version: "v1"\nresource_claims:\n  dns:\n    - !unsafe "{{ raw }}"\n')
        assert manifest.resource_claims == {"dns": ["{{ raw }}"]}

    def test_keys_are_never_templated(self):
        # As in a playbook: the role a claim is addressed to is named as written
        message = error('version: "v1"\nresource_claims:\n  "{{ role }}": []\n', role="dns")
        assert message.startswith(f"resource_claims in {PATH} names '{{{{ role }}}}', which is not")

    def test_the_version_is_never_templated(self):
        assert error('version: "{{ v }}"\n', v="v1") == (
            f"Unknown version in {PATH}: '{{{{ v }}}}'. This collection reads v1, optionally "
            'followed by "/" and a variant, such as "v1/testing". The version is never '
            "templated: write it out."
        )

    def test_keys_that_are_ignored_are_never_templated(self):
        # A variable that is not defined, in a value nothing reads, does not fail the run
        manifest = parse('version: "v1"\nx_note: "{{ nope }}"\ntypo: "{{ nope }}"\n')
        assert len(manifest.warnings) == 1

    def test_an_undefined_variable(self):
        text = 'version: "v1"\nresource_claims:\n  dns:\n    - name: "{{ nope }}"\n'
        assert error(text) == f"Failed to template resource_claims in {PATH}: 'nope' is undefined"

    @pytest.mark.parametrize("key", ["main_container", "resource_claims"])
    def test_omitted_keeps_the_default(self, key):
        manifest = parse(f'version: "v1"\n{key}: "{{{{ nope | default(omit) }}}}"\n')
        assert manifest == Manifest(version="v1")

    def test_only_the_keys_asked_for(self):
        # What quadlet_app reads on its own: a variable only the claims use need not be defined
        manifest = Manifest.parse(
            'version: "v1"\nmain_container: "{{ name }}"\n'
            'resource_claims:\n  dns:\n    - name: "{{ nope }}"\n',
            PATH,
            Templar(loader=DataLoader(), variables={"name": "web"}),
            keys=("systemctl_timeout", "main_container"),
        )
        assert manifest == Manifest(version="v1", main_container="web")


def strings(value):
    """Every string in a value, its keys included."""
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in strings(key) + strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return [value] if isinstance(value, str) else []


def test_nothing_returned_is_left_to_be_templated_again():
    # Whoever reads the result later would render it with their own variables
    manifest = parse(
        'version: "v1/{{ variant }}"\n'
        'main_container: "web"\n'
        "resource_claims:\n"
        "  dns:\n"
        '    - labels: {"router.{{ quadlet_app_name }}.rule": "{{ rule }}", plain: text}\n'
        '      raw: !unsafe "{{ raw }}"\n',
        rule="r",
    )
    values = [manifest.version, manifest.main_container, manifest.resource_claims]
    assert strings(values) == [
        "v1/{{ variant }}",
        "web",
        "dns",
        "labels",
        "router.{{ quadlet_app_name }}.rule",
        "r",
        "plain",
        "text",
        "raw",
        "{{ raw }}",
    ]
    assert not any(is_trusted_as_template(s) for s in strings(values))
    # The exact type: isinstance would also accept the subclass Ansible's reader returns
    assert all(type(s) is str for s in strings(values))  # pylint: disable=unidiomatic-typecheck
