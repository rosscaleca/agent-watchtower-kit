#!/usr/bin/env python3
"""Session roots: sessions from OTHER projects must not drain the seat's queue.

On the owner's Mac the Stop hook is registered in the project AND globally, and they
keep both, so every Claude Code session on the host runs it. While the seat's own session
is dead, the first unrelated session to end a turn claims the cursor, then surfaces and
clears the seat's events: a side conversation in another project did exactly that on
2026-09-19. Session roots are the opt-in, per-host fix. These pin it:

- no setting: the hook is unchanged;
- a session inside a root (subdirectories included; symlinks and letter case resolved by
  file identity): unchanged;
- a session outside every root: the hook does NOTHING — no block, no claim (not even over
  a dead owner), no contender record, no write of any kind;
- where a session is: the payload's ``cwd`` (the process cwd when it is absent) OR its
  project root, ``CLAUDE_PROJECT_DIR``. ``cwd`` follows ``cd``, and the seat must not go
  silent because it ended a turn inside another repo;
- an unreadable setting fails OPEN, and says so;
- the CLI (``just acted`` / ``unactioned`` / ``takeover``) is unaffected.

Temp trees only: the live defaults are pointed at them, and the actor, liveness and
process identity are injected, never read from this process.
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
import wt_acted  # noqa: E402

MAIL = "type=mail_received inbox=claude-seat auth=pass message_id=%s ts=2026-09-19T15:00:00Z"
ME = "7e4151da-0000-4000-8000-000000000001"       # the seat's session
OTHER = "3b2c9d1e-0000-4000-8000-000000000002"    # a session in another project
THIRD = "5d6e7f80-0000-4000-8000-000000000003"    # the seat's replacement session


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        top = os.path.realpath(self.tmp.name)     # macOS: /var/folders is /private/var/folders
        self.logs = os.path.join(top, "logs")
        self.state = os.path.join(top, "state")
        self.seat = os.path.join(top, "Acme")
        self.other = os.path.join(top, "Vector")
        for d in (self.logs, self.state, self.seat, self.other):
            os.makedirs(d)
        self.top = top
        self.profile = "acme"
        self.alive = {11: True, 22: True, 33: True}
        patches = {
            "DEFAULT_LOG_DIR": self.logs, "DEFAULT_STATE_ROOT": self.state, "LOCK_WAIT": 0.2,
            "pid_alive": lambda pid: self.alive.get(pid),
            "boot_identity": lambda: "boot-A",
            "process_start": lambda pid: None,
        }
        for name, value in patches.items():
            p = mock.patch.object(stop_drain, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {})     # restored whole on stop, removals included
        env.start()
        self.addCleanup(env.stop)
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_PROJECT_DIR",
                     "WATCHTOWER_PROFILE"):
            os.environ.pop(name, None)
        self.addCleanup(os.chdir, os.getcwd())

    def write_log(self, source, lines):
        with open(os.path.join(self.logs, "%s-%s.log" % (self.profile, source)), "a") as fh:
            for line in lines:
                fh.write(line + "\n")

    def set_roots_file(self, content):
        """Write the setting as-is (a list is written as a valid setting) — no CLI involved."""
        path = os.path.join(self.state, self.profile, "session-roots.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content if isinstance(content, str) else json.dumps({"version": 1, "roots": content}))

    def hook(self, sid=ME, pid=11, cwd=None, project=None, event="Stop"):
        """One Stop-hook run through ``run_hook``, as Claude Code invokes it."""
        payload = {"session_id": sid}
        if event:
            payload["hook_event_name"] = event
        if cwd is not None:
            payload["cwd"] = cwd
        env = {"CLAUDE_PID": str(pid)}
        if project:
            env["CLAUDE_PROJECT_DIR"] = project
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stderr(err):
            out = stop_drain.run_hook(payload, self.logs, self.state, self.profile)
        return (json.loads(out) if out else {}), err.getvalue()

    def served_inside(self, **kw):
        """Served AS INSIDE: a block, and no fail-open note. An unknown verdict serves the
        session too, so a bare block cannot tell inside from failing open."""
        decision, err = self.hook(**kw)
        self.assertEqual((decision.get("decision"), err), ("block", ""))
        return decision

    def files(self):
        """Every file in the profile's state dir, with its bytes."""
        d = os.path.join(self.state, self.profile)
        out = {}
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, name), "rb") as fh:
                out[name] = fh.read()
        return out

    def cursor(self):
        return stop_drain.load_cursor(self.state, self.profile)


