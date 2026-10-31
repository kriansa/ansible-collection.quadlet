# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
An app's manifest: what the app says about itself beyond its quadlets.

It lives beside the app's quadlets as application.yml (or application.yaml), and is read the way
Ansible reads a playbook: as YAML first, and then the values are templated, so a value that is a
single "{{ expression }}" keeps the type the expression has. It never reaches the managed host.
"""

import re
from dataclasses import dataclass, field

import yaml

# pylint: disable=import-error
from ansible.errors import (  # type: ignore[import-not-found]
    AnsibleError,
    AnsibleParserError,
    AnsibleValueOmittedError,
)
from ansible.module_utils.common.validation import (  # type: ignore[import-not-found]
    check_type_int,
)

# The parser Ansible's reader uses: the C one when it is installed. PyYAML's own parser refuses tabs
# the C one accepts, so with it a file Ansible reads as YAML would not be checked as YAML here.
from ansible.module_utils.common.yaml import SafeLoader  # type: ignore[import-not-found]
from ansible.module_utils.datatag import native_type_name  # type: ignore[import-not-found]
from ansible.parsing.utils.yaml import from_yaml  # type: ignore[import-not-found]
from ansible.template import trust_as_template  # type: ignore[import-not-found]

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

# The keys whose values are templated. The version is not one: it says how the rest of the file
# is read, so it has to be readable before anything is templated. Nor are the keys that are
# ignored: a variable they use that is not defined would fail the run over a value nothing reads.
TEMPLATED_KEYS = tuple(key for key in KNOWN_KEYS if key != "version")

# The tag of YAML's merge key, "<<", which brings the keys of another mapping into this one
MERGE_TAG = "tag:yaml.org,2002:merge"

# Keys starting with one of these are the author's own, and are ignored without a word
PRIVATE_KEY_PREFIXES = ("x_", "_")

# A role inside a collection, as Ansible accepts it: namespace.collection.role, with any number of
# subdirectories of roles/ in between. Ansible's own rule (VALID_FQCR_RE) - and \w has no "-".
COLLECTION_ROLE_RE = re.compile(r"^\w+(\.\w+){2,}$")

# A role found on the roles path, by its directory name. It may contain dashes, and dots: a role
# installed from Galaxy is named author.role, as in geerlingguy.docker
ROLES_PATH_ROLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*)*$")


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
    def parse(cls, text: str, path: str, templar, keys=TEMPLATED_KEYS) -> "Manifest":
        """
        Read a manifest from the file's text, the way Ansible reads a playbook.

        The text is read as YAML by Ansible's own reader, so !unsafe and !vault work as they do in
        a playbook. Only then are the values templated, each one as a whole: a value that is a
        single "{{ expression }}" keeps the type of what the expression returns.

        Args:
            text: The file's content, as written
            path: Where the file is, for messages
            templar: A Templar holding every variable the values may use
            keys: Which of TEMPLATED_KEYS to read. The others keep their defaults, and are never
                templated: a variable only they use need not be defined.

        Raises:
            AnsibleError: If it is not valid YAML, repeats a key, a value cannot be templated, or
                the file breaks a rule of the format
        """
        try:
            # The C parser only takes plain text, without the markings Ansible adds to what it reads
            nodes = yaml.compose(str(text), Loader=SafeLoader)
        except yaml.YAMLError as e:
            nodes, not_yaml = None, e
        else:
            not_yaml = None
        _refuse_repeated_keys(nodes, path)

        try:
            # Ansible only templates text it has been told is a template rather than data
            data = from_yaml(trust_as_template(text), file_name=path)
        except AnsibleParserError as e:
            # The task's message is all that is shown of the error, so it says where the problem
            # is: the position Ansible found is only kept on the error, not in its text
            raise AnsibleError(f"Failed to read {path}{_location(e.obj)}: {e.message}") from e

        # Ansible's reader tries JSON before YAML, so it also reads the little JSON that is not
        # YAML, such as JSON starting with a tab. Repeated keys are only looked for in YAML, so a
        # manifest has to be YAML. This comes after Ansible's reader, whose messages explain most
        # mistakes better.
        if not_yaml is not None:
            raise AnsibleError(
                f"Failed to read {path}{_mark_location(getattr(not_yaml, 'problem_mark', None))}: "
                f"{getattr(not_yaml, 'problem', None) or not_yaml}. It reads as JSON but not as "
                "YAML, and a manifest has to be YAML."
            )

        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise AnsibleError(f"{path} must be a mapping of keys, not {_kind(data)}")

        version = cls._version(data, path)
        values = _templated(data, path, templar, keys)
        return cls(
            version=version,
            systemctl_timeout=cls._systemctl_timeout(values, path),
            main_container=cls._main_container(values, path),
            resource_claims=cls._resource_claims(values, path),
            warnings=cls._unknown_keys(data, path),
        )

    @staticmethod
    def _version(data: dict, path: str) -> str:
        """
        The version is required, and has to be one this collection knows.

        Reading on with a version it does not know would risk misreading the file - the failure
        the version exists to prevent. And it has no default: an absent version would make a file
        written before versioning and one whose author forgot it look the same.

        It is read as written, never templated: it says how the rest of the file is read.
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
            message = (
                f"Unknown version in {path}: {version!r}. "
                f"This collection reads {', '.join(SUPPORTED_VERSIONS)}, optionally followed by "
                '"/" and a variant, such as "v1/testing".'
            )
            if "{{" in version or "{%" in version:
                message += " The version is never templated: write it out."
            raise AnsibleError(message)
        return _plain(version)

    @staticmethod
    def _systemctl_timeout(data: dict, path: str) -> int:
        """
        A whole number of seconds, read the way Ansible's modules read a number option.

        So text holding a whole number is accepted - as a variable given with -e key=value always
        is - and so are 900.0, "900.0" and "1e3"; 1.5 is not.
        """
        value = data.get("systemctl_timeout", constants.DEFAULT_SYSTEMCTL_TIMEOUT)
        try:
            # bool is an int in Python, and YAML reads yes as True
            seconds = None if isinstance(value, bool) else int(check_type_int(value))
        except (TypeError, ValueError, OverflowError):
            # check_type_int lets the error converting "inf" through, as OverflowError
            seconds = None
        if seconds is None or seconds <= 0:
            raise AnsibleError(
                f"systemctl_timeout in {path} must be a whole number of seconds above 0, "
                f"not {_kind(value)} {value!r}"
            )
        return seconds

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
        return _plain(value)

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
        return _plain(claims)

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


