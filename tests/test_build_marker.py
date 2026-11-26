#!/usr/bin/env python3
"""Every daemon says what bytes it is running, and the watchdog can tell.

The predicate is the LOADED BYTES, not the checkout's HEAD and not the
restart time: an incident found three daemons that had not restarted in a week
and were current by content, because the import had moved files verbatim.
A marker keyed on HEAD or on process start would have failed all three.

These tests use temp files only — no network, no launchd, no real state.
Run: `just test`.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))

import _build  # noqa: E402
import bitbucket_wake  # noqa: E402
import github_wake  # noqa: E402
import inbox_wake  # noqa: E402
import jira_wake  # noqa: E402
import schedule_wake  # noqa: E402
import watchdog_wake as wd  # noqa: E402

DAEMONS = {"github": github_wake, "jira": jira_wake, "bitbucket": bitbucket_wake,
           "inbox": inbox_wake, "schedule": schedule_wake}


class Marker(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _file(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_source_hash_is_a_function_of_the_bytes(self):
        a = self._file("a.py", "x = 1\n")
        b = self._file("b.py", "x = 1\n")
        c = self._file("c.py", "x = 2\n")
        self.assertEqual(_build.source_hash(a), _build.source_hash(b))
        self.assertNotEqual(_build.source_hash(a), _build.source_hash(c))
        self.assertEqual(len(_build.source_hash(a)), 12)

    def test_describe_never_raises_on_missing_file_or_no_git(self):
        build = _build.describe(os.path.join(self.tmp, "nope.py"))
        self.assertEqual(build, {"src": None, "head": None})

    def test_compare_current_outdated_unknown(self):
        path = self._file("d.py", "v = 1\n")
        loaded = {"src": _build.source_hash(path)}
        self.assertEqual(_build.compare(loaded, path)[0], "current")
        with open(path, "w") as fh:
            fh.write("v = 2\n")
        status, disk = _build.compare(loaded, path)
        self.assertEqual(status, "outdated")
        self.assertEqual(disk, _build.source_hash(path))
        self.assertEqual(_build.compare({}, path)[0], "unknown")          # pre-marker state
        self.assertEqual(_build.compare(loaded, path + ".gone")[0], "unknown")  # unreadable disk

    def test_every_daemon_carries_a_marker_for_its_own_file(self):
        for name, mod in DAEMONS.items():
            self.assertEqual(mod.BUILD["src"], _build.source_hash(mod.__file__), name)


class SavedWithState(unittest.TestCase):
    """The marker reaches the state file, so the watchdog can read it without asking the process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _saved(self, mod, state):
        with mock.patch.object(mod, "STATE_DIR", self.tmp):
            mod.save_state("p", state)
        with open(os.path.join(self.tmp, "p.json")) as fh:
            return json.load(fh)

    def test_github(self):
        saved = self._saved(github_wake, {"since": "t", "seen": {"a"}, "prs": {}, "wf_last": {}})
        self.assertEqual(saved["build"], github_wake.BUILD)

    def test_jira(self):
        self.assertEqual(self._saved(jira_wake, {"since": "t", "seen": set()})["build"], jira_wake.BUILD)

    def test_bitbucket(self):
        saved = self._saved(bitbucket_wake, {"since": "t", "seen": set(), "prs": {}})
        self.assertEqual(saved["build"], bitbucket_wake.BUILD)

    def test_inbox(self):
        self.assertEqual(self._saved(inbox_wake, {"since": "t", "seen": set()})["build"], inbox_wake.BUILD)

    def test_schedule(self):
        saved = self._saved(schedule_wake, {"fired": {}})
        self.assertEqual(saved["build"], schedule_wake.BUILD)
        self.assertEqual(saved["fired"], {})


class WatchdogFleet(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.daemons = os.path.join(self.tmp, "daemons")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.daemons)
        self.disk = os.path.join(self.daemons, "github_wake.py")
        with open(self.disk, "w") as fh:
            fh.write("# v1\n")

    def _state(self, build):
        os.makedirs(os.path.join(self.state, "github"), exist_ok=True)
        with open(os.path.join(self.state, "github", "acme.json"), "w") as fh:
            json.dump({"since": "t", "seen": [], "prs": {}, "wf_last": {}, "build": build}, fh)

    def test_current_when_loaded_matches_disk(self):
        self._state({"src": _build.source_hash(self.disk), "head": "abc1234"})
        with mock.patch.object(wd, "STATE_ROOT", self.state):
            status, loaded, disk, head = wd.build_status("acme", "github", self.daemons)
        self.assertEqual((status, head), ("current", "abc1234"))
        self.assertEqual(loaded, disk)

    def test_outdated_when_disk_moved_on(self):
        self._state({"src": _build.source_hash(self.disk), "head": "abc1234"})
        with open(self.disk, "w") as fh:
            fh.write("# v2 — merged, not yet reloaded\n")
        with mock.patch.object(wd, "STATE_ROOT", self.state):
            status, loaded, disk, _ = wd.build_status("acme", "github", self.daemons)
        self.assertEqual(status, "outdated")
        self.assertNotEqual(loaded, disk)

    def test_unknown_for_a_pre_marker_state_and_for_no_state(self):
        with mock.patch.object(wd, "STATE_ROOT", self.state):
            self.assertEqual(wd.build_status("acme", "github", self.daemons)[0], "unknown")
            self._state(None)
            self.assertEqual(wd.build_status("acme", "github", self.daemons)[0], "unknown")

    def test_fleet_report_answers_per_profile_and_source(self):
        self._state({"src": _build.source_hash(self.disk), "head": "abc1234"})
        config = {"profiles": {"acme": {}}}
        with mock.patch.object(wd, "STATE_ROOT", self.state), \
                mock.patch.object(wd, "OWN_BUILD", os.path.join(self.state, "watchdog", "build.json")):
            lines = wd.fleet_report(config, self.daemons)
        # + two host-scoped rows: the watchdog itself and the checkout
        self.assertEqual(len(lines), len(wd.SOURCES) + 2)
        self.assertTrue(lines[-2].startswith("host/watchdog: "), lines[-2])
        self.assertTrue(lines[-1].startswith("host/checkout: "), lines[-1])
        self.assertTrue(lines[0].startswith("acme/github: current loaded="), lines[0])
        self.assertIn("acme/jira: unknown", "\n".join(lines))