class TestHookWithoutAndInsideRoots(Base):
    def test_no_setting_leaves_the_hook_unchanged(self):
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(sid=OTHER, pid=22, cwd=self.other)
        self.assertEqual(decision.get("decision"), "block")
        self.assertEqual(err, "")
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER, "no roots: anyone claims, as today")

    def test_a_session_inside_a_root_is_unchanged(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(cwd=self.seat)
        self.assertEqual(decision.get("decision"), "block")
        self.assertEqual(decision["reason"], stop_drain.format_reason(
            {"inbox": stop_drain.parse_events(MAIL % "a")}, self.profile), "block text untouched")
        self.assertEqual(err, "")
        c = self.cursor()
        self.assertEqual((c["owner"]["session_id"], c["sources"]["inbox"]["surfaced_through"]), (ME, 1))

    def test_a_subdirectory_of_a_root_counts_as_inside(self):
        self.set_roots_file([self.seat])
        deep = os.path.join(self.seat, "sub", "deeper")
        os.makedirs(deep)
        self.write_log("inbox", [MAIL % "a"])
        self.served_inside(cwd=deep)

    def test_a_root_given_through_a_symlink_resolves(self):
        """Resolved AND enforced — an unusable root would fail open, and serve the seat too."""
        link = os.path.join(self.top, "link-to-seat")
        os.symlink(self.seat, link)
        self.set_roots_file([link])                                   # as hand-written, unresolved
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other), ({}, ""), "enforced")
        decision, err = self.hook(cwd=self.seat)
        self.assertEqual((decision.get("decision"), err), ("block", ""), "served, not failing open")

    def test_a_session_working_through_a_symlink_resolves(self):
        link = os.path.join(self.top, "link-to-seat")
        os.symlink(self.seat, link)
        os.makedirs(os.path.join(self.seat, "sub"))
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        self.served_inside(cwd=os.path.join(link, "sub"))

    def test_letter_case_cannot_make_the_seat_foreign(self):
        """A case-insensitive filesystem (macOS by default) takes any spelling of a path, and
        realpath keeps the one typed: identity, not the string, decides."""
        variant = os.path.join(self.top, "acme")
        if not os.path.isdir(variant):
            self.skipTest("case-sensitive filesystem")
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        self.served_inside(cwd=variant)


