# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""The text of a systemd unit file - a quadlet is one too - and the edits the module makes to it."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace

# A directive line as systemd reads it: optional indentation, the key, then "=" with optional
# whitespace on either side - `Key=value` and `Key = value` mean the same - then the value
DIRECTIVE_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


@dataclass(frozen=True)
class Directive:
    """
    One directive line of a unit file, split into its parts.

    The separator is "=" with whatever whitespace surrounded it, so a rule rewriting the value
    keeps the line as it was written.
    """

    indentation: str
    key: str
    separator: str
    value: str

    @classmethod
    def parse(cls, line: str) -> "Directive | None":
        """
        Read one line of a unit file as a directive.

        Every rule reads directives through this, so they all agree on what counts as one.

        Returns:
            The directive, or None for comments, section headers, blank lines and anything else
        """
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            return None
        match = DIRECTIVE_RE.match(line)
        if match is None:
            return None
        return cls(*match.groups())

    def with_value(self, value: str) -> "Directive":
        """The same directive, written the same way, holding another value."""
        return replace(self, value=value)

    def __str__(self) -> str:
        return f"{self.indentation}{self.key}{self.separator}{self.value}"


class UnitFile:
    """
    The lines of a unit file, edited in place.

    The text is split on "\\n" alone: a file saved with Windows line endings keeps the "\\r" at the
    end of each line, and gets it back when the text is put together again.
    """

    def __init__(self, text: str):
        self.lines = text.split("\n")

    @property
    def text(self) -> str:
        """The file as it stands after the edits made so far."""
        return "\n".join(self.lines)

    def directives(self, key: str | None = None) -> Iterator[Directive]:
        """The directives of the file in order, or only those with this key."""
        for line in self.lines:
            directive = Directive.parse(line)
            if directive is not None and key in (None, directive.key):
                yield directive

    def has_section(self, section: str) -> bool:
        """Whether a section header appears, for a section name given without brackets."""
        header = f"[{section}]"
        return any(line.strip() == header for line in self.lines)

    def has_directive(self, key: str) -> bool:
        """Whether a directive is set anywhere in the file, comments aside."""
        return any(directive.key == key for directive in self.directives())

    def has_directive_value(self, key: str, value: str) -> bool:
        """Whether a directive already lists a value among the space-separated ones it carries."""
        return any(value in directive.value.split() for directive in self.directives(key))

    def strip_directive(self, key: str) -> None:
        """Remove every occurrence of a directive."""
        self.lines = [line for line in self.lines if not self._sets(line, key)]

    def map_directives(self, rewrite: Callable[[Directive], Directive]) -> None:
        """
        Pass every directive line through a rule, which hands back the directive to write instead.

        Comments, section headers and blank lines are left as they are.
        """
        for i, line in enumerate(self.lines):
            directive = Directive.parse(line)
            if directive is not None:
                self.lines[i] = str(rewrite(directive))

    def ensure_directive(self, section: str, key: str, value: str) -> None:
        """
        Set a directive first thing in a section, unless the section sets it already.

        Only the first section of that name counts. A file without one is left as written: podman's
        generator rejects it, and says why, rather than it deploying with a section made up here.
        """
        start = next(
            (
                i
                for i, line in enumerate(self.lines)
                if self._is_section_header(line) and line.strip()[1:-1] == section
            ),
            None,
        )
        if start is None:
            return

        end = self._section_end(start)
        if any(self._sets(line, key) for line in self.lines[start:end]):
            return

        self.lines.insert(start + 1, f"{key}={value}")

    def append_to_section(self, section: str, lines: list[str], at_end: bool = False) -> None:
        """
        Append lines to a section, creating the section when it is absent.

        Args:
            section: Section name without brackets (e.g. "Unit")
            lines: Lines to add
            at_end: Open a missing section at the end of the file rather than the top. [Install]
                reads as an afterthought and belongs last; [Unit] belongs first.
        """
        header = f"[{section}]"
        start = next((i for i, line in enumerate(self.lines) if line.strip() == header), None)

        if start is None:
            if at_end:
                # Trailing blank lines belong before the new section, not inside it
                while self.lines and not self.lines[-1].strip():
                    self.lines.pop()
                separator = [""] if self.lines else []
                self.lines = self.lines + separator + [header] + lines + [""]
                return

            # No such section - open one ahead of every other, but below the comments that open
            # the file. A comment with no blank line between it and the first header is about that
            # section, so it stays with it: the new section goes after the last blank line.
            first_header = next(
                (i for i, line in enumerate(self.lines) if self._is_section_header(line)),
                len(self.lines),
            )
            split = 0
            for i in range(first_header):
                if not self.lines[i].strip():
                    split = i + 1
            if not any(line.strip() for line in self.lines[:split]):
                split = 0
            self.lines = self.lines[:split] + [header] + lines + [""] + self.lines[split:]
            return

        # Insert at the end of the section, keeping the blank line that separated it from the next
        end = self._section_end(start)
        while end > start + 1 and not self.lines[end - 1].strip():
            end -= 1

        self.lines = self.lines[:end] + lines + self.lines[end:]

    def _section_end(self, start: int) -> int:
        """Where the section whose header is at this line ends: at the next header, or the end."""
        return next(
            (
                i
                for i in range(start + 1, len(self.lines))
                if self._is_section_header(self.lines[i])
            ),
            len(self.lines),
        )

    @staticmethod
    def _is_section_header(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("[") and stripped.endswith("]")

    @staticmethod
    def _sets(line: str, key: str) -> bool:
        """Whether a line sets this directive."""
        directive = Directive.parse(line)
        return directive is not None and directive.key == key
