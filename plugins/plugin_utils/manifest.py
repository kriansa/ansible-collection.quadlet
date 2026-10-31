# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
An app's manifest: what the app says about itself beyond its quadlets.

It lives beside the app's quadlets as application.yml (or application.yaml), is rendered with
Ansible's templating like every other file of the app, and is then read as YAML. It never reaches
the managed host.
"""

import re
from dataclasses import dataclass, field

import yaml

# pylint: disable=import-error
from ansible.errors import AnsibleError  # type: ignore[import-not-found]

from ansible_collections.kriansa.quadlet.plugins.module_utils import (  # type: ignore[import-not-found]
    constants,
)

# pylint: enable=import-error

# What the manifest may be called. One app may have either, but not both.
MANIFEST_FILENAMES = ("application.yml", "application.yaml")

# The versions this collection reads. A version is "<compatibility version>[/<variant>]", and only
# the part before the first "/" decides how the file is read.
SUPPORTED_VERSIONS = ("v1",)

KNOWN_KEYS = ("version", "systemctl_timeout", "main_container", "resource_claims")

# Keys starting with one of these are the author's own, and are ignored without a word
PRIVATE_KEY_PREFIXES = ("x_", "_")

# A role inside a collection, as Ansible accepts it: namespace.collection.role, with any number of
# subdirectories of roles/ in between. Ansible's own rule (VALID_FQCR_RE) - and \w has no "-".
COLLECTION_ROLE_RE = re.compile(r"^\w+(\.\w+){2,}$")

# A role found on the roles path, by its directory name. It may contain dashes, and dots: a role
# installed from Galaxy is named author.role, as in geerlingguy.docker
ROLES_PATH_ROLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*)*$")

# How to template a value so YAML reads it back as what it was, whatever it holds
TEMPLATING_HINT = (
    "Template values as {{ var | to_json }}, unquoted: it writes any value - a number, a string "
    "with quotes or backslashes in it, a list - as YAML that reads back as that same value."
)


@dataclass(frozen=True)
class Manifest:
    """
    The parsed manifest of one app.

    An app without a manifest file has the defaults, and no version at all - so a file written
    without one can still be told apart from no file.
    """

    version: str | None = None
    systemctl_timeout: int = constants.DEFAULT_SYSTEMCTL_TIMEOUT
    main_container: str = constants.DEFAULT_MAIN_CONTAINER
    # Role name -> the claims the app addresses to it, in the order the file lists them
    resource_claims: dict[str, list] = field(default_factory=dict)
    # Things worth telling the user that do not stop the run
    warnings: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str, path: str) -> "Manifest":
        """
        Read a manifest from its rendered text.

        Args:
            text: The file's content, already rendered
            path: Where the file is, for messages

        Raises:
            AnsibleError: If it is not valid YAML, or breaks a rule of the format. The rendered
                text is shown on a YAML error: that is what broke, not the file on disk.
        """
        try:
            # A subclass of yaml.SafeLoader, so as safe as yaml.safe_load
            data = yaml.load(text, Loader=_UniqueKeyLoader)
        except yaml.YAMLError as e:
            # How to template a value has nothing to do with a key written twice
            hint = "" if isinstance(e, _RepeatedKeyError) else f"\n\n{TEMPLATING_HINT}"
            # Not chained: Ansible would append the YAML error's message again after the hint
            raise AnsibleError(
                f"{path} is not valid YAML once rendered: {e}\n\n"
                f"The rendered content:\n{_numbered(text)}{hint}"
            ) from None

        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise AnsibleError(f"{path} must be a mapping of keys, not {_kind(data)}")

        return cls(
            version=cls._version(data, path),
            systemctl_timeout=cls._systemctl_timeout(data, path),
            main_container=cls._main_container(data, path),
            resource_claims=cls._resource_claims(data, path),
            warnings=cls._unknown_keys(data, path),
        )

    @staticmethod
    def _version(data: dict, path: str) -> str:
        """
        The version is required, and has to be one this collection knows.

        Reading on with a version it does not know would risk misreading the file - the failure
        the version exists to prevent. And it has no default: an absent version would make a file
        written before versioning and one whose author forgot it look the same.
        """
        if "version" not in data:
            raise AnsibleError(f'{path} has no version. Start it with: version: "v1"')

        version = data["version"]
        if not isinstance(version, str):
            raise AnsibleError(
                f'The version in {path} must be a string, such as "v1", not {_kind(version)} '
                f"{version!r}. Quote it: YAML reads 1 as a number and 1.0 as a fraction."
            )

        if version.split("/")[0] not in SUPPORTED_VERSIONS:
            raise AnsibleError(
                f"Unknown version in {path}: {version!r}. "
                f"This collection reads {', '.join(SUPPORTED_VERSIONS)}, optionally followed by "
                '"/" and a variant, such as "v1/testing".'
            )
        return version

    @staticmethod
    def _systemctl_timeout(data: dict, path: str) -> int:
        value = data.get("systemctl_timeout", constants.DEFAULT_SYSTEMCTL_TIMEOUT)
        # bool is an int in Python, and YAML reads yes as True
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            message = (
                f"systemctl_timeout in {path} must be a whole number of seconds above 0, "
                f"not {_kind(value)} {value!r}"
            )
            if isinstance(value, str):
                # A number templated inside quotes reads back as text
                message += f". {TEMPLATING_HINT}"
            raise AnsibleError(message)
        return value

    @staticmethod
    def _main_container(data: dict, path: str) -> str:
        value = data.get("main_container", constants.DEFAULT_MAIN_CONTAINER)
        if not isinstance(value, str) or not value or "/" in value:
            raise AnsibleError(
                f"main_container in {path} must be the name of a quadlet in quadlets/, "
                f"not {_kind(value)} {value!r}"
            )
        if value.endswith(constants.VALID_QUADLET_EXTENSIONS):
            stem = value.rsplit(".", 1)[0]
            raise AnsibleError(
                f"main_container in {path} is the quadlet's name without its extension: "
                f"{stem!r}, not {value!r}"
            )
        return value

    @staticmethod
    def _resource_claims(data: dict, path: str) -> dict[str, list]:
        """
        A map from the role that satisfies each claim to the list of them.

        Only the shape is checked. What a claim holds is for the role receiving it to judge.
        """
        claims = data.get("resource_claims")
        if claims is None:
            return {}
        if not isinstance(claims, dict):
            raise AnsibleError(
                f"resource_claims in {path} must map role names to lists of claims, "
                f"not {_kind(claims)}"
            )

        for role, role_claims in claims.items():
            _check_role_name(role, path)
            if not isinstance(role_claims, list):
                raise AnsibleError(
                    f"The claims for {role} in {path} must be a list, not {_kind(role_claims)}. "
                    "Write each claim as an item starting with '- '."
                )
        return dict(claims)

    @staticmethod
    def _unknown_keys(data: dict, path: str) -> tuple[str, ...]:
        """A warning for each key this version does not define, but the author's own."""
        warnings = []
        for key in data:
            if key in KNOWN_KEYS:
                continue
            if isinstance(key, str) and key.startswith(PRIVATE_KEY_PREFIXES):
                continue
            warnings.append(
                f"{path}: unknown key {key!r} is ignored. The keys a manifest has are "
                f"{', '.join(KNOWN_KEYS)}; a key of your own starting with x_ is ignored "
                "without this warning."
            )
        return tuple(warnings)