class TestHookOutsideRoots(Base):
    def test_a_session_outside_every_root_does_nothing_and_writes_nothing(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        before = self.files()
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other), ({}, ""))
        self.assertEqual(self.files(), before, "no lock, cursor or contenders file — no write at all")

    def test_a_hand_run_probe_from_outside_reports_says_so_and_writes_nothing(self):
        """A review, note 3: silence from a hand-run probe read exactly like "nothing
        pending", and gating it bought no safety, since a probe writes nothing."""
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        before = self.files()
        decision, err = self.hook(sid=OTHER, pid=22, cwd=self.other, event=None)
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("message_id=a", decision["reason"])
        self.assertIn("outside the session roots of profile acme", err)
        self.assertIn("at a real turn end its hook does nothing", decision["reason"])
        self.assertEqual(self.files(), before, "a probe writes nothing, outside or in")
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other), ({}, ""),
                         "a real turn end from outside is still silent")

    def test_an_outside_session_cannot_claim_even_when_the_owner_is_dead(self):
        """The 09-19 capture: the seat's session is dead and another project's session ends a turn."""
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        self.served_inside(cwd=self.seat)                                        # the seat owns it
        self.alive[11] = False                                                   # ...and dies
        self.write_log("inbox", [MAIL % "b"])
        before = self.files()
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other), ({}, ""))
        self.assertEqual(self.files(), before, "no claim, no surfacing, no contender record")
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)
        self.served_inside(sid=THIRD, pid=33, cwd=self.seat)                    # the seat comes back
        self.assertEqual(self.cursor()["owner"]["session_id"], THIRD, "an inside session still claims")

    def test_an_outside_session_leaves_no_contender_record_under_a_live_owner(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        self.hook(cwd=self.seat)
        self.hook(sid=OTHER, pid=22, cwd=self.other)
        self.assertNotIn("contenders.json", self.files())

    def test_a_missing_cwd_falls_back_to_the_process_cwd(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        os.chdir(self.other)
        self.assertEqual(self.hook(sid=OTHER, pid=22), ({}, ""), "no cwd in stdin: the process cwd decides")
        os.chdir(self.seat)
        self.served_inside()

    def test_the_project_root_keeps_a_session_that_cd_d_away_inside(self):
        """Claude Code's payload `cwd` follows `cd`; CLAUDE_PROJECT_DIR is where the session
        started. The seat ending a turn inside another repo must not silence its drain."""
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other, project=self.other), ({}, ""))
        self.served_inside(cwd=self.other, project=self.seat)

    def test_the_entry_point_prints_nothing_and_exits_0_for_an_outside_session(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        payload = json.dumps({"hook_event_name": "Stop", "session_id": OTHER, "cwd": self.other})
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(out):
            rc = stop_drain.main(["--profile", self.profile, "--log-dir", self.logs,
                                  "--state-root", self.state])
        self.assertEqual((rc, out.getvalue()), (0, ""))


class TestAnUnreadableSettingFailsOpen(Base):
    BAD = {
        "not json": "{not json",
        "not an object": "[]",
        "no roots key": json.dumps({"version": 1}),
        "roots not a list": json.dumps({"roots": "/x"}),
        "empty roots": json.dumps({"roots": []}),
        "a relative root": json.dumps({"roots": ["relative/path"]}),
        "no root exists": json.dumps({"roots": ["/nonexistent/wt/roots/a"]}),
        # codex on caca76c: strings no filesystem call accepts (stat raises ValueError).
        "a NUL in a root": json.dumps({"roots": ["/tmp/\x00"]}),
        "an unencodable root": json.dumps({"roots": ["/tmp/\ud800"]}),
        "an overlong root": json.dumps({"roots": ["/" + "x" * 300]}),
    }

    def test_every_unusable_setting_fails_open_to_todays_behaviour_and_says_so(self):
        """A corrupt file must never silence the seat — nor, while it stands, anyone else."""
        for label, content in sorted(self.BAD.items()):
            with self.subTest(setting=label):
                for name in os.listdir(os.path.join(self.state)):
                    for f in os.listdir(os.path.join(self.state, name)):
                        os.remove(os.path.join(self.state, name, f))
                self.set_roots_file(content)
                self.write_log("inbox", [MAIL % label.replace(" ", "-")])
                decision, err = self.hook(sid=OTHER, pid=22, cwd=self.other)
                self.assertEqual(decision.get("decision"), "block", "fails open: drains as today")
                self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)
                self.assertIn("session-roots.json", err, "the reason goes to stderr (Claude Code's hook log)")
                self.assertIn("restriction is OFF", decision["reason"], "and to the session it blocks")
                # A review, note 4: it can reach another project's agent, so it
                # asks for the operator rather than inviting the reader to fix it.
                self.assertIn("Tell the operator; do not change it from this session", err)

    def test_a_dangling_symlinked_setting_fails_open_and_says_so(self):
        """A review, decision 3: unreadable, not "missing", so the note appears."""
        path = os.path.join(self.state, self.profile, "session-roots.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(os.path.join(self.top, "gone.json"), path)
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(sid=OTHER, pid=22, cwd=self.other)
        self.assertEqual(decision.get("decision"), "block", "fails open")
        self.assertIn("restriction is OFF", err)

    def test_a_root_that_is_a_regular_file_fails_open(self):
        """codex on d12809a: a file stats fine, yet no session's directory can ever be it —
        counting it enforced a restriction nothing could satisfy, silencing everyone."""
        afile = os.path.join(self.top, "not-a-dir")
        open(afile, "w").close()
        self.set_roots_file([afile])
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(sid=OTHER, pid=22, cwd=self.other)
        self.assertEqual(decision.get("decision"), "block", "no usable root: fail open")
        self.assertIn("session-roots.json", err)
        self.assertIn("restriction is OFF", decision["reason"])

    def test_a_nul_root_beside_a_good_one_is_a_malformed_setting(self):
        """Pinned: a root that is not a path at all is malformed CONTENT, like a relative
        root — the whole setting is ignored (fail open) and says so, as `show` reports."""
        self.set_roots_file([self.seat, "/tmp/\x00"])
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(sid=OTHER, pid=22, cwd=self.other)
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("session-roots.json", err)
        self.assertIn("restriction is OFF", decision["reason"])

    def test_resolving_roots_survives_strings_no_filesystem_call_accepts(self):
        """Behind the loader, the resolution itself must not raise: an unusable root is
        skipped like a missing one (codex on caca76c: ValueError reached main's handler)."""
        seat = os.stat(self.seat)
        got = stop_drain._root_ids(["/tmp/\x00", "/tmp/\ud800", "/" + "x" * 300, self.seat])
        self.assertEqual(got, {(seat.st_dev, seat.st_ino)})

    def test_a_directory_root_still_enforces_beside_a_file_root(self):
        """Pinned: an unusable root is ignored, not a reason to drop the usable ones."""
        afile = os.path.join(self.top, "not-a-dir")
        open(afile, "w").close()
        self.set_roots_file([self.seat, afile])
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other), ({}, ""))
        self.served_inside(cwd=self.seat)


