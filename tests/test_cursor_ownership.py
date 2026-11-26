#!/usr/bin/env python3
"""The cursor has an OWNER (a design decision: dead PID + take-over, no timer).

The first drain keyed the cursor by profile alone and recorded no actor, so
every process running the hook or ``wt_acted.py`` with ``--profile X`` WAS
the session as far as the file knew. Once, sessions from another
project surfaced this seat's events (consuming the one-shot block) and one
ran ``just acted`` over events nobody here had handled; nothing said who.
The docstring promised a "session-owned cursor" and the charter says one
session per profile — these pin that the code now means it:

- the owner drains and acts exactly as before (single-session behaviour is
  unchanged, including the block text);
- a FOREIGN live session's hook is silent AND non-mutating on the cursor,
  and is visible in ``contenders.json``;
- a foreign session's ``just acted`` is REFUSED, loudly, naming the owner;
- ownership is freed by a DEAD owner pid or ``--take-over`` — never a timer;
- the OPERATOR may act (attributed) but never owns, and a hook run by hand
  reports without consuming the surfacing;
- every advance is attributed (``by``), and a version-1 cursor still loads.

No network, no live files: everything runs under a temp state/log tree and
the actor is injected, never read from this process's real environment.

A blanket act clears only what a listing showed the actor (see
test_blind_clear.py): blanket acts here list first, and the two write-failure
tests that need NO cursor file use the deliberate override instead.
"""
import fcntl
import io
import json
import os
import subprocess
import sys
import threading
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import stop_drain  # noqa: E402
import wt_acted  # noqa: E402

MAIL = "type=mail_received inbox=claude-seat auth=pass message_id=%s ts=2026-09-09T15:00:00Z"
ME = "7e4151da-0000-4000-8000-000000000001"
OTHER = "3b2c9d1e-0000-4000-8000-000000000002"


def _bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def session(sid, pid=None, cwd="/w/Acme"):
    return {"kind": "session", "session_id": sid, "pid": pid, "cwd": cwd}


