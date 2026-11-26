#!/usr/bin/env python3
"""The Stop-hook drain and its ``acted_through`` cursor.

What these pin, and why each matters (the spike's findings):

- A wake line that reached a session and was never acted on is invisible
  today. The daemon watermark means "observed by the daemon", NOT "acted
  on by the session" — that gap is the defect this layer closes.
- The drain must block turn-end ONCE per event, never forever: the
  ``stop_hook_active`` loop guard plus ``surfaced_through`` are what stop
  a session that cannot act on an item from re-blocking to death.
- Surfacing an event is not the same as acting on it. An item the hook
  has shown but the session never handled must STILL read as unactioned
  in the accounting, or the mechanism launders the very gap it exists to
  expose.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import stop_drain  # noqa: E402


MAIL = "type=mail_received inbox=pm-acme auth=pass message_id=%s ts=2026-08-20T23:46:00Z"
ERR = "error: watchdog: inbox stale — watermark 12m old ts=2026-08-20T23:50:00Z"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.logs)
        os.makedirs(self.state)
        self.profile = "acme"
        # Ownership: these suites pin the SESSION path. The
        # actor comes from the environment, so pin one here rather than inherit
        # whatever the machine running the tests happens to have.
        self._env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "test-session-0001", "CLAUDE_PID": "0"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def write_log(self, source, lines):
        path = os.path.join(self.logs, "%s-%s.log" % (self.profile, source))
        with open(path, "a") as fh:
            for line in lines:
                fh.write(line + "\n")
        return path

    def drain(self, stop_hook_active=False):
        return stop_drain.drain(
            log_dir=self.logs,
            state_root=self.state,
            profile=self.profile,
            stop_hook_active=stop_hook_active,
            real_invocation=True,
        )


class TestParsing(Base):
    def test_only_event_and_error_lines_count(self):
        """The drain counts exactly what a session's Monitor greps: ^type= / ^error:."""
        text = "\n".join([
            MAIL % "aaa",
            "some daemon chatter that is not an event",
            ERR,
            "",
            "note: starting up",
        ])
        events = stop_drain.parse_events(text)
        self.assertEqual([e.index for e in events], [1, 2])
        self.assertEqual(events[0].kind, "event")
        self.assertEqual(events[1].kind, "error")

    def test_index_is_stable_across_appends(self):
        """Event N keeps index N when later lines arrive — cursors depend on it."""
        first = stop_drain.parse_events(MAIL % "aaa")
        later = stop_drain.parse_events("\n".join([MAIL % "aaa", MAIL % "bbb"]))
        self.assertEqual(first[0].index, 1)
        self.assertEqual(later[0].index, 1)
        self.assertEqual(later[1].index, 2)


