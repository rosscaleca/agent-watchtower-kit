#!/usr/bin/env python3
"""A cycle that completes with a hole in it must say so.

The defect these pin: a sub-fetch failure was swallowed into
`daemon-<source>.log` via `note()` while the cycle completed and `save_state`
advanced the watermark, so every health signal read green while the source was
partially blind. Observed in production as a 404 on a PR's reviews —
the PR the merge gate hangs on.

The predicate under test is NOT "the profile log is empty on a healthy cycle":
that passes against the fixed daemon AND against the broken one, which is the
shape of check this repo keeps getting caught by. Each daemon test therefore
injects a failing sub-fetch into an OTHERWISE SUCCESSFUL cycle and asserts the
profile log gains a line naming the sub-resource (AC-1, AC-2), with a paired
healthy-cycle case so the assertion cannot pass by emitting unconditionally.
"""
import os
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))

import _degraded  # noqa: E402
import github_wake as gw  # noqa: E402

REPO = "acme/app"


class Reconcile(unittest.TestCase):
    """The shared severity/rate-limit/wording half."""

    def test_a_new_hole_is_reported_once_and_then_rate_limited(self):
        episodes, cyc = {}, _degraded.Cycle().record("reviews", "gh: Not Found (HTTP 404)")
        first = _degraded.reconcile(cyc, episodes, now=1000, renotify_secs=3600)
        self.assertEqual(len(first), 1)
        self.assertIn("type=degraded", first[0][0])
        self.assertIn("sub=reviews", first[0][0])
        # The episode is a record, not a bare epoch, since the flapping fix
        # (a review finding) needs to remember the last emit ACROSS a recovery.
        # Amended 2026-09-04: the record also says whether this
        # episode's degraded was ANNOUNCED and how many repeats were silenced.
        # Amended 2026-09-06: and how many cycles in a row it has
        # failed, which is what the promotion to `error:` counts.
        self.assertEqual(episodes, {"reviews": {"last": 1000, "open": True, "announced": True,
                                                "suppressed": 0, "last_silenced": 0,
                                                "consecutive": 1}})

        # Same hole, still inside the window: silence, not a second page (AC-3).
        again = _degraded.reconcile(_degraded.Cycle().record("reviews", "x"), episodes,
                                    now=1000 + 60, renotify_secs=3600)
        self.assertEqual(again, [])

        # Past the window: it is worth saying again.
        later = _degraded.reconcile(_degraded.Cycle().record("reviews", "x"), episodes,
                                    now=1000 + 3600, renotify_secs=3600)
        # Amended 2026-09-06: this is the THIRD consecutive failing
        # cycle, so the re-announced degraded line is now joined by the one
        # promotion pair. The degraded line itself is unchanged; the old
        # `len(later) == 1` was counting pairs, not degraded lines.
        self.assertEqual([l for l, _ in later if l and l.startswith("type=degraded ")],
                         ["type=degraded sub=reviews detail=x"])
        self.assertEqual([l for l, _ in later if l and l.startswith("error:")],
                         ["error: sub-fetch reviews failing repeatedly (3 consecutive cycles) — last: x"])

    def test_a_hole_that_closes_reports_recovery_and_is_remembered(self):
        """Recovery announces itself and CLOSES the episode — it no longer deletes it.

        ⚠ Amended with the flapping fix: this asserted `episodes == {}`, which
        was the very behaviour that let an alternating fail/ok fetch re-enter
        as a first failure every other cycle. The last-emit time now survives
        the recovery so the window still governs; forgetting it once the
        window has passed is pinned by
        `FlappingIsRateLimitedToo.test_a_closed_episode_past_its_window_is_forgotten`.
        """
        episodes = {"reviews": 1000}
        out = _degraded.reconcile(_degraded.Cycle(), episodes, now=2000, renotify_secs=3600)
        self.assertEqual(len(out), 1)
        self.assertIn("type=degraded_recovered", out[0][0])
        self.assertIn("sub=reviews", out[0][0])
        # Amended 2026-09-04: a legacy int episode reads as announced.
        # Amended 2026-09-06: a closed episode's run of failures is 0.
        self.assertEqual(episodes, {"reviews": {"last": 1000, "open": False, "announced": True,
                                                "suppressed": 0, "last_silenced": 0,
                                                "consecutive": 0}})

    def test_a_clean_cycle_with_no_history_says_nothing(self):
        episodes = {}
        self.assertEqual(_degraded.reconcile(_degraded.Cycle(), episodes, now=1), [])
        self.assertEqual(episodes, {})

    def test_the_detail_names_what_was_not_fetched(self):
        cyc = _degraded.Cycle().record("runs", "gh api repos/x/actions/runs failed: HTTP 504")
        line = _degraded.reconcile(cyc, {}, now=1)[0][0]
        self.assertIn("sub=runs", line)
        self.assertIn("504", line)

    def test_first_failure_wins_so_a_repo_loop_cannot_flood_one_cycle(self):
        cyc = _degraded.Cycle()
        for n in range(20):
            cyc.record("reviews", "failure #%d" % n)
        self.assertEqual(len(_degraded.reconcile(cyc, {}, now=1)), 1)