class WatchdogEmits(unittest.TestCase):
    """An outdated daemon is a typed event, once per disk revision, with a recovery line."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_outdated_emits_once_then_recovers(self):
        emitted = []
        config = {"profiles": {"acme": {"github": {"repos": ["x/y"]}}}, "watchdog": {"stale_minutes": 10}}
        statuses = iter([("outdated", "aaaa", "bbbb", "h1"), ("outdated", "aaaa", "bbbb", "h1"),
                         ("current", "bbbb", "bbbb", "h2")])
        sleeps = []

        def fake_sleep(_):
            sleeps.append(1)
            if len(sleeps) == 3:
                raise KeyboardInterrupt

        with mock.patch.object(wd, "CONFIG_PATH", self._write_config(config)), \
                mock.patch.object(wd, "OWN_BUILD", os.path.join(self.tmp, "build.json")), \
                mock.patch.object(wd, "STATE_DIR", os.path.join(self.tmp, "watchdog")), \
                mock.patch.object(wd, "loaded_labels", lambda: None), \
                mock.patch.object(wd, "classify", lambda *a, **k: ("ok", "")), \
                mock.patch.object(wd, "build_status", lambda p, s, d=None: next(statuses)), \
                mock.patch.object(wd, "emit", lambda profile, line: emitted.append(line)), \
                mock.patch.object(wd, "load_episodes", lambda: {}), \
                mock.patch.object(wd, "save_episodes", lambda e: None), \
                mock.patch.object(wd, "SOURCES", {"github": wd.SOURCES["github"]}), \
                mock.patch.object(wd, "checkout_status", lambda d=None: {"state": "current", "fetched": 0}), \
                mock.patch.object(wd.time, "sleep", fake_sleep):
            with self.assertRaises(KeyboardInterrupt):
                wd.main()
        self.assertEqual(emitted, [
            "type=fleet_outdated source=github loaded=aaaa disk=bbbb head=h1",
            "type=fleet_current source=github src=bbbb",
        ])

    def _write_config(self, config):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as fh:
            json.dump(config, fh)
        return path




class WatchdogWatchesItself(unittest.TestCase):
    """The reporter is in the fleet too — a stale watchdog must not read as a current fleet."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.daemons = os.path.join(self.tmp, "daemons")
        os.makedirs(self.daemons)
        self.disk = os.path.join(self.daemons, "watchdog_wake.py")
        with open(self.disk, "w") as fh:
            fh.write("# watchdog v1\n")
        self.own = os.path.join(self.tmp, "watchdog", "build.json")

    def test_records_its_own_marker_on_start(self):
        with mock.patch.object(wd, "OWN_BUILD", self.own):
            wd.record_own_build()
        with open(self.own) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["build"], wd.BUILD)

    def test_own_row_current_outdated_unknown(self):
        with mock.patch.object(wd, "OWN_BUILD", self.own):
            self.assertEqual(wd.watchdog_build_status(self.daemons)[0], "unknown")  # never started
            with mock.patch.object(wd, "BUILD", {"src": _build.source_hash(self.disk), "head": "h"}):
                wd.record_own_build()
            self.assertEqual(wd.watchdog_build_status(self.daemons)[0], "current")
            with open(self.disk, "w") as fh:
                fh.write("# watchdog v2 — merged, not reloaded\n")
            self.assertEqual(wd.watchdog_build_status(self.daemons)[0], "outdated")

    def test_fleet_report_shows_the_watchdog_once_per_host_not_per_profile(self):
        config = {"profiles": {"a": {}, "b": {}}}
        with mock.patch.object(wd, "OWN_BUILD", self.own), mock.patch.object(wd, "STATE_ROOT", self.tmp):
            lines = wd.fleet_report(config, self.daemons)
        self.assertEqual(sum(1 for line in lines if line.startswith("host/watchdog:")), 1)

    def test_main_reports_itself_outdated_from_memory(self):
        """BUILD (loaded bytes) vs the file on disk — no state file involved, no restart needed."""
        emitted = []
        config = {"profiles": {"acme": {"github": {"repos": ["x/y"]}}}}
        with open(self.disk, "w") as fh:
            fh.write("# watchdog v2 on disk\n")

        def stop(_):
            raise KeyboardInterrupt

        with mock.patch.object(wd, "CONFIG_PATH", self._config(config)), \
                mock.patch.object(wd, "OWN_BUILD", self.own), \
                mock.patch.object(wd, "STATE_DIR", os.path.dirname(self.own)), \
                mock.patch.object(wd, "DAEMONS_DIR", self.daemons), \
                mock.patch.object(wd, "BUILD", {"src": "oldoldoldold", "head": "h1"}), \
                mock.patch.object(wd, "loaded_labels", lambda: None), \
                mock.patch.object(wd, "classify", lambda *a, **k: ("ok", "")), \
                mock.patch.object(wd, "build_status", lambda p, s, d=None: ("current", "x", "x", "h")), \
                mock.patch.object(wd, "emit", lambda profile, line: emitted.append(line)), \
                mock.patch.object(wd, "load_episodes", lambda: {}), \
                mock.patch.object(wd, "save_episodes", lambda e: None), \
                mock.patch.object(wd, "SOURCES", {"github": wd.SOURCES["github"]}), \
                mock.patch.object(wd, "checkout_status", lambda d=None: {"state": "current", "fetched": 0}), \
                mock.patch.object(wd.time, "sleep", stop):
            with self.assertRaises(KeyboardInterrupt):
                wd.main()
        self.assertEqual(emitted, ["type=fleet_outdated source=watchdog loaded=oldoldoldold disk=%s head=h1"
                                   % _build.source_hash(self.disk)])

    def _run_main_one_cycle(self, stored, loaded_src):
        """One watchdog cycle against the disk file; returns (emitted, saved episodes)."""
        emitted = []
        saved = []
        config = {"profiles": {"acme": {"github": {"repos": ["x/y"]}}}}

        def stop(_):
            raise KeyboardInterrupt

        with mock.patch.object(wd, "CONFIG_PATH", self._config(config)), \
                mock.patch.object(wd, "OWN_BUILD", self.own), \
                mock.patch.object(wd, "STATE_DIR", os.path.dirname(self.own)), \
                mock.patch.object(wd, "DAEMONS_DIR", self.daemons), \
                mock.patch.object(wd, "BUILD", {"src": loaded_src, "head": "h1"}), \
                mock.patch.object(wd, "loaded_labels", lambda: None), \
                mock.patch.object(wd, "classify", lambda *a, **k: ("ok", "")), \
                mock.patch.object(wd, "build_status", lambda p, s, d=None: ("current", "x", "x", "h")), \
                mock.patch.object(wd, "emit", lambda profile, line: emitted.append(line)), \
                mock.patch.object(wd, "load_episodes", lambda: dict(stored)), \
                mock.patch.object(wd, "save_episodes", lambda e: saved.append(dict(e))), \
                mock.patch.object(wd, "SOURCES", {"github": wd.SOURCES["github"]}), \
                mock.patch.object(wd, "checkout_status", lambda d=None: {"state": "current", "fetched": 0}), \
                mock.patch.object(wd.time, "sleep", stop):
            with self.assertRaises(KeyboardInterrupt):
                wd.main()
        return emitted, saved

    def test_main_closes_its_own_episode_when_current_again(self):
        """The stale self key left by an old episode closes, with a fleet_current line."""
        disk_hash = _build.source_hash(self.disk)
        emitted, saved = self._run_main_one_cycle(
            {"build:acme/watchdog": disk_hash}, disk_hash)
        self.assertEqual(emitted, ["type=fleet_current source=watchdog src=%s" % disk_hash])
        self.assertEqual(saved, [{}])

    def test_main_emits_nothing_for_itself_when_current_and_no_episode(self):
        """Control — a current watchdog with no self key stays silent and saves nothing."""
        disk_hash = _build.source_hash(self.disk)
        emitted, saved = self._run_main_one_cycle({}, disk_hash)
        self.assertEqual(emitted, [])
        self.assertEqual(saved, [])

    def _config(self, config):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as fh:
            json.dump(config, fh)
        return path