def _refuse_repeated_keys(root, path: str) -> None:
    """
    Fail on a key written twice in the same mapping.

    YAML keeps only the last value of a repeated key, and Ansible's reader only warns about it - or
    says nothing at all, for a file written as JSON. In a manifest that drops claims: a role listed
    twice keeps only its second block, and a provider removing what nothing claims would then
    remove what the first block needed.

    The file is only read as a tree of nodes here, never turned into values, so the tags Ansible
    adds to YAML, such as !vault, need no support.
    """
    seen_nodes = set()
    pending = [root] if root is not None else []
    while pending:
        node = pending.pop()
        # An anchor used twice is the same node, and one may even contain itself
        if id(node) in seen_nodes:
            continue
        seen_nodes.add(id(node))

        if isinstance(node, yaml.SequenceNode):
            pending.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            keys = set()
            for key_node, value_node in node.value:
                pending.append(value_node)
                # A merge key is not a key of its own, and the keys it brings in may be
                # overridden on purpose
                if key_node.tag == MERGE_TAG or not isinstance(key_node, yaml.ScalarNode):
                    continue
                # The tag tells 1 from "1", which are different keys
                key = (key_node.tag, key_node.value)
                if key in keys:
                    raise AnsibleError(
                        f"Failed to read {path}{_mark_location(key_node.start_mark)}: the key "
                        f"{key_node.value!r} is written more than once in the same mapping. Only "
                        "its last value would be kept."
                    )
                keys.add(key)


def _templated(data: dict, path: str, templar, keys) -> dict:
    """
    The values of these keys, templated. A key the file does not have stays out.

    Keys are never templated, as in a playbook: the role a claim is addressed to is named as
    written.
    """
    values = {}
    for key in keys:
        if key not in data:
            continue
        try:
            values[key] = templar.template(data[key])
        except AnsibleValueOmittedError:
            # As in a playbook, a value that is omitted leaves its default in place
            continue
        except AnsibleError as e:
            raise AnsibleError(f"Failed to template {key} in {path}: {e}") from e
    return values


def _plain(value):
    """
    The value as plain Python types, without the markings Ansible's reader adds to it.

    Ansible marks every string its reader returns as a template it may render. What is left of
    those marks after templating - on keys, on the version, on text with nothing to render -
    would let whoever reads the result later render that text again, with their own variables.
    """
    if isinstance(value, dict):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, str):
        return str(value)
    return value


def _location(origin) -> str:
    """
    Where in the file a YAML error is, as words to follow the file's path.

    Ansible tags the error with the position it found, but the task's message, built from the
    error's text alone, would lose it.
    """
    line = getattr(origin, "line_num", None)
    if line is None:
        return ""
    column = getattr(origin, "col_num", None)
    return f", at line {line}" if column is None else f", at line {line} column {column}"


def _mark_location(mark) -> str:
    """The same words for a position PyYAML found, which counts lines and columns from 0."""
    if mark is None:
        return ""
    return f", at line {mark.line + 1} column {mark.column + 1}"


def _kind(value) -> str:
    """How a value reads in a message: what YAML, or the template, made of it."""
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
    # Not type(value).__name__: Ansible's reader returns its own subclasses, whose internal names
    # say nothing to the user
    return native_type_name(value)