def _pr(number, state="open", head="abc123456"):
    return {"number": number, "state": state, "head": {"sha": head + "0" * 31},
            "merged_at": None, "title": "t", "user": {"login": "bob"}}


class _GH:
    """Answers the daemon's paths; `fail_on` substrings raise instead."""

    def __init__(self, prs, fail_on=()):
        self.prs, self.fail_on = prs, tuple(fail_on)

    def __call__(self, path):
        for frag in self.fail_on:
            if frag in path:
                raise RuntimeError("gh api %s failed: gh: Not Found (HTTP 404)" % path.split("?")[0])
        if path.startswith("repos/%s/pulls?" % REPO):
            return self.prs
        if "/actions/runs" in path:
            return {"workflow_runs": []}
        return []


class GithubCycle(unittest.TestCase):
    """AC-4 — the predicate, on the daemon the defect was observed in."""

    def _poll(self, fail_on=()):
        prs = [_pr(547)]
        state = {"since": "2026-09-01T00:00:00+00:00", "seen": set(), "first_run": False,
                 "wf_last": {}, "degraded": {},
                 "prs": {REPO: {"547": {"head": prs[0]["head"]["sha"][:9], "state": "open"}}}}
        emitted = []
        with mock.patch.object(gw, "gh_api", _GH(prs, fail_on)), \
                mock.patch.object(gw, "emit", lambda p, line: emitted.append(line)), \
                mock.patch.object(gw, "save_state", lambda p, st: None):
            gw.poll_profile("p", {"repos": [REPO]}, state)
        return emitted, state

    def test_a_healthy_cycle_reports_no_hole(self):
        emitted, state = self._poll()
        self.assertEqual([l for l in emitted if "degraded" in l], [])
        self.assertEqual(state.get("degraded"), {})

    def test_a_failing_reviews_subfetch_reaches_the_profile_log(self):
        """THE case: #547's reviews 404 inside a cycle that otherwise succeeds."""
        emitted, state = self._poll(fail_on=("/reviews",))
        degraded = [l for l in emitted if l.startswith("type=degraded ")]
        self.assertEqual(len(degraded), 1, "expected exactly one degraded line, got %r" % emitted)
        self.assertIn("sub=reviews", degraded[0])
        self.assertIn("reviews", state["degraded"])

    def test_a_failing_runs_subfetch_names_runs_not_reviews(self):
        """AC-2 — the two real instances differ and want different responses."""
        emitted, _ = self._poll(fail_on=("/actions/runs",))
        degraded = [l for l in emitted if l.startswith("type=degraded ")]
        self.assertEqual(len(degraded), 1)
        self.assertIn("sub=runs", degraded[0])

    def test_the_cycle_still_completes_and_still_emits_real_events(self):
        """The fix must not turn a partial failure into a lost cycle."""
        prs = [_pr(547), _pr(900)]
        state = {"since": "2026-09-01T00:00:00+00:00", "seen": set(), "first_run": False,
                 "wf_last": {}, "degraded": {},
                 "prs": {REPO: {"547": {"head": prs[0]["head"]["sha"][:9], "state": "open"}}}}
        emitted = []
        with mock.patch.object(gw, "gh_api", _GH(prs, ("/reviews",))), \
                mock.patch.object(gw, "emit", lambda p, line: emitted.append(line)), \
                mock.patch.object(gw, "save_state", lambda p, st: None):
            gw.poll_profile("p", {"repos": [REPO]}, state)
        self.assertTrue(any(l.startswith("type=pr_opened") for l in emitted),
                        "the new PR must still be reported: %r" % emitted)
        self.assertTrue(any(l.startswith("type=degraded ") for l in emitted))