OPERATOR = {"kind": "operator", "user": "alice", "tty": "/dev/ttys003", "cwd": "/w"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.logs)
        os.makedirs(self.state)
        self.profile = "acme"
        # Liveness is injected: {pid: True/False}; unknown pids read as None.
        self.alive = {}
        self._alive_patch = mock.patch.object(
            stop_drain, "pid_alive", lambda pid: self.alive.get(pid))
        self._alive_patch.start()
        # A held lock must make callers stand down quickly in tests.
        self._wait_patch = mock.patch.object(stop_drain, "LOCK_WAIT", 0.2)
        self._wait_patch.start()
        # Process identity is injected too (codex pass 5): boot id and start times.
        self.boot = "boot-A"
        self.starts = {}
        self._boot_patch = mock.patch.object(stop_drain, "boot_identity", lambda: self.boot)
        self._start_patch = mock.patch.object(stop_drain, "process_start", lambda pid: self.starts.get(pid))
        self._boot_patch.start()
        self._start_patch.start()

    def tearDown(self):
        self._start_patch.stop()
        self._boot_patch.stop()
        self._wait_patch.stop()
        self._alive_patch.stop()
        self.tmp.cleanup()

    def hold_lock(self):
        """Take the profile lock from the test (a second fd contends). Non-blocking, so a
        leaked lock is a loud error here rather than a hang."""
        path = stop_drain.lock_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def release_lock(self, fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def write_log(self, source, lines):
        with open(os.path.join(self.logs, "%s-%s.log" % (self.profile, source)), "a") as fh:
            for line in lines:
                fh.write(line + "\n")

    def drain(self, actor, stop_hook_active=False, real=True):
        """A REAL turn-end by default (the harness's payload marker); ``real=False``
        is a hook run by hand — from a terminal or from a session's Bash tool."""
        return stop_drain.drain(self.logs, self.state, self.profile,
                                stop_hook_active=stop_hook_active, actor=actor,
                                real_invocation=real)

    def cursor(self):
        return stop_drain.load_cursor(self.state, self.profile)

    def cursor_bytes(self):
        try:
            with open(stop_drain.cursor_path(self.state, self.profile), "rb") as fh:
                return fh.read()
        except OSError:
            return b""

    def cli(self, actor, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(stop_drain, "actor_identity", lambda payload=None, environ=None: actor):
            with redirect_stdout(out), redirect_stderr(err):
                rc = wt_acted.main(["--profile", self.profile, "--log-dir", self.logs,
                                    "--state-root", self.state, *argv])
        return rc, out.getvalue(), err.getvalue()


class TestIdentity(unittest.TestCase):
    """Who is asking — from the harness env, the hook payload, or nobody (the operator)."""

    def test_session_from_the_harness_environment(self):
        a = stop_drain.actor_identity(environ={"CLAUDE_CODE_SESSION_ID": ME, "CLAUDE_PID": "4242"})
        self.assertEqual((a["kind"], a["session_id"], a["pid"]), ("session", ME, 4242))

    def test_hook_payload_session_id_is_honoured_first(self):
        a = stop_drain.actor_identity(payload={"session_id": OTHER},
                                      environ={"CLAUDE_CODE_SESSION_ID": ME})
        self.assertEqual(a["session_id"], OTHER)

    def test_no_session_identity_is_the_operator(self):
        a = stop_drain.actor_identity(environ={"USER": "alice"})
        self.assertEqual((a["kind"], a["user"]), ("operator", "alice"))

    def test_unparseable_pid_is_none_not_a_crash(self):
        a = stop_drain.actor_identity(environ={"CLAUDE_CODE_SESSION_ID": ME, "CLAUDE_PID": "x"})
        self.assertIsNone(a["pid"])


class TestLiveness(unittest.TestCase):
    """The real pid_alive: our own pid, a reaped child, and no pid at all."""

    def test_own_pid_is_alive(self):
        self.assertIs(stop_drain.pid_alive(os.getpid()), True)

    def test_a_reaped_child_is_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIs(stop_drain.pid_alive(child.pid), False)

    def test_no_pid_is_unknown_not_dead(self):
        """Unknown must never free a lease: only a positive 'gone' does."""
        self.assertIsNone(stop_drain.pid_alive(None))
        self.assertIsNone(stop_drain.pid_alive(0))


class TestHookOwnership(Base):
    def test_unowned_cursor_is_claimed_by_the_first_drain_and_blocks(self):
        self.write_log("inbox", [MAIL % "a"])
        out = self.drain(session(ME, pid=11))
        self.assertEqual(out.get("decision"), "block")
        owner = self.cursor()["owner"]
        self.assertEqual((owner["session_id"], owner["pid"]), (ME, 11))

    def test_the_owner_drains_exactly_as_before(self):
        """Single-session behaviour is unchanged: block once, then quiet, block text identical."""
        self.write_log("inbox", [MAIL % "a"])
        first = self.drain(session(ME, pid=11))
        self.assertEqual(first["reason"],
                         stop_drain.format_reason({"inbox": stop_drain.parse_events(MAIL % "a")},
                                                  self.profile))
        self.assertEqual(self.drain(session(ME, pid=11)), {})

    def test_a_foreign_live_session_is_silent_and_touches_nothing(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))            # ME owns; event surfaced (owner saw it)
        self.write_log("inbox", [MAIL % "b"])      # a NEW event ME has not seen yet
        self.alive[11] = True
        before = self.cursor_bytes()
        out = self.drain(session(OTHER, pid=22))
        self.assertEqual(out, {}, "a non-owner must never block its own session on the seat's events")
        self.assertEqual(self.cursor_bytes(), before, "a non-owner must not touch the seat's cursor")
        # ...and the owner still gets its one-shot block for the new event.
        self.assertEqual(self.drain(session(ME, pid=11)).get("decision"), "block")

    def test_a_foreign_session_is_recorded_as_a_contender(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.drain(session(OTHER, pid=22, cwd="/w/globex"))
        self.drain(session(OTHER, pid=22, cwd="/w/globex"))
        c = stop_drain.load_contenders(self.state, self.profile)[0][OTHER]
        self.assertEqual((c["cwd"], c["count"]), ("/w/globex", 2))
        self.assertNotIn(OTHER, json.dumps(self.cursor()), "contenders live in their own file")

    def test_a_dead_owner_is_replaced_by_the_next_session(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = False
        self.write_log("inbox", [MAIL % "b"])
        out = self.drain(session(OTHER, pid=22))
        self.assertEqual(out.get("decision"), "block")
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)

    def test_an_owner_with_unknown_pid_is_not_stale(self):
        """No pid recorded means 'unknown', and unknown never frees a lease."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=None))
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {})
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)

    def test_no_timer_an_idle_owner_keeps_the_cursor(self):
        """Pinned in the negative: nothing in the cursor decays with time."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        c = self.cursor()
        c["owner"]["since"] = c["owner"]["last_active"] = "2020-01-01T00:00:00+00:00"
        stop_drain.save_cursor(self.state, self.profile, c)
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {})

    def test_the_operator_running_the_hook_by_hand_sees_but_does_not_consume(self):
        """2026-09-08: a probe of the hook ate the seat's one-shot surfacing of 1148 events."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.write_log("inbox", [MAIL % "b"])
        out = self.drain(OPERATOR)
        self.assertEqual(out.get("decision"), "block", "the operator is shown what a session would see")
        self.assertEqual(self.cursor()["sources"]["inbox"]["surfaced_through"], 1,
                         "…but the seat's surfaced cursor must not move")
        self.assertEqual(self.cursor()["owner"]["session_id"], ME, "and the operator never owns")
        self.assertEqual(self.drain(session(ME, pid=11)).get("decision"), "block")

    def test_stop_hook_active_stands_down_for_everyone(self):
        self.write_log("inbox", [MAIL % "a"])
        for actor in (session(ME, pid=11), session(OTHER, pid=22), OPERATOR):
            self.assertEqual(self.drain(actor, stop_hook_active=True), {})
        self.assertNotIn("owner", self.cursor(), "standing down must not even claim")


class TestAttribution(Base):
    def test_a_surfacing_is_stamped_with_the_session(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        by = self.cursor()["sources"]["inbox"]["by"]
        self.assertEqual((by["kind"], by["session_id"], by["field"]),
                         ("session", ME, "surfaced_through"))

    def test_an_act_is_stamped_and_replaces_the_surfacing_stamp(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.cli(session(ME, pid=11), "--list")
        rc, _, _ = self.cli(session(ME, pid=11))
        self.assertEqual(rc, 0)
        by = self.cursor()["sources"]["inbox"]["by"]
        self.assertEqual((by["session_id"], by["field"]), (ME, "acted_through"))

    def test_a_no_op_advance_keeps_the_real_actors_stamp(self):
        self.write_log("inbox", [MAIL % "a"])
        self.cli(session(ME, pid=11), "--list")
        self.cli(session(ME, pid=11))
        self.alive[11] = False
        self.cli(session(OTHER, pid=22))          # claims (dead owner) but nothing to advance
        self.assertEqual(self.cursor()["sources"]["inbox"]["by"]["session_id"], ME)


class TestCliOwnership(Base):
    def test_a_foreign_session_is_refused_loudly_naming_the_owner(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        before = self.cursor_bytes()
        rc, out, err = self.cli(session(OTHER, pid=22))
        self.assertEqual(rc, 2)
        self.assertIn(ME[:8], err)
        self.assertIn("takeover", err)
        self.assertEqual(self.cursor_bytes(), before)

    def test_take_over_claims_and_clears_nothing(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        rc, out, _ = self.cli(session(OTHER, pid=22), "--take-over")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing cleared", out)
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)
        self.assertEqual(self.cursor()["sources"]["inbox"]["acted_through"], 0)
        self.cli(session(OTHER, pid=22), "--list")
        rc, _, _ = self.cli(session(OTHER, pid=22))   # now it may act
        self.assertEqual(rc, 0)
        self.assertEqual(self.cursor()["sources"]["inbox"]["acted_through"], 1)

    def test_the_operator_acts_without_owning(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.cli(OPERATOR, "--list")
        rc, out, _ = self.cli(OPERATOR)
        self.assertEqual(rc, 0)
        self.assertEqual(self.cursor()["sources"]["inbox"]["acted_through"], 1)
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)
        self.assertEqual(self.cursor()["sources"]["inbox"]["by"]["kind"], "operator")

    def test_the_operator_take_over_releases_without_owning(self):
        """A review (P2): every recovery message tells the human at the terminal
        to run `just takeover`, and the operator was the one actor it refused."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True                                 # the owner is LIVE; the human decides
        rc, out, err = self.cli(OPERATOR, "--take-over")
        self.assertEqual(rc, 0, err)
        self.assertIn("RELEASED", out)
        self.assertIsNone(self.cursor()["owner"], "released: nobody owns it, the operator least of all")
        self.assertEqual(self.cursor()["sources"]["inbox"]["acted_through"], 0, "clears nothing")
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)).get("decision"), "block")
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER, "the next session claims it")

    def test_an_unowned_cursor_is_claimed_by_the_first_act(self):
        self.write_log("inbox", [MAIL % "a"])
        self.cli(session(ME, pid=11), "--list")
        rc, _, _ = self.cli(session(ME, pid=11))
        self.assertEqual(rc, 0)
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)

    def test_list_labels_a_dead_contender_as_gone_not_running(self):
        """codex pass 2: a contender entry is a historical observation; only a live
        process is a live conflict. Reporting every entry as running made every
        resolved conflict a permanent warning."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.drain(session(OTHER, pid=22, cwd="/w/globex"))     # foreign, recorded
        self.alive[22] = False                                      # ...and then it exited
        rc, out, _ = self.cli(session(ME, pid=11), "--list")
        self.assertEqual(rc, 0)
        self.assertIn("gone", out)
        self.assertNotIn("RUNNING", out)
        self.alive[22] = True                                       # back, or never left
        rc, out, _ = self.cli(session(ME, pid=11), "--list")
        self.assertIn("RUNNING", out)

    def test_list_shows_owner_and_contenders_and_mutates_nothing(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.drain(session(OTHER, pid=22, cwd="/w/globex"))
        before = self.cursor_bytes()
        rc, out, _ = self.cli(session(OTHER, pid=22), "--list")
        self.assertEqual(rc, 0)
        self.assertIn("owner: session " + ME[:8], out)
        self.assertIn("NOT the owner", out)
        self.assertIn("/w/globex", out)
        self.assertEqual(self.cursor_bytes(), before)


class TestCompatibility(Base):
    def test_a_version_1_cursor_without_an_owner_still_loads_and_is_claimable(self):
        path = stop_drain.cursor_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            json.dump({"version": 1, "sources": {"inbox": {"acted_through": 1, "surfaced_through": 1,
                                                            "bytes": 10, "first": MAIL % "a"}}}, fh)
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(stop_drain.ownership(self.cursor(), session(ME)), "claimable")
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(ME, pid=11)).get("decision"), "block")
        c = self.cursor()
        self.assertEqual((c["version"], c["owner"]["session_id"]), (2, ME))
        self.assertEqual(c["sources"]["inbox"]["acted_through"], 1, "old cursors are preserved")


class TestSerialisation(Base):
    """codex P2 on this branch: the ownership check, the claim and the advance are
    ONE transaction. Atomic replace protects each write; it does nothing for the
    read-modify-write, so two sessions could both read "claimable", both claim,
    and both consume the same events."""

    def test_a_held_lock_makes_the_hook_stand_down_without_claiming(self):
        self.write_log("inbox", [MAIL % "a"])
        fd = self.hold_lock()
        try:
            self.assertEqual(self.drain(session(ME, pid=11)), {})
            self.assertNotIn("owner", self.cursor(), "standing down must not claim")
        finally:
            self.release_lock(fd)
        self.assertEqual(self.drain(session(ME, pid=11)).get("decision"), "block",
                         "the events resurface once the lock is free")

    def test_a_held_lock_makes_the_cli_refuse_and_change_nothing(self):
        self.write_log("inbox", [MAIL % "a"])
        before = self.cursor_bytes()
        fd = self.hold_lock()
        try:
            rc, _, err = self.cli(session(ME, pid=11))
        finally:
            self.release_lock(fd)
        self.assertEqual(rc, 2)
        self.assertIn("busy", err)
        self.assertEqual(self.cursor_bytes(), before)

    def test_two_sessions_racing_for_an_unowned_cursor_yield_exactly_one_owner(self):
        """A real race: two threads, two fds, one lock. The loser acquires after the
        winner releases, reads 'foreign', and stands down."""
        self.write_log("inbox", [MAIL % "a"])
        self.alive.update({11: True, 22: True})
        results = {}

        def go(actor):
            results[actor["session_id"]] = self.drain(actor)

        threads = [threading.Thread(target=go, args=(session(ME, pid=11),)),
                   threading.Thread(target=go, args=(session(OTHER, pid=22),))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        owner = self.cursor()["owner"]["session_id"]
        self.assertIn(owner, (ME, OTHER))
        blocked = [sid for sid, out in results.items() if out.get("decision") == "block"]
        self.assertEqual(blocked, [owner], "exactly the owner is blocked; the other stood down")

    def test_an_unopenable_lock_file_makes_both_entry_points_stand_down(self):
        """codex pass 2: the first version yielded UNLOCKED when `.lock` could not be
        opened, reasoning the save would fail too. But `.lock` can be unopenable (a
        directory in its place, a wrong owner) while acted.json is still replaceable
        — the unprotected transaction the lock exists to prevent."""
        self.write_log("inbox", [MAIL % "a"])
        os.makedirs(stop_drain.lock_path(self.state, self.profile))   # a directory: O_RDWR fails
        self.assertEqual(self.drain(session(ME, pid=11)), {})
        self.assertEqual(self.cursor_bytes(), b"", "the hook must not claim or advance unlocked")
        rc, _, err = self.cli(session(ME, pid=11))
        self.assertEqual(rc, 2)
        self.assertIn("lock", err)
        self.assertEqual(self.cursor_bytes(), b"", "the CLI must not act unlocked")

    def test_the_lock_is_released_after_every_path(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))                       # claim + block
        self.assertEqual(self.cli(session(ME, pid=11))[0], 2)  # blind blanket act: refused
        self.cli(session(ME, pid=11), "--list")               # list: recorded under the lock
        self.assertEqual(self.cli(session(ME, pid=11))[0], 0)  # act
        self.alive[11] = True
        self.cli(session(OTHER, pid=22))                      # refused
        self.cli(session(OTHER, pid=22), "--take-over")       # take over
        self.drain(OPERATOR)                                  # operator report
        try:
            fd = self.hold_lock()                             # non-blocking: a leak raises
        except BlockingIOError:
            self.fail("the profile lock is still held after the paths above")
        self.assertGreaterEqual(fd, 0)
        self.release_lock(fd)


class TestResumedOwner(Base):
    """codex P2 on this branch: a resumed session keeps its id but gets a new pid.
    Left unrefreshed, the stored pid goes stale, reads as dead, and the next
    arrival takes a LIVE seat."""

    def test_the_hook_refreshes_the_owners_pid_on_resume(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=33))                       # resumed: same id, new pid
        self.assertEqual(self.cursor()["owner"]["pid"], 33)
        self.alive.update({11: False, 33: True})              # old pid dead, new one alive
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "a live resumed owner keeps the seat")

    def test_the_hook_refreshes_even_when_nothing_is_pending(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.assertEqual(self.drain(session(ME, pid=33)), {})   # quiet turn-end, nothing new
        self.assertEqual(self.cursor()["owner"]["pid"], 33)

    def test_the_cli_refreshes_the_owners_pid_on_resume(self):
        self.write_log("inbox", [MAIL % "a"])
        self.cli(session(ME, pid=11), "--list")
        self.cli(session(ME, pid=11))
        self.write_log("inbox", [MAIL % "b"])
        self.cli(session(ME, pid=33), "--list")
        self.cli(session(ME, pid=33))
        self.assertEqual(self.cursor()["owner"]["pid"], 33)

    def test_a_resume_without_a_pid_keeps_a_recorded_pid_that_is_still_alive(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=None))
        self.assertEqual(self.cursor()["owner"]["pid"], 11)

    def test_a_resume_without_a_pid_drops_a_recorded_pid_that_is_dead(self):
        """codex pass 5: keeping the corpse's pid let the next arrival read the seat
        as claimable while the resumed session was live. Unknown liveness holds it."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = False                                # that process is gone
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=None))                     # resumed, pid unknown
        self.assertIsNone(self.cursor()["owner"]["pid"])
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {},
                         "unknown liveness must hold the lease against a newcomer")


