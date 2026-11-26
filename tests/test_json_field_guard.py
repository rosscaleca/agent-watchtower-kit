"""An executable guard for the nested-connection vector.

An earlier defect took the GitHub leg dark for hours: a ``gh ... --json`` field set named
``commits``, gh expanded each commit with an implicit ``authors(first:100)``
connection, and GitHub rejected the request on the REQUESTED LIMIT before it
looked at any data. Afterwards only two prose comments stood in the way of the
next caller doing the same, and the defect itself was a two-commit one that no
single diff showed. These tests pin a PROPERTY rather than a caller list: no
``--json`` argument anywhere in the scanned tree may name a field whose gh
expansion opens a nested connection. That includes a field set that arrives
through a module constant, which is exactly how it arrived.
"""
import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DAEMONS = os.path.join(REPO, "daemons")
sys.path.insert(0, HERE)
import json_field_guard as guard  # noqa: E402


def _daemon_files():
    return sorted(os.path.join(DAEMONS, f) for f in os.listdir(DAEMONS) if f.endswith(".py"))


class TestTheRealTree(unittest.TestCase):
    def test_no_json_argument_under_daemons_names_a_nested_connection_field(self):
        findings = guard.scan(_daemon_files())
        self.assertEqual(findings, [], "\n".join(guard.describe(f) for f in findings))

    def test_commits_is_on_the_deny_list_with_its_reason(self):
        reason = guard.NESTED_FIELDS["commits"]
        self.assertIn("authors(first:100)", reason)
        self.assertIn("rejects the request", reason)


class TestTheGuardRunsOnEveryCiPython(unittest.TestCase):
    def test_the_guard_names_no_ast_node_class_removed_in_python_3_14(self):
        """CI runs 3.9 and 3.14. ast.Str / Num / Bytes / NameConstant / Ellipsis
        are gone in 3.14, and an isinstance check against one raises
        AttributeError there, which fails every scan."""
        with open(guard.__file__) as fh:
            source = fh.read()
        for removed in ("ast.Str", "ast.Num", "ast.Bytes", "ast.NameConstant", "ast.Ellipsis"):
            self.assertNotIn(removed, source)