class EverySwallowSiteRecords(unittest.TestCase):
    """Derived, not enumerated — a fourth daemon is covered the day it is added.

    The defect is structural: an `except` handler that reports a failed
    sub-fetch with `note()` and then carries on is exactly the silent hole.
    Rather than list today's seven sites (an enumeration that goes stale the
    way this repo keeps being caught by), this walks each daemon's
    `poll_profile` and asserts that any handler which notes a failure also
    records it against the cycle.
    """

    DAEMONS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons")

    def _daemons(self):
        """Every daemon on disk — DERIVED, so a fourth is covered the day it lands.

        This previously read `for name in ("github", "jira", "bitbucket")`
        under a docstring promising derivation, which is the enumeration this
        class exists to argue against. Daemons with no `note()`-carrying
        handler simply contribute nothing, so no exclusion list is needed.
        """
        import glob
        return sorted(
            os.path.basename(p) for p in glob.glob(os.path.join(self.DAEMONS_DIR, "*_wake.py")))

    def _calls(self, node):
        import ast
        names = set()
        for c in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
            if isinstance(c.func, ast.Name):
                names.add(c.func.id)
            elif isinstance(c.func, ast.Attribute):
                names.add(c.func.attr)
        return names

    def _poll_profile(self, path):
        import ast
        for fn in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(fn, ast.FunctionDef) and fn.name == "poll_profile":
                return fn
        return None

    def _handlers(self, path):
        import ast
        tree = ast.parse(Path(path).read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "poll_profile":
                for node in ast.walk(fn):
                    if isinstance(node, ast.ExceptHandler):
                        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
                        names = set()
                        for c in calls:
                            if isinstance(c.func, ast.Name):
                                names.add(c.func.id)
                            elif isinstance(c.func, ast.Attribute):
                                names.add(c.func.attr)
                        yield node.lineno, names

    def test_a_noted_subfetch_failure_is_always_recorded(self):
        missing = []
        checked = 0
        for fname in self._daemons():
            path = os.path.join(self.DAEMONS_DIR, fname)
            for lineno, names in self._handlers(path):
                if "note" not in names:
                    continue
                checked += 1
                if "record" not in names:
                    missing.append("%s:%d" % (fname, lineno))
        self.assertTrue(checked >= 7,
                        "expected to inspect at least the 7 known swallow sites, saw %d" % checked)
        self.assertEqual(missing, [],
                         "sub-fetch failures noted but not recorded: %s" % missing)

    def test_a_daemon_that_records_holes_also_reports_them(self):
        """Recording without reconciling is the original defect, one layer up.

        The test above proves every swallowed failure reaches a `Cycle`. It
        says nothing about the cycle then SAYING anything — a daemon whose
        `reconcile` loop is deleted still captures every hole and still emits
        to no profile log, which is exactly the silence this ticket is about.
        Found by mutation: deleting jira's reconcile loop left
        all 41 tests green.
        """
        silent = []
        for fname in self._daemons():
            path = os.path.join(self.DAEMONS_DIR, fname)
            if not any("note" in names for _, names in self._handlers(path)):
                continue  # nothing swallowed here; nothing to report
            fn = self._poll_profile(path)
            self.assertIsNotNone(fn, "%s has swallow sites but no poll_profile" % fname)
            if "reconcile" not in self._calls(fn):
                silent.append(fname)
        self.assertEqual(silent, [],
                         "daemons record holes but never report them: %s" % silent)


class FlappingIsRateLimitedToo(unittest.TestCase):
    """AC-3 held for a persistent failure and not for a flapping one.

    Recovery used to DELETE the episode, so a fetch failing on alternate
    cycles re-entered as a first failure each time and emitted every time —
    and a flapping upstream is what a rate-limited or overloaded API actually
    looks like.
    """

    def _run(self, pattern, renotify=3600, step=60):
        episodes, emitted = {}, 0
        for i, failing in enumerate(pattern):
            cyc = _degraded.Cycle()
            if failing:
                cyc.record("reviews", "HTTP 504")
            out = _degraded.reconcile(cyc, episodes, now=1000 + i * step,
                                      renotify_secs=renotify)
            # A silenced repeat comes back with emit_line=None.
            emitted += sum(1 for line, _ in out if line and line.startswith("type=degraded "))
        return emitted, episodes

    def test_a_flapping_fetch_is_reported_once_per_window(self):
        emitted, _ = self._run([True, False] * 5)
        self.assertEqual(emitted, 1,
                         "a flapping fetch must obey the window, not re-enter as new each cycle")

    def test_recovery_is_still_announced(self):
        episodes = {}
        _degraded.reconcile(_degraded.Cycle().record("reviews", "x"), episodes, now=1000)
        out = _degraded.reconcile(_degraded.Cycle(), episodes, now=1060)
        self.assertTrue(any("type=degraded_recovered" in line for line, _ in out),
                        "closing an episode must still say so")

    def test_a_closed_episode_past_its_window_is_forgotten(self):
        _, episodes = self._run([True, False], renotify=60, step=3600)
        self.assertEqual(episodes, {}, "closed episodes must not accumulate forever")

    def test_a_pre_td885_integer_episode_still_loads(self):
        """State written before this shape existed must not crash the daemon."""
        episodes = {"reviews": 1000}
        out = _degraded.reconcile(_degraded.Cycle(), episodes, now=1060)
        self.assertTrue(any("type=degraded_recovered" in line for line, _ in out))


class RecoveryIsAnnouncedOnlyForAnAnnouncedFailure(unittest.TestCase):
    """The rate limit was applied to one half of a pair.

    A failure inside the renotify window was silenced but reopened the
    episode, so the next clean cycle announced a recovery for a failure nobody
    was told about: one `degraded`, two `recovered`, and a real failed
    sub-fetch in no log at all. Observed on both hosts within two hours of
    shipping.

    Every case here CROSSES the window boundary — fail, recover, fail inside
    the window, recover — because a case that stays outside it passes against
    the broken code and the fixed one alike (AC-2).
    """

    WINDOW = 3600

    def _cycles(self, pattern, step=60, start=1000, window=WINDOW):
        """Run `pattern` (True = the sub-fetch fails) and return the emitted
        profile-log lines in order, plus the daemon-log notes and episodes."""
        episodes, emitted, notes = {}, [], []
        for i, failing in enumerate(pattern):
            cyc = _degraded.Cycle()
            if failing:
                cyc.record("reviews", "HTTP 504")
            for emit_line, note_line in _degraded.reconcile(
                    cyc, episodes, now=start + i * step, renotify_secs=window):
                if emit_line:
                    emitted.append(emit_line)
                notes.append(note_line)
        return emitted, notes, episodes

    @staticmethod
    def _counts(lines):
        return (sum(1 for l in lines if l.startswith("type=degraded ")),
                sum(1 for l in lines if l.startswith("type=degraded_recovered")))

    def test_ac2_a_repeat_inside_the_window_does_not_announce_its_recovery(self):
        """fail@0m, ok@1m, fail@26m (inside), ok@27m — reds on 417dbc09 (1 vs 2)."""
        pattern = [False] * 28
        pattern[0] = True   # t=1000  -> degraded
        pattern[26] = True  # t=2560  -> inside the 60-minute window: silenced
        emitted, _, _ = self._cycles(pattern)
        self.assertEqual(self._counts(emitted), (1, 1), emitted)
        self.assertEqual(emitted[0].split(" detail=")[0], "type=degraded sub=reviews")
        self.assertEqual(emitted[1], "type=degraded_recovered sub=reviews")

    def test_the_silenced_repeat_is_counted_on_the_next_announcement(self):
        """The evidence survives the rate limit: suppressed=N rides the next degraded."""
        pattern = [False] * 62
        pattern[0] = True    # announced
        pattern[26] = True   # silenced (1)
        pattern[40] = True   # silenced (2)
        pattern[61] = True   # t=1000+61*60=4660 >= 1000+3600 -> announced again
        emitted, _, _ = self._cycles(pattern)
        self.assertEqual(self._counts(emitted), (2, 1), emitted)
        self.assertTrue(emitted[-1].startswith("type=degraded sub=reviews detail=HTTP 504 suppressed=2"),
                        emitted[-1])

    def test_a_silenced_repeat_still_reaches_the_daemon_log_with_its_detail(self):
        """AC-4 — discoverable by someone: the note half of the pair carries it."""
        pattern = [False] * 28
        pattern[0] = True
        pattern[26] = True
        _, notes, _ = self._cycles(pattern)
        silenced = [n for n in notes if "silenced" in n and "HTTP 504" in n]
        self.assertEqual(len(silenced), 1, notes)
        self.assertIn("suppressed=1", silenced[0])
        quiet = [n for n in notes if "recovered (silently" in n]
        self.assertEqual(len(quiet), 1, notes)

    def test_pairing_holds_over_random_fail_ok_sequences(self):
        """The invariant, not a fixture: every recovered follows an unmatched degraded."""
        import random
        rng = random.Random(889)
        for trial in range(200):
            pattern = [rng.random() < 0.3 for _ in range(rng.randint(5, 80))]
            step = rng.choice([30, 60, 600, 1800])
            emitted, _, _ = self._cycles(pattern, step=step)
            # A persistent failure is re-announced when its window expires
            # (two `degraded` in a row is legitimate); a `recovered` must
            # always follow at least one `degraded` since the last `recovered`.
            announced_since_recovery = 0
            for line in emitted:
                if line.startswith("type=degraded "):
                    announced_since_recovery += 1
                elif line.startswith("type=degraded_recovered"):
                    self.assertGreater(announced_since_recovery, 0,
                                       "trial %d: recovered without a preceding degraded: %s"
                                       % (trial, emitted))
                    announced_since_recovery = 0

    def test_the_anti_flapping_property_is_kept(self):
        """AC-3 — alternating fail/ok inside one window: one degraded, one recovered,
        and the silenced repeats do NOT each announce a recovery (the unpaired-recovery shape
        of the same test)."""
        emitted, _, _ = self._cycles([True, False] * 10)
        self.assertEqual(self._counts(emitted), (1, 1), emitted)

    def test_a_pre_td889_dict_episode_still_loads_as_announced(self):
        """State written by 417dbc09 has no announced/suppressed keys; read it as an
        announced episode so a live recovery is not swallowed by the upgrade."""
        episodes = {"reviews": {"last": 1000, "open": True}}
        out = _degraded.reconcile(_degraded.Cycle(), episodes, now=1060)
        self.assertTrue(any(l == "type=degraded_recovered sub=reviews" for l, _ in out), out)

    def test_every_daemon_skips_the_profile_log_for_a_none_emit_line(self):
        """DERIVED from the daemons that call reconcile(): each guards emit() on the
        line being truthy, otherwise a silenced repeat would be written as the
        literal 'None' into the profile log."""
        import ast, glob
        daemons_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons")
        unguarded, checked = [], 0
        for path in sorted(glob.glob(os.path.join(daemons_dir, "*_wake.py"))):
            tree = ast.parse(Path(path).read_text())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.For) and isinstance(node.iter, ast.Call)
                        and isinstance(node.iter.func, ast.Attribute)
                        and node.iter.func.attr == "reconcile"):
                    continue
                checked += 1
                guarded = any(
                    isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "emit_line"
                    for n in ast.walk(node))
                if not guarded:
                    unguarded.append(os.path.basename(path))
        self.assertGreaterEqual(checked, 3, "expected at least the three reporting daemons")
        self.assertEqual(unguarded, [], "emit() not guarded on emit_line: %s" % unguarded)