class TestCursorIO(Base):
    """codex pass 3 on this branch: the failure handling written for the drain's
    positions is wrong for ownership. `load_cursor` launders an unreadable
    acted.json into an EMPTY cursor (a live owner's seat looks claimable), and
    `save_cursor` swallows write failures (a take-over reports success for a
    write that never happened)."""

    def _write_cursor_bytes(self, data):
        path = stop_drain.cursor_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_read_cursor_tells_missing_from_ok_from_unreadable(self):
        self.assertEqual(stop_drain.read_cursor(self.state, self.profile)[1], "missing")
        self._write_cursor_bytes(b'{"version": 2, "sources": {}}')
        self.assertEqual(stop_drain.read_cursor(self.state, self.profile)[1], "ok")
        self._write_cursor_bytes(b"{not json")
        self.assertEqual(stop_drain.read_cursor(self.state, self.profile)[1], "unreadable")
        self._write_cursor_bytes(b"[]")            # parses, is not a cursor
        self.assertEqual(stop_drain.read_cursor(self.state, self.profile)[1], "unreadable")

    def test_an_unreadable_cursor_makes_the_hook_stand_down_untouched(self):
        self.write_log("inbox", [MAIL % "a"])
        path = self._write_cursor_bytes(b"{not json")
        before = _bytes(path)
        self.assertEqual(self.drain(session(OTHER, pid=22)), {},
                         "ownership is indeterminate, so nothing may be claimed or surfaced")
        self.assertEqual(_bytes(path), before)

    def test_an_unreadable_cursor_makes_the_cli_refuse_untouched(self):
        self.write_log("inbox", [MAIL % "a"])
        path = self._write_cursor_bytes(b"{not json")
        before = _bytes(path)
        rc, _, err = self.cli(session(ME, pid=11))
        self.assertEqual((rc, "indeterminate" in err), (2, True))
        rc, _, err = self.cli(session(ME, pid=11), "--take-over")
        self.assertEqual((rc, "indeterminate" in err), (2, True),
                         "the recovery is manual — remove or repair — not a silent replace")
        self.assertEqual(_bytes(path), before)
        rc, out, _ = self.cli(session(ME, pid=11), "--list")
        self.assertEqual(rc, 0)
        self.assertIn("UNREADABLE", out)

    def test_a_missing_cursor_is_unowned_not_unreadable(self):
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(self.drain(session(ME, pid=11)).get("decision"), "block")
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)

    def test_a_take_over_whose_write_fails_reports_failure_not_success(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        # A directory at the temp path: open(tmp, "w") fails, os.replace never runs.
        os.makedirs(stop_drain.cursor_path(self.state, self.profile) + ".tmp")
        rc, out, err = self.cli(session(OTHER, pid=22), "--take-over")
        self.assertEqual(rc, 2)
        self.assertIn("NOT saved", err)
        self.assertNotIn("taken", out)
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)

    def test_an_act_whose_write_fails_reports_failure(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.cli(session(ME, pid=11), "--list")
        os.makedirs(stop_drain.cursor_path(self.state, self.profile) + ".tmp")
        rc, out, err = self.cli(session(ME, pid=11))
        self.assertEqual(rc, 2)
        self.assertIn("FAILED", err)
        self.assertNotIn("cleared", out)
        self.assertEqual(self.cursor()["sources"]["inbox"]["acted_through"], 0)

    def test_a_claim_whose_write_fails_does_not_proceed_to_act(self):
        self.write_log("inbox", [MAIL % "a"])
        os.makedirs(stop_drain.cursor_path(self.state, self.profile) + ".tmp")
        # The override, not a listing: a listing would create the cursor file this
        # test needs absent — and the refusal also says "Nothing changed", so FAILED
        # pins that it was the claim's write that failed.
        rc, out, err = self.cli(session(ME, pid=11), "--all-including-unseen")
        self.assertEqual(rc, 2)
        self.assertIn("Nothing changed", err)
        self.assertIn("FAILED", err)
        self.assertEqual(self.cursor_bytes(), b"")


class TestTogetherOrNotAtAll(Base):
    """codex pass 4 on this branch: the ownership write and the position writes were
    separate, and the advance reloaded from disk — so a LOST claim or refresh write
    followed by a successful advance persisted consumed positions under a missing or
    stale owner, and the next session could take a live seat. Now one save carries
    both. Pinned by fault injection: the first write is lost, the rest succeed."""

    def _lose_first_write(self):
        real = stop_drain.save_cursor
        calls = {"n": 0}

        def flaky(state_root, profile, cursor):
            calls["n"] += 1
            return False if calls["n"] == 1 else real(state_root, profile, cursor)

        return mock.patch.object(stop_drain, "save_cursor", flaky)

    def test_hook_persists_ownership_and_positions_together_or_not_at_all(self):
        self.write_log("inbox", [MAIL % "a"])
        with self._lose_first_write():
            self.drain(session(ME, pid=11))
        c = self.cursor()
        surfaced = c["sources"].get("inbox", {}).get("surfaced_through", 0)
        self.assertEqual(surfaced > 0, "owner" in c,
                         "positions without the ownership that authorised them (or vice versa)")

    def test_hook_never_advances_under_a_stale_pid(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.write_log("inbox", [MAIL % "b"])
        with self._lose_first_write():
            self.drain(session(ME, pid=33))                    # resumed: same id, new pid
        c = self.cursor()
        if c["sources"]["inbox"]["surfaced_through"] > 1:
            self.assertEqual(c["owner"]["pid"], 33, "advanced positions must carry the refreshed pid")
        else:
            self.assertEqual(c["owner"]["pid"], 11, "nothing advanced, so nothing refreshed — consistent")

    def test_cli_persists_claim_and_positions_together_or_not_at_all(self):
        self.write_log("inbox", [MAIL % "a"])
        with self._lose_first_write():   # override: no listing may take the first write
            rc, _, err = self.cli(session(ME, pid=11), "--all-including-unseen")
        c, state = stop_drain.read_cursor(self.state, self.profile)
        if state == "missing":
            self.assertEqual(rc, 2, "nothing persisted must be reported as failure")
            self.assertIn("FAILED", err, "…by the lost write, not by a refusal")
        else:
            acted = c["sources"].get("inbox", {}).get("acted_through", 0)
            self.assertEqual(acted > 0, "owner" in c)


class TestProcessIdentity(Base):
    """codex pass 5 on this branch: "dead pid" must mean the PROCESS the pid named is
    gone. After a reboot, or a recycled pid, an unrelated live process wearing the
    stored pid read as a live foreign owner — replacement sessions' hooks stood
    down silently and `just acted` refused until someone noticed."""

    def test_claim_records_boot_and_start(self):
        self.starts[11] = "Tue Sep  9 07:00:00 2026"
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        o = self.cursor()["owner"]
        self.assertEqual((o["boot"], o["start"]), ("boot-A", "Tue Sep  9 07:00:00 2026"))

    def test_a_reboot_frees_a_dead_owner_even_if_its_pid_is_reused(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True                                 # some process wears pid 11 now
        self.boot = "boot-B"                                  # ...but the host has rebooted
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)).get("decision"), "block")
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)

    def test_a_recycled_pid_within_one_boot_frees_the_lease_when_the_start_differs(self):
        self.starts[11] = "Tue Sep  9 07:00:00 2026"
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.starts[11] = "Tue Sep  9 09:30:00 2026"         # a different process has pid 11
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)).get("decision"), "block")
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)

    def test_the_same_process_is_still_the_owner(self):
        self.starts[11] = "Tue Sep  9 07:00:00 2026"
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "same boot, same start: alive, foreign stands down")

    def test_an_unknown_start_falls_back_to_pid_liveness(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))                       # starts[11] absent → None recorded
        self.alive[11] = True
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {})

    def test_the_real_instruments_answer_on_this_host(self):
        """The two identity instruments, unpatched, against this process."""
        with mock.patch.object(stop_drain, "_BOOT_CACHE", {}):
            self._boot_patch.stop()
            self._start_patch.stop()
            try:
                self.assertIsNotNone(stop_drain.boot_identity())
                self.assertIsNotNone(stop_drain.process_start(os.getpid()))
                self.assertIsNone(stop_drain.process_start(None))
            finally:
                self._boot_patch.start()
                self._start_patch.start()