def _check_role_name(role, path: str) -> None:
    """
    A claim is addressed to the role that satisfies it, so its key has to be a name Ansible can
    find a role by.

    A dotted name can be either: a role in a collection (namespace.collection.role), or a role on
    the roles path whose directory has dots in its name, as a Galaxy role (author.role) does.
    Dashes are only invalid in a collection's names, so a blanket ban would refuse valid roles.
    """
    # The whole name: with re.match, $ would also accept a trailing newline
    if isinstance(role, str) and (
        COLLECTION_ROLE_RE.fullmatch(role) or ROLES_PATH_ROLE_RE.fullmatch(role)
    ):
        return
    raise AnsibleError(
        f"resource_claims in {path} names {role!r}, which is not a role name. Use a role on the "
        "roles path (letters, digits, '_', '-' and '.', as in geerlingguy.docker) or one in a "
        "collection (namespace.collection.role)."
    )


class _UniqueKeyLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors
    """
    YAML's safe loader, refusing a key a mapping repeats.

    YAML keeps only the last value of a repeated key, and says nothing. In a manifest that drops
    claims - a role listed twice, the second time inside an {% if %} - and a provider removing
    what nothing claims would then remove what the dropped claims needed.
    """


class _RepeatedKeyError(yaml.constructor.ConstructorError):
    """A key a mapping repeats - a mistake in the manifest, not in how a value was templated."""


# The tag of YAML's merge key, "<<", which brings the keys of another mapping into this one
MERGE_TAG = "tag:yaml.org,2002:merge"


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep=False):
    seen = set()
    for key_node, _value_node in node.value:
        # A merge key is not a key of its own, and the keys it brings in may be overridden here on
        # purpose: the mapping's construction applies it, after this check
        if key_node.tag == MERGE_TAG:
            continue
        key = loader.construct_object(key_node, deep=deep)
        try:
            repeated = key in seen
        except TypeError:
            # A key YAML cannot use either, which the mapping's construction reports
            continue
        if repeated:
            raise _RepeatedKeyError(
                "while reading a mapping",
                node.start_mark,
                f"found the key {key!r} more than once",
                key_node.start_mark,
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _kind(value) -> str:
    """How a value reads in a message: what YAML made of it."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "a number"
    if isinstance(value, float):
        return "a fraction"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a mapping"
    return type(value).__name__


def _numbered(text: str) -> str:
    """The text with line numbers, which YAML's error messages refer to."""
    lines = text.split("\n")
    width = len(str(len(lines)))
    return "\n".join(f"{number:>{width}} | {line}" for number, line in enumerate(lines, start=1))