class SubFetchPromotesToErrorAtThree(unittest.TestCase):
    """A sub-fetch that fails forever never became an ``error:``.

    A whole-cycle failure promotes in ``main()`` after three consecutive
    cycles, once per episode, and resets with a recovery note. A sub-resource
    fetch had no equivalent: ``degraded`` / ``degraded_recovered`` and the
    renotify window govern HOW OFTEN a hole is repeated, and nothing ever said
    "this is broken rather than noisy". A dead sub-resource produced
    rate-limited ``degraded`` lines forever.

    The half a tidy-up would break is the second one: the promotion ADDS a
    pair and changes nothing about the pairing or the rate limit. `test_promotion_changes_nothing_else` pins that by running the
    same sequences with promotion off and comparing everything that is not
    the new pair.
    """

    WINDOW = 3600

    def _cycles(self, pattern, step=60, start=1000, window=WINDOW,
                promote_after=_degraded.PROMOTE_AFTER):
        episodes, emitted, notes = {}, [], []
        for i, failing in enumerate(pattern):
            cyc = _degraded.Cycle()
            if failing:
                cyc.record("reviews", "HTTP 504")
            for emit_line, note_line in _degraded.reconcile(
                    cyc, episodes, now=start + i * step, renotify_secs=window,
                    promote_after=promote_after):
                if emit_line:
                    emitted.append(emit_line)
                notes.append(note_line)
        return emitted, notes, episodes

    @staticmethod
    def _errors(lines):
        return [l for l in lines if l.startswith("error:")]

    def test_three_consecutive_failures_promote_once_and_only_once(self):
        """fail x6 inside one window: one degraded (cycle 1), one error (cycle
        3), nothing more on cycles 4-6. The silenced repeats on cycles 2-3 do
        not stop the promotion — the rate limit is about what is said, not
        about whether the fetch failed."""
        emitted, _, episodes = self._cycles([True] * 6)
        self.assertEqual(
            emitted,
            ["type=degraded sub=reviews detail=HTTP 504",
             "error: sub-fetch reviews failing repeatedly (3 consecutive cycles) — last: HTTP 504"],
            emitted)
        self.assertEqual(episodes["reviews"]["consecutive"], 6)

    def test_the_error_line_fires_at_exactly_the_threshold(self):
        for n in range(1, 6):
            emitted, _, _ = self._cycles([True] * n)
            self.assertEqual(len(self._errors(emitted)),
                             1 if n >= _degraded.PROMOTE_AFTER else 0,
                             "after %d failing cycles: %s" % (n, emitted))

    def test_reset_on_success_then_a_fresh_episode_promotes_again(self):
        """fail x3 (error), ok (recovery note), fail x2, ok (no note — only
        two), fail x3 (error again). Once per episode, and the counter
        restarts from the recovery."""
        pattern = [True, True, True, False, True, True, False, True, True, True]
        emitted, notes, episodes = self._cycles(pattern)
        self.assertEqual(len(self._errors(emitted)), 2, emitted)
        recovered = [n for n in notes if "recovered after" in n]
        self.assertEqual(recovered,
                         ["note: sub-fetch reviews recovered after 3 consecutive failures"],
                         notes)
        self.assertEqual(episodes["reviews"]["consecutive"], 3)

    def test_a_flapping_fetch_never_promotes(self):
        """Flapping is the rate limit's case, not the promotion's: fail/ok
        never reaches three in a row."""
        emitted, _, _ = self._cycles([True, False] * 10)
        self.assertEqual(self._errors(emitted), [])

    def test_promotion_changes_nothing_else(self):
        """The invariant: with promotion off the output is what it was before
        the promotion; with it on, the output is the same plus the new pair. Every
        degraded / degraded_recovered / suppressed=N line, every note, and
        every episode field other than the count are identical over random
        fail/ok sequences that cross the window."""
        import random
        rng = random.Random(904)

        def strip(ep):
            return {k: {kk: vv for kk, vv in v.items() if kk != "consecutive"}
                    for k, v in ep.items()}

        for trial in range(200):
            pattern = [rng.random() < 0.5 for _ in range(rng.randint(5, 80))]
            step = rng.choice([30, 60, 600, 1800])
            on_e, on_n, on_ep = self._cycles(pattern, step=step)
            off_e, off_n, off_ep = self._cycles(pattern, step=step, promote_after=None)
            self.assertEqual([l for l in on_e if not l.startswith("error:")], off_e,
                             "trial %d: profile-log lines changed" % trial)
            self.assertEqual([n for n in on_n if "consecutive" not in n], off_n,
                             "trial %d: daemon-log notes changed" % trial)
            self.assertEqual(strip(on_ep), strip(off_ep),
                             "trial %d: episode state changed" % trial)

    def test_the_threshold_matches_the_whole_cycle_counter_in_every_daemon(self):
        """DERIVED: main() promotes at `failures == N`; N must equal
        PROMOTE_AFTER in every daemon that carries the counter, so the two
        promotion paths cannot drift apart."""
        import glob, re
        daemons_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons")
        seen = {}
        for path in sorted(glob.glob(os.path.join(daemons_dir, "*_wake.py"))):
            m = re.search(r"if failures == (\d+):", Path(path).read_text())
            if m:
                seen[os.path.basename(path)] = int(m.group(1))
        self.assertGreaterEqual(len(seen), 4, seen)
        self.assertEqual(set(seen.values()), {_degraded.PROMOTE_AFTER}, seen)

    def test_pre_td904_records_load_with_consecutive_zero(self):
        """State written before the count existed must not crash the daemon,
        and an old open episode counts from its next failure."""
        self.assertEqual(_degraded._record({"last": 1000, "open": True})["consecutive"], 0)
        self.assertEqual(_degraded._record(1000)["consecutive"], 0)
        episodes = {"reviews": {"last": 1000, "open": True, "announced": True,
                                "suppressed": 0, "last_silenced": 0}}
        _degraded.reconcile(_degraded.Cycle().record("reviews", "x"), episodes, now=1060)
        self.assertEqual(episodes["reviews"]["consecutive"], 1)