class TestIdentityRobustness(Base):
    """codex pass 6 on this branch: three ways the pass-5 identity could still misread a
    live owner as dead — or a dead contender as live."""

    def test_process_start_is_timezone_independent(self):
        """`ps -o lstart=` prints local time; two sessions with different TZ must not
        disagree about when one process started. The instrument pins TZ itself."""
        self._start_patch.stop()
        try:
            with mock.patch.dict(os.environ, {"TZ": "UTC"}):
                a = stop_drain.process_start(os.getpid())
            with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                b = stop_drain.process_start(os.getpid())
        finally:
            self._start_patch.start()
        self.assertIsNotNone(a)
        self.assertEqual(a, b)

    def test_a_resume_that_gets_its_old_pid_back_after_a_reboot_refreshes_identity(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))                       # recorded under boot-A
        self.boot = "boot-B"                                  # rebooted; the same numeric pid comes back
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=11))                       # resumed: same id, same pid
        self.assertEqual(self.cursor()["owner"]["boot"], "boot-B")
        self.alive[11] = True
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "a live resumed owner keeps the seat")

    def test_a_transiently_silent_instrument_does_not_erase_a_known_start(self):
        self.starts[11] = "t1"
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.starts[11] = None                                # ps says nothing this time
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=11))
        self.assertEqual(self.cursor()["owner"]["start"], "t1")

    def test_a_contender_whose_pid_was_recycled_is_reported_as_gone(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.drain(session(OTHER, pid=22, cwd="/w/globex"))  # contender, recorded under boot-A
        self.boot = "boot-B"                                   # rebooted...
        self.alive[22] = True                                  # ...and something unrelated wears pid 22
        rc, out, _ = self.cli(session(ME, pid=11), "--list")
        self.assertEqual(rc, 0)
        self.assertIn("gone", out)
        self.assertNotIn("RUNNING", out)


class TestBootFirstLiveness(Base):
    """codex pass 7 on this branch: liveness is decided boot-first, and the owner's own
    hook run is proof of life. A pid-less owner recorded under another boot is DEAD —
    no process survives a reboot — not "unknown, hold until someone runs take-over";
    and a resume across a reboot never inherits the previous process's identity."""

    def test_a_pidless_owner_under_another_boot_is_dead_not_unknown(self):
        self.assertIsNone(stop_drain.owner_alive({"pid": None, "boot": "boot-A"}))
        self.assertIsNone(stop_drain.owner_alive({"pid": None, "boot": None}))
        self.boot = "boot-B"
        self.assertIs(stop_drain.owner_alive({"pid": None, "boot": "boot-A"}), False)
        self.assertIsNone(stop_drain.owner_alive({"pid": None, "boot": None}),
                          "an unknown boot is unknown, not dead")

    def test_a_reboot_frees_a_pidless_owner_without_take_over(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=None))                     # claimed with no CLAUDE_PID
        self.assertEqual(self.cursor()["owner"]["boot"], "boot-A")
        self.boot = "boot-B"                                  # rebooted: that session is gone
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)).get("decision"), "block")
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)

    def test_the_cli_acts_on_a_pidless_seat_from_a_previous_boot_without_take_over(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=None))
        self.boot = "boot-B"
        self.cli(session(OTHER, pid=22), "--list")
        rc, out, err = self.cli(session(OTHER, pid=22))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)

    def test_a_pidless_owner_that_resumes_after_a_reboot_keeps_the_seat(self):
        """The boot-first rule alone would free a LIVE seat: a pid-less session resumed
        after a reboot has no pid to attest for it, only its boot — so its own hook
        run must re-record the boot."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=None))
        self.boot = "boot-B"
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=None))                     # resumed, still no pid
        self.assertEqual(self.cursor()["owner"]["boot"], "boot-B")
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "unknown liveness holds the lease")

    def test_a_pidless_owner_records_an_unknown_boot_as_unknown_not_stale(self):
        """With no pid on record the boot is the only identity there is: a silent
        instrument must leave it unknown (holds), not the previous boot (frees)."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=None))
        self.boot = None                                      # the instrument says nothing
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=None))
        self.assertIsNone(self.cursor()["owner"]["boot"])
        self.boot = "boot-B"
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "unknown liveness holds the lease")

    def test_a_same_pid_resume_across_a_reboot_does_not_inherit_the_old_start(self):
        """codex pass 7: same numeric pid after a reboot, and `ps` silent for that one
        run — the pass-6 refresh kept the previous process's start; once the
        instrument recovered the live owner read as dead and was taken over."""
        self.starts[11] = "t1"
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))                       # recorded (11, boot-A, t1)
        self.boot = "boot-B"
        self.starts[11] = None                                # ps says nothing this time
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=11))                       # resumed: same id, same pid
        o = self.cursor()["owner"]
        self.assertEqual((o["boot"], o["start"]), ("boot-B", None), "a boot change is a new process")
        self.alive[11] = True
        self.starts[11] = "t2"                                # the instrument recovers
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "a live resumed owner keeps the seat")
        self.write_log("inbox", [MAIL % "d"])
        self.drain(session(ME, pid=11))
        self.assertEqual(self.cursor()["owner"]["start"], "t2")

    def test_a_pidless_contender_from_a_previous_boot_is_reported_as_gone(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        self.drain(session(OTHER, pid=None, cwd="/w/globex"))  # contender: no pid, boot-A
        self.boot = "boot-B"                                   # rebooted...
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=11))                        # ...and the owner resumed
        rc, out, _ = self.cli(session(ME, pid=11), "--list")
        self.assertEqual(rc, 0)
        self.assertIn("its process is gone", out)
        self.assertNotIn("liveness unknown", out)


