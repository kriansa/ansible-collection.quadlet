"""UnitFile and Directive: reading unit file text, and the edits every rule is built from."""

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.unit_file import Directive, UnitFile


def edited(text, edit):
    """The text of a unit file after one edit."""
    unit = UnitFile(text)
    edit(unit)
    return unit.text


class TestDirective:
    @pytest.mark.parametrize(
        "line, parts",
        [
            ("Key=value", ("", "Key", "=", "value")),
            ("  Key = value", ("  ", "Key", " = ", "value")),
            ("X-Owner\t=5:5", ("", "X-Owner", "\t=", "5:5")),
            ("Key=", ("", "Key", "=", "")),
            # The value is everything after the separator, a Windows line ending included
            ("Environment=A=b c\r", ("", "Environment", "=", "A=b c\r")),
        ],
    )
    def test_parts(self, line, parts):
        directive = Directive.parse(line)
        assert (directive.indentation, directive.key, directive.separator, directive.value) == parts
        assert str(directive) == line

    @pytest.mark.parametrize(
        "line", ["", "   ", "# Key=value", "; Key=value", "[Unit]", "no equals sign", "Bad Key=x"]
    )
    def test_not_a_directive(self, line):
        assert Directive.parse(line) is None

    def test_with_value_keeps_how_it_was_written(self):
        assert str(Directive.parse("  Key = a").with_value("b")) == "  Key = b"


class TestTextHelpers:
    def test_strip_directive(self):
        content = (
            "[Volume]\nX-Owner=1:1\n  X-Owner=2:2\n# X-Owner=3:3\nX-OwnerFoo=4\nDriver=local\n"
        )
        assert edited(content, lambda unit: unit.strip_directive("X-Owner")) == (
            "[Volume]\n# X-Owner=3:3\nX-OwnerFoo=4\nDriver=local\n"
        )

    def test_spaced_directive_is_stripped(self):
        assert edited(
            "[Volume]\nX-Owner = 1:1\n", lambda unit: unit.strip_directive("X-Owner")
        ) == ("[Volume]\n")

    @pytest.mark.parametrize(
        "content, expected",
        [
            ("[Unit]\nUserNS=host\n", True),
            ("[Unit]\n  UserNS=host\n", True),
            ("[Unit]\nUserNS = host\n", True),
            ("[Unit]\n#UserNS=host\n", False),
            ("[Unit]\nUserNSX=host\n", False),
            ("", False),
        ],
    )
    def test_has_directive(self, content, expected):
        assert UnitFile(content).has_directive("UserNS") is expected

    @pytest.mark.parametrize(
        "content, expected",
        [
            ("[Timer]\n", True),
            ("  [Timer]  \n", True),
            ("# [Timer]\n", False),
            ("[Timers]\n", False),
        ],
    )
    def test_has_section(self, content, expected):
        assert UnitFile(content).has_section("Timer") is expected

    @pytest.mark.parametrize(
        "content, expected",
        [
            ("RequiresMountsFor=/a /b", True),
            ("RequiresMountsFor = /b", True),
            ("RequiresMountsFor=/b/", False),
            ("# RequiresMountsFor=/b", False),
            ("Other=/b", False),
        ],
    )
    def test_has_directive_value(self, content, expected):
        assert UnitFile(content).has_directive_value("RequiresMountsFor", "/b") is expected

    def test_directives_of_one_key(self):
        unit = UnitFile("[Container]\nVolume=a:/a\n# Volume=b:/b\nImage=x\n  Volume = c:/c\n")
        assert [directive.value for directive in unit.directives("Volume")] == ["a:/a", "c:/c"]

    def test_values_of_one_key_in_one_section(self):
        unit = UnitFile(
            "Name=before\n[Network]\nName=a\n[Unit]\nName=b\n# Name=c\n\n[Network]\nName=d\n"
        )
        # Every section of that name, as podman reads them all as one
        assert unit.section_values("Network", "Name") == ["a", "d"]

    @pytest.mark.parametrize(
        "lines, value",
        [
            # Each read the way podman's generator turns it into `podman network create`
            ('Name="edge"', "edge"),
            ('Name= "spaced" \r', "spaced"),
            ("Name='single'", "'single'"),
            ('Name=ed"ge"x', 'ed"ge"x'),
            ('Name="esc\\x41ped"', "esc\\x41ped"),
            ("Name=edge\\\n2", "edge2"),
            ("Name=edge \\\n  two", "edge two"),
            ('Name=""', ""),
        ],
    )
    def test_values_read_as_podman_reads_them(self, lines, value):
        unit = UnitFile(f"[Network]\n{lines}\nOther=x\n")
        assert unit.section_values("Network", "Name") == [value]

    def test_map_directives_leaves_everything_else_alone(self):
        content = "# Key=1\n[Unit]\n\nKey=2\n; Key=3\nnot a directive\n"
        assert edited(content, lambda unit: unit.map_directives(lambda d: d.with_value("x"))) == (
            "# Key=1\n[Unit]\n\nKey=x\n; Key=3\nnot a directive\n"
        )

    def test_windows_line_endings_survive(self):
        content = "[Unit]\r\nKey=1\r\n"
        assert edited(content, lambda unit: unit.map_directives(lambda d: d)) == content