class NotAttemptedIsNeitherFailedNorRecovered(unittest.TestCase):
    """A tier waiting on config — a
    placeholder token — is a third state. Recording it as a failure escalated a
    fresh install to `error:` in fifteen minutes; not recording it announced a
    recovery for an episode nothing had run to recover."""

    def test_a_skipped_sub_holds_its_open_episode_without_a_recovery(self):
        episodes = {"jira": {"last": 1000, "open": True, "announced": True,
                             "suppressed": 0, "last_silenced": 0, "consecutive": 2}}
        out = _degraded.reconcile(_degraded.Cycle().skip("jira", "api_token is a placeholder"),
                                  episodes, now=1000 + 7200)
        self.assertEqual(out, [], "nothing said in either log")
        self.assertTrue(episodes["jira"]["open"])
        self.assertEqual(episodes["jira"]["consecutive"], 2, "not a failure: the count does not move")

    def test_a_skipped_sub_with_no_episode_says_nothing_and_opens_nothing(self):
        episodes = {}
        out = _degraded.reconcile(_degraded.Cycle().skip("jira", "placeholder"), episodes, now=5000)
        self.assertEqual((out, episodes), ([], {}))

    def test_a_skip_never_promotes(self):
        episodes = {}
        cycle = _degraded.Cycle().skip("jira", "placeholder")
        for i in range(10):
            _degraded.reconcile(cycle, episodes, now=5000 + 600 * i)
        self.assertEqual(episodes, {})

    def test_a_skipped_cycle_is_not_a_hole(self):
        self.assertFalse(bool(_degraded.Cycle().skip("jira", "placeholder")))
        self.assertTrue(bool(_degraded.Cycle().record("jira", "HTTP 500")))