class TestCrossSideReview(Base):
    """A cross-side review found three P2s.
    A seat handover let the dead seat's surfacing count against the new owner; a
    hook run by hand from a session's Bash tool was the OWNER and consumed; and the
    documented escape hatch was refused to the human it addresses."""

    def test_a_new_owner_is_shown_what_the_dead_seat_never_acted_on(self):
        """Owner surfaces a,b (surfaced=2, acted=0) and dies; c arrives; the replacement's
        block used to list only c while `just unactioned` said three."""
        self.write_log("inbox", [MAIL % "a", MAIL % "b"])
        self.assertEqual(self.drain(session(ME, pid=11)).get("decision"), "block")
        self.alive[11] = False
        self.write_log("inbox", [MAIL % "c"])
        out = self.drain(session(OTHER, pid=22))
        self.assertEqual(out.get("decision"), "block")
        for m in ("a", "b", "c"):
            self.assertIn("message_id=%s" % m, out["reason"])
        c = self.cursor()
        self.assertEqual(c["owner"]["session_id"], OTHER)
        self.assertEqual(c["sources"]["inbox"]["surfaced_through"], 3)
        self.assertEqual(c["sources"]["inbox"]["acted_through"], 0, "nothing that records handling moved")

    def test_a_take_over_re_surfaces_what_the_previous_owner_never_acted_on(self):
        self.write_log("inbox", [MAIL % "a", MAIL % "b"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        rc, _, _ = self.cli(session(OTHER, pid=22), "--take-over")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cursor()["sources"]["inbox"]["surfaced_through"], 0,
                         "a new owner starts from what was handled")
        out = self.drain(session(OTHER, pid=22))
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("message_id=a", out["reason"])
        self.assertIn("message_id=b", out["reason"])

    def test_a_probe_from_the_owning_sessions_bash_reports_without_consuming(self):
        """`echo '{}' | python3 stop_drain.py …` from a session's Bash tool carries that
        session's identity — it read as the OWNER and ate the surfacing (2026-09-08)."""
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.write_log("inbox", [MAIL % "b"])
        out = self.drain(session(ME, pid=11), real=False)      # by hand: no payload marker
        self.assertEqual(out.get("decision"), "block", "the probe is shown what a turn-end would see")
        self.assertEqual(self.cursor()["sources"]["inbox"]["surfaced_through"], 1,
                         "…and the seat's surfaced cursor must not move")
        self.assertEqual(self.drain(session(ME, pid=11)).get("decision"), "block",
                         "the real turn-end still blocks")

    def test_a_probe_never_claims_or_writes(self):
        self.write_log("inbox", [MAIL % "a"])
        self.assertEqual(self.drain(session(ME, pid=11), real=False).get("decision"), "block")
        self.assertEqual(self.cursor_bytes(), b"", "a hook run by hand owns nothing and writes nothing")

    def test_run_hook_consumes_only_on_a_real_stop_invocation(self):
        """The marker is the harness's own `hook_event_name: "Stop"` — the one thing a
        hand-run `echo '{}'` does not carry, whatever environment it inherits."""
        self.write_log("inbox", [MAIL % "a"])
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ME, "CLAUDE_PID": "11"}):
            self.assertIn("block", stop_drain.run_hook({}, self.logs, self.state, self.profile))
            self.assertEqual(self.cursor_bytes(), b"",
                             "a hand-run hook, even from the session, writes nothing")
            self.assertIn("block", stop_drain.run_hook({"hook_event_name": "Stop", "session_id": ME},
                                                       self.logs, self.state, self.profile))
        self.assertEqual(self.cursor()["sources"]["inbox"]["surfaced_through"], 1)
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)

    def test_the_operator_take_over_on_an_unowned_cursor_changes_nothing(self):
        self.write_log("inbox", [MAIL % "a"])
        rc, out, _ = self.cli(OPERATOR, "--take-over")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to release", out)
        self.assertEqual(self.cursor_bytes(), b"")

    def test_a_release_whose_write_fails_is_reported_not_claimed_as_success(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        before = self.cursor_bytes()
        with mock.patch.object(stop_drain, "save_cursor", lambda *a, **k: False):
            rc, _, err = self.cli(OPERATOR, "--take-over")
        self.assertEqual(rc, 2)
        self.assertIn("NOT saved", err)
        self.assertEqual(self.cursor_bytes(), before)

    def test_an_unreadable_contenders_file_is_reported_not_emptied(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True
        path = stop_drain.contenders_path(self.state, self.profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.drain(session(OTHER, pid=22)), {})    # foreign: silent…
        self.assertEqual(_bytes(path), b"{not json", "…and it must not rebuild the file from nothing")
        rc, out, _ = self.cli(session(ME, pid=11), "--list")
        self.assertEqual(rc, 0)
        self.assertIn("contenders: UNREADABLE", out)

    def test_the_foreign_refusal_labels_unknown_liveness_as_unknown(self):
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=None))                     # pid-less owner: liveness unknown
        rc, _, err = self.cli(session(OTHER, pid=22))
        self.assertEqual(rc, 2)
        self.assertIn("liveness unknown", err)
        self.assertNotIn("alive)", err)