class TestEnsureDirective:
    def ensure(self, content):
        return edited(content, lambda unit: unit.ensure_directive("Container", "Name", "app--main"))

    def test_injected_after_the_header(self):
        assert self.ensure("[Unit]\nDescription=x\n\n[Container]\nFoo=1\n") == (
            "[Unit]\nDescription=x\n\n[Container]\nName=app--main\nFoo=1\n"
        )

    @pytest.mark.parametrize("existing", ["Name=custom", "Name = custom", "  Name=custom"])
    def test_explicit_value_is_kept(self, existing):
        content = f"[Container]\n{existing}\nImage=x\n"
        assert self.ensure(content) == content

    def test_set_in_another_section_does_not_count(self):
        assert self.ensure("[Unit]\nName=x\n[Container]\nImage=x\n") == (
            "[Unit]\nName=x\n[Container]\nName=app--main\nImage=x\n"
        )

    def test_commented_value_does_not_count(self):
        assert self.ensure("[Container]\n# Name=custom\nImage=x\n") == (
            "[Container]\nName=app--main\n# Name=custom\nImage=x\n"
        )

    def test_no_target_section_leaves_content_alone(self):
        assert self.ensure("[Unit]\nDescription=x\n") == "[Unit]\nDescription=x\n"

    def test_only_the_first_of_duplicate_sections(self):
        assert self.ensure("[Container]\nImage=x\n[Container]\nExec=y\n") == (
            "[Container]\nName=app--main\nImage=x\n[Container]\nExec=y\n"
        )

    def test_lines_before_the_first_header_are_kept(self):
        assert self.ensure("# Why\n# and who\n\n[Container]\nImage=x\n") == (
            "# Why\n# and who\n\n[Container]\nName=app--main\nImage=x\n"
        )

    def test_file_without_any_header_is_kept_as_written(self):
        # Podman's generator rejects it, and says why, rather than it deploying empty
        assert self.ensure("Image=x\nExec=y\n") == "Image=x\nExec=y\n"

    def test_header_with_inner_spaces_is_not_matched(self):
        # Pinned: "[ Container ]" is a different section name to this code
        assert self.ensure("[ Container ]\nImage=x\n") == "[ Container ]\nImage=x\n"


class TestAppendToSection:
    @pytest.mark.parametrize(
        "content, at_end, expected",
        [
            # Into a middle section, ahead of the blank line separating it from the next one
            ("[Unit]\nA=1\n\n[Service]\nB=2\n", False, "[Unit]\nA=1\nX=1\n\n[Service]\nB=2\n"),
            # Into the last section
            ("[Unit]\nA=1\n", False, "[Unit]\nA=1\nX=1\n"),
            # No blank line before the next header
            ("[Unit]\nA=1\n[Service]", False, "[Unit]\nA=1\nX=1\n[Service]"),
            # An empty section
            ("[Unit]\n[Service]", False, "[Unit]\nX=1\n[Service]"),
            # Blank lines only: inserted right after the header
            ("[Unit]\n\n\n[Service]\n", False, "[Unit]\nX=1\n\n\n[Service]\n"),
            # Missing section, opened at the top
            ("[Service]\nB=2\n", False, "[Unit]\nX=1\n\n[Service]\nB=2\n"),
            ("", False, "[Unit]\nX=1\n\n"),
            # ... but below the comments that open the file
            (
                "# The app\n\n[Service]\nB=2\n",
                False,
                "# The app\n\n[Unit]\nX=1\n\n[Service]\nB=2\n",
            ),
            # ... and above a comment touching the first header, which is about that section
            (
                "# The app\n\n# The service\n[Service]\nB=2\n",
                False,
                "# The app\n\n[Unit]\nX=1\n\n# The service\n[Service]\nB=2\n",
            ),
            (
                "# The service\n[Service]\nB=2\n",
                False,
                "[Unit]\nX=1\n\n# The service\n[Service]\nB=2\n",
            ),
            # A file of comments only
            ("# Nothing yet\n", False, "# Nothing yet\n\n[Unit]\nX=1\n"),
            # Missing section, opened at the end, trailing blank lines moved before it
            ("[Service]\nB=2\n\n\n", True, "[Service]\nB=2\n\n[Unit]\nX=1\n"),
            ("", True, "[Unit]\nX=1\n"),
            # Only the first of duplicate sections is used
            ("[Unit]\nA=1\n[Unit]\nB=2\n", False, "[Unit]\nA=1\nX=1\n[Unit]\nB=2\n"),
        ],
    )
    def test_placement(self, content, at_end, expected):
        unit = UnitFile(content)
        unit.append_to_section("Unit", ["X=1"], at_end=at_end)
        assert unit.text == expected

    def test_at_end_only_matters_when_the_section_is_missing(self):
        content = "[Unit]\nA=1\n\n[Service]\nB=2\n"
        assert edited(
            content, lambda unit: unit.append_to_section("Unit", ["X=1"], at_end=True)
        ) == ("[Unit]\nA=1\nX=1\n\n[Service]\nB=2\n")

    def test_several_lines_keep_their_order(self):
        assert edited("[Unit]\n", lambda unit: unit.append_to_section("Unit", ["A=1", "B=2"])) == (
            "[Unit]\nA=1\nB=2\n"
        )

    def test_edits_build_on_each_other(self):
        unit = UnitFile("[Service]\nB=2\n")
        unit.append_to_section("Unit", ["A=1"])
        unit.append_to_section("Unit", ["A=2"])
        unit.append_to_section("Install", ["W=1"], at_end=True)
        assert unit.text == "[Unit]\nA=1\nA=2\n\n[Service]\nB=2\n\n[Install]\nW=1\n"