class TestASessionPlaceThatCannotBeResolved(Base):
    """codex on caca76c: an odd session cwd raised out of the hook and silenced the drain.
    A place this OS cannot resolve is UNKNOWN — never "outside" — so that session fails
    open, and says so; a usable place beside it still decides."""

    ODD = {"a NUL": "/tmp/\x00x", "an unencodable name": "/tmp/\ud800", "an overlong name": "/" + "x" * 300}

    def test_an_odd_cwd_fails_open_for_that_session_and_says_so(self):
        self.set_roots_file([self.seat])
        for label, cwd in sorted(self.ODD.items()):
            with self.subTest(cwd=label):
                for f in os.listdir(os.path.join(self.state, self.profile)):
                    if f != "session-roots.json":
                        os.remove(os.path.join(self.state, self.profile, f))
                self.write_log("inbox", [MAIL % label.replace(" ", "-")])
                decision, err = self.hook(sid=OTHER, pid=22, cwd=cwd)
                self.assertEqual(decision.get("decision"), "block", "served: its place is unknown")
                self.assertIn("restriction is OFF for this session", decision["reason"])
                self.assertIn("restriction is OFF for this session", err)

    def test_a_usable_project_root_still_decides_beside_an_odd_cwd(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(cwd="/tmp/\x00x", project=self.seat)
        self.assertEqual((decision.get("decision"), err), ("block", ""), "inside by its project root")

    def test_an_odd_cwd_is_never_outside_even_beside_an_outside_project_root(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(sid=OTHER, pid=22, cwd="/tmp/\x00x", project=self.other)
        self.assertEqual(decision.get("decision"), "block", "unknown fails open, whatever else is known")
        self.assertIn("restriction is OFF for this session", err)

    def test_the_entry_point_survives_a_nul_cwd(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        payload = json.dumps({"hook_event_name": "Stop", "session_id": OTHER, "cwd": "/tmp/\x00x"})
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(out), \
                redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, {"CLAUDE_PID": "22"}):
            rc = stop_drain.main(["--profile", self.profile, "--log-dir", self.logs,
                                  "--state-root", self.state])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()).get("decision"), "block", "not silenced by a crash")


@contextlib.contextmanager
def locked(testcase, path):
    """``chmod 000`` for the duration, restored in a finally; skipped where it is not enforced."""
    os.chmod(path, 0)
    try:
        try:
            os.listdir(path)
        except OSError:
            pass
        else:
            testcase.skipTest("permissions are not enforced for this user")
        yield
    finally:
        os.chmod(path, 0o755)