class IsOpen(unittest.TestCase):
    """Is a sub-resource's episode OPEN in a persisted ``degraded`` dict?

    The state file is the only record of what a daemon's last cycle could not read. The jira and
    bitbucket migrations read it: an open episode for a sub-fetch that feeds the dedup memory means a
    slice of the source was not fetched, so an item the file does not list may never have been
    announced. ``_record`` already decides what an episode IS, including the two older shapes, and this
    asks it, so the two cannot disagree about what open means.
    """

    @staticmethod
    def _episodes(sub="comments", open_=True):
        """``degraded`` exactly as `reconcile` leaves it in a state file."""
        episodes = {}
        _degraded.reconcile(_degraded.Cycle().record(sub, "HTTP 502"), episodes, now=1000.0)
        if not open_:
            _degraded.reconcile(_degraded.Cycle(), episodes, now=1060.0)
        return episodes

    def test_a_cycle_with_a_failing_sub_fetch_leaves_its_episode_open(self):
        self.assertTrue(_degraded.is_open(self._episodes("comments"), "comments"))

    def test_a_recovered_episode_is_closed_but_still_on_record(self):
        """The closed record outlives the recovery (the anti-flapping window), so what matters is the
        `open` flag, not whether the sub is mentioned at all."""
        episodes = self._episodes("comments", open_=False)
        self.assertIn("comments", episodes)
        self.assertFalse(_degraded.is_open(episodes, "comments"))

    def test_only_the_sub_asked_about_counts(self):
        self.assertFalse(_degraded.is_open(self._episodes("comments"), "changelog"))
        self.assertTrue(_degraded.is_open(dict(self._episodes("comments"), **self._episodes("changelog")),
                                          "changelog"))

    def test_the_older_shapes_read_as_open_as_record_reads_them(self):
        """The legacy int form and an older dict with no `open` key are episodes that were announced
        and never closed, which is the only honest default for a record written before the flag existed."""
        self.assertTrue(_degraded.is_open({"comments": 1000}, "comments"))
        self.assertTrue(_degraded.is_open({"comments": {"last": 1000}}, "comments"))
        self.assertTrue(_degraded.is_open({"comments": {"last": 1000, "open": True}}, "comments"))
        self.assertFalse(_degraded.is_open({"comments": {"last": 1000, "open": False}}, "comments"))

    def test_nothing_to_read_is_not_open_and_never_raises(self):
        for episodes in (None, {}, [], "x", 7, {"comments": None}, {"comments": "garbage"}, {"comments": []}):
            with self.subTest(episodes=episodes):
                self.assertFalse(_degraded.is_open(episodes, "comments"))

    def test_it_agrees_with_record_on_every_shape(self):
        shapes = [None, 0, 1000, "1000", "x", {}, {"open": True}, {"open": False}, {"last": 1, "open": 0},
                  {"last": 1, "open": "yes"}, [], True]
        for value in shapes:
            with self.subTest(value=value):
                record = _degraded._record(value)
                self.assertEqual(_degraded.is_open({"s": value}, "s"), bool(record and record["open"]))