class TestUnactioned(Base):
    def test_no_cursor_means_everything_is_unactioned(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        found = stop_drain.collect_unactioned(self.logs, self.state, self.profile)
        self.assertEqual([e.index for e in found["inbox"]], [1, 2])

    def test_acted_cursor_hides_handled_events(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb", MAIL % "ccc"])
        stop_drain.mark_acted(self.logs, self.state, self.profile, source="inbox", through=2)
        found = stop_drain.collect_unactioned(self.logs, self.state, self.profile)
        self.assertEqual([e.index for e in found["inbox"]], [3])

    def test_multiple_sources_are_drained_independently(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.write_log("github", ["type=pr_opened repo=x ts=2026-08-20T23:00:00Z"])
        stop_drain.mark_acted(self.logs, self.state, self.profile, source="inbox", through=1)
        found = stop_drain.collect_unactioned(self.logs, self.state, self.profile)
        self.assertNotIn("inbox", found)
        self.assertEqual(len(found["github"]), 1)

    def test_truncated_log_resets_the_cursor_instead_of_swallowing_events(self):
        """Rotation must not leave a cursor past the end, silently hiding new mail."""
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb", MAIL % "ccc"])
        stop_drain.mark_acted(self.logs, self.state, self.profile, source="inbox", through=3)
        path = os.path.join(self.logs, "%s-inbox.log" % self.profile)
        with open(path, "w") as fh:  # rotated: file is now smaller
            fh.write(MAIL % "ddd" + "\n")
        found = stop_drain.collect_unactioned(self.logs, self.state, self.profile)
        self.assertEqual([e.index for e in found["inbox"]], [1])


class TestDrainDecision(Base):
    def test_blocks_with_the_unactioned_list(self):
        self.write_log("inbox", [MAIL % "aaa"])
        decision = self.drain()
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("message_id=aaa", decision["reason"])
        self.assertIn("inbox", decision["reason"])

    def test_quiet_when_nothing_is_pending(self):
        self.write_log("inbox", [MAIL % "aaa"])
        stop_drain.mark_acted(self.logs, self.state, self.profile, source="inbox", through=1)
        self.assertEqual(self.drain(), {})

    def test_no_logs_at_all_is_quiet_not_an_error(self):
        self.assertEqual(self.drain(), {})

    def test_stop_hook_active_never_blocks(self):
        """The harness loop guard: we already re-injected once this turn-end."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.assertEqual(self.drain(stop_hook_active=True), {})

    def test_an_event_blocks_only_once(self):
        """Surfacing advances `surfaced_through`, so an unactionable item can't wedge the session."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.assertEqual(self.drain().get("decision"), "block")
        self.assertEqual(self.drain(), {})  # same event, second turn-end: let it go

    def test_new_event_after_a_surfaced_one_still_blocks(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.drain()
        self.write_log("inbox", [MAIL % "bbb"])
        decision = self.drain()
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("message_id=bbb", decision["reason"])
        self.assertNotIn("message_id=aaa", decision["reason"])

    def test_surfaced_but_unacted_still_reads_as_unactioned(self):
        """The accounting must not launder the gap it exists to expose."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.drain()
        still = stop_drain.report_unactioned(self.logs, self.state, self.profile)
        self.assertEqual([e.index for e in still["inbox"]], [1])

    def test_acting_clears_the_report(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.drain()
        stop_drain.mark_acted(self.logs, self.state, self.profile)
        self.assertEqual(stop_drain.report_unactioned(self.logs, self.state, self.profile), {})

    def test_error_lines_block_too(self):
        self.write_log("watchdog", [ERR])
        self.assertIn("watchdog", self.drain().get("reason", ""))

    def test_the_block_leads_with_unactioned_as_a_command_of_its_own(self):
        """The block is not a listing, so the CLI refuses a
        blanket `just acted` right after it; its advice leads with `just unactioned`.
        A review's note 2: the clear must be a SEPARATE command — chained onto the
        listing with `&&`, a blanket clear passes the gate for an event nobody read yet; note
        6, no by-number literal — the block shows only what is newly surfaced, so "through
        #n" could sweep an older open event it never showed (the listing gives the clears)."""
        self.write_log("inbox", [MAIL % "aaa"])
        reason = self.drain()["reason"]
        commands = [ln.strip() for ln in reason.splitlines() if ln.strip().startswith("just ")]
        self.assertEqual(len(commands), 1, commands)
        self.assertTrue(commands[0].startswith("just unactioned acme "), commands)
        self.assertIn("THEN, as a separate command", commands[0])
        # A review's approval: this line is shaped to be copied whole, and pasted
        # into an interactive zsh (no comments by default) a backticked `just acted` would
        # run as a command substitution: a blanket clear, its output swallowed.
        self.assertNotIn("`", commands[0])
        self.assertIn("the listing ends with the by-number clears", reason)
        self.assertFalse(any(c.split("#")[0].split()[:2] == ["just", "acted"] for c in commands),
                         "no `just acted` command line at all: not blanket, not a literal number")
        self.assertNotIn("inbox 13", reason)
        self.assertNotIn("through the current tail", reason)


class TestCursorFile(Base):
    def test_cursor_is_written_where_the_design_says(self):
        self.write_log("inbox", [MAIL % "aaa"])
        stop_drain.mark_acted(self.logs, self.state, self.profile)
        path = os.path.join(self.state, self.profile, "acted.json")
        self.assertTrue(os.path.exists(path))
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(data["sources"]["inbox"]["acted_through"], 1)

    def test_corrupt_cursor_is_survived_not_fatal(self):
        """A hook that crashes would block every turn-end on this machine.

        Contract change (ownership): a corrupt
        cursor used to read as EMPTY and the events resurfaced. With an owner
        in the file that is wrong — an unreadable acted.json is not an unowned
        one — so the hook now STANDS DOWN: no crash, no block, and the file is
        left exactly as found for the operator to remove or repair. The old
        "reset and resurface" is what deleting the file does, made explicit.
        """
        os.makedirs(os.path.join(self.state, self.profile), exist_ok=True)
        path = os.path.join(self.state, self.profile, "acted.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        self.write_log("inbox", [MAIL % "aaa"])
        self.assertEqual(self.drain(), {})
        with open(path) as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_mark_acted_all_sources_advances_each(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.write_log("github", ["type=pr_opened repo=x ts=2026-08-20T23:00:00Z"])
        stop_drain.mark_acted(self.logs, self.state, self.profile)
        self.assertEqual(stop_drain.report_unactioned(self.logs, self.state, self.profile), {})


class TestHookIO(Base):
    def test_run_hook_emits_block_json(self):
        self.write_log("inbox", [MAIL % "aaa"])
        out = stop_drain.run_hook(
            {"stop_hook_active": False, "hook_event_name": "Stop"},
            log_dir=self.logs,
            state_root=self.state,
            profile=self.profile,
        )
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_run_hook_emits_nothing_when_quiet(self):
        self.assertEqual(
            stop_drain.run_hook(
                {"stop_hook_active": False, "hook_event_name": "Stop"},
                log_dir=self.logs,
                state_root=self.state,
                profile=self.profile,
            ),
            "",
        )

    def test_unknown_profile_is_quiet(self):
        """A misconfigured profile must not wedge every turn on the machine."""
        out = stop_drain.run_hook(
            {"stop_hook_active": False, "hook_event_name": "Stop"},
            log_dir=self.logs,
            state_root=self.state,
            profile="no-such-profile",
        )
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()


class TestSilentDropRegressions(Base):
    """Two silent-drop bugs found in review.

    Both are members of the exact class this tool exists to prevent: the
    cursor ends up ahead of events nobody handled, and nothing says so.
    """

    def test_rotate_then_outgrow_does_not_park_the_cursor(self):
        """Size-shrink-only detection misses rotate-then-grow.

        If a log is rotated and then grows past its old byte count before
        the next drain runs, the shrink is never observed — the cursor
        stays parked over the NEW file's early events and they are lost
        silently, which is the failure the drain exists to prevent.
        """
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb", MAIL % "ccc"])
        stop_drain.mark_acted(self.logs, self.state, self.profile, source="inbox", through=3)
        path = os.path.join(self.logs, "%s-inbox.log" % self.profile)
        old_size = os.path.getsize(path)
        with open(path, "w") as fh:  # rotated, then grown past the old size
            for i in range(6):
                fh.write((MAIL % ("new%d" % i)) + " padding=%s\n" % ("x" * 40))
        self.assertGreater(os.path.getsize(path), old_size, "fixture must outgrow the old file")
        found = stop_drain.collect_unactioned(self.logs, self.state, self.profile)
        self.assertEqual(len(found.get("inbox", [])), 6)

    def test_through_beyond_the_tail_cannot_premark_future_events(self):
        """An overshooting --through must not silently pre-handle real events."""
        self.write_log("inbox", [MAIL % "aaa"])
        stop_drain.mark_acted(self.logs, self.state, self.profile, source="inbox", through=999)
        self.write_log("inbox", [MAIL % "bbb", MAIL % "ccc"])
        still = stop_drain.report_unactioned(self.logs, self.state, self.profile)
        self.assertEqual([e.index for e in still.get("inbox", [])], [2, 3])

    def test_cursor_upgrade_from_a_pre_fingerprint_file_is_not_a_reset(self):
        """Old cursors lack the fingerprint; that must not resurface everything."""
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        os.makedirs(os.path.join(self.state, self.profile), exist_ok=True)
        path = os.path.join(self.logs, "%s-inbox.log" % self.profile)
        legacy = {"version": 1, "sources": {"inbox": {
            "acted_through": 2, "surfaced_through": 2,
            "bytes": os.path.getsize(path)}}}  # no "first" key
        with open(os.path.join(self.state, self.profile, "acted.json"), "w") as fh:
            json.dump(legacy, fh)
        self.assertEqual(stop_drain.report_unactioned(self.logs, self.state, self.profile), {})
