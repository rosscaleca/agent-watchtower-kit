#!/usr/bin/env python3
"""The ``acted_through`` CLI (``wt_acted.py``).

The drain reports what a session was handed; this is how the session
records what it actually handled. The recording is deliberately explicit
rather than inferred from a transcript — a wrong inference marks an
unhandled event handled, silently, which is the exact blind spot this
layer closes.

These pin the properties that matter: acting clears, listing never
moves a position, precise advancement stops at the named event, and a
missing profile fails loudly HERE (unlike the hook, which must stay silent —
a human at a prompt should be told; a turn-boundary hook must not wedge).

A blanket act clears only what a listing showed the session (pinned in
test_blind_clear.py), so the blanket acts here list first.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import stop_drain  # noqa: E402
import wt_acted  # noqa: E402


MAIL = "type=mail_received inbox=pm-acme auth=pass message_id=%s ts=2026-08-20T23:46:00Z"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.logs)
        os.makedirs(self.state)
        self.profile = "acme"
        # Ownership: pin the SESSION path; see test_stop_drain.
        self._env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "test-session-0001", "CLAUDE_PID": "0"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write_log(self, source, lines):
        with open(os.path.join(self.logs, "%s-%s.log" % (self.profile, source)), "a") as fh:
            for line in lines:
                fh.write(line + "\n")

    def run_cli(self, *extra):
        argv = ["--profile", self.profile,
                "--log-dir", self.logs, "--state-root", self.state] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = wt_acted.main(argv)
        return code, out.getvalue(), err.getvalue()

    def open_count(self):
        return sum(len(v) for v in
                   stop_drain.report_unactioned(self.logs, self.state, self.profile).values())


class TestActing(Base):
    def test_acting_clears_every_source(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.write_log("github", ["type=pr_opened repo=x ts=2026-08-20T23:00:00Z"])
        self.assertEqual(self.open_count(), 2)
        self.run_cli("--list")
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("2 event(s) cleared", out)
        self.assertEqual(self.open_count(), 0)

    def test_precise_advance_leaves_later_events_open(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb", MAIL % "ccc"])
        code, out, _ = self.run_cli("--source", "inbox", "--through", "2")
        self.assertEqual(code, 0)
        self.assertEqual(self.open_count(), 1)
        self.assertIn("message_id=ccc", out)

    def test_one_source_does_not_clear_another(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.write_log("github", ["type=pr_opened repo=x ts=2026-08-20T23:00:00Z"])
        self.run_cli("--list")
        self.run_cli("--source", "inbox")
        still = stop_drain.report_unactioned(self.logs, self.state, self.profile)
        self.assertEqual(list(still), ["github"])

    def test_acting_also_clears_the_block_path(self):
        """After acting, the hook must not still be holding the turn open."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.run_cli("--list")
        self.run_cli()
        self.assertEqual(
            stop_drain.drain(self.logs, self.state, self.profile, real_invocation=True), {})


class TestListing(Base):
    def test_list_reports_and_moves_no_position(self):
        """`--list` records what it showed (test_blind_clear.py), but it moves no position."""
        self.write_log("inbox", [MAIL % "aaa"])
        code, out, _ = self.run_cli("--list")
        self.assertEqual(code, 0)
        self.assertIn("1 unactioned event(s)", out)
        self.assertIn("message_id=aaa", out)
        self.assertEqual(self.open_count(), 1)  # unchanged

    def test_list_is_quiet_when_clean(self):
        code, out, _ = self.run_cli("--list")
        self.assertEqual(code, 0)
        self.assertIn("no unactioned events", out)

    def test_surfaced_but_unacted_still_lists(self):
        """Being shown an item is not the same as having handled it."""
        self.write_log("inbox", [MAIL % "aaa"])
        stop_drain.drain(self.logs, self.state, self.profile, real_invocation=True)  # surfaces it
        _, out, _ = self.run_cli("--list")
        self.assertIn("message_id=aaa", out)


class TestProfileResolution(Base):
    def test_missing_profile_fails_loudly_here(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = wt_acted.main(["--log-dir", self.logs, "--state-root", self.state])
        self.assertEqual(code, 2)
        self.assertIn("no profile", err.getvalue())


    def test_auto_resolution_uses_the_given_log_dir(self):
        """--log-dir must drive profile auto-detection too, not the default dir.

        Otherwise a caller pointed at a non-default tree silently resolves
        against whatever the machine happens to have (or nothing at all).
        """
        self.write_log("inbox", [MAIL % "aaa"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = wt_acted.main(["--log-dir", self.logs, "--state-root", self.state, "--list"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("message_id=aaa", out.getvalue())


class TestCursorShape(Base):
    def test_cursor_records_bytes_for_rotation_detection(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.run_cli("--list")
        self.run_cli()
        with open(os.path.join(self.state, self.profile, "acted.json")) as fh:
            data = json.load(fh)
        entry = data["sources"]["inbox"]
        self.assertEqual(entry["acted_through"], 1)
        self.assertGreater(entry["bytes"], 0)


if __name__ == "__main__":
    unittest.main()


class TestOvershootIsLoud(Base):
    """`--through` past the tail is a typo; the human surface must say so.

    The library clamps (safe), but silent clamping on the CLI would let a
    fat-fingered `--through 133` look like it worked. Found in review
    (a review): the failure it prevents is a cursor parked
    in the future, silently pre-marking events nobody has seen.
    """

    def test_through_past_the_tail_refuses_loudly(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        code, out, err = self.run_cli("--source", "inbox", "--through", "99")
        self.assertEqual(code, 2)
        self.assertIn("99", err)
        self.assertIn("2", err)  # names the real tail
        self.assertEqual(self.open_count(), 2, "a refused call must change nothing")

    def test_through_exactly_at_the_tail_is_fine(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        code, _, _ = self.run_cli("--source", "inbox", "--through", "2")
        self.assertEqual(code, 0)
        self.assertEqual(self.open_count(), 0)
