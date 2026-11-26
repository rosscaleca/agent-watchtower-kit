#!/usr/bin/env python3
"""A blanket ``just acted`` must not sweep what nobody read.

An incident: ``just acted pm-acme`` at the end of a turn
that had handled ONE mail cleared TWO. The second had landed mid-turn, while the
first was being written up, and the only trace was the number. A blanket clear
went through the CURRENT tail and printed a count. These pin the two changes:

- every act prints each event it cleared, in ``just unactioned``'s own format;
- a blanket act refuses — printing the strays, changing nothing — while an open
  event exists that no ``just unactioned`` listing showed this session (the
  operator, who has no session identity: the last listing by anyone).
  ``--all-including-unseen`` is the deliberate override; ``--through N`` needs
  no listing;
- no act clears from a log replaced after the act read it (TestCodexOnF58b31c).

Temp log/state trees only — the live defaults are pointed at them too — and the
actor, liveness and process identity are injected, never read from this process
(which may itself be a Claude Code session with its own identity).
"""
import fcntl
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import stop_drain  # noqa: E402
import wt_acted  # noqa: E402

MAIL = "type=mail_received inbox=pm-acme auth=pass message_id=%s ts=2026-09-17T05:23:07Z"
PR = "type=pr_opened repo=x number=%s ts=2026-09-17T05:00:00Z"
ME = "7e4151da-0000-4000-8000-000000000001"
OTHER = "3b2c9d1e-0000-4000-8000-000000000002"
OPERATOR = {"kind": "operator", "user": "alice", "tty": "/dev/ttys003", "cwd": "/w"}
WT_ACTED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "wt_acted.py")