class TestTheGuardCatchesTheVector(unittest.TestCase):
    """Negative controls: each shape must be caught, or the guard proves nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, source):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(source))
        return path

    def fields(self, findings):
        return [f.field for f in findings]

    def test_a_literal_field_set_naming_commits_is_caught_with_file_line_and_reason(self):
        path = self.write("caller.py", """\
            import subprocess
            argv = ["gh", "pr", "list", "--limit", "30",
                    "--json", "number,title,commits"]
            """)
        findings = guard.scan([path])
        self.assertEqual(self.fields(findings), ["commits"])
        self.assertEqual(findings[0].path, path)
        self.assertEqual(findings[0].line, 3)
        self.assertIn("authors(first:100)", findings[0].reason)
        self.assertIn("caller.py:3", guard.describe(findings[0]))

    def test_the_json_equals_form_is_caught(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json=number,commits"]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_same_module_constant_is_resolved_and_caught(self):
        path = self.write("caller.py", """\
            FIELDS = "number,commits"
            argv = ["gh", "pr", "list", "--json", FIELDS]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_join_over_a_tuple_constant_in_another_module_is_caught_the_td958_shape(self):
        fields_mod = self.write("fields_mod.py", """\
            FIELDS = ("number", "reviews", "commits")
            """)
        caller = self.write("caller.py", """\
            import fields_mod
            argv = ["gh", "pr", "list", "--json", ",".join(fields_mod.FIELDS)]
            """)
        findings = guard.scan([fields_mod, caller])
        self.assertEqual(self.fields(findings), ["commits"])
        self.assertEqual(findings[0].path, caller)

    def test_an_aliased_import_is_resolved(self):
        fields_mod = self.write("fields_mod.py", """\
            FIELDS = ["number", "commits"]
            """)
        caller = self.write("caller.py", """\
            import fields_mod as fm
            argv = ["gh", "pr", "list", "--json", ",".join(fm.FIELDS)]
            """)
        self.assertEqual(self.fields(guard.scan([fields_mod, caller])), ["commits"])

    def test_a_from_import_is_resolved(self):
        fields_mod = self.write("fields_mod.py", """\
            FIELDS = "number,commits"
            """)
        caller = self.write("caller.py", """\
            from fields_mod import FIELDS
            argv = ["gh", "pr", "list", "--json", FIELDS]
            """)
        self.assertEqual(self.fields(guard.scan([fields_mod, caller])), ["commits"])

    def test_an_argv_extended_in_place_is_caught(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list"]
            argv += ["--json", "number,commits"]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_the_real_trap_constant_would_be_caught_if_wired_into_json(self):
        """GITHUB_PR_REVIEW_FIELDS still names commits (its classifier contract
        needs the head commit). Wiring it into --json is that defect exactly."""
        caller = self.write("caller.py", """\
            import _review_binding
            argv = ["gh", "pr", "list", "--json", ",".join(_review_binding.GITHUB_PR_REVIEW_FIELDS)]
            """)
        rb = os.path.join(DAEMONS, "_review_binding.py")
        self.assertEqual(self.fields(guard.scan([rb, caller])), ["commits"])

    def test_an_unresolvable_field_set_fails_closed(self):
        path = self.write("caller.py", """\
            def fields():
                return "number"
            argv = ["gh", "pr", "list", "--json", fields()]
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unresolvable"), findings[0].reason)
        self.assertEqual(findings[0].line, 3)

    def test_a_graphql_query_that_mentions_commits_is_not_a_json_argument(self):
        path = self.write("caller.py", """\
            QUERY = "commits(last:1) { nodes { commit { oid committedDate } } }"
            argv = ["gh", "api", "graphql", "-f", "query=" + QUERY]
            """)
        self.assertEqual(guard.scan([path]), [])

    def test_an_unparseable_file_fails_closed(self):
        """A guard that skips a file it cannot parse passes the one file it never read."""
        path = self.write("broken.py", """\
            argv = ["gh", "pr", "list", "--json", "number,commits"
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unparseable"), findings[0].reason)
        self.assertEqual(findings[0].path, path)

    def test_a_bare_json_flag_outside_an_argv_list_fails_closed(self):
        """argv.append("--json") pairs the flag with nothing the guard can see."""
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list"]
            argv.append("--json")
            argv.append("number,commits")
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unresolvable"), findings[0].reason)
        self.assertEqual(findings[0].line, 2)

    def test_a_json_flag_that_ends_its_list_fails_closed(self):
        """["gh", ..., "--json"] + [fields] puts the field set in ANOTHER list:
        the flag has no next element to check, so it must not count as checked."""
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json"] + ["number,commits"]
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unresolvable"), findings[0].reason)

    def test_a_json_equals_literal_outside_a_list_is_still_checked(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list"]
            argv.append("--json=number,commits")
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    # ---- A review found: --json=<fields> built by an f-string,
    # a concatenation or a %-format must be seen, not skipped as "not a
    # Constant". Each shape is a negative control; the safe f-string is the
    # positive control that keeps the fix from over-firing.

    def test_an_fstring_json_equals_over_a_constant_is_caught(self):
        path = self.write("caller.py", """\
            FIELDS = "number,commits"
            argv = ["gh", "pr", "list", f"--json={FIELDS}"]
            """)
        findings = guard.scan([path])
        self.assertEqual(self.fields(findings), ["commits"])
        self.assertEqual(findings[0].line, 2)

    def test_a_concatenated_json_equals_is_caught(self):
        path = self.write("caller.py", """\
            FIELDS = "number,commits"
            argv = ["gh", "pr", "list", "--json=" + FIELDS]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_percent_formatted_json_equals_is_caught(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json=%s" % "number,commits"]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_percent_format_over_a_tuple_is_caught(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json=%s,%s" % ("number", "commits")]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_concatenation_chain_is_reported_exactly_once(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json=" + "number," + "commits"]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_an_fstring_with_a_literal_field_set_is_reported_exactly_once(self):
        path = self.write("caller.py", """\
            SUFFIX = ""
            argv = ["gh", "pr", "list", f"--json=number,commits{SUFFIX}"]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_an_fstring_json_equals_over_a_safe_constant_is_not_flagged(self):
        path = self.write("caller.py", """\
            SAFE = "number,title"
            argv = ["gh", "pr", "list", f"--json={SAFE}"]
            """)
        self.assertEqual(guard.scan([path]), [])

    def test_an_fstring_json_equals_over_an_unresolvable_value_fails_closed(self):
        path = self.write("caller.py", """\
            def fields():
                return "number"
            argv = ["gh", "pr", "list", f"--json={fields()}"]
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unresolvable"), findings[0].reason)
        self.assertEqual(findings[0].line, 3)

    def test_an_fstring_with_a_conversion_or_format_spec_fails_closed(self):
        """f"{X!r}" / f"{X:>9}" do not render X verbatim, so the guard must not
        read the field set as if they did."""
        for expr in ('f"--json={SAFE!r}"', 'f"--json={SAFE:>20}"'):
            path = self.write("caller.py", 'SAFE = "number,title"\nargv = ["gh", "pr", "list", %s]\n' % expr)
            findings = guard.scan([path])
            self.assertEqual(len(findings), 1, expr)
            self.assertIsNone(findings[0].field, expr)
            self.assertTrue(findings[0].reason.startswith("unresolvable"), expr)

    # ---- A re-review found: str.format is the fourth common way to
    # build --json=<fields>, and the maximal node is then an ast.Call.

    def test_a_str_format_json_equals_is_caught(self):
        path = self.write("caller.py", """\
            FIELDS = "number,commits"
            argv = ["gh", "pr", "list", "--json={}".format(FIELDS)]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_str_format_with_a_keyword_is_caught(self):
        path = self.write("caller.py", """\
            FIELDS = "number,commits"
            argv = ["gh", "pr", "list", "--json={f}".format(f=FIELDS)]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_str_format_is_reported_exactly_once(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json={},commits".format("number")]
            """)
        self.assertEqual(self.fields(guard.scan([path])), ["commits"])

    def test_a_str_format_over_a_safe_constant_is_not_flagged(self):
        path = self.write("caller.py", """\
            SAFE = "number,title"
            argv = ["gh", "pr", "list", "--json={}".format(SAFE)]
            """)
        self.assertEqual(guard.scan([path]), [])

    def test_a_str_format_over_an_unresolvable_value_fails_closed(self):
        path = self.write("caller.py", """\
            def fields():
                return "number"
            argv = ["gh", "pr", "list", "--json={}".format(fields())]
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unresolvable"), findings[0].reason)

    def test_any_other_method_on_a_json_equals_literal_fails_closed(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json=number,title".strip()]
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unresolvable"), findings[0].reason)

    def test_a_concatenation_over_an_unresolvable_value_fails_closed(self):
        path = self.write("caller.py", """\
            def fields():
                return "number"
            argv = ["gh", "pr", "list", "--json=" + fields()]
            """)
        findings = guard.scan([path])
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].field)
        self.assertTrue(findings[0].reason.startswith("unresolvable"), findings[0].reason)

    def test_each_file_is_parsed_once(self):
        """A review found: the guard runs in every CI job, so parse each file once."""
        a = self.write("a.py", 'X = "number"\n')
        b = self.write("b.py", 'argv = ["gh", "pr", "list", "--json", "number"]\n')
        real_parse = guard.ast.parse
        calls = []

        def counting_parse(*args, **kwargs):
            calls.append(1)
            return real_parse(*args, **kwargs)

        with mock.patch.object(guard.ast, "parse", counting_parse):
            self.assertEqual(guard.scan([a, b]), [])
        self.assertEqual(len(calls), 2)

    def test_a_safe_field_set_is_not_flagged(self):
        path = self.write("caller.py", """\
            argv = ["gh", "pr", "list", "--json", "number,title,body,mergedAt"]
            """)
        self.assertEqual(guard.scan([path]), [])


if __name__ == "__main__":
    unittest.main()