class TestAnErrorIsNeverOutside(Base):
    """codex on 3fc801b: an access error on the way to a session's place walked the alias's
    LEXICAL parents and answered "outside", silencing the seat. "Outside" silences, so it
    must be a definite answer: every component examined, no root matched. Any refusal, a
    symlink left unresolved, or a root that cannot be examined makes it unknown instead —
    served, with the note — however "outside" the truth may be."""

    def locked_dir(self):
        d = os.path.join(self.top, "locked")
        os.makedirs(os.path.join(d, "inner"))
        return d

    def test_a_symlink_under_an_inaccessible_parent_that_targets_a_root_is_unknown(self):
        """codex's reproduction: non-strict realpath keeps the alias it cannot examine."""
        self.set_roots_file([self.seat])
        os.makedirs(os.path.join(self.seat, "sub"))
        d = self.locked_dir()
        os.symlink(self.seat, os.path.join(d, "link"))
        self.write_log("inbox", [MAIL % "a"])
        with locked(self, d):
            decision, err = self.hook(cwd=os.path.join(d, "link", "sub"))
        self.assertEqual(decision.get("decision"), "block", "never silenced by an access error")
        self.assertIn("restriction is OFF for this session", err)

    def test_a_directory_under_an_inaccessible_parent_is_unknown_even_when_truly_outside(self):
        self.set_roots_file([self.seat])
        d = self.locked_dir()
        self.write_log("inbox", [MAIL % "a"])
        with locked(self, d):
            decision, err = self.hook(sid=OTHER, pid=22, cwd=os.path.join(d, "inner"))
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("restriction is OFF for this session", err)

    def test_a_symlink_loop_is_unknown_not_outside(self):
        """realpath leaves a loop unresolved: a symlink still in the path is not a place."""
        self.set_roots_file([self.seat])
        a, b = os.path.join(self.top, "loopA"), os.path.join(self.top, "loopB")
        os.symlink(b, a)
        os.symlink(a, b)
        self.write_log("inbox", [MAIL % "a"])
        decision, err = self.hook(sid=OTHER, pid=22, cwd=a)
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("restriction is OFF for this session", err)

    def test_a_genuine_outside_path_is_still_outside(self):
        """Examined cleanly — including a leaf deleted since (a clean ENOENT) — and no root."""
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other), ({}, ""))
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=os.path.join(self.other, "gone")), ({}, ""))

    def test_a_root_that_cannot_be_examined_makes_outside_unknown(self):
        """The sweep: a refused root was skipped like a missing one, so a session living in it
        was "outside" the remaining roots — and silenced. It might be inside: unknown."""
        second = os.path.join(self.top, "Second")
        os.makedirs(second)
        d = self.locked_dir()
        os.symlink(self.seat, os.path.join(d, "link"))
        self.set_roots_file([os.path.join(d, "link"), second])
        self.write_log("inbox", [MAIL % "a"])
        with locked(self, d):
            decision, err = self.hook(cwd=self.seat)
        self.assertEqual(decision.get("decision"), "block", "the seat, inside the refused root")
        self.assertIn("restriction is OFF for this session", err)
        self.assertIn("cannot be examined", err)

    def test_an_unexpected_error_deciding_never_silences_the_drain(self):
        """The sweep: any exception in the gate reached main's blanket handler, and that is
        silence. However deciding fails, it must serve the session and say why."""
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        with mock.patch.object(stop_drain, "session_verdict", side_effect=RuntimeError("boom")):
            decision, err = self.hook(sid=OTHER, pid=22, cwd=self.other)
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("restriction is OFF", err)
        self.assertIn("RuntimeError", err)

    def test_a_definitely_missing_root_beside_a_usable_one_keeps_outside_definite(self):
        self.set_roots_file([os.path.join(self.top, "gone"), self.seat])
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(self.hook(sid=OTHER, pid=22, cwd=self.other), ({}, ""))
        self.served_inside(cwd=self.seat)


class TestTheCliIsUnaffected(Base):
    def cli(self, actor, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(stop_drain, "actor_identity", lambda payload=None, environ=None: actor):
            with redirect_stdout(out), redirect_stderr(err):
                rc = wt_acted.main(["--profile", self.profile, "--log-dir", self.logs,
                                    "--state-root", self.state] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_acted_unactioned_and_takeover_ignore_roots(self):
        self.set_roots_file([self.seat])
        self.write_log("inbox", [MAIL % "a"])
        outside = {"kind": "session", "session_id": OTHER, "pid": 22, "cwd": self.other}
        rc, out, _ = self.cli(outside, "--list")
        self.assertEqual(rc, 0)
        self.assertIn("message_id=a", out)
        self.assertEqual(self.cli(outside, "--take-over")[0], 0)
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER, "a deliberate take-over still works")
        self.assertEqual(self.cli(outside, "--source", "inbox", "--through", "1")[0], 0)
        self.assertEqual(self.cursor()["sources"]["inbox"]["acted_through"], 1)


if __name__ == "__main__":
    unittest.main()