class CheckoutBehindOrigin(unittest.TestCase):
    """The THIRD staleness layer: origin -> checkout.

    The other two layers compare a running process against a file. Neither can
    see this one, and on one host all three were distinct at once: origin/main
    5bf8592 -> checkout 44ea263 -> processes 4be56a7. Every daemon read
    `current` while two closed tickets' fixes sat unexecuted.

    These build REAL git repositories in a temp dir with a local bare "origin".
    No network: a local bare repo is a legitimate remote, and `upstream_status`
    never fetches anyway — which is the property under test as much as the
    counts are.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin = os.path.join(self.tmp, "origin.git")
        self._run(["git", "init", "--bare", "--initial-branch=main", self.origin], cwd=self.tmp)
        self.work = self._clone("work")
        self._commit(self.work, "one.txt", "1")
        self._run(["git", "push", "-u", "origin", "main"], cwd=self.work)
        #: the file whose CHECKOUT is the subject — `upstream_status` takes a path
        #: inside the repo, exactly as the watchdog passes it its own module file.
        self.probe = os.path.join(self.work, "one.txt")

    def _run(self, argv, cwd):
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise AssertionError("%s failed: %s%s" % (" ".join(argv), out.stdout, out.stderr))
        return out.stdout.strip()

    def _clone(self, name):
        path = os.path.join(self.tmp, name)
        self._run(["git", "clone", self.origin, path], cwd=self.tmp)
        self._run(["git", "config", "user.email", "t@example.invalid"], cwd=path)
        self._run(["git", "config", "user.name", "t"], cwd=path)
        return path

    def _commit(self, repo, name, body):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(body + "\n")
        self._run(["git", "add", name], cwd=repo)
        self._run(["git", "commit", "-m", "add " + name], cwd=repo)

    def _push_from_a_second_clone(self, n=1):
        """Move origin/main forward WITHOUT touching self.work's HEAD."""
        other = self._clone("other")
        for i in range(n):
            self._commit(other, "from-other-%d.txt" % i, str(i))
        self._run(["git", "push", "origin", "main"], cwd=other)

    def test_current_when_the_checkout_is_the_upstream_ref(self):
        st = _build.upstream_status(self.probe)
        self.assertEqual(st["state"], "current")
        self.assertEqual((st["ahead"], st["behind"]), (0, 0))
        self.assertEqual(st["ref"], "refs/remotes/origin/main")

    def test_behind_after_origin_moves_and_this_checkout_fetches(self):
        self._push_from_a_second_clone(n=2)
        self._run(["git", "fetch", "origin"], cwd=self.work)
        st = _build.upstream_status(self.probe)
        self.assertEqual(st["state"], "behind")
        self.assertEqual(st["behind"], 2)
        self.assertEqual(st["ahead"], 0)
        self.assertNotEqual(st["head"], st["upstream"])

    def test_a_checkout_that_never_fetched_cannot_see_origin_move(self):
        """The honest limit, pinned so nobody reads more freshness into it than it has.

        `upstream_status` is fetch-free by design — the watchdog makes no network
        calls. So origin moving is INVISIBLE until something fetches, and the
        verdict stays `current`. That is why the fetch age travels with every
        rendering: `current` is only ever "current with the origin I last heard
        about".
        """
        self._push_from_a_second_clone(n=1)
        st = _build.upstream_status(self.probe)          # no fetch in between
        self.assertEqual(st["state"], "current")
        self._run(["git", "fetch", "origin"], cwd=self.work)
        self.assertEqual(_build.upstream_status(self.probe)["state"], "behind")

    def test_diverged_when_each_side_has_commits_the_other_lacks(self):
        self._push_from_a_second_clone(n=1)
        self._commit(self.work, "local.txt", "local")
        self._run(["git", "fetch", "origin"], cwd=self.work)
        st = _build.upstream_status(self.probe)
        self.assertEqual(st["state"], "diverged")
        self.assertEqual((st["ahead"], st["behind"]), (1, 1))

    def test_ahead_alone_is_current_not_a_complaint(self):
        """An unpushed local commit is not a stale checkout — nothing to pull."""
        self._commit(self.work, "local.txt", "local")
        st = _build.upstream_status(self.probe)
        self.assertEqual(st["state"], "current")
        self.assertEqual((st["ahead"], st["behind"]), (1, 0))

    def test_unknown_outside_a_repo_is_never_an_accusation(self):
        st = _build.upstream_status(os.path.join(self.tmp, "not-a-repo", "x.py"))
        self.assertEqual(st["state"], "unknown")
        self.assertIsNone(st["behind"])

    def test_a_checkout_with_no_remote_at_all_is_unknown_not_current(self):
        """The second of the five `unknown` exits, on a real repository.

        `@{u}` fails and `origin/main` does not exist, so there is nothing to
        compare against and the only honest answer is `unknown`. What it must
        not be is `current` — see `EveryUnknownExitStaysUnknown` for what a
        false `current` costs one layer up.
        """
        lone = os.path.join(self.tmp, "lone")
        os.makedirs(lone)
        self._run(["git", "init", "--initial-branch=main", "."], cwd=lone)
        self._run(["git", "config", "user.email", "t@example.invalid"], cwd=lone)
        self._run(["git", "config", "user.name", "t"], cwd=lone)
        self._commit(lone, "only.txt", "x")
        st = _build.upstream_status(os.path.join(lone, "only.txt"))
        self.assertEqual(st["state"], "unknown")
        self.assertEqual(st["why"], "no upstream ref")
        self.assertIsNone(st["behind"])

    def test_the_upstream_ref_is_the_TRACKED_branch_not_always_origin_main(self):
        """Ref selection — the sibling gap to the `unknown` exits.

        Every other fixture here tracks `origin/main`, so an implementation that
        ignored `@{u}` and always compared against `origin/main` passed all of
        them. This one separates the two answers: `origin/main` moves ahead
        while `origin/side`, the branch actually tracked, does not. Reading the
        tracked branch gives `current`; reading the fleet convention would give
        `behind=1`.
        """
        self._run(["git", "checkout", "-b", "side"], cwd=self.work)
        self._run(["git", "push", "-u", "origin", "side"], cwd=self.work)
        self._push_from_a_second_clone(n=1)
        self._run(["git", "fetch", "origin"], cwd=self.work)
        st = _build.upstream_status(self.probe)
        self.assertEqual(st["ref"], "refs/remotes/origin/side")
        self.assertEqual(st["state"], "current")
        self.assertEqual((st["ahead"], st["behind"]), (0, 0))

    def test_a_DETACHED_head_falls_back_to_origin_main_and_really_compares(self):
        """The other half of ref selection, and the one that fired on the reviewer's host.

        A detached checkout has no `@{u}`. Answering `unknown` there would be
        silent about a checkout that plainly has an origin, so `origin/main` is
        named explicitly as the fleet convention. The fallback has to produce a
        real comparison, though, not merely a non-empty `ref` string — so this
        asserts the count, which is the part a cosmetic fallback would miss.
        """
        self._push_from_a_second_clone(n=1)
        self._run(["git", "fetch", "origin"], cwd=self.work)
        self._run(["git", "checkout", "--detach", "HEAD"], cwd=self.work)
        st = _build.upstream_status(self.probe)
        self.assertEqual(st["ref"], "refs/remotes/origin/main")
        self.assertEqual(st["state"], "behind")
        self.assertEqual(st["behind"], 1)

    def test_it_never_runs_git_fetch(self):
        """The property that keeps the watchdog credential-free and offline.

        Asserted against the ARGV of every git call, not against the verdict —
        a test that only checked the counts would pass just as well on an
        implementation that fetched first.
        """
        seen = []
        real = subprocess.run

        def spy(argv, *a, **k):
            seen.append(list(argv))
            return real(argv, *a, **k)

        self._push_from_a_second_clone(n=1)
        self._run(["git", "fetch", "origin"], cwd=self.work)
        with mock.patch.object(_build.subprocess, "run", spy):
            self.assertEqual(_build.upstream_status(self.probe)["state"], "behind")
        self.assertTrue(seen, "no git calls were observed at all")
        for argv in seen:
            self.assertNotIn("fetch", argv, argv)
            self.assertNotIn("pull", argv, argv)
            self.assertNotIn("remote", argv, argv)

    def test_the_age_is_the_LAST_FETCH_and_is_labelled_as_such(self):
        """The honest bound, pinned — including what it does NOT claim.

        Git records when a fetch happened, never when a particular ref was last
        CONFIRMED. An earlier version of this read FETCH_HEAD's CONTENT for the
        branch name to close that gap, and review took it apart three ways, each
        correct: the entry does not name the REMOTE (a fetch from a fork
        matched), a `--append` fetch RETAINS old entries under a new mtime (an
        untouched ref looked fresh), and a slash-separated branch was truncated
        (a genuinely fresh fetch read `never`).

        So the value is labelled `last-fetch`, and it puts a FLOOR under
        staleness and no ceiling on it: large proves the ref is unconfirmed at
        least that long, small proves nothing. This test pins the label and the
        direction, since the label IS the claim a reader will act on.
        """
        self._run(["git", "push", "origin", "main:side"], cwd=self.work)
        self._push_from_a_second_clone(n=1)
        self._run(["git", "fetch", "origin", "side"], cwd=self.work)   # NOT main
        st = _build.upstream_status(self.probe)
        rendered = _build.render_upstream(st)
        self.assertIn("last-fetch=", rendered, "the label is the claim: %s" % rendered)
        self.assertNotIn("fetched=", rendered.replace("last-fetch=", ""),
                         "nothing here may read as 'this ref is fresh'")
        # A fetch DID happen, so the age is small — and that is exactly the case
        # the label refuses to over-claim: origin/main has still not been
        # confirmed, and the state below proves it.
        self.assertIsNotNone(st["fetched"])
        self.assertEqual(st["state"], "current", "origin/main was not refreshed, so it looks current")
        self._run(["git", "fetch", "origin", "main"], cwd=self.work)
        self.assertEqual(_build.upstream_status(self.probe)["state"], "behind")

    def test_a_LINKED_WORKTREE_sees_a_fetch_run_in_THE_MAIN_CHECKOUT(self):
        """codex: worktrees SHARE remote-tracking refs and each keeps its OWN
        FETCH_HEAD, so a fetch in the main checkout refreshes the refs a linked
        worktree compares against while leaving that worktree's FETCH_HEAD
        untouched. Reading only the worktree's file reported `never` for a ref
        that had just been confirmed — a staleness warning nobody had earned,
        and one of this repository's own worktrees reproduced it.

        Newest-across-the-repository is sound BECAUSE the value is a
        one-directional bound: if the most recent fetch anywhere was nine days
        ago, the ref is unconfirmed at least nine days.
        """
        linked = os.path.join(self.tmp, "linked")
        self._run(["git", "worktree", "add", "--detach", linked, "HEAD"], cwd=self.work)
        self.addCleanup(lambda: self._run(["git", "worktree", "prune"], cwd=self.work))
        probe = os.path.join(linked, "one.txt")
        self._push_from_a_second_clone(n=1)
        self._run(["git", "fetch", "origin", "main"], cwd=self.work)     # in the MAIN checkout
        st = _build.upstream_status(probe)
        self.assertIsNotNone(st["fetched"], "the main checkout's fetch must be visible here")
        self.assertLess(st["fetched"], 120)
        self.assertNotIn("stale-fetch", _build.render_upstream(st))

    def test_a_LINKED_WORKTREE_also_sees_its_own_fetch(self):
        """The other direction, kept: the worktree's own FETCH_HEAD counts too."""
        linked = os.path.join(self.tmp, "linked2")
        self._run(["git", "worktree", "add", "--detach", linked, "HEAD"], cwd=self.work)
        self.addCleanup(lambda: self._run(["git", "worktree", "prune"], cwd=self.work))
        self._push_from_a_second_clone(n=1)
        self._run(["git", "fetch", "origin", "main"], cwd=linked)
        st = _build.upstream_status(os.path.join(linked, "one.txt"))
        self.assertIsNotNone(st["fetched"])
        self.assertLess(st["fetched"], 120)

    def test_the_NEWEST_fetch_in_the_repository_wins_not_the_oldest(self):
        """Which way the aggregation goes is the whole point, and it survived a
        mutation until this test existed: with only one recent fetch in play,
        newest and oldest are the same number.

        Here the worktree fetched ten days ago and the main checkout fetched
        just now. Newest is the sound answer — the refs they SHARE were
        confirmed a moment ago — and taking the oldest would raise a
        `stale-fetch` on a ref that is demonstrably fresh.
        """
        linked = os.path.join(self.tmp, "linked3")
        self._run(["git", "worktree", "add", "--detach", linked, "HEAD"], cwd=self.work)
        self.addCleanup(lambda: self._run(["git", "worktree", "prune"], cwd=self.work))
        probe = os.path.join(linked, "one.txt")
        self._push_from_a_second_clone(n=1)
        self._run(["git", "fetch", "origin", "main"], cwd=linked)      # the worktree's own...
        wt_head = os.path.join(
            self._run(["git", "rev-parse", "--absolute-git-dir"], cwd=linked), "FETCH_HEAD")
        old = time.time() - 10 * 86400
        os.utime(wt_head, (old, old))                                  # ...aged ten days
        self._run(["git", "fetch", "origin", "main"], cwd=self.work)   # main checkout, just now

        st = _build.upstream_status(probe)
        self.assertIsNotNone(st["fetched"])
        self.assertLess(st["fetched"], 120, "the newest fetch in the repository is the answer")
        self.assertNotIn("stale-fetch", _build.render_upstream(st))

    def test_a_clean_row_over_a_STALE_comparison_reads_current_unverified(self):
        """The asymmetry applies to the VERDICT, not only to the number
        (claude-pm-acme, from the consumer side).

        `behind` is proof and a reader should act on it. `current` only says the
        checkout matches the ref AS OF THE LAST FETCH — so on a host nobody has
        fetched in a week it is a clean row over a week-old comparison, and it
        would otherwise render identically to a genuinely current checkout. That
        is this ticket's own defect one layer out: a green light that cannot mean
        what it looks like. Encoding it in the row means every reader inherits
        it rather than deriving it, and the digest does not have to be told.

        The DICT keeps `state="current"` so the daemon's recovery logic is
        untouched; only what a human reads is qualified.
        """
        base = {"state": "current", "head": "aaaaaaa", "ref": "refs/remotes/origin/main",
                "upstream": "aaaaaaa", "ahead": 0, "behind": 0}
        self.assertIn("state=current ", _build.render_upstream(dict(base, fetched=300.0)))
        for stale in (_build.STALE_FETCH_SECS + 1, None):
            rendered = _build.render_upstream(dict(base, fetched=stale))
            self.assertIn("state=current-unverified", rendered, rendered)
        # `behind` is PROOF and must never be softened by a stale fetch.
        behind = _build.render_upstream(
            dict(base, state="behind", behind=2, fetched=_build.STALE_FETCH_SECS + 1))
        self.assertIn("state=behind ", behind)
        self.assertNotIn("unverified", behind)
        # and the dict is untouched, so the emit logic cannot drift with the prose
        st = dict(base, fetched=None)
        _build.render_upstream(st)
        self.assertEqual(st["state"], "current")

    def test_render_carries_the_last_fetch_and_marks_a_stale_one(self):
        fresh = {"state": "current", "head": "aaaaaaa", "ref": "refs/remotes/origin/main",
                 "upstream": "aaaaaaa", "ahead": 0, "behind": 0, "fetched": 300.0}
        self.assertIn("last-fetch=5m", _build.render_upstream(fresh))
        self.assertNotIn("stale-fetch", _build.render_upstream(fresh))
        self.assertIn("ref=origin/main", _build.render_upstream(fresh))

        stale = dict(fresh, fetched=_build.STALE_FETCH_SECS + 1)
        self.assertIn("stale-fetch", _build.render_upstream(stale))
        never = dict(fresh, fetched=None)
        self.assertIn("last-fetch=never", _build.render_upstream(never))
        self.assertIn("stale-fetch", _build.render_upstream(never))