class TestCodexPass8(Base):
    """codex pass 8 on this branch (re-run on dd3878d): two more ways a LIVE owner read
    as dead. macOS `kern.boottime` is calendar-derived and moves when the clock is
    stepped — no reboot needed — so it cannot be the boot identity; and a same-pid
    resume whose START changed while the boot instrument was silent kept the old boot
    on record, reading as dead once the instrument recovered."""

    def test_macos_boot_identity_is_the_boot_session_uuid_not_boottime(self):
        """The instrument, unpatched except for its two probes: the per-boot UUID is
        preferred; boottime is the last resort only."""
        self._boot_patch.stop()
        try:
            answers = {"kern.bootsessionuuid": "B1BD678D-046E-4700-8CE4-D4A37993044A",
                       "kern.boottime": "{ sec = 1788906927, usec = 877534 } Tue Sep  8 15:35:27 2026"}
            with mock.patch.object(stop_drain, "_BOOT_CACHE", {}), \
                 mock.patch.object(stop_drain, "_linux_boot_id", lambda: None), \
                 mock.patch.object(stop_drain, "_sysctl", lambda name: answers.get(name)):
                self.assertEqual(stop_drain.boot_identity(), answers["kern.bootsessionuuid"])
            answers.pop("kern.bootsessionuuid")
            with mock.patch.object(stop_drain, "_BOOT_CACHE", {}), \
                 mock.patch.object(stop_drain, "_linux_boot_id", lambda: None), \
                 mock.patch.object(stop_drain, "_sysctl", lambda name: answers.get(name)):
                self.assertEqual(stop_drain.boot_identity(), "boottime:1788906927",
                                 "boottime is the fallback when there is no session uuid")
            answers["kern.bootsessionuuid"] = "not a uuid"
            with mock.patch.object(stop_drain, "_BOOT_CACHE", {}), \
                 mock.patch.object(stop_drain, "_linux_boot_id", lambda: None), \
                 mock.patch.object(stop_drain, "_sysctl", lambda name: answers.get(name)):
                self.assertEqual(stop_drain.boot_identity(), "boottime:1788906927",
                                 "a malformed uuid is not an identity")
        finally:
            self._boot_patch.start()

    def test_the_real_boot_identity_on_this_host_is_not_boottime_when_a_uuid_exists(self):
        self._boot_patch.stop()
        try:
            with mock.patch.object(stop_drain, "_BOOT_CACHE", {}):
                ident = stop_drain.boot_identity()
            uuid = stop_drain._sysctl("kern.bootsessionuuid")
            if uuid and stop_drain._UUID.match(uuid):
                self.assertEqual(ident, uuid)
            else:
                self.assertIsNotNone(ident)   # Linux, or an old macOS: whatever the host has
        finally:
            self._boot_patch.start()

    def test_a_same_pid_resume_with_a_new_start_is_a_new_process_even_with_boot_unknown(self):
        self.starts[11] = "t1"
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))                       # recorded (11, boot-A, t1)
        self.boot = None                                      # the boot instrument is silent…
        self.starts[11] = "t2"                                # …but the start time has changed
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=11))                       # resumed: same id, same pid
        o = self.cursor()["owner"]
        self.assertEqual((o["boot"], o["start"]), (None, "t2"),
                         "a new start is a new process: the old boot must not survive beside it")
        self.boot = "boot-B"                                  # the instrument recovers, after a reboot
        self.alive[11] = True
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "a live resumed owner keeps the seat")