def session(sid, pid=11, cwd="/w/Acme"):
    return {"kind": "session", "session_id": sid, "pid": pid, "cwd": cwd}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, "logs")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.logs)
        os.makedirs(self.state)
        self.profile = "acme"
        self.alive = {11: True, 22: True}
        patches = {
            # The live trees are the argparse DEFAULTS: point them here as well, so a
            # call that forgot --log-dir / --state-root lands in the temp tree.
            "DEFAULT_LOG_DIR": self.logs,
            "DEFAULT_STATE_ROOT": self.state,
            "LOCK_WAIT": 0.2,
            "pid_alive": lambda pid: self.alive.get(pid),
            "boot_identity": lambda: "boot-A",
            "process_start": lambda pid: None,
        }
        for name, value in patches.items():
            p = mock.patch.object(stop_drain, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_log(self, source, lines, mode="a"):
        with open(os.path.join(self.logs, "%s-%s.log" % (self.profile, source)), mode) as fh:
            for line in lines:
                fh.write(line + "\n")

    def cli(self, *argv, **kw):
        actor = kw.get("actor") or session(ME)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(stop_drain, "actor_identity",
                               lambda payload=None, environ=None: actor):
            with redirect_stdout(out), redirect_stderr(err):
                rc = wt_acted.main(["--profile", self.profile, "--log-dir", self.logs,
                                    "--state-root", self.state] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def drain(self, actor=None):
        """A REAL turn-end by ``actor`` (default: this session)."""
        return stop_drain.drain(self.logs, self.state, self.profile,
                                actor=actor or session(ME), real_invocation=True)

    def cursor(self):
        return stop_drain.load_cursor(self.state, self.profile)

    def acted(self, source):
        return self.cursor()["sources"].get(source, {}).get("acted_through", 0)

    def cursor_bytes(self):
        try:
            with open(stop_drain.cursor_path(self.state, self.profile), "rb") as fh:
                return fh.read()
        except OSError:
            return b""


class TestActedListsWhatItCleared(Base):
    def test_a_blanket_act_prints_each_cleared_event_and_keeps_the_count(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        self.write_log("github", [PR % 7])
        self.cli("--list")
        rc, out, err = self.cli()
        self.assertEqual(rc, 0, err)
        self.assertIn("acted: 3 event(s) cleared (profile=acme)", out)
        self.assertIn("  [inbox #1] " + MAIL % "aaa", out)
        self.assertIn("  [inbox #2] " + MAIL % "bbb", out)
        self.assertIn("  [github #1] " + PR % 7, out)

    def test_an_explicit_act_prints_only_what_it_cleared(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb", MAIL % "ccc"])
        rc, out, err = self.cli("--source", "inbox", "--through", "2")
        self.assertEqual(rc, 0, err)
        cleared, _, still_open = out.partition("unactioned event(s)")
        self.assertIn("acted: 2 event(s) cleared", cleared)
        self.assertIn("[inbox #1]", cleared)
        self.assertIn("[inbox #2]", cleared)
        self.assertNotIn("message_id=ccc", cleared)
        self.assertIn("[inbox #3] " + MAIL % "ccc", still_open, "what is still open is still said")

    def test_a_cleared_line_is_the_unactioned_line_truncation_included(self):
        self.write_log("inbox", [MAIL % ("x" * 300)])
        _, listed, _ = self.cli("--list")
        _, acted, _ = self.cli()
        line = [ln for ln in listed.splitlines() if ln.startswith("  [inbox #1]")][0]
        self.assertTrue(line.endswith("…") and len(line) < 200, line)
        self.assertIn(line, acted.splitlines())


class TestBlindBlanketClearIsRefused(Base):
    def test_an_event_that_landed_after_the_listing_is_refused_shown_and_left_open(self):
        """The PM's case: list, handle one, a second lands mid-turn, clear the lot."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list")
        self.write_log("inbox", [MAIL % "bbb"])                   # lands mid-turn
        before = self.cursor_bytes()
        rc, out, err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("  [inbox #2] " + MAIL % "bbb", err)        # the stray is shown…
        self.assertNotIn("message_id=aaa", err)                   # …and only the stray
        self.assertIn("Nothing changed", err)
        self.assertNotIn("cleared", out)
        self.assertEqual(self.cursor_bytes(), before, "a refused act changes nothing")
        self.assertEqual(self.acted("inbox"), 0)

    def test_a_session_that_never_listed_is_refused(self):
        self.write_log("inbox", [MAIL % "aaa"])
        rc, _, err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("message_id=aaa", err)
        self.assertIn("this session has not listed", err)
        self.assertEqual(self.cursor_bytes(), b"", "refused before any claim: nothing written")

    def test_another_sessions_listing_does_not_count(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list", actor=session(OTHER, pid=22))
        rc, _, err = self.cli()
        self.assertEqual(rc, 2, "OTHER's listing is not this session's reading")
        self.assertIn("message_id=aaa", err)
        self.cli("--list")
        self.assertEqual(self.cli()[0], 0, "its own listing is")

    def test_the_hooks_block_is_not_a_listing(self):
        """The block prints LIST_CAP events and '… and N more', yet marks all surfaced."""
        n = stop_drain.LIST_CAP + 5
        self.write_log("inbox", [MAIL % i for i in range(1, n + 1)])
        self.assertIn("and 5 more", self.drain()["reason"])
        rc, _, err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("[inbox #%d]" % n, err, "never printed by the block, so never seen")
        self.assertEqual(self.acted("inbox"), 0)

    def test_a_source_scoped_blanket_clear_checks_only_that_source(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list")
        self.write_log("github", [PR % 7])                         # unlisted, other source
        rc, out, err = self.cli("--source", "inbox")
        self.assertEqual(rc, 0, err)
        self.assertIn("[inbox #1]", out)
        rc, _, err = self.cli("--source", "github")
        self.assertEqual(rc, 2)
        self.assertIn("[github #1]", err)


class TestBlanketClearOfWhatWasListed(Base):
    def test_nothing_new_since_the_listing_clears_to_the_tail(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        self.write_log("github", [PR % 7])
        self.cli("--list")
        rc, _, err = self.cli()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((self.acted("inbox"), self.acted("github")), (2, 1))

    def test_a_listing_survives_the_hooks_rewrite_of_the_cursor(self):
        """The record lives beside the positions; the hook's save must carry it."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list")
        self.assertEqual(self.drain().get("decision"), "block")   # rewrites acted.json
        rc, _, err = self.cli()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acted("inbox"), 1)


class TestOverride(Base):
    def test_the_override_clears_what_no_listing_showed_and_names_it(self):
        self.write_log("inbox", [MAIL % "aaa"])
        rc, out, err = self.cli("--all-including-unseen")
        self.assertEqual(rc, 0, err)
        self.assertIn("acted: 1 event(s) cleared", out)
        self.assertIn("  [inbox #1] " + MAIL % "aaa", out)
        self.assertEqual(self.acted("inbox"), 1)

    def test_the_override_does_not_combine_with_an_explicit_number(self):
        self.write_log("inbox", [MAIL % "aaa"])
        with self.assertRaises(SystemExit):
            self.cli("--through", "1", "--all-including-unseen")
        self.assertEqual(self.cursor_bytes(), b"")


class TestExplicitThroughNeedsNoListing(Base):
    def test_an_explicit_number_needs_no_listing_and_ignores_one(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb", MAIL % "ccc"])
        rc, _, err = self.cli("--source", "inbox", "--through", "1")   # never listed
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acted("inbox"), 1)
        self.cli("--list")
        self.write_log("inbox", [MAIL % "ddd"])                       # unlisted
        rc, _, err = self.cli("--source", "inbox", "--through", "4")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acted("inbox"), 4, "a named number is neither widened nor refused")


class TestOperatorFallback(Base):
    """The operator has no session identity, so the last listing by ANYONE counts."""

    def test_the_operator_may_rely_on_the_last_listing_by_anyone(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list")                                          # a session listed
        rc, _, err = self.cli(actor=OPERATOR)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acted("inbox"), 1)

    def test_the_operator_is_refused_what_arrived_after_that_listing(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list", actor=OPERATOR)
        self.write_log("inbox", [MAIL % "bbb"])
        rc, _, err = self.cli(actor=OPERATOR)
        self.assertEqual(rc, 2)
        self.assertIn("message_id=bbb", err)
        self.assertIn("last listing by anyone", err)

    def test_an_operator_listing_does_not_count_for_a_session(self):
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list", actor=OPERATOR)
        self.assertEqual(self.cli()[0], 2)


class TestTheListingRecord(Base):
    def hold_lock(self):
        path = stop_drain.lock_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def test_a_listing_moves_no_position_and_claims_nothing(self):
        self.write_log("inbox", [MAIL % "aaa"])
        rc, _, _ = self.cli("--list")
        self.assertEqual(rc, 0)
        c = self.cursor()
        self.assertNotIn("owner", c, "listing is not claiming")
        self.assertEqual(c["sources"], {}, "listing is not surfacing or acting")
        self.assertEqual(c[stop_drain.LISTED_KEY]["by"]["session_id"], ME)
        self.assertEqual(c[stop_drain.LISTED_KEY]["sources"]["inbox"]["through"], 1)

    def test_a_busy_profile_still_lists_but_records_nothing(self):
        self.write_log("inbox", [MAIL % "aaa"])
        fd = self.hold_lock()
        rc, out, err = self.cli("--list")
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(rc, 0)
        self.assertIn("message_id=aaa", out)
        self.assertIn("listing NOT recorded", err)
        self.assertEqual(self.cursor_bytes(), b"")
        self.assertEqual(self.cli()[0], 2, "an unrecorded listing lets nothing through")

    def test_a_listing_whose_write_fails_says_so_and_lets_nothing_through(self):
        self.write_log("inbox", [MAIL % "aaa"])
        with mock.patch.object(stop_drain, "save_cursor", lambda *a, **k: False):
            rc, _, err = self.cli("--list")
        self.assertEqual(rc, 0)
        self.assertIn("listing NOT recorded", err)
        self.assertEqual(self.cli()[0], 2)

    def test_an_event_landing_while_the_listing_prints_is_not_recorded_as_shown(self):
        """The record comes from the listing's own read, never from a later one."""
        self.write_log("inbox", [MAIL % "aaa"])
        real = wt_acted.render

        def render_then_land(pending, profile):
            text = real(pending, profile)
            self.write_log("inbox", [MAIL % "bbb"])               # after the read, before the record
            return text

        with mock.patch.object(wt_acted, "render", render_then_land):
            self.cli("--list")
        rc, _, err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("message_id=bbb", err)

    def test_a_rotated_log_voids_the_listing(self):
        """Rotate-then-outgrow: same name, bigger file, different events."""
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        self.cli("--list")
        self.write_log("inbox", [MAIL % ("new%d" % i) + " pad=" + "x" * 40 for i in range(3)],
                       mode="w")
        rc, _, err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("[inbox #1]", err, "the new file's first event was never listed")


class TestNoSweepMidAct(Base):
    def test_an_event_landing_after_the_check_stays_open(self):
        """A blanket act clears through the tails its check read, not the tail since."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list")
        real = stop_drain.unseen

        def unseen_then_land(cursor, actor, view):
            found = real(cursor, actor, view)
            self.write_log("inbox", [MAIL % "bbb"])               # between the check and the advance
            return found

        with mock.patch.object(stop_drain, "unseen", unseen_then_land):
            rc, out, err = self.cli()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acted("inbox"), 1, "bbb was never checked: it must stay open")
        cleared, _, still_open = out.partition("unactioned event(s)")
        self.assertIn("acted: 1 event(s) cleared", cleared)
        self.assertNotIn("message_id=bbb", cleared)
        self.assertIn("message_id=bbb", still_open)


class _PipeWhoseReaderHasGone(io.StringIO):
    """stdout into a pipe nobody reads any more: writes land in the buffer, as a
    pipe's block-buffered stdout takes them, and only the flush fails."""

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


class TestCodexOnF58b31c(Base):
    """codex's review of f58b31c: two ways the new safeguards could still authorise
    clearing events nobody saw — a log replaced between an act's check and its
    advance (P2-1), and a listing recorded as shown that never reached a reader (P2-2)."""

    def _rotate_during_act(self, *argv):
        """An act whose inbox log is replaced right after the act read it."""
        real = stop_drain.read_view

        def read_then_rotate(*a, **kw):
            view = real(*a, **kw)
            # Same size, same count — two events nobody has seen (codex's example).
            self.write_log("inbox", [MAIL % "yyy", MAIL % "zzz"], mode="w")
            return view

        with mock.patch.object(stop_drain, "read_view", read_then_rotate):
            return self.cli(*argv)

    def open_texts(self, source):
        still = stop_drain.report_unactioned(self.logs, self.state, self.profile)
        return [e.text for e in still.get(source, [])]

    def test_a_blanket_act_clears_nothing_from_a_log_replaced_after_its_check(self):
        """P2-1: the checked positions must not land on the replacement's events."""
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        self.write_log("github", [PR % 7])
        self.cli("--list")
        rc, out, err = self._rotate_during_act()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("replaced", err)
        self.assertEqual(self.open_texts("inbox"), [MAIL % "yyy", MAIL % "zzz"],
                         "never checked, so both stay open")
        self.assertNotIn("message_id=aaa", out, "nothing of the checked log was cleared or claimed so")
        self.assertEqual(self.acted("github"), 1, "a log that was not replaced still clears")

    def test_the_override_clears_nothing_from_a_log_replaced_after_it_read_it(self):
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        rc, out, err = self._rotate_during_act("--all-including-unseen")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("replaced", err)
        self.assertEqual(self.open_texts("inbox"), [MAIL % "yyy", MAIL % "zzz"])

    def test_an_explicit_act_clears_nothing_from_a_log_replaced_after_its_check(self):
        """A review found: `--through N` is bound to its log's generation as the blanket
        path is. A log replaced after the read that validated N is refused — nothing cleared
        from it, and said so; a log that was not replaced clears through N as before."""
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        self.write_log("github", [PR % 7, PR % 8])
        rc, out, err = self._rotate_during_act("--through", "1")          # every source, N=1
        self.assertEqual(rc, 2, out + err)
        self.assertIn("replaced", err)
        self.assertEqual(self.open_texts("inbox"), [MAIL % "yyy", MAIL % "zzz"],
                         "N was validated on the old log: nothing of the new one is cleared")
        self.assertNotIn("message_id=aaa", out, "nor is the old log's event claimed as cleared")
        self.assertEqual(self.acted("github"), 1, "a log that was not replaced clears through N")

    def test_an_explicit_number_is_bound_to_the_very_read_that_validated_it(self):
        """The overshoot check validates N against a read; the advance must be bound to that
        SAME read. A log replaced right after the act's first read is refused, not re-read
        and cleared through a number that was checked against the old one."""
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        real, calls = stop_drain.read_source, []

        def first_read_then_rotate(*a, **kw):
            got = real(*a, **kw)
            calls.append(1)
            if len(calls) == 1:
                self.write_log("inbox", [MAIL % "yyy", MAIL % "zzz"], mode="w")
            return got

        with mock.patch.object(stop_drain, "read_source", first_read_then_rotate):
            rc, out, err = self.cli("--source", "inbox", "--through", "2")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("replaced", err)
        self.assertEqual(self.open_texts("inbox"), [MAIL % "yyy", MAIL % "zzz"])

    def test_a_listing_whose_flush_fails_is_not_recorded(self):
        """P2-2: a listing counts as shown only once it has left the process."""
        self.write_log("inbox", [MAIL % "aaa"])
        err = io.StringIO()
        with mock.patch.object(stop_drain, "actor_identity",
                               lambda payload=None, environ=None: session(ME)):
            with redirect_stdout(_PipeWhoseReaderHasGone()), redirect_stderr(err):
                rc = wt_acted.main(["--profile", self.profile, "--log-dir", self.logs,
                                    "--state-root", self.state, "--list"])
        self.assertEqual(rc, 2)
        self.assertIn("listing NOT recorded", err.getvalue())
        self.assertNotIn(stop_drain.LISTED_KEY, self.cursor())
        self.assertEqual(self.cli()[0], 2, "an undelivered listing lets nothing through")

    def test_a_listing_into_a_closed_pipe_is_not_recorded(self):
        """P2-2 as codex reproduced it: a real pipe whose reader closed before any output."""
        self.write_log("inbox", [MAIL % "aaa"])
        r, w = os.pipe()
        os.close(r)
        env = dict(os.environ, HOME=self.tmp.name, CLAUDE_CODE_SESSION_ID=ME)
        for name in ("CLAUDE_PID", "WATCHTOWER_PROFILE"):
            env.pop(name, None)
        try:
            proc = subprocess.run([sys.executable, WT_ACTED, "--profile", self.profile,
                                   "--log-dir", self.logs, "--state-root", self.state, "--list"],
                                  stdout=w, stderr=subprocess.PIPE, env=env, timeout=60)
        finally:
            os.close(w)
        err = proc.stderr.decode("utf-8", "replace")
        self.assertNotIn(stop_drain.LISTED_KEY, self.cursor(), err)
        self.assertEqual(proc.returncode, 2, err)
        self.assertIn("listing NOT recorded", err)
        self.assertEqual(self.cli()[0], 2, "nothing reached a reader, so nothing may be cleared")

    def test_a_listing_with_stdout_closed_is_not_recorded(self):
        """P2-2's purest form: stdout closed before start (`>&-`). Python sets it to None
        and print() drops the listing SILENTLY — under f58b31c it was recorded as shown."""
        self.write_log("inbox", [MAIL % "aaa"])
        env = dict(os.environ, HOME=self.tmp.name, CLAUDE_CODE_SESSION_ID=ME)
        for name in ("CLAUDE_PID", "WATCHTOWER_PROFILE"):
            env.pop(name, None)
        proc = subprocess.run(["/bin/sh", "-c", 'exec "$0" "$@" >&-', sys.executable, WT_ACTED,
                               "--profile", self.profile, "--log-dir", self.logs,
                               "--state-root", self.state, "--list"],
                              stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, timeout=60)
        err = proc.stderr.decode("utf-8", "replace")
        self.assertNotIn(stop_drain.LISTED_KEY, self.cursor(), err)
        self.assertEqual(proc.returncode, 2, err)
        self.assertIn("listing NOT recorded", err)
        self.assertNotIn("Traceback", err)


class TestBlanketActReview(Base):
    """Review findings on the blanket-act refusal: notes 1, 2, 3 and 5."""

    def _listed_two_then_two_more(self):
        """Its scratch run: the session lists inbox m1 and github pr 7, then m2 and pr 8 land."""
        self.write_log("inbox", [MAIL % "m1"])
        self.write_log("github", [PR % 7])
        self.cli("--list")
        self.write_log("inbox", [MAIL % "m2"])
        self.write_log("github", [PR % 8])

    def test_a_refused_scoped_clear_keeps_the_source_in_every_suggestion(self):
        """Note 1: the advice for a refused `just acted <p> inbox` named all-source commands."""
        self._listed_two_then_two_more()
        rc, _, err = self.cli("--source", "inbox")
        self.assertEqual(rc, 2)
        self.assertIn("[inbox #2]", err)
        self.assertNotIn("[github", err, "only the act's own source is checked and named")
        self.assertIn("`just acted acme inbox 2`", err)                  # by number, exact
        self.assertIn("`just acted acme inbox`", err)                    # the scoped re-run
        self.assertIn("`just acted-including-unseen acme inbox`", err)   # the scoped override
        self.assertNotIn("`just acted acme`", err)
        self.assertNotIn("`just acted-including-unseen acme`", err)
        self.assertIn("THEN, as a separate command", err)                      # note 2's wording

    def test_following_the_advised_override_leaves_the_other_source_open(self):
        """Note 1's failure, pinned: the override exactly as advised clears only inbox."""
        self._listed_two_then_two_more()
        _, _, err = self.cli("--source", "inbox")
        advised = re.search(r"`just acted-including-unseen acme(?: (\w+))?`", err)
        self.assertEqual(advised.group(1), "inbox", err)
        rc, _, err = self.cli("--all-including-unseen", "--source", advised.group(1))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.acted("inbox"), self.acted("github")), (2, 0),
                         "github pr 8, shown to nobody, stays open")

    def test_an_unscoped_refusal_suggests_unscoped_commands_and_every_sources_clear(self):
        self._listed_two_then_two_more()
        rc, _, err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("`just acted acme github 2`", err)
        self.assertIn("`just acted acme inbox 2`", err)
        self.assertIn("`just acted acme`", err)
        self.assertIn("`just acted-including-unseen acme`", err)

    def test_the_refusal_says_whose_listing_it_was_when_not_this_sessions(self):
        """Note 5: only the latest listing is kept, so an operator's voids the session's."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.cli("--list")
        self.cli("--list", actor=OPERATOR)
        rc, _, err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("the last listing was by operator alice@/dev/ttys003", err)
        self.assertNotIn("this session has not listed", err)

    def test_the_listing_ends_with_what_it_showed_and_the_exact_clears(self):
        """Notes 2 and 3: the count and the by-number clears are the LAST lines, so a
        cut-down view (`| tail`) still shows them."""
        self.write_log("inbox", [MAIL % "aaa", MAIL % "bbb"])
        self.write_log("github", [PR % 7])
        rc, out, _ = self.cli("--list")
        self.assertEqual(rc, 0)
        self.assertEqual(out.rstrip("\n").splitlines()[-4:], [
            "shown: 3 event(s) — github #1; inbox #1–#2.",
            "Handle each one; THEN, as a separate command, clear exactly these by number:",
            "  just acted acme github 1",
            "  just acted acme inbox 2",
        ])

    def test_the_listings_clears_leave_a_mid_turn_arrival_open(self):
        """Note 2's chained case, done the way the listing now says: nothing unread goes."""
        self.write_log("inbox", [MAIL % "aaa"])
        _, out, _ = self.cli("--list")
        self.write_log("inbox", [MAIL % "bbb"])                    # lands before the clear
        clears = [ln.split() for ln in out.splitlines() if ln.startswith("  just acted ")]
        self.assertEqual(clears, [["just", "acted", "acme", "inbox", "1"]])
        rc, _, err = self.cli("--source", clears[0][3], "--through", clears[0][4])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acted("inbox"), 1, "the arrival no listing showed stays open")

    def test_a_foreign_sessions_listing_offers_no_clears(self):
        """The seat refuses a foreign session's act, so its listing suggests none."""
        self.write_log("inbox", [MAIL % "aaa"])
        self.assertEqual(self.drain().get("decision"), "block")    # this session owns the seat
        rc, out, _ = self.cli("--list", actor=session(OTHER, pid=22))
        self.assertEqual(rc, 0)
        self.assertIn("shown: 1 event(s) — inbox #1.", out)
        self.assertNotIn("just acted", out.partition("shown:")[2])

    def test_a_clean_listing_has_no_footer(self):
        rc, out, _ = self.cli("--list")
        self.assertEqual(rc, 0)
        self.assertNotIn("shown:", out)


if __name__ == "__main__":
    unittest.main()