class WatchdogReportsTheCheckout(unittest.TestCase):
    """A checkout behind origin needs a PULL; `fleet_outdated` needs a RESTART.

    Different remedies, so different events. A host can need both at once, and
    one host did.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _config(self, config):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as fh:
            json.dump(config, fh)
        return path

    def _main_with(self, states, initial=None, times=None, renotify_minutes=60):
        """Run `main` over `states`, one per cycle, returning the emitted lines."""
        emitted = []
        it = iter(states)
        config = {"profiles": {"acme": {"github": {"repos": ["x/y"]}}},
                  "watchdog": {"renotify_minutes": renotify_minutes}}
        episodes = copy.deepcopy(initial or {})
        base = 1789500000.0
        times = list(times or [base + i * wd.POLL_SECS for i in range(len(states))])
        clock = iter([times[0] - wd.STARTUP_GRACE_SECS - 1] + times)
        #: the store itself is an assertion target, not just the emitted lines:
        #: a FALSE recovery is two harms, and only one of them is an emit.
        self.episodes = episodes
        #: the NOTE is the operator-facing half and names the remedy, so it is
        #: an assertion target too — and capturing it keeps the suite quiet.
        self.notes = notes = []

        cycles = []

        def sleeper(_):
            cycles.append(1)
            if len(cycles) >= len(states):
                raise KeyboardInterrupt

        with mock.patch.object(wd, "CONFIG_PATH", self._config(config)), \
                mock.patch.object(wd, "OWN_BUILD", os.path.join(self.tmp, "build.json")), \
                mock.patch.object(wd, "STATE_DIR", os.path.join(self.tmp, "watchdog")), \
                mock.patch.object(wd, "loaded_labels", lambda: None), \
                mock.patch.object(wd, "classify", lambda *a, **k: ("ok", "")), \
                mock.patch.object(wd, "build_status", lambda p, s, d=None: ("current", "x", "x", "h")), \
                mock.patch.object(wd, "_build", mock.Mock(
                    compare=lambda *a: ("current", "x"),
                    render_upstream=_build.render_upstream,
                    stale_fetch_bound_exceeded=_build.stale_fetch_bound_exceeded)), \
                mock.patch.object(wd, "checkout_status", lambda d=None: next(it)), \
                mock.patch.object(wd, "emit", lambda profile, line: emitted.append(line)), \
                mock.patch.object(wd, "note", lambda line: notes.append(line)), \
                mock.patch.object(wd, "load_episodes", lambda: episodes), \
                mock.patch.object(wd, "save_episodes", lambda e: None), \
                mock.patch.object(wd, "SOURCES", {"github": wd.SOURCES["github"]}), \
                mock.patch.object(wd.time, "time", lambda: next(clock)), \
                mock.patch.object(wd.time, "sleep", sleeper):
            with self.assertRaises(KeyboardInterrupt):
                wd.main()
        return emitted

    BEHIND = {"state": "behind", "head": "aaaaaaa", "ref": "refs/remotes/origin/main",
              "upstream": "bbbbbbb", "ahead": 0, "behind": 2, "fetched": 60.0}
    CURRENT = {"state": "current", "head": "bbbbbbb", "ref": "refs/remotes/origin/main",
               "upstream": "bbbbbbb", "ahead": 0, "behind": 0, "fetched": 60.0}
    AHEAD_CURRENT = dict(CURRENT, head="ccccccc", ahead=1)
    STALE_CURRENT = dict(CURRENT, fetched=_build.STALE_FETCH_SECS)
    NEVER_FETCHED_CURRENT = dict(CURRENT, fetched=None)

    def test_behind_emits_once_per_upstream_revision_then_catches_up(self):
        emitted = self._main_with([self.BEHIND, self.BEHIND, self.CURRENT])
        self.assertEqual(len(emitted), 2, emitted)
        self.assertTrue(emitted[0].startswith("type=fleet_behind source=checkout "), emitted[0])
        self.assertIn("behind=2", emitted[0])
        self.assertIn("last-fetch=1m", emitted[0])
        self.assertIn("checkout behind recorded upstream by 2 commit(s) \u2014 pull", "\n".join(self.notes))
        self.assertTrue(emitted[1].startswith("type=fleet_caught_up source=checkout "), emitted[1])

    def test_a_new_upstream_revision_re_emits(self):
        further = dict(self.BEHIND, upstream="ccccccc", behind=5)
        emitted = self._main_with([self.BEHIND, further])
        self.assertEqual(len(emitted), 2, emitted)
        self.assertIn("behind=2", emitted[0])
        self.assertIn("behind=5", emitted[1])

    def test_a_standing_behind_condition_re_emits_at_the_existing_cadence(self):
        base = 1789500000.0
        emitted = self._main_with(
            [self.BEHIND, self.BEHIND, self.BEHIND],
            times=[base, base + 3599, base + 3600])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_behind", "type=fleet_behind_still"])
        self.assertEqual(emitted[0].split("since=", 1)[1].split()[0],
                         emitted[1].split("since=", 1)[1].split()[0])

    def test_the_configured_cadence_is_used_not_a_hard_coded_hour(self):
        base = 1789500000.0
        emitted = self._main_with(
            [self.BEHIND, self.BEHIND, self.BEHIND], renotify_minutes=5,
            times=[base, base + 299, base + 300])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_behind", "type=fleet_behind_still"])

    def test_persisted_topology_episode_stays_quiet_inside_the_cadence(self):
        base = 1789500000.0
        key = "upstream:acme/checkout"
        initial = {key: {"state": "behind", "upstream": "bbbbbbb",
                         "first": base - 600, "last_emit": base - 60}}
        self.assertEqual(self._main_with([self.BEHIND], initial=initial, times=[base]), [])
        self.assertEqual(self.episodes, initial)

    def test_a_standing_diverged_condition_re_emits_at_the_existing_cadence(self):
        base = 1789500000.0
        emitted = self._main_with(
            [self.DIVERGED, self.DIVERGED], times=[base, base + 3600])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_behind", "type=fleet_behind_still"])
        self.assertTrue(all("state=diverged" in line for line in emitted))

    def test_a_changed_topology_is_new_evidence_even_inside_the_cadence(self):
        base = 1789500000.0
        emitted = self._main_with(
            [self.BEHIND, self.DIVERGED], times=[base, base + wd.POLL_SECS])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_behind", "type=fleet_behind"])

    def test_stale_fetch_predicate_and_rendering_share_the_exact_boundary(self):
        fresh = dict(self.CURRENT, fetched=_build.STALE_FETCH_SECS - 1)
        stale = dict(self.CURRENT, fetched=_build.STALE_FETCH_SECS)
        never = dict(self.CURRENT, fetched=None)
        for status, expected in ((fresh, False), (stale, True), (never, True),
                                 (dict(self.BEHIND, fetched=_build.STALE_FETCH_SECS), False),
                                 (dict(self.DIVERGED, fetched=_build.STALE_FETCH_SECS), False),
                                 (dict(self.UNKNOWN, fetched=_build.STALE_FETCH_SECS), False)):
            before = dict(status)
            self.assertEqual(_build.stale_fetch_bound_exceeded(status), expected)
            self.assertEqual("state=current-unverified" in _build.render_upstream(status), expected)
            self.assertEqual(status, before, "classification/rendering must not mutate the verdict")

    def test_stale_current_wakes_and_repeats_without_poll_loop_noise(self):
        base = 1789500000.0
        emitted = self._main_with(
            [self.STALE_CURRENT, self.STALE_CURRENT, self.STALE_CURRENT],
            times=[base, base + 3599, base + 3600])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_fetch_stale", "type=fleet_fetch_stale_still"])
        self.assertTrue(all("state=current-unverified" in line for line in emitted))
        self.assertTrue(all("reason=stale-fetch-bound" in line for line in emitted))

    def test_persisted_stale_fetch_episode_stays_quiet_inside_the_cadence(self):
        base = 1789500000.0
        key = "fetch-bound:acme/checkout"
        initial = {key: {"state": "stale", "upstream": "bbbbbbb",
                         "first": base - 600, "last_emit": base - 60}}
        self.assertEqual(
            self._main_with([self.STALE_CURRENT], initial=initial, times=[base]), [])
        self.assertEqual(self.episodes, initial)

    def test_never_fetched_current_is_unverified_and_recent_current_is_silent(self):
        emitted = self._main_with([self.NEVER_FETCHED_CURRENT])
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0].startswith("type=fleet_fetch_stale source=checkout "))
        self.assertIn("last-fetch=never", emitted[0])
        self.assertIn("reason=no-fetch-record", emitted[0])
        fresh = dict(self.CURRENT, fetched=_build.STALE_FETCH_SECS - 1)
        self.assertEqual(self._main_with([fresh]), [])

    def test_an_unrelated_fetch_clears_only_the_measurable_stale_bound(self):
        """c15635: a small repository-wide age need not mean this ref was fetched."""
        emitted = self._main_with([self.STALE_CURRENT, self.CURRENT])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_fetch_stale", "type=fleet_fetch_stale_cleared"])
        self.assertIn("reason=stale-bound-no-longer-proven", emitted[1])
        self.assertIn("tracked-ref freshness remains unestablished", "\n".join(self.notes))

    def test_a_stale_current_closes_the_known_gap_but_opens_uncertainty(self):
        emitted = self._main_with([self.BEHIND, self.STALE_CURRENT])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_behind", "type=fleet_caught_up", "type=fleet_fetch_stale"])
        self.assertIn("state=current-unverified", emitted[1])
        self.assertNotIn("caught up with origin", "\n".join(self.notes))
        self.assertNotIn("upstream:acme/checkout", self.episodes)
        self.assertIn("fetch-bound:acme/checkout", self.episodes)

    def test_observed_gap_supersedes_stale_bound_without_a_fake_clear(self):
        emitted = self._main_with([self.STALE_CURRENT, self.BEHIND])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_fetch_stale", "type=fleet_behind"])
        self.assertNotIn("fetch-bound:acme/checkout", self.episodes)
        self.assertIn("upstream:acme/checkout", self.episodes)

    def test_legacy_sha_episode_reannounces_then_migrates(self):
        key = "upstream:acme/checkout"
        emitted = self._main_with([self.BEHIND], initial={key: "bbbbbbb"})
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0].startswith("type=fleet_behind_still source=checkout "))
        self.assertEqual(self.episodes[key]["upstream"], "bbbbbbb")
        self.assertEqual(self.episodes[key]["state"], "behind")

    def test_legacy_sha_episode_still_recovers_and_other_values_are_untouched(self):
        key = "upstream:acme/checkout"
        initial = {key: "bbbbbbb", "other/source": 123.0, "build:x": "abcdef"}
        emitted = self._main_with([self.CURRENT], initial=initial)
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0].startswith("type=fleet_caught_up source=checkout "))
        self.assertNotIn(key, self.episodes)
        self.assertEqual(self.episodes["other/source"], 123.0)
        self.assertEqual(self.episodes["build:x"], "abcdef")

    def test_unknown_says_nothing_at_all(self):
        """And the STATE is what suppresses it, not a None sneaking through the guard.

        The first fixture here is deliberately the awkward shape: `rev-list`
        failed AFTER the upstream ref was read, so `upstream` is populated while
        `state` is unknown. An earlier version of this test used the all-None
        shape and passed for the wrong reason — the episode guard compared None
        to None and suppressed the emit whatever the state check did. A mutation
        that made unknown emit like behind survived it.
        """
        partial = {"state": "unknown", "head": "aaaaaaa", "ref": "refs/remotes/origin/main",
                   "upstream": "bbbbbbb", "ahead": None, "behind": None,
                   "fetched": 60.0, "why": "rev-list failed"}
        self.assertEqual(self._main_with([partial, partial]), [])

        bare = {"state": "unknown", "head": None, "ref": None, "upstream": None,
                "ahead": None, "behind": None, "fetched": None, "why": "not a git checkout"}
        self.assertEqual(self._main_with([bare, bare]), [])

    def test_a_current_checkout_that_was_never_behind_stays_silent(self):
        self.assertEqual(self._main_with([self.CURRENT, self.CURRENT]), [])

    DIVERGED = {"state": "diverged", "head": "aaaaaaa", "ref": "refs/remotes/origin/main",
                "upstream": "bbbbbbb", "ahead": 1, "behind": 2, "fetched": 60.0}
    UNKNOWN = {"state": "unknown", "head": "aaaaaaa", "ref": "refs/remotes/origin/main",
               "upstream": "bbbbbbb", "ahead": None, "behind": None,
               "fetched": 60.0, "why": "rev-list failed"}

    def test_diverged_pages_at_the_same_gate_but_names_a_DIFFERENT_remedy(self):
        """`diverged` was in the emit gate and in no test.

        It is not a hypothetical state: the reviewer's own worktree was in it
        while reviewing this branch. A gate reading `("behind",)` instead of
        `("behind", "diverged")` passed the whole suite.

        It shares the EVENT with `behind` — both mean this host is not running
        what origin has — but not the REMEDY, and the operator-facing note is
        where that distinction has to survive. A diverged checkout cannot be
        fast-forwarded, so "pull" names an action that will merge or conflict.
        """
        emitted = self._main_with([self.DIVERGED, self.DIVERGED])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertTrue(emitted[0].startswith("type=fleet_behind source=checkout "), emitted[0])
        self.assertIn("state=diverged", emitted[0])
        self.assertIn("ahead=1", emitted[0])
        #: and the operator-facing line names the right remedy: a diverged
        #: checkout cannot be fast-forwarded, so "pull" would be the wrong verb.
        line = [l for l in self.notes if "checkout" in l][0]
        self.assertIn("diverged from recorded upstream", line)
        self.assertIn("1 ahead, 2 behind", line)
        self.assertNotIn("pull", line)

    def test_unknown_does_not_CLOSE_an_episode_that_is_already_open(self):
        """The half `test_unknown_says_nothing_at_all` cannot reach, and the worse half.

        That test starts with an EMPTY episode store, so `ukey in episodes` is
        false on every cycle and the recovery branch is unreachable in it
        whatever the state does. It therefore pins that `unknown` never OPENS an
        episode, and says nothing about whether `unknown` CLOSES one.

        Closing is the harm that reaches the digest. `fleet_caught_up` deletes
        the entry, and the watchers-dark window in the morning digest is sourced
        from exactly these entries — so a window correctly opened and then
        falsely closed does not merely stop alarming, it becomes unreportable
        retroactively, with nothing left to mark as unmeasured
        (claude-pm-acme, from the consumer side). Not alarming loses the
        present; deleting the episode fabricates the past.
        """
        emitted = self._main_with([self.BEHIND, self.UNKNOWN, self.UNKNOWN])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertTrue(emitted[0].startswith("type=fleet_behind source=checkout "), emitted[0])
        self.assertEqual(self.episodes["upstream:acme/checkout"]["upstream"], "bbbbbbb",
                         "the open episode was deleted by a verdict that knows nothing")

    def test_a_REAL_recovery_still_closes_the_episode(self):
        """The control for the test above: holding the episode open must not be
        achieved by never closing it. `current` is evidence and does close it."""
        emitted = self._main_with([self.BEHIND, self.CURRENT])
        self.assertEqual(len(emitted), 2, emitted)
        self.assertTrue(emitted[1].startswith("type=fleet_caught_up source=checkout "), emitted[1])
        self.assertNotIn("upstream:acme/checkout", self.episodes)

    def test_ahead_only_is_also_no_longer_a_gap_not_an_exact_match(self):
        emitted = self._main_with([self.BEHIND, self.AHEAD_CURRENT])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=fleet_behind", "type=fleet_caught_up"])
        self.assertIn("ahead=1", emitted[1])
        self.assertIn("no longer behind/diverged", "\n".join(self.notes))


class EveryUnknownExitStaysUnknown(unittest.TestCase):
    """A review finding — an `unknown` that degrades to `current` is a FALSE ALL-CLEAR.

    `upstream_status` has five exits that answer `unknown`. Four of them were
    reachable by no test at all, so each could be mutated into a silent
    `state="current"` with the entire suite green.

    The cost is not the wrong word in a log line. `watchdog_wake.main` treats
    `current` inside an OPEN episode as recovery: it emits `fleet_caught_up` and
    DELETES the episode. So such a mutant does not merely fail to alarm on a
    host that is behind — it erases the record that the host was ever behind,
    and the morning digest's watchers-dark window is sourced from exactly those
    episodes. Not alarming loses the present; deleting the episode fabricates
    the past, and no rendering-level qualifier recovers a deleted entry
    (claude-pm-acme, from the consumer side). `current-unverified` fixed
    the misleading present; this fixes the one that reaches history.

    These drive each exit through a stubbed `_git` rather than through a
    side-effect LIST, so what is pinned is the exit and not the order
    `upstream_status` happens to call git in today.
    """

    #: what a healthy repository answers. Every test below changes exactly one
    #: of these, so a failure names the exit rather than the fixture.
    HEALTHY = {"head": "a" * 40,
               "upstream_ref": "refs/remotes/origin/main",
               "origin_main": "refs/remotes/origin/main",
               "resolve": "b" * 40,
               "counts": "0\t0"}

    @staticmethod
    def _label(args):
        """Which question is this git call asking? Dispatch on argv, not on order."""
        if args[0] == "rev-list":
            return "counts"
        if args[0] != "rev-parse":
            raise AssertionError("unexpected git subcommand: %r" % (args,))
        if args[1:] == ("HEAD",):
            return "head"
        if "--symbolic-full-name" in args:
            return "upstream_ref"
        if "--verify" in args:
            return "origin_main"
        return "resolve"

    def _status(self, **overrides):
        answers = dict(self.HEALTHY, **overrides)
        seen = []

        def fake_git(path, *args):
            seen.append(args)
            return answers[self._label(args)]

        #: `_fetch_age` reads the filesystem and asks git its own question; it is
        #: not under test here and would otherwise trip the argv dispatch.
        with mock.patch.object(_build, "_git", fake_git), \
                mock.patch.object(_build, "_fetch_age", lambda path: 60.0):
            st = _build.upstream_status("/nowhere/at/all/x.py")
        self.seen = seen
        return st

    def test_the_stub_alone_reads_current_so_each_override_is_the_only_difference(self):
        """The positive control. Without it, every test below could be passing
        because the stub is broken rather than because the exit is guarded."""
        st = self._status()
        self.assertEqual(st["state"], "current")
        self.assertIsNone(st["why"])
        self.assertEqual((st["ahead"], st["behind"]), (0, 0))

    def test_no_head_is_unknown(self):
        st = self._status(head=None)
        self.assertEqual(st["state"], "unknown")
        self.assertEqual(st["why"], "not a git checkout")

    def test_no_upstream_ref_and_no_origin_main_is_unknown(self):
        st = self._status(upstream_ref=None, origin_main=None)
        self.assertEqual(st["state"], "unknown")
        self.assertEqual(st["why"], "no upstream ref")

    def test_an_upstream_ref_that_will_not_resolve_is_unknown(self):
        st = self._status(resolve=None)
        self.assertEqual(st["state"], "unknown")
        self.assertEqual(st["why"], "upstream ref unreadable")

    def test_a_failed_rev_list_is_unknown(self):
        st = self._status(counts=None)
        self.assertEqual(st["state"], "unknown")
        self.assertEqual(st["why"], "rev-list failed")

    def test_rev_list_output_that_is_not_two_numbers_is_unknown(self):
        st = self._status(counts="fatal: bad revision")
        self.assertEqual(st["state"], "unknown")
        self.assertTrue(st["why"].startswith("rev-list output unparsed"), st["why"])

    def test_CURRENT_IS_NEVER_REACHABLE_WITHOUT_COUNTS(self):
        """The property under all five, stated once rather than five times.

        `current` closes an open episode, so it is a claim that has to be EARNED
        — by a `rev-list` that actually returned two numbers. Any exit that
        reaches `current` without them is the false all-clear, whichever exit it
        is. `behind` must be None too: the emit gate reads it, and a count that
        survives an unknown verdict is the shape that let an earlier version of
        `test_unknown_says_nothing_at_all` pass for the wrong reason.
        """
        for overrides in ({"head": None},
                          {"upstream_ref": None, "origin_main": None},
                          {"resolve": None},
                          {"counts": None},
                          {"counts": "fatal: bad revision"}):
            st = self._status(**overrides)
            self.assertEqual(st["state"], "unknown", overrides)
            self.assertIsNone(st["behind"], overrides)
            self.assertIsNone(st["ahead"], overrides)
            self.assertTrue(st["why"], overrides)

    def test_a_verdict_carrying_why_still_renders_it(self):
        """What survives a deleted episode (claude-pm-acme's residue question).

        The episode STORE keeps nothing — the entry is removed, not marked
        closed. The event stream does keep something: `fleet_caught_up` is
        emitted with the full render, so the window is reconstructable from
        events even when the store has lost it. Under the mutants above that
        line would also carry a `why=` beside `state=current`, which is
        self-contradicting, since a genuine `current` has no `why`. That is a
        tell rather than a guarantee — worth keeping renderable instead of
        tidied away, and worth saying plainly that it is the only residue.
        """
        line = _build.render_upstream(
            {"state": "current", "head": "aaaaaaa", "ref": "refs/remotes/origin/main",
             "upstream": "bbbbbbb", "ahead": None, "behind": None,
             "fetched": 60.0, "why": "rev-list failed"})
        self.assertIn("why=rev-list-failed", line)
        self.assertNotIn("why=", _build.render_upstream(self._status()))


if __name__ == "__main__":
    unittest.main()