class TestCodexPass9(Base):
    """codex pass 9 on this branch: the fallback chain in `boot_identity` can answer
    for ONE boot as a session uuid in one process and as `boottime:…` in another;
    comparing those read the same boot as a reboot and a live owner as dead."""

    UUID_A = "B1BD678D-046E-4700-8CE4-D4A37993044A"

    def test_boot_identities_of_different_kinds_are_unknown_not_different(self):
        self.assertFalse(stop_drain.boot_differs(self.UUID_A, "boottime:1788906927"))
        self.assertFalse(stop_drain.boot_differs("boottime:1788906927", self.UUID_A))
        self.assertFalse(stop_drain.boot_differs(None, self.UUID_A))
        self.assertFalse(stop_drain.boot_differs(self.UUID_A, None))
        self.assertFalse(stop_drain.boot_differs(self.UUID_A, self.UUID_A))
        self.assertTrue(stop_drain.boot_differs(self.UUID_A, "2755581D-0000-4000-8000-000000000000"))
        self.assertTrue(stop_drain.boot_differs("boottime:1", "boottime:2"))

    def test_a_transient_probe_fallback_does_not_free_a_live_seat(self):
        self.boot = self.UUID_A
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))                       # recorded under the uuid
        self.alive[11] = True
        self.boot = "boottime:1788906927"                     # the uuid probe fails in THIS process
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {}, "same boot, other kind: unknown holds")
        self.boot = self.UUID_A                               # …and the reverse, on recovery
        self.write_log("inbox", [MAIL % "c"])
        self.assertEqual(self.drain(session(OTHER, pid=22)), {})
        self.assertEqual(self.cursor()["owner"]["session_id"], ME)

    def test_the_owner_re_records_whichever_kind_the_instrument_gives(self):
        self.boot = "boottime:1788906927"                     # e.g. the pre-uuid format at rollout
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.boot = self.UUID_A
        self.write_log("inbox", [MAIL % "b"])
        self.drain(session(ME, pid=11))                       # the owner's own turn-end
        self.assertEqual(self.cursor()["owner"]["boot"], self.UUID_A)

    def test_a_real_reboot_within_one_kind_still_frees_the_seat(self):
        self.boot = self.UUID_A
        self.write_log("inbox", [MAIL % "a"])
        self.drain(session(ME, pid=11))
        self.alive[11] = True                                 # something wears pid 11 after the reboot
        self.boot = "2755581D-0000-4000-8000-000000000000"
        self.write_log("inbox", [MAIL % "b"])
        self.assertEqual(self.drain(session(OTHER, pid=22)).get("decision"), "block")
        self.assertEqual(self.cursor()["owner"]["session_id"], OTHER)


if __name__ == "__main__":
    unittest.main()