class OneLineDropsWhatATerminalActsOn(unittest.TestCase):
    """A degraded detail is gh or bkt error text, and that can quote text from outside the
    project. `one_line` drops what a terminal or a bidi-aware reader acts on but does not show,
    through `_fields.unshown_dropped`, as the daemons' own `one_line` does."""

    CRAFTED = "gh: 404 \x1b[2K\r\x1b[1G type=forged \x1b[31mred\x1b[0m \x9b2K a\u202eb\u200dc\u200bd"
    CLEAN = "gh: 404 [2K [1G type=forged [31mred[0m 2K ab\u200dcd"
    HIDDEN = ("\x1b", "\x9b", "\u202e", "\u200b", "\r")

    def test_one_line_drops_the_controls_the_bidi_marks_and_the_zero_width_space(self):
        out = _degraded.one_line(self.CRAFTED)
        for ch in self.HIDDEN:
            self.assertNotIn(ch, out, repr(ch))
        self.assertEqual(out, self.CLEAN)

    def test_one_line_keeps_the_two_joiners(self):
        self.assertEqual(_degraded.one_line("a\u200cb\u200dc"), "a\u200cb\u200dc")

    def test_one_line_still_collapses_whitespace_and_caps(self):
        self.assertEqual(_degraded.one_line("a  b\n c\t"), "a b c")
        self.assertEqual(_degraded.one_line("x" * 200), "x" * 160)
        self.assertEqual(_degraded.one_line("x" * 50, cap=10), "x" * 10)

    def test_one_line_strips_after_the_drop_and_caps_after_it(self):
        # A review found two mutants survived (no strip after the drop; the cap taken before it).
        self.assertEqual(_degraded.one_line("a \x1b"), "a")
        self.assertEqual(_degraded.one_line("\x1b" * 10 + "x" * 160), "x" * 160)

    def test_a_recorded_detail_reaches_the_cycle_clean(self):
        cycle = _degraded.Cycle()
        cycle.record("pulls/7/reviews", self.CRAFTED)
        cycle.skip("pulls/8/reviews", self.CRAFTED)
        self.assertEqual(cycle.failures["pulls/7/reviews"], self.CLEAN)
        self.assertEqual(cycle.skipped["pulls/8/reviews"], self.CLEAN)


if __name__ == "__main__":
    unittest.main()
