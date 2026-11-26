#!/usr/bin/env python3
"""``wt_roots.py`` / ``just session-roots``: show, set and clear a
profile's session roots (see ``test_session_roots.py`` for what the hook does with them).

These pin that setting is safe to get wrong loudly rather than quietly: a root must be an
existing directory, roots are stored realpath-normalized and de-duplicated, an unreadable
setting is reported as IGNORED (the hook fails open), and setting roots that leave the
cursor's owner outside says so. Temp trees only; the live defaults point at them.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import stop_drain  # noqa: E402
import wt_roots  # noqa: E402

MAIL = "type=mail_received inbox=claude-seat auth=pass message_id=%s ts=2026-09-19T15:00:00Z"
OPERATOR = {"kind": "operator", "user": "alice", "tty": "/dev/ttys003", "cwd": "/w"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        top = os.path.realpath(self.tmp.name)
        self.top = top
        self.logs, self.state = os.path.join(top, "logs"), os.path.join(top, "state")
        self.seat, self.other = os.path.join(top, "Acme"), os.path.join(top, "Vector")
        for d in (self.logs, self.state, self.seat, self.other):
            os.makedirs(d)
        self.profile = "acme"
        for name, value in {"DEFAULT_LOG_DIR": self.logs, "DEFAULT_STATE_ROOT": self.state,
                            "LOCK_WAIT": 0.2, "pid_alive": lambda pid: True,
                            "boot_identity": lambda: "boot-A",
                            "process_start": lambda pid: None}.items():
            p = mock.patch.object(stop_drain, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"HOME": top})
        env.start()
        self.addCleanup(env.stop)
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_PROJECT_DIR", "WATCHTOWER_PROFILE"):
            os.environ.pop(name, None)

    def roots(self, *argv, **kw):
        actor = kw.get("actor") or OPERATOR
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(stop_drain, "actor_identity", lambda payload=None, environ=None: actor):
            with redirect_stdout(out), redirect_stderr(err):
                rc = wt_roots.main(["--profile", self.profile, "--state-root", self.state] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def stored(self):
        path = stop_drain.roots_path(self.state, self.profile)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return json.load(fh)

    def hook_from(self, cwd, sid="3b2c9d1e-0000-4000-8000-000000000002"):
        with mock.patch.dict(os.environ, {"CLAUDE_PID": "22"}):
            out = stop_drain.run_hook({"hook_event_name": "Stop", "session_id": sid, "cwd": cwd},
                                      self.logs, self.state, self.profile)
        return json.loads(out) if out else {}

    def write_log(self, lines):
        with open(os.path.join(self.logs, "%s-inbox.log" % self.profile), "a") as fh:
            for line in lines:
                fh.write(line + "\n")


class TestTheRootsCommand(Base):
    def test_show_when_unset_says_every_session_is_served(self):
        rc, out, _ = self.roots()
        self.assertEqual(rc, 0)
        self.assertIn("not set", out)
        self.assertIsNone(self.stored(), "showing writes nothing")

    def test_set_stores_realpaths_deduplicated(self):
        link = os.path.join(self.top, "link-to-seat")
        os.symlink(self.seat, link)
        rc, out, err = self.roots(link, self.seat + os.sep, "~/Acme", self.other)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.stored()["roots"], [self.seat, self.other],
                         "symlink, trailing slash and ~ all resolve to one stored realpath")
        self.assertEqual(self.stored()["by"]["kind"], "operator")
        self.assertIn(self.seat, out)

    def test_set_refuses_what_is_not_an_existing_directory_and_changes_nothing(self):
        afile = os.path.join(self.top, "a-file")
        open(afile, "w").close()
        for bad in (os.path.join(self.top, "no-such-dir"), afile):
            with self.subTest(root=bad):
                rc, _, err = self.roots(self.seat, bad)
                self.assertEqual(rc, 2)
                self.assertIn("Nothing changed", err)
                self.assertIsNone(self.stored())

    def test_setting_and_clearing_is_what_the_hook_follows(self):
        self.write_log([MAIL % "a"])
        self.assertEqual(self.roots(self.seat)[0], 0)
        self.assertEqual(self.hook_from(self.other), {}, "outside the roots: nothing")
        rc, out, _ = self.roots("--clear")
        self.assertEqual(rc, 0)
        self.assertIn("cleared", out)
        self.assertIsNone(self.stored())
        self.assertEqual(self.hook_from(self.other).get("decision"), "block", "cleared: served again")
        rc, out, _ = self.roots("--clear")
        self.assertEqual(rc, 0)
        self.assertIn("not set", out)

    def test_show_reports_an_unreadable_setting_as_ignored(self):
        path = stop_drain.roots_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("{not json")
        rc, out, _ = self.roots()
        self.assertEqual(rc, 0)
        self.assertIn("UNREADABLE", out)
        self.assertIn("fails open", out)

    def test_setting_roots_that_leave_the_owner_outside_says_so(self):
        cursor = {"version": 2, "sources": {}, "owner": {
            "session_id": "3b2c9d1e-0000-4000-8000-000000000002", "pid": 22, "cwd": self.other}}
        os.makedirs(os.path.join(self.state, self.profile))
        with open(stop_drain.cursor_path(self.state, self.profile), "w") as fh:
            json.dump(cursor, fh)
        rc, out, _ = self.roots(self.seat)
        self.assertEqual(rc, 0)
        self.assertIn("outside these roots", out)
        self.assertIn("just takeover %s" % self.profile, out)
        # A review found: the cwd is the owner's last ACTION, a hook run or a `just`
        # command (note 1), and the check never sees the owner's project root, so it must
        # not reassure that a seat cd'd elsewhere "is still served" (note 2).
        self.assertIn("last hook run or `just` command", out)
        self.assertNotIn("still served", out)
        # Second review, note 2: after any `just acted` from the checkout this fires for the
        # RIGHT roots too, so its advice must be safe then: never store the named directory
        # (that replaces the setting and silences the seat); confirm the next wake blocks.
        self.assertIn("Do not store that directory unless it is the seat's project root", out)
        self.assertIn("Confirm that the seat's next wake still blocks", out)
        self.assertNotIn("fix the roots", out)

    def test_clear_with_roots_is_refused(self):
        with self.assertRaises(SystemExit):
            self.roots("--clear", self.seat)
        self.assertIsNone(self.stored())

    def write_setting(self, roots):
        path = stop_drain.roots_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"version": 1, "roots": roots}, fh)

    def test_show_flags_the_roots_the_hook_ignores(self):
        """codex on d12809a: show and the hook must agree on which roots count."""
        afile, missing = os.path.join(self.top, "a-file"), os.path.join(self.top, "gone")
        open(afile, "w").close()
        self.write_setting([self.seat, afile, missing])
        _, out, _ = self.roots()
        self.assertIn("%s   (not a directory: ignored)" % afile, out)
        self.assertIn("%s   (missing: ignored)" % missing, out)
        self.assertNotIn("fails open", out, "a usable root remains, so the setting is enforced")
        self.write_setting([afile, missing])
        _, out, _ = self.roots()
        self.assertIn("fails open", out)

    def test_show_reports_a_root_that_is_not_a_path_as_unreadable(self):
        """codex on caca76c: show and the hook must agree — a NUL root is malformed content."""
        self.write_setting(["/tmp/\x00"])
        rc, out, _ = self.roots()
        self.assertEqual(rc, 0)
        self.assertIn("UNREADABLE", out)
        self.assertIn("fails open", out)

    def test_set_refuses_a_root_that_is_not_a_path_and_never_stores_one(self):
        """The sweep: Python 3.14's realpath raises for these where 3.9's passes them on."""
        for bad in ("/tmp/\x00x", "/tmp/\ud800"):
            with self.subTest(root=repr(bad)):
                rc, _, err = self.roots(self.seat, bad)
                self.assertEqual(rc, 2)
                self.assertIn("Nothing changed", err)
                self.assertIsNone(self.stored())

    def test_setting_roots_survives_an_owner_whose_recorded_cwd_is_not_a_path(self):
        """An odd recorded cwd is unknown, not "outside": no crash, and no false warning."""
        cursor = {"version": 2, "sources": {}, "owner": {
            "session_id": "3b2c9d1e-0000-4000-8000-000000000002", "pid": 22, "cwd": "/tmp/\x00x"}}
        os.makedirs(os.path.join(self.state, self.profile))
        with open(stop_drain.cursor_path(self.state, self.profile), "w") as fh:
            json.dump(cursor, fh)
        rc, out, _ = self.roots(self.seat)
        self.assertEqual(rc, 0)
        self.assertNotIn("outside these roots", out)

    def test_show_says_which_root_cannot_be_examined(self):
        """codex on 3fc801b / the sweep: a refused root is not "missing" — it may hold sessions."""
        d = os.path.join(self.top, "locked")
        os.makedirs(d)
        os.symlink(self.seat, os.path.join(d, "link"))
        self.write_setting([self.seat, os.path.join(d, "link")])
        os.chmod(d, 0)
        try:
            try:
                os.listdir(d)
            except OSError:
                pass
            else:
                self.skipTest("permissions are not enforced for this user")
            _, out, _ = self.roots()
        finally:
            os.chmod(d, 0o755)
        self.assertIn("%s   (cannot be examined" % os.path.join(d, "link"), out)
        self.assertIn("any session not inside the other roots is served", out)

    def test_overlapping_updates_never_corrupt_the_setting_and_the_last_completed_wins(self):
        """codex on d12809a: two overlapping `just session-roots` runs shared one temp file,
        so the second could write into the file the first had just published. Here the
        second runs in the middle of the first one's write, the tightest interleaving."""
        real_json, done, nested = wt_roots.json, [], []

        class Interleaved(object):
            load = staticmethod(real_json.load)

            @staticmethod
            def dump(obj, fh, **kw):
                text = real_json.dumps(obj, **kw)
                fh.write(text[:len(text) // 2])
                fh.flush()
                if not nested:                                   # the overlapping run
                    nested.append(1)
                    done.append(([self.other], self.roots(self.other)[0]))
                fh.write(text[len(text) // 2:])

        with mock.patch.object(wt_roots, "json", Interleaved):
            done.append(([self.seat], self.roots(self.seat)[0]))
        try:
            stored = self.stored()
        except ValueError as exc:
            self.fail("the published setting is corrupt: %s" % exc)
        succeeded = [roots for roots, rc in done if rc == 0]
        self.assertTrue(succeeded, done)
        self.assertEqual(stored["roots"], succeeded[-1],
                         "the setting is the last update that reported success: %r" % (done,))


SEAT_SESSION = {"kind": "session", "session_id": "7e4151da-0000-4000-8000-000000000001",
                "pid": 11, "cwd": "/w"}


class TestSessionRootsReview(Base):
    """Review findings: note 1, note 2 and decision 3."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())

    def test_a_relative_root_is_refused_wherever_it_would_resolve(self):
        """Note 1: `just` ran the command from the checkout, so `.` stored the checkout, a
        valid setting that left every session outside, the seat's included, and said nothing.
        Refused even when it names a real directory from here."""
        os.chdir(self.top)                                   # "Acme" exists from here
        for bad in (".", "Acme", "./Acme", "../x"):
            with self.subTest(root=bad):
                rc, _, err = self.roots(bad)
                self.assertEqual(rc, 2)
                self.assertIn("is a relative path", err)
                self.assertIn("Nothing changed", err)
                self.assertIsNone(self.stored())
        rc, _, err = self.roots(self.seat, "Acme")     # one bad root refuses the lot
        self.assertEqual((rc, self.stored()), (2, None), err)

    def test_the_recipe_runs_where_it_is_called(self):
        """Note 1, the other half: `[no-cd]`, so the warnings below see the caller's cwd."""
        justfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "justfile")
        with open(justfile) as fh:
            lines = fh.read().splitlines()
        at = next(i for i, ln in enumerate(lines) if ln.startswith("session-roots "))
        attributes = []
        for ln in reversed(lines[:at]):
            if not ln.startswith("["):
                break
            attributes.append(ln)
        self.assertIn("[no-cd]", attributes)

    def test_the_usual_path_gets_a_hedged_note_that_warns_off_storing_the_cwd(self):
        """Second review, note 1. A Bash tool has no CLAUDE_PROJECT_DIR, and `just` runs from the
        Watchtower checkout, so a seat setting the RIGHT root was told its hook "now does
        nothing, fix the roots now". Its hook still blocked, and the obvious "fix" (storing
        the directory it was in) silenced it. Here `self.other` stands in for the checkout."""
        os.chdir(self.other)
        rc, out, _ = self.roots(self.seat, actor=SEAT_SESSION)
        self.assertEqual(rc, 0)
        self.assertIn("It cannot see the session's project root", out)
        self.assertIn("Do not store this directory unless it is the seat's project root", out)
        self.assertIn("Confirm that the seat's next wake still blocks", out)
        self.assertNotIn("fix the roots now", out)
        self.assertNotIn("its own Stop hook now does nothing", out)

    def test_a_known_project_root_outside_the_roots_gets_the_definite_warning(self):
        """Note 2: with the project root known and outside too, the seat HAS silenced itself."""
        os.chdir(self.other)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.other}):
            rc, out, _ = self.roots(self.seat, actor=SEAT_SESSION)
        self.assertEqual(rc, 0)
        self.assertIn("this session is outside these roots", out)
        self.assertIn("If this session is the seat, fix the roots now", out)

    def test_a_session_inside_by_its_project_root_is_not_warned(self):
        os.chdir(self.other)                                 # cd'd away, but started in the seat
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.seat}):
            rc, out, _ = self.roots(self.seat, actor=SEAT_SESSION)
        self.assertEqual(rc, 0)
        self.assertNotIn("this session is outside", out)

    def test_the_operator_is_not_warned_about_its_own_directory(self):
        os.chdir(self.other)
        rc, out, _ = self.roots(self.seat)
        self.assertEqual(rc, 0)
        self.assertNotIn("this session is outside", out)

    def test_a_dangling_symlinked_setting_reads_unreadable_and_clears(self):
        """Decision 3: `lexists`, so show says UNREADABLE instead of "not set"."""
        path = stop_drain.roots_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path))
        os.symlink(os.path.join(self.top, "gone.json"), path)
        rc, out, _ = self.roots()
        self.assertEqual(rc, 0)
        self.assertIn("UNREADABLE", out)
        rc, out, _ = self.roots("--clear")
        self.assertEqual(rc, 0)
        self.assertIn("cleared", out)
        self.assertFalse(os.path.lexists(path))


if __name__ == "__main__":
    unittest.main()
