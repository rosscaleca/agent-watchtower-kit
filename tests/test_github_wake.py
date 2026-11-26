#!/usr/bin/env python3
"""github_wake emits the full PR lifecycle — pinned, because a ticket once said it didn't.

A ticket recorded a "regression, precisely": the Watchtower GitHub daemon emits
five event types where the standalone `com.acme.github-wake` emits
seven, missing `pr_merged` and `pr_closed`. On this team PRs from one seat are
merged by the other, so `pr_merged` is how a session learns its own work
landed; a cutover that dropped it would be a regression that looks like
quiet.

Checked at source before porting anything: `daemons/github_wake.py` has
emitted both since the initial scaffold. The parity the
ticket asked for was already there — what was missing was any test saying so,
which is why the claim could be made and believed. These tests are that
statement, so the next measurement is a test run rather than a diff count.

The poller is exercised with `gh_api` and `emit` replaced: no network, no
files. Run: `just test` (or `python3 -m unittest discover -s tests`).
"""
import collections
import contextlib
import copy
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))

import github_wake as gw  # noqa: E402

REPO = "acme/app"
CFG = {"repos": [REPO], "suppress_authors": ["alice"]}

# Every event type the standalone com.acme.github-wake emits. If the
# Watchtower daemon stops producing one of these, the migration silently
# loses wakes — the case that ticket was filed to prevent.
STANDALONE_TYPES = {"pr_opened", "pr_push", "pr_merged", "pr_closed", "review", "comment", "run"}

#: A run fixture's default `created_at`: long before any poll, so a test that is
#: not about the page-back stops at page one by the page-back's own rule (the page's
#: oldest run predates the previous poll), not through the malformed-answer branch.
OLD_CREATED = "2026-01-01T00:00:00Z"


_REAL_SIGNAL = []


def setUpModule():
    """`main()` registers a SIGTERM handler. The tests that run the real `main()` loop
    must not leave one registered on the test process, so `signal.signal` is a recording stand-in
    for the whole module; no test here sends a signal."""
    patch = mock.patch.object(signal, "signal")
    patch.start()
    _REAL_SIGNAL.append(patch)


def tearDownModule():
    while _REAL_SIGNAL:
        _REAL_SIGNAL.pop().stop()


def _pr(number, state="open", head="abc123456", merged_at=None, title="t", user="bob", body=None):
    return {"number": number, "state": state, "head": {"sha": head + "0" * 31},
            "merged_at": merged_at, "title": title, "user": {"login": user}, "body": body}


def _seeded_state(prs):
    """State as it would be after a first-run baseline over ``prs``."""
    return {"since": "2026-09-01T00:00:00+00:00", "seen": set(), "first_run": False,
            "wf_last": {},
            "prs": {REPO: {str(p["number"]): {"head": p["head"]["sha"][:9], "state": p["state"]}
                           for p in prs}}}


class _FakeGH:
    """Answers the daemon's `gh api` paths from a fixed PR list; everything else empty."""

    def __init__(self, prs):
        self.prs = prs

    def __call__(self, path):
        if path.startswith("repos/%s/pulls?" % REPO):
            return self.prs
        one = re.match(r"repos/%s/pulls/(\d+)$" % re.escape(REPO), path)
        if one:                                   # a single PR by number, as `gh api` answers it
            for pr in self.prs:
                if str(pr["number"]) == one.group(1):
                    return pr
            raise RuntimeError("gh api %s failed: HTTP 404" % path)
        if "/actions/runs" in path:
            return {"workflow_runs": []}
        return []


def _poll(prs_before, prs_now, state=None):
    state = _seeded_state(prs_before) if state is None else state
    emitted = []
    # LOG_DIR / STATE_DIR are patched to a throwaway tree as well as `emit` and
    # `save_state`: a test must be unable to reach the live watchtower
    # directories however the patch bookkeeping goes (2026-09-10).
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(gw, "LOG_DIR", os.path.join(tmp, "logs")), \
            mock.patch.object(gw, "STATE_DIR", os.path.join(tmp, "state")), \
            mock.patch.object(gw, "gh_api", _FakeGH(prs_now)), \
            mock.patch.object(gw, "emit", lambda profile, line: emitted.append(line)), \
            mock.patch.object(gw, "save_state", lambda profile, st: None):
        gw.poll_profile("acme", CFG, state)
    return emitted, state


class Lifecycle(unittest.TestCase):

    def test_open_to_merged_emits_pr_merged(self):
        emitted, _ = _poll([_pr(407)], [_pr(407, state="closed", merged_at="2026-09-02T20:00:00Z",
                                            title="[PROJ-825] fix(ci): green-skips")])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertTrue(emitted[0].startswith("type=pr_merged repo=app #407 | "), emitted[0])
        self.assertIn("[PROJ-825]", emitted[0])

    def test_open_to_closed_without_merge_emits_pr_closed(self):
        emitted, _ = _poll([_pr(37)], [_pr(37, state="closed", merged_at=None)])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertTrue(emitted[0].startswith("type=pr_closed repo=app #37 | "), emitted[0])

    def test_merge_is_emitted_once_not_every_cycle(self):
        """The state records the transition, so a merged PR that stays in the
        `state=all` listing does not re-emit on the next poll."""
        merged = _pr(407, state="closed", merged_at="2026-09-02T20:00:00Z")
        emitted, state = _poll([_pr(407)], [merged])
        self.assertEqual(len(emitted), 1)
        again = []
        with mock.patch.object(gw, "gh_api", _FakeGH([merged])), \
                mock.patch.object(gw, "emit", lambda profile, line: again.append(line)), \
                mock.patch.object(gw, "save_state", lambda profile, st: None):
            gw.poll_profile("acme", CFG, state)
        self.assertEqual(again, [])

    def test_lifecycle_is_never_suppressed_by_author(self):
        """A merge or close is state worth confirming whoever clicked it —
        suppression applies to comments and reviews only (module docstring)."""
        own = _pr(500, user="alice")
        emitted, _ = _poll([own], [_pr(500, state="closed", merged_at="2026-09-02T20:00:00Z",
                                       user="alice")])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertIn("type=pr_merged", emitted[0])

    def test_new_open_pr_and_push_still_emit(self):
        emitted, _ = _poll([_pr(1)], [_pr(1, head="fedcba987"), _pr(2, title="new one")])
        types = sorted(line.split()[0] for line in emitted)
        self.assertEqual(types, ["type=pr_opened", "type=pr_push"], emitted)


class Roster(unittest.TestCase):

    def test_every_standalone_event_type_has_an_emitter_here(self):
        """Parity with com.acme.github-wake, by reading the emitter rather
        than trusting a count: each `type=<name>` the standalone produces must
        appear as an emit format in this daemon's source."""
        with open(gw.__file__) as fh:
            src = fh.read()
        present = set()
        for t in STANDALONE_TYPES:
            if "type=%s " % t in src or '"%s"' % t in src:
                present.add(t)
        self.assertEqual(present, STANDALONE_TYPES, "missing: %s" % sorted(STANDALONE_TYPES - present))


class PairWindows(unittest.TestCase):
    """A merged half whose `Lands-with:` companion is still unmerged is a
    live window, said as an event at the moment it opens."""

    BODY = "Adds the ref input.\n\nLands-with: acme/Agent-Watchtower#8\n"

    def _merge(self, companion):
        """Merge #569 with the trailer; `companion` is True/False or an Exception."""
        def check(repo, num):
            if isinstance(companion, Exception):
                raise companion
            return companion
        with mock.patch.object(gw, "companion_merged", check):
            emitted, _ = _poll([_pr(569, body=self.BODY)],
                               [_pr(569, state="closed", merged_at="2026-09-06T05:24:00Z",
                                    title="[PROJ-900] perf nightly ref", body=self.BODY)])
        return emitted

    def test_a_merged_half_with_an_open_companion_opens_a_window(self):
        emitted = self._merge(companion=False)
        types = [line.split()[0] for line in emitted]
        self.assertEqual(types, ["type=pr_merged", "type=pair_window_open"], emitted)
        self.assertIn("repo=app #569 waits-on=acme/Agent-Watchtower#8 | [PROJ-900] perf nightly ref", emitted[1])

    def test_no_window_when_the_companion_already_merged(self):
        emitted = self._merge(companion=True)
        self.assertEqual([line.split()[0] for line in emitted], ["type=pr_merged"], emitted)

    def test_an_unreachable_companion_is_a_hole_not_a_closed_window(self):
        emitted = self._merge(companion=RuntimeError("HTTP 502"))
        types = [line.split()[0] for line in emitted]
        self.assertIn("type=pr_merged", types)
        self.assertNotIn("type=pair_window_open", types, "unknown is not open — and not closed either")
        self.assertTrue(any(line.startswith("type=degraded sub=pairs") for line in emitted), emitted)

    def test_a_merge_without_a_trailer_never_consults_the_companion_check(self):
        with mock.patch.object(gw, "companion_merged", side_effect=AssertionError("must not be called")):
            emitted, _ = _poll([_pr(407)], [_pr(407, state="closed", merged_at="2026-09-02T20:00:00Z")])
        self.assertEqual([line.split()[0] for line in emitted], ["type=pr_merged"])

    CLOSED = dict(number=569, state="closed", merged_at="2026-09-06T05:24:00Z", title="[PROJ-900] perf nightly ref")

    INDENTED = "Adds the ref input.\n\n  Lands-with: acme/Agent-Watchtower#8\n"

    def _pending(self, body):
        """A `pair_pending` entry from cycle one, then cycle two re-reads `body`.

        Cycle one fails the companion check so the entry survives; cycle two is
        the retry path, which re-reads the origin — the path a profile WITHOUT
        the optional stalled daemon depends on entirely.
        """
        closed = _pr(body=self.BODY, **self.CLOSED)
        with mock.patch.object(gw, "companion_merged", side_effect=RuntimeError("HTTP 502")):
            _, state = _poll([_pr(569, body=self.BODY)], [closed])
        self.assertEqual(len(state["pair_pending"]), 1, "cycle one must leave the entry")
        return state, _pr(body=body, **self.CLOSED)

    def test_a_pending_entry_whose_trailer_became_UNREADABLE_is_held_not_settled(self):
        """A review found: the round-four predicate never reached here.

        A pending entry created BEFORE the indented-trailer fix from an indented trailer parses to
        no declarations under the column-zero rule, so the loop ran zero times,
        `settled` stayed True, and the caller deleted the entry and announced
        `degraded_recovered` — companion never checked, window never opened,
        nothing said. An empty parse is not evidence that there is nothing left
        to check; it is evidence that this parser read nothing.
        """
        state, reread = self._pending(self.INDENTED)
        with mock.patch.object(gw, "companion_merged",
                               side_effect=AssertionError("must not be consulted")):
            emitted, state = _poll(None, [reread], state=state)
        types = [line.split()[0] for line in emitted]
        self.assertIn("type=pair_trailer_unparsed", types, emitted)
        self.assertNotIn("type=degraded_recovered", types, "nothing recovered — nothing was read")
        self.assertEqual(len(state["pair_pending"]), 1, "the entry is HELD for the next cycle")

    def test_a_genuinely_withdrawn_trailer_still_settles_and_does_not_wedge(self):
        """The other side, and the reason the hold is conditioned on `unparsed`.

        A body with nothing trailer-shaped in it was withdrawn. Holding THAT
        would keep an entry on record for ever on an author who simply removed
        the line — the failure mode of a predicate applied too widely.
        """
        state, reread = self._pending("The trailer is gone; this landed alone.\n")
        with mock.patch.object(gw, "companion_merged",
                               side_effect=AssertionError("must not be consulted")):
            emitted, state = _poll(None, [reread], state=state)
        self.assertNotIn("type=pair_trailer_unparsed", [line.split()[0] for line in emitted], emitted)
        self.assertEqual(state["pair_pending"], [], "settled and removed")

    def test_one_readable_declaration_beside_an_unreadable_line_is_still_checked(self):
        """Holding is for NOTHING readable. With one good declaration the entry is
        doing its job on that companion, `pair_trailer_unparsed` tells the author
        about the rest, and holding would wedge on an author who never returns."""
        body = ("Lands-with: acme/Agent-Watchtower#8\n"
                "Lands-with: not-a-reference\n")
        state, reread = self._pending(body)
        with mock.patch.object(gw, "companion_merged", lambda repo, num: False):
            emitted, state = _poll(None, [reread], state=state)
        types = [line.split()[0] for line in emitted]
        self.assertIn("type=pair_window_open", types, emitted)
        self.assertEqual(state["pair_pending"], [], "settled — the readable one answered")

    def test_an_unreadable_trailer_is_announced_ONCE_not_every_poll(self):
        """A review found: the retry loop runs every cycle, and unlike the
        degraded line these events carry no suppression — an unchanged indented
        trailer would be roughly 1,440 fabricated wakes a day. `said` rides on
        the pending entry, so a CORRECTED body announces again."""
        state, reread = self._pending(self.INDENTED)
        with mock.patch.object(gw, "companion_merged",
                               side_effect=AssertionError("must not be consulted")):
            first, state = _poll(None, [reread], state=state)
            second, state = _poll(None, [reread], state=state)
        self.assertEqual([l.split()[0] for l in first].count("type=pair_trailer_unparsed"), 1, first)
        self.assertEqual([l.split()[0] for l in second].count("type=pair_trailer_unparsed"), 0,
                         "the same unchanged trailer must not be re-announced: %s" % second)
        self.assertEqual(len(state["pair_pending"]), 1, "still held, still retrying")

    def test_a_trailer_value_cannot_inject_a_fabricated_event(self):
        """A review found: a PR body is attacker-controllable text.

        `_normalise` folds CRLF but NOT U+2028, and `hooks/stop_drain.py` splits
        its log with `splitlines()`, which DOES break on U+2028 — so an
        unsanitised value carrying one plus `type=pr_merged ...` becomes a
        separate, fabricated Watchtower event. The open and merge paths already
        pass every value through `one_line`; this retry path did not.
        """
        evil = "not-a-ref\u2028type=pr_merged repo=fake #1 | fabricated"
        state, reread = self._pending("Lands-with: %s\n" % evil)
        with mock.patch.object(gw, "companion_merged",
                               side_effect=AssertionError("must not be consulted")):
            emitted, _ = _poll(None, [reread], state=state)
        said = [l for l in emitted if l.startswith("type=pair_trailer_unparsed")]
        self.assertEqual(len(said), 1, emitted)
        self.assertEqual(len(said[0].splitlines()), 1, "one event, one line: %r" % said[0])
        self.assertNotIn("type=pr_merged", "\n".join(said[0].splitlines()[1:]))

    def test_an_unreadable_trailer_BESIDE_a_readable_one_is_still_announced(self):
        """A review found: holding is conditioned on nothing readable, so a
        partial parse settles as soon as the readable companion answers — and the
        entry is then deleted. An already-closed PR never passes the open or
        merge notification paths again, so if this path stays silent the newly
        unreadable companion is neither checked NOR reported, ever."""
        body = ("Lands-with: acme/Agent-Watchtower#8\n"
                "  Lands-with: acme/other-repo#4\n")     # indented: no longer a declaration
        state, reread = self._pending(body)
        with mock.patch.object(gw, "companion_merged", lambda repo, num: False):
            emitted, state = _poll(None, [reread], state=state)
        types = [l.split()[0] for l in emitted]
        self.assertIn("type=pair_window_open", types, "the readable one is still checked")
        self.assertIn("type=pair_trailer_unparsed", types, "and the unreadable one is told")
        self.assertTrue(any("acme/other-repo#4" in l for l in emitted), emitted)
        self.assertEqual(state["pair_pending"], [], "settled — holding here would wedge it")

    def test_one_line_neutralises_EVERY_separator_splitlines_honours(self):
        """The injection guard is incidental, so it is pinned here (a reviewer's
        framing, corroborated on `main`).

        `one_line` is a DISPLAY helper — collapse whitespace, truncate. It stops
        log injection only because Python's `\\s` on a `str` pattern is
        Unicode-aware and happens to include U+2028, U+2029, U+0085 and the
        file/group/record separators. Nothing in the code says that is a security
        property, so a reasonable-looking simplification to
        `s.replace("\n", " ").replace("\r", " ")` would remove it silently, with
        no test failing.

        The surface is whatever `hooks/stop_drain.py`'s `parse_events` splits on,
        which is `str.splitlines()`. So the separator set is DERIVED from
        `splitlines` itself rather than hand-written — a hand-written list is how
        a surface like this goes stale when a Python release adds one.
        """
        separators = [chr(c) for c in range(0x2100)
                      if len(("a%sb" % chr(c)).splitlines()) > 1]
        self.assertGreaterEqual(len(separators), 8, "derivation found suspiciously few")
        self.assertIn("\u2028", separators)
        self.assertIn("\x1c", separators)          # file separator; not whitespace by eye
        for sep in separators:
            hostile = "x%stype=pr_merged repo=app #999 | FABRICATED" % sep
            collapsed = gw.one_line(hostile, 200)
            self.assertEqual(len(collapsed.splitlines()), 1,
                             "%r survives one_line and would inject a fabricated event" % sep)

    def test_the_merge_transition_announces_an_unreadable_trailer_exactly_once(self):
        """A review found: `poll_profile` already emits the event before it
        calls `settle_pair`, and a fresh entry's `said` list was empty (a
        seeds it now) — so a merged body carrying one readable and one unreadable
        trailer fired the identical diagnostic twice in the same cycle, into the
        log and the drain. Only a RE-READ announces."""
        body = ("Lands-with: acme/Agent-Watchtower#8\n"
                "  Lands-with: acme/other-repo#4\n")
        closed = _pr(body=body, **self.CLOSED)
        with mock.patch.object(gw, "companion_merged", lambda repo, num: False):
            emitted, _ = _poll([_pr(569, body=body)], [closed])
        said = [l for l in emitted if l.startswith("type=pair_trailer_unparsed")]
        self.assertEqual(len(said), 1, "one trailer, one diagnostic: %s" % said)
        self.assertIn("acme/other-repo#4", said[0])

    def test_a_failed_companion_check_is_retried_next_cycle_not_lost(self):
        """A review found: the merge transition runs once. A check that fails then
        is kept in state and retried until it answers — the window is said one cycle
        late instead of never, and the recovery is earned by a request, not by the
        absence of a transition."""
        closed = _pr(body=self.BODY, **self.CLOSED)
        with mock.patch.object(gw, "companion_merged", side_effect=RuntimeError("HTTP 502")):
            emitted, state = _poll([_pr(569, body=self.BODY)], [closed])
        self.assertNotIn("type=pair_window_open", [line.split()[0] for line in emitted])
        self.assertEqual([(e["repo"], e["number"], e["done"]) for e in state["pair_pending"]],
                         [(REPO, "569", [])])
        # next cycle: no transition (the PR is on record as closed); the companion answers "open"
        with mock.patch.object(gw, "companion_merged", lambda repo, num: False):
            emitted, state = _poll(None, [closed], state=state)
        types = [line.split()[0] for line in emitted]
        self.assertEqual(types, ["type=pair_window_open", "type=degraded_recovered"], emitted)
        self.assertIn("repo=app #569 waits-on=acme/Agent-Watchtower#8 | [PROJ-900] perf nightly ref", emitted[0])
        self.assertEqual(state["pair_pending"], [])

    def test_a_retried_check_that_finds_the_companion_merged_says_nothing_and_forgets(self):
        state = _seeded_state([_pr(body=self.BODY, **self.CLOSED)])
        state["pair_pending"] = [{"repo": REPO, "number": "569", "title": "[PROJ-900] perf nightly ref", "done": []}]
        with mock.patch.object(gw, "companion_merged", lambda repo, num: True):
            emitted, state = _poll(None, [_pr(body=self.BODY, **self.CLOSED)], state=state)
        self.assertEqual(emitted, [])
        self.assertEqual(state["pair_pending"], [])

    def test_a_check_that_keeps_failing_is_kept_and_keeps_the_hole_open(self):
        state = _seeded_state([_pr(body=self.BODY, **self.CLOSED)])
        state["pair_pending"] = [{"repo": REPO, "number": "569", "title": "t", "done": []}]
        with mock.patch.object(gw, "companion_merged", side_effect=RuntimeError("HTTP 404")):
            emitted, state = _poll(None, [_pr(body=self.BODY, **self.CLOSED)], state=state)
        self.assertTrue(any(line.startswith("type=degraded sub=pairs") for line in emitted), emitted)
        self.assertEqual(len(state["pair_pending"]), 1, "a mistyped trailer stays a visible hole, not a forgotten one")

    def test_pending_checks_for_a_repo_no_longer_configured_are_dropped(self):
        """A review found: a retry for a repo the profile stopped watching would
        announce a window that is not this profile's, or fail forever."""
        state = _seeded_state([])
        state["pair_pending"] = [{"repo": "acme/other", "number": "1", "title": "t", "done": []}]
        with mock.patch.object(gw, "companion_merged", side_effect=AssertionError("must not be called")):
            emitted, state = _poll(None, [], state=state)
        self.assertEqual(emitted, [])
        self.assertEqual(state["pair_pending"], [])

    def test_a_corrected_trailer_is_read_on_retry_not_the_cached_one(self):
        """A review found: the retry re-reads the origin's body. A mistyped
        number that 404'd is not asked again once the author has fixed it."""
        typo = "Adds the ref input.\n\nLands-with: acme/Agent-Watchtower#88\n"
        with mock.patch.object(gw, "companion_merged", side_effect=RuntimeError("HTTP 404")):
            emitted, state = _poll([_pr(569, body=typo)], [_pr(body=typo, **self.CLOSED)])
        self.assertEqual([(e["repo"], e["number"], e["done"]) for e in state["pair_pending"]], [(REPO, "569", [])])
        asked = []

        def check(repo, num):
            asked.append((repo, num))
            return False
        with mock.patch.object(gw, "companion_merged", check):
            emitted, state = _poll(None, [_pr(body=self.BODY, **self.CLOSED)], state=state)  # the body says #8 now
        self.assertEqual(asked, [("acme/Agent-Watchtower", 8)], "the corrected declaration, not the cached one")
        self.assertIn("type=pair_window_open repo=app #569 waits-on=acme/Agent-Watchtower#8 |",
                      "\n".join(emitted))
        self.assertEqual(state["pair_pending"], [])

    def test_a_partly_answered_entry_does_not_repeat_the_answered_window(self):
        two = "Adds the ref input.\n\nLands-with: acme/Agent-Watchtower#8\nLands-with: acme/service-auth#3\n"
        answers = {("acme/Agent-Watchtower", 8): False,
                   ("acme/service-auth", 3): RuntimeError("HTTP 502")}

        def check(repo, num):
            out = answers[(repo, num)]
            if isinstance(out, Exception):
                raise out
            return out
        with mock.patch.object(gw, "companion_merged", check):
            emitted, state = _poll([_pr(569, body=two)], [_pr(body=two, **self.CLOSED)])
        self.assertEqual([line.split()[0] for line in emitted].count("type=pair_window_open"), 1, emitted)
        self.assertEqual(state["pair_pending"][0]["done"], ["acme/Agent-Watchtower#8"])
        answers[("acme/service-auth", 3)] = False
        with mock.patch.object(gw, "companion_merged", check):
            emitted, state = _poll(None, [_pr(body=two, **self.CLOSED)], state=state)
        windows = [line for line in emitted if line.startswith("type=pair_window_open")]
        self.assertEqual(len(windows), 1, emitted)
        self.assertIn("waits-on=acme/service-auth#3", windows[0], "only the one not yet answered")
        self.assertEqual(state["pair_pending"], [])

    def test_a_near_miss_trailer_is_announced_at_open_and_at_merge_not_swallowed(self):
        """A review found: `Lands with: Agent-Watchtower#8` (owner missing)
        parsed to nothing, silently — the daemon degraded back to prose on a typo."""
        near = "Adds the ref input.\n\nLands with: Agent-Watchtower#8\n"
        never = mock.patch.object(gw, "companion_merged", side_effect=AssertionError("nothing parsed, nothing to ask"))
        with never:
            emitted, state = _poll([], [_pr(569, body=near)])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=pr_opened", "type=pair_trailer_unparsed"], emitted)
        self.assertIn("repo=app #569 | Lands-with: Agent-Watchtower#8", emitted[1])
        with never:
            emitted, state = _poll(None, [_pr(body=near, **self.CLOSED)], state=state)
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=pr_merged", "type=pair_trailer_unparsed"], emitted)

    def test_a_pull_url_trailer_opens_a_window_like_the_short_form(self):
        url_body = "Lands-with: https://github.com/acme/Agent-Watchtower/pull/8\n"
        asked = []

        def check(repo, num):
            asked.append((repo, num))
            return False
        with mock.patch.object(gw, "companion_merged", check):
            emitted, _ = _poll([_pr(569, body=url_body)], [_pr(body=url_body, **self.CLOSED)])
        self.assertEqual(asked, [("acme/Agent-Watchtower", 8)])
        self.assertIn("type=pair_window_open", [line.split()[0] for line in emitted])

    def test_a_fenced_example_is_not_a_declaration(self):
        fenced = "How to declare:\n```\nLands-with: acme/Agent-Watchtower#8\n```\n"
        with mock.patch.object(gw, "companion_merged", side_effect=AssertionError("must not be called")):
            emitted, _ = _poll([_pr(569, body=fenced)], [_pr(body=fenced, **self.CLOSED)])
        self.assertEqual([line.split()[0] for line in emitted], ["type=pr_merged"], emitted)

    def test_a_crlf_body_still_opens_the_window_at_merge_time(self):
        """A review found, on the immediate path: a body written through GitHub's web
        form is CRLF, and used to parse to nothing — no window, no unparsed
        note, silence."""
        crlf = self.BODY.replace("\n", "\r\n")
        asked = []

        def check(repo, num):
            asked.append((repo, num))
            return False
        with mock.patch.object(gw, "companion_merged", check):
            emitted, _ = _poll([_pr(569, body=crlf)], [_pr(body=crlf, **self.CLOSED)])
        self.assertEqual(asked, [("acme/Agent-Watchtower", 8)])
        self.assertEqual([line.split()[0] for line in emitted],
                         ["type=pr_merged", "type=pair_window_open"], emitted)

    def test_a_tilde_fenced_example_is_not_a_declaration(self):
        """A review found: valid GitHub Markdown the old three-backtick strip missed."""
        fenced = "How to declare:\n~~~\nLands-with: acme/Agent-Watchtower#8\n~~~\n"
        with mock.patch.object(gw, "companion_merged", side_effect=AssertionError("must not be called")):
            emitted, _ = _poll([_pr(569, body=fenced)], [_pr(body=fenced, **self.CLOSED)])
        self.assertEqual([line.split()[0] for line in emitted], ["type=pr_merged"], emitted)

    def test_pending_checks_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gw, "STATE_DIR", tmp):
            st = gw.load_state("acme")
            st["since"] = "2026-09-06T05:30:00+00:00"
            st["pair_pending"] = [{"repo": REPO, "number": "569", "title": "t", "done": ["acme/other#3"]}]
            gw.save_state("acme", st)
            self.assertEqual(gw.load_state("acme")["pair_pending"], st["pair_pending"])


if __name__ == "__main__":
    unittest.main()


def _run(rid, event, conclusion, name="Perf nightly", branch="main"):
    return {"id": rid, "status": "completed", "run_attempt": 1, "name": name,
            "conclusion": conclusion, "event": event, "head_branch": branch,
            "display_title": name, "created_at": OLD_CREATED}


class _RunsGH(_FakeGH):
    """Answers the runs endpoint from a fixed list instead of empty."""

    def __init__(self, runs):
        super().__init__([])
        self.runs = runs

    def __call__(self, path):
        if "/actions/runs" in path:
            return {"workflow_runs": self.runs}
        return super().__call__(path)


def _poll_runs(runs, state):
    emitted = []
    with mock.patch.object(gw, "gh_api", _RunsGH(runs)), \
            mock.patch.object(gw, "emit", lambda profile, line: emitted.append(line)), \
            mock.patch.object(gw, "save_state", lambda profile, st: None):
        gw.poll_profile("acme", CFG, state)
    return emitted


class ScheduledBaseline(unittest.TestCase):
    """Only a scheduled run may move the scheduled baseline.

    ⚠ THIS BECAME REACHABLE WHEN `perf-nightly.yml` GAINED A `ref` INPUT.
    A candidate dispatched from `main` reports the same workflow NAME and
    `head_branch=main` to the runs API — nothing in the payload says it
    measured a branch. `wf_last[repo][name]` is read in exactly one place, the
    suppression rule for `event == "schedule"`, so a write from any other
    event cannot be read for its own sake. It can only corrupt the next
    scheduled comparison.
    """

    def test_a_dispatched_run_does_not_consume_the_nightly_recovery(self):
        """The failure a review found, reproduced.

        Nightly fails; someone dispatches a candidate branch that passes;
        the next genuine nightly success must STILL announce the recovery.
        Before the fix the dispatch set the baseline to success and the real
        recovery was suppressed — the watcher went quiet exactly when the
        thing it watches came back.
        """
        state = _seeded_state([])
        self.assertEqual(_poll_runs([_run(1, "schedule", "failure")], state),
                         ["type=run repo=app run=1.1 Perf nightly@main: failure | Perf nightly"])
        self.assertEqual(state["wf_last"][REPO]["Perf nightly"], "failure")

        # A candidate dispatch passes. It still emits — manual runs always do —
        # but it must not speak for the cron.
        dispatched = _poll_runs([_run(2, "workflow_dispatch", "success")], state)
        self.assertEqual(len(dispatched), 1, dispatched)
        self.assertEqual(state["wf_last"][REPO]["Perf nightly"], "failure",
                         "a dispatched run moved the scheduled baseline")

        # The real recovery, which must not be swallowed.
        recovery = _poll_runs([_run(3, "schedule", "success")], state)
        self.assertEqual(recovery,
                         ["type=run repo=app run=3.1 Perf nightly@main: success | Perf nightly"],
                         "the genuine nightly recovery was suppressed by an earlier dispatch")
        self.assertEqual(state["wf_last"][REPO]["Perf nightly"], "success")

    def test_steady_state_cron_success_is_still_suppressed(self):
        """The noise rule added earlier must survive the fix: a scheduled
        success while already passing stays quiet."""
        state = _seeded_state([])
        _poll_runs([_run(1, "schedule", "success")], state)
        self.assertEqual(_poll_runs([_run(2, "schedule", "success")], state), [])


def _live(rid, name="CI", branch="main", status="in_progress", conclusion=None, event="push"):
    return {"id": rid, "status": status, "run_attempt": 1, "name": name,
            "conclusion": conclusion, "event": event, "head_branch": branch,
            "display_title": "%s %d" % (name, rid), "created_at": OLD_CREATED}


def _done(run, conclusion="success"):
    return dict(run, status="completed", conclusion=conclusion)


def _newer(first, n=20):
    """``n`` completed runs newer than everything else: what pushes a run off the page."""
    return [_done(_live(i, name="Deploy (dev)")) for i in range(first, first + n)]


class _PagedRunsGH(_FakeGH):
    """The runs endpoint answers a fixed page; ``runs/<id>`` answers from ``by_id``.

    A ``by_id`` value is a run, or an exception to raise. An id missing from
    ``by_id`` answers 404, as `gh api` does for a deleted run. Every per-id
    request is recorded in ``fetched``.
    """

    def __init__(self, page, by_id=None):
        super().__init__([])
        self.page = page
        self.by_id = by_id or {}
        self.fetched = []

    def __call__(self, path):
        one = re.match(r"repos/%s/actions/runs/(\d+)$" % re.escape(REPO), path)
        if one:
            rid = int(one.group(1))
            self.fetched.append(rid)
            answer = self.by_id.get(rid)
            if answer is None:
                raise RuntimeError("gh api repos/%s/actions/runs/%d failed: HTTP 404" % (REPO, rid))
            if isinstance(answer, Exception):
                raise answer
            return answer
        if "/actions/runs" in path:
            return {"workflow_runs": self.page}
        return super().__call__(path)


def _cycle(gh, state):
    emitted = []
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(gw, "LOG_DIR", os.path.join(tmp, "logs")), \
            mock.patch.object(gw, "STATE_DIR", os.path.join(tmp, "state")), \
            mock.patch.object(gw, "gh_api", gh), \
            mock.patch.object(gw, "emit", lambda profile, line: emitted.append(line)), \
            mock.patch.object(gw, "save_state", lambda profile, st: None):
        gw.poll_profile("acme", CFG, state)
    return emitted


def _ci(lines):
    return [line for line in lines if " CI@" in line]


class InFlightRuns(unittest.TestCase):
    """A run's completion is announced even after it falls off the page.

    The runs poll reads one page: the 20 NEWEST runs created since the
    watermark's date. A merge to app starts six or seven runs, so a
    ~36-minute ``CI@main`` run still going when two or three more merges land is
    off that page by the time it finishes. Its completion was NEVER emitted, not
    late: six of the nine CI@main outcomes on 2026-09-29 never arrived, and on an
    earlier night one of the missing six was a failure. A seat waiting on
    "CI@main: success" waited forever.

    The fix: a run seen in flight is remembered, and once it is off the page it
    is fetched by id each cycle until it completes.
    """

    def test_a_run_that_finishes_off_the_page_still_emits_exactly_once(self):
        """The ticket's regression test: a run created before the last poll
        completes after it, with more than 20 newer runs in between."""
        state = _seeded_state([])
        ci = _live(100)
        self.assertEqual(_ci(_cycle(_PagedRunsGH([ci]), state)), [], "an in-flight run emitted")

        gh = _PagedRunsGH(_newer(101), by_id={100: _done(ci)})
        self.assertEqual(_ci(_cycle(gh, state)),
                         ["type=run repo=app run=100.1 CI@main: success | CI 100"],
                         "a run that finished off the page was never announced")
        self.assertEqual(gh.fetched, [100])

        again = _PagedRunsGH(_newer(101), by_id={100: _done(ci)})
        self.assertEqual(_ci(_cycle(again, state)), [], "the completion was announced twice")
        self.assertEqual(again.fetched, [], "a completed run is still being fetched")

    def test_a_failure_that_finishes_off_the_page_is_announced(self):
        state = _seeded_state([])
        ci = _live(100)
        _cycle(_PagedRunsGH([ci]), state)
        self.assertEqual(_ci(_cycle(_PagedRunsGH(_newer(101), by_id={100: _done(ci, "failure")}), state)),
                         ["type=run repo=app run=100.1 CI@main: failure | CI 100"])

    def test_a_run_still_going_off_the_page_is_checked_again_next_cycle(self):
        state = _seeded_state([])
        ci = _live(100)
        _cycle(_PagedRunsGH([ci]), state)

        still = _PagedRunsGH(_newer(101), by_id={100: ci})
        self.assertEqual(_ci(_cycle(still, state)), [])
        self.assertEqual(still.fetched, [100])

        finished = _PagedRunsGH(_newer(101), by_id={100: _done(ci)})
        self.assertEqual(_ci(_cycle(finished, state)),
                         ["type=run repo=app run=100.1 CI@main: success | CI 100"])

    def test_a_run_still_on_the_page_is_not_fetched_by_id(self):
        state = _seeded_state([])
        ci = _live(100)
        _cycle(_PagedRunsGH([ci]), state)
        going = _PagedRunsGH([ci])
        _cycle(going, state)
        finished = _PagedRunsGH([_done(ci)])
        self.assertEqual(_ci(_cycle(finished, state)),
                         ["type=run repo=app run=100.1 CI@main: success | CI 100"])
        self.assertEqual(going.fetched + finished.fetched, [], "a run on the page was fetched by id")

    def test_a_run_in_flight_at_the_first_run_is_announced_when_it_finishes(self):
        """The first run seeds completed runs silently. A run still going then has
        not finished, so its finish is news, even if it lands off the page."""
        state = _seeded_state([])
        state["first_run"] = True
        ci = _live(100)
        self.assertEqual(_cycle(_PagedRunsGH([_done(_live(99)), ci]), state), [])
        self.assertEqual(_ci(_cycle(_PagedRunsGH(_newer(101), by_id={100: _done(ci)}), state)),
                         ["type=run repo=app run=100.1 CI@main: success | CI 100"])

    def test_a_deleted_run_is_dropped_not_fetched_forever(self):
        """A 404 means the run is gone. That is not a coverage hole, and asking again
        every minute would never get a different answer."""
        state = _seeded_state([])
        _cycle(_PagedRunsGH([_live(100)]), state)
        gone = _PagedRunsGH(_newer(101))
        self.assertEqual([line for line in _cycle(gone, state) if line.startswith("type=degraded")], [])
        self.assertEqual(gone.fetched, [100])
        later = _PagedRunsGH(_newer(101))
        _cycle(later, state)
        self.assertEqual(later.fetched, [], "a deleted run is still being fetched")

    def test_a_failed_fetch_is_a_hole_and_is_retried(self):
        state = _seeded_state([])
        ci = _live(100)
        _cycle(_PagedRunsGH([ci]), state)
        err = RuntimeError("gh api repos/%s/actions/runs/100 failed: HTTP 502" % REPO)
        emitted = _cycle(_PagedRunsGH(_newer(101), by_id={100: err}), state)
        self.assertTrue([line for line in emitted if line.startswith("type=degraded sub=runs")], emitted)
        self.assertEqual(_ci(_cycle(_PagedRunsGH(_newer(101), by_id={100: _done(ci)}), state)),
                         ["type=run repo=app run=100.1 CI@main: success | CI 100"],
                         "a run whose fetch failed once was forgotten")

    def test_in_flight_runs_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gw, "STATE_DIR", tmp):
            state = gw.load_state("acme")
            state["first_run"] = False
            ci = _live(100)
            _cycle(_PagedRunsGH([ci]), state)
            gw.save_state("acme", state)
            reloaded = gw.load_state("acme")
        self.assertEqual(_ci(_cycle(_PagedRunsGH(_newer(101), by_id={100: _done(ci)}), reloaded)),
                         ["type=run repo=app run=100.1 CI@main: success | CI 100"],
                         "a restart forgot the runs in flight")

    def test_a_run_in_flight_past_the_limit_is_dropped(self):
        state = _seeded_state([])
        _cycle(_PagedRunsGH([_live(100)]), state)
        tracked = state["runs_inflight"][REPO]
        for rid in tracked:
            tracked[rid] -= gw.INFLIGHT_MAX_SECS + 1
        stale = _PagedRunsGH(_newer(101), by_id={100: _live(100)})
        _cycle(stale, state)
        self.assertEqual(stale.fetched, [])
        self.assertEqual(state["runs_inflight"][REPO], {})

    def test_per_cycle_fetches_are_bounded(self):
        many = [_live(i) for i in range(100, 100 + gw.INFLIGHT_FETCH_CAP + 5)]
        state = _seeded_state([])
        _cycle(_PagedRunsGH(many), state)
        gh = _PagedRunsGH(_newer(1000), by_id={r["id"]: r for r in many})
        _cycle(gh, state)
        self.assertEqual(len(gh.fetched), gw.INFLIGHT_FETCH_CAP)
        self.assertEqual(gh.fetched, sorted(gh.fetched), "oldest first")

    def test_a_cycle_that_defers_runs_past_the_cap_says_so_in_the_daemon_log(self):
        """A review found: a budget squeeze needs a visible cause. When more
        runs are parked off the page than the cap fetches, the daemon log says how
        many were fetched and how many wait. It is a note, not an event."""
        many = [_live(i) for i in range(100, 100 + gw.INFLIGHT_FETCH_CAP + 5)]
        state = _seeded_state([])
        _cycle(_PagedRunsGH(many), state)
        notes = []
        with mock.patch.object(gw, "note", notes.append):
            emitted = _cycle(_PagedRunsGH(_newer(1000), by_id={r["id"]: r for r in many}), state)
        deferred = [n for n in notes if "wait" in n and "off the page" in n]
        self.assertEqual(len(deferred), 1, notes)
        self.assertIn("checking %d this cycle" % gw.INFLIGHT_FETCH_CAP, deferred[0])
        self.assertIn("5 wait", deferred[0])
        self.assertEqual([line for line in emitted if "wait" in line], [], "the deferral became an event")

    def test_no_deferral_note_when_the_cap_is_not_reached(self):
        state = _seeded_state([])
        _cycle(_PagedRunsGH([_live(100)]), state)
        notes = []
        with mock.patch.object(gw, "note", notes.append):
            _cycle(_PagedRunsGH(_newer(101), by_id={100: _live(100)}), state)
        self.assertEqual([n for n in notes if "off the page" in n], [])


# ---------------------------------------------------------------------------
# The dedup memory survives a restart
#
# `save_state` wrote `list(state["seen"])[-2000:]`. `seen` was a SET, so that is
# hash order, randomised per process: past 2000 ids an arbitrary 2000 were kept
# and the rest dropped at random. Whatever the API still returned and had been
# dropped was announced a second time after the next restart — four overnight
# CI completions at one host's restart, with the state files
# at exactly 2000 entries. The standalone watcher found and fixed
# the same one-liner on 2026-08-24; this is that fix, in the multi-profile daemon.
# ---------------------------------------------------------------------------

def _gh_ts(dt):
    """A timestamp the way GitHub's REST API spells it."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Clock(object):
    """Time that passes only when a test says so.

    ``install`` replaces the daemon's ``datetime`` (cycle starts, watermarks) and ``time``
    (in-flight ages, the by-id rotation, the degraded rate limit) with this clock, so a
    test can poll at a real interval, stop the daemon for half an hour, or put two events
    thirty minutes apart. A hub built with the clock stamps runs created NOW on it.
    """

    def __init__(self, start=datetime(2026, 9, 30, 20, 40, tzinfo=timezone.utc)):
        self.now = start

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def at(self, seconds=0):
        """A GitHub timestamp ``seconds`` from now on this clock."""
        return _gh_ts(self.now + timedelta(seconds=seconds))

    def install(self, case):
        clock = self

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now if tz is not None else clock.now.replace(tzinfo=None)

        fake_time = types.SimpleNamespace(time=lambda: clock.now.timestamp(), sleep=time.sleep)
        for patch in (mock.patch.object(gw, "datetime", _FrozenDatetime),
                      mock.patch.object(gw, "time", fake_time)):
            patch.start()
            case.addCleanup(patch.stop)
        return self


class _Hub(object):
    """A miniature GitHub: the reads the poller makes, with the API's own windowing.

    The windowing is the point. The runs endpoint answers the NEWEST ``per_page``
    runs; a PR's reviews answer the FIRST ``per_page`` with no ``since`` filter; a
    repo's comments answer only what changed inside the recent window; the PR
    listing answers the ``per_page`` most recently updated. ``per_page`` is read
    from the request, so a poller that changes what it asks for is served what
    GitHub would serve for that. A request this hub does not model fails the test
    rather than quietly answering empty.
    """

    #: `since` is the watermark minus a five-minute overlap and the poll is a
    #: minute, so a comment stays in the answer for about six polls.
    COMMENT_POLLS = 6

    def __init__(self, repos=(REPO,), clock=None):
        self.repos = list(repos)
        self.clock = clock                                   # a _Clock, or None
        self.polls = 0
        self.listing = dict((r, []) for r in self.repos)     # PRs, most recently updated first
        self.reviews = {}                                    # (repo, "number") -> [review]
        self.comments = dict((r, []) for r in self.repos)    # issue comments, oldest first
        self.runs = dict((r, {}) for r in self.repos)        # id -> run
        self.asked = []
        self.fail = ()                                       # substrings of paths that answer 502
        self.arrive_after_page = {}                          # page -> runs created right after serving it
        self.null_pages = set()                              # pages answered `workflow_runs: null`
        self.fail_when = None                                # a predicate on the path, answers 502
        self._id = 5000000

    def _new_id(self):
        self._id += 1
        return self._id

    def add_run(self, repo=REPO, status="completed", **fields):
        rid = fields.pop("id", None) or self._new_id()
        # With a clock, a run is created NOW on it; without one, long ago (OLD_CREATED).
        stamped = _gh_ts(self.clock.now) if self.clock else _gh_ts(datetime.now(timezone.utc))
        run = {"id": rid, "status": status, "run_attempt": 1, "name": "CI", "conclusion": "success",
               "event": "push", "head_branch": "main", "display_title": "run %d" % rid,
               "updated_at": stamped,
               "created_at": stamped if self.clock else OLD_CREATED}
        run.update(fields)
        self.runs[repo][rid] = run
        return run

    def add_comment(self, repo=REPO, **fields):
        cid = fields.pop("id", None) or self._new_id()
        stamped = _gh_ts(self.clock.now) if self.clock else _gh_ts(datetime.now(timezone.utc))
        comment = {"id": cid, "user": {"login": "bob"}, "body": "comment %d" % cid,
                   "issue_url": "https://api.github.com/repos/%s/issues/7" % repo,
                   "created_at": stamped, "updated_at": stamped,
                   "_first_poll": self.polls + 1}
        comment.update(fields)
        self.comments[repo].append(comment)
        return comment

    def new_review(self, **fields):
        rid = fields.pop("id", None) or self._new_id()
        review = {"id": rid, "user": {"login": "bob"}, "state": "COMMENTED", "body": "review %d" % rid,
                  "submitted_at": _gh_ts(datetime.now(timezone.utc))}
        review.update(fields)
        return review

    def __call__(self, path):
        self.asked.append(path)
        if self.fail_when is not None and self.fail_when(path):
            raise RuntimeError("gh api %s failed: HTTP 502" % path.split("?")[0])
        for needle in self.fail:
            if needle in path:
                raise RuntimeError("gh api %s failed: HTTP 502" % path.split("?")[0])
        m = re.match(r"repos/([^/]+/[^/?]+)/(.*)$", path)
        repo, rest = m.group(1), m.group(2)
        pp = re.search(r"per_page=(\d+)", rest)
        per_page = int(pp.group(1)) if pp else 30
        if rest.startswith("pulls?"):
            return self.listing[repo][:per_page]
        rv = re.match(r"pulls/(\d+)/reviews\?", rest)
        if rv:
            return self.reviews.get((repo, rv.group(1)), [])[:per_page]
        if rest.startswith("issues/comments?"):
            # With a clock, `since` is honoured the way GitHub honours it (updated at or
            # after it, oldest first); without one, a comment is returned for COMMENT_POLLS polls.
            since = re.search(r"since=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", rest)
            if self.clock and since:
                live = [c for c in self.comments[repo] if (c.get("updated_at") or "") >= since.group(1)]
            else:
                live = [c for c in self.comments[repo] if self.polls - c["_first_poll"] < self.COMMENT_POLLS]
            # `page=N` is the Nth slice of the oldest-first list, as GitHub serves it.
            cp = re.search(r"[?&]page=(\d+)", rest)
            cpage = int(cp.group(1)) if cp else 1
            return live[(cpage - 1) * per_page:cpage * per_page]
        if rest.startswith("pulls/comments?"):
            return []
        if rest.startswith("actions/runs?"):
            # Newest first by `created_at`, as GitHub orders the listing, then id.
            # A `_hidden` run is created but not listed yet (listing lag). With a clock the
            # `created>=<date>` filter is honoured too; without one every stamp is OLD_CREATED
            # and a date filter would drop them all, so it is not.
            listed = [r for r in self.runs[repo].values() if not r.get("_hidden")]
            since = re.search(r"created=%3E%3D(\d{4}-\d{2}-\d{2})", rest)
            if self.clock and since:
                listed = [r for r in listed if (r.get("created_at") or "9")[:10] >= since.group(1)]
            # `status=` filters on the run's CURRENT attempt, whatever its age, so a re-run
            # of an old run is listed. The date filter above belongs to the plain listing only: a
            # status listing carries no `created=` and is not narrowed by one.
            by_status = re.search(r"[?&]status=(\w+)", rest)
            if by_status:
                listed = [r for r in listed if r.get("status") == by_status.group(1)]
            # `_order_as` places a run whose `created_at` a test has broken where its real one was.
            newest = sorted(listed, key=lambda r: (r.get("_order_as") or r.get("created_at") or "", r["id"]),
                            reverse=True)
            # `page=N` is served the way GitHub serves it — the Nth slice of the
            # newest-first list. `per_page=` must not match, hence the `[?&]` anchor.
            pg = re.search(r"[?&]page=(\d+)", rest)
            page = int(pg.group(1)) if pg else 1
            served = newest[(page - 1) * per_page:page * per_page]
            if page in self.null_pages:
                return {"workflow_runs": None}
            for _ in range(self.arrive_after_page.pop(page, 0)):
                self.add_run(repo=repo, name="Late")
            return {"workflow_runs": served}
        one = re.match(r"actions/runs/(\d+)$", rest)
        if one:
            run = self.runs[repo].get(int(one.group(1)))
            if run is None:
                raise RuntimeError("gh api %s failed: HTTP 404" % path)
            return run
        raise AssertionError("the poller asked for something this hub does not model: %s" % path)


class _Poller(object):
    """``poll_profile`` against a hub, with the REAL ``load_state`` / ``save_state`` on a private tree.

    ``emit`` and ``note`` are captured; ``LOG_DIR`` and ``STATE_DIR`` point at a
    throwaway directory (the suite's live-write audit fails the run if a test
    reaches the live ones). Every poll ends in the daemon's own save, so
    ``load()`` afterwards is exactly what a restart does.
    """

    def __init__(self, case, hub, profile="acme"):
        self.hub, self.profile = hub, profile
        self.emitted, self.notes, self.raised = [], [], []
        tmp = tempfile.mkdtemp()
        case.addCleanup(shutil.rmtree, tmp, True)
        self.state_dir = os.path.join(tmp, "state")
        for patch in (mock.patch.object(gw, "STATE_DIR", self.state_dir),
                      mock.patch.object(gw, "LOG_DIR", os.path.join(tmp, "logs")),
                      mock.patch.object(gw, "gh_api", hub),
                      mock.patch.object(gw, "emit", lambda p, line: self.emitted.append(line)),
                      mock.patch.object(gw, "note", self.notes.append)):
            patch.start()
            case.addCleanup(patch.stop)
        self.cfg = {"repos": list(hub.repos), "suppress_authors": ["alice"]}

    @property
    def state_path(self):
        return os.path.join(self.state_dir, "%s.json" % self.profile)

    def load(self):
        return gw.load_state(self.profile)

    def saved(self):
        with open(self.state_path) as fh:
            return json.load(fh)

    def poll(self, state, tolerate=False):
        """One cycle; the lines it emitted. ``tolerate`` catches a raised cycle the way
        ``main()`` does (the cycle failed and nothing after the raise ran; since the checkpoint was added
        `poll_profile` has already checkpointed what the cycle recorded, unless it was a
        first run or a legacy-baseline cycle) and records it in ``raised``."""
        self.hub.polls += 1
        del self.emitted[:]
        try:
            gw.poll_profile(self.profile, self.cfg, state)
        except Exception as e:
            if not tolerate:
                raise
            self.raised.append(str(e))
        return list(self.emitted)


_EVENTS = ("type=review", "type=comment", "type=run")


def _news(lines):
    """The review, comment and run lines among ``lines`` — the three kinds the dedup memory guards."""
    return [line for line in lines if line.startswith(_EVENTS)]


def _titles(lines):
    """What each review / comment / run line says after its `` | `` — the item's own body or title."""
    return sorted(line.split(" | ", 1)[1] for line in _news(lines))


def _seed_open_prs(hub, prs=3, reviews_each=10):
    """PRs open from before anyone looked, each with reviews on it."""
    for n in range(1, prs + 1):
        hub.listing[REPO].append(_pr(n))
        hub.reviews[(REPO, str(n))] = [hub.new_review() for _ in range(reviews_each)]


def _churn(hub, poller, state, cycles, runs=6, comment_every=3):
    """The everyday load: ``runs`` new completed runs a poll and now and then a comment.
    Returns every line emitted along the way."""
    out = []
    for i in range(cycles):
        for _ in range(runs):
            hub.add_run()
        if comment_every and i % comment_every == 0:
            hub.add_comment()
        out.extend(poller.poll(state))
    return out


class SeenMemory(unittest.TestCase):
    """The helpers behind the dedup memory: an ordered container, refreshed on sight and
    evicted oldest-first per kind. Ported from the standalone watcher's 2026-08-24 fix
    (github-wake/test_github_wake.py), then extended for a profile with several repos."""

    def test_the_container_is_ordered_and_never_a_set(self):
        """The root cause, directly: a set would make the pruning arbitrary however
        correct the pruning itself is."""
        seen = gw.new_seen(["run:1:1", "review:2", "c:3"])
        self.assertNotIsInstance(seen, set)
        seen["run:4:1"] = None
        self.assertEqual(list(seen), ["run:1:1", "review:2", "c:3", "run:4:1"])
        self.assertIn("review:2", seen)

    def test_touch_moves_a_known_id_to_the_newest_position(self):
        """Eviction must be LAST-seen, not first-seen. Re-assigning a key in a dict
        keeps its old place, so an id inserted once and merely re-observed would
        still drift to the front — and a review on a PR that stays open is
        re-fetched in full every cycle."""
        seen = gw.new_seen(["run:1:1", "review:2", "run:3:1"])
        gw.touch_seen(seen, "review:2")
        self.assertEqual(list(seen), ["run:1:1", "run:3:1", "review:2"])

    def test_touch_is_a_no_op_for_an_unknown_id(self):
        seen = gw.new_seen(["run:1:1"])
        gw.touch_seen(seen, "run:99:1")
        self.assertEqual(list(seen), ["run:1:1"])

    def test_insertion_order_is_preserved(self):
        order = ["run:%d:1" % i for i in range(10)]
        self.assertEqual(gw.prune_seen(order), order)

    def test_the_newest_ids_of_an_overflowing_kind_are_kept_in_order(self):
        cap = gw.SEEN_CAPS["run"]
        order = ["run:%d:1" % i for i in range(cap + 50)]
        kept = gw.prune_seen(order)
        self.assertEqual(kept, order[-cap:])
        self.assertNotIn("run:0:1", kept)

    def test_exempt_ids_are_kept_past_the_cap_and_count_against_nothing(self):
        """An exempt id (one a held runs read can still return) survives whatever its
        age, in its place, and does not use up a slot: the other ids of the kind keep the whole cap."""
        cap = gw.SEEN_CAPS["run"]
        order = ["run:%d:1" % i for i in range(cap + 50)]
        exempt = frozenset(order[:5] + order[20:22])
        kept = gw.prune_seen(order, exempt=exempt)
        self.assertEqual(kept, [m for m in order if m in exempt] + order[-cap:])
        self.assertEqual(len(kept), cap + len(exempt))

    def test_an_id_that_is_not_exempt_is_pruned_to_the_cap_however_many_are(self):
        cap = gw.SEEN_CAPS["run"]
        order = ["run:%d:1" % i for i in range(cap * 3)]
        exempt = frozenset(order[:3])
        kept = gw.prune_seen(order, exempt=exempt)
        self.assertEqual(len(kept), cap + 3)
        self.assertEqual(set(kept) - exempt, set(order[-cap:]))
        self.assertEqual(gw.prune_seen(order, exempt=frozenset()), order[-cap:])
        self.assertEqual(gw.prune_seen(order), order[-cap:])

    def test_an_exempt_id_that_is_not_in_the_memory_adds_nothing(self):
        order = ["run:%d:1" % i for i in range(10)]
        self.assertEqual(gw.prune_seen(order, exempt=frozenset(["run:99:1"])), order)

    def test_a_burst_of_one_kind_cannot_evict_another_kinds_ids(self):
        """One shared budget was the standalone bug's second half: the kind that
        needs the SHORTEST memory (runs) was winning the eviction race against the
        one that needs the longest (reviews)."""
        order = (["review:%d" % i for i in range(50)]
                 + ["run:%d:1" % i for i in range(gw.SEEN_CAPS["run"] + 500)]
                 + ["c:%d" % i for i in range(50)])
        kept = gw.prune_seen(order)
        kinds = {}
        for mid in kept:
            kinds[mid.split(":", 1)[0]] = kinds.get(mid.split(":", 1)[0], 0) + 1
        self.assertEqual(kinds, {"review": 50, "c": 50, "run": gw.SEEN_CAPS["run"]})

    def test_unknown_kinds_share_one_bounded_budget(self):
        """A kind nobody sized still gets a bound — one bucket for ALL of them, so a
        stream of distinct unknown prefixes cannot grow the file without limit."""
        order = ["weird%d:%d" % (k, i) for k in range(50) for i in range(20)]
        self.assertEqual(len(gw.prune_seen(order)), gw.SEEN_CAP_OTHER)

    def test_caps_scale_with_the_number_of_repos_a_profile_watches(self):
        """Every window is per repo: a profile watching three repos asks three times
        the questions, so it must remember three times the answers."""
        one, three = gw.seen_caps(1), gw.seen_caps(3)
        self.assertEqual(three, dict((k, 3 * v) for k, v in one.items()))
        self.assertEqual(gw.seen_caps(0), one, "no repos is still a usable cap")
        order = ["run:%d:1" % i for i in range(gw.SEEN_CAPS["run"] * 4)]
        self.assertEqual(len(gw.prune_seen(order, 3)), 3 * gw.SEEN_CAPS["run"])

    def test_poll_profile_prunes_with_the_profiles_repo_count(self):
        """The count reaches the pruning: with a cap of two per repo, three repos keep six."""
        other = "acme/agent-watchtower"
        hub = _Hub(repos=[REPO, other, "acme/workstation-daemons"])
        for repo in hub.repos:
            for _ in range(5):
                hub.add_run(repo=repo)
        with mock.patch.object(gw, "SEEN_CAPS", {"review": 2, "run": 2, "c": 2}):
            poller = _Poller(self, hub)
            state = poller.load()
            poller.poll(state)
            self.assertEqual(sum(1 for k in state["seen"] if k.startswith("run:")), 6)
            solo = _Hub()
            for _ in range(5):
                solo.add_run()
            solo_poller = _Poller(self, solo)
            solo_state = solo_poller.load()
            solo_poller.poll(solo_state)
            self.assertEqual(sum(1 for k in solo_state["seen"] if k.startswith("run:")), 2)

    def test_the_caps_cover_every_query_window_at_least_twice(self):
        """Sizing is DERIVED from the queries, not guessed. The window per repo is what one
        poll can return:

          review  15 listed PRs x 20 reviews, no `since` filter   = 300
          run     the newest 20; an id never comes back once off  =  20
          c       2 endpoints x 30 x 5 pages, `since`-filtered = 300

        An id that keeps being returned needs one window. Two is the floor because an id
        the API returns this poll but not the next (a PR at the edge of the newest-15
        listing, say) has to survive a whole window of other ids being refreshed over it."""
        self.assertEqual(gw.SEEN_WINDOW, {"review": 300, "run": 20, "c": 300})
        for kind, window in gw.SEEN_WINDOW.items():
            self.assertGreaterEqual(gw.SEEN_CAPS[kind], 2 * window, kind)

    def test_the_caps_are_pinned_so_a_change_is_deliberate(self):
        """review 2 windows (room for a PR that flickers at the edge of the newest-15
        listing, and for one that leaves it and comes back); run 10 windows (slack that
        costs no other kind a slot); c 6 windows (an id is returnable for OVERLAP + one
        poll = six polls, at most one window of new ids in each). The reasoning is in the
        comment above `SEEN_KEEP`; the behaviour it buys is tested in
        `RestartKeepsTheDedupMemory`."""
        self.assertEqual(gw.SEEN_CAPS, {"review": 600, "run": 200, "c": 1800})    # c was 360
        self.assertEqual(gw.SEEN_CAP_OTHER, 300)

    def test_the_windows_are_what_the_poller_really_asks_for(self):
        """The window constants BUILD the request URLs, and this reads the URLs back.
        A cap sized from a number the query does not use is a cap sized from a guess."""
        hub = _Hub()
        hub.listing[REPO] = [_pr(1)]
        hub.add_run()
        poller = _Poller(self, hub)
        poller.poll(poller.load())
        for needle, window in (("/pulls?state=all", gw.PRS_PER_PAGE),
                               ("/reviews", gw.REVIEWS_PER_PAGE),
                               ("/issues/comments", gw.COMMENTS_PER_PAGE),
                               ("/pulls/comments", gw.COMMENTS_PER_PAGE),
                               ("/actions/runs?", gw.RUNS_PER_PAGE),
                               ("/actions/runs?status=", gw.RUNS_STATUS_PER_PAGE)):
            paths = [p for p in hub.asked if needle in p]
            if needle == "/actions/runs?":
                # The plain listing only; the status listings ask their own page size, below.
                paths = [p for p in paths if "status=" not in p]
            self.assertTrue(paths, "the poller never asked for %s" % needle)
            for path in paths:
                self.assertIn("per_page=%d" % window, path)
        self.assertEqual(gw.SEEN_WINDOW["review"], gw.PRS_PER_PAGE * gw.REVIEWS_PER_PAGE)
        self.assertEqual(gw.SEEN_WINDOW["run"], gw.RUNS_PER_PAGE)
        self.assertEqual(gw.SEEN_WINDOW["c"], gw.COMMENT_ENDPOINTS * gw.COMMENTS_PER_PAGE * gw.COMMENTS_MAX_PAGES)
        # The two status listings (in progress, queued) are counted apart; they are not the page-back.
        self.assertEqual(len([p for p in hub.asked if "/actions/runs?" in p and "status=" not in p]), 1,
                         "a quiet repo paged back: the steady state is one runs request a poll")
        self.assertEqual(len([p for p in hub.asked if "/actions/runs?" in p and "status=" in p]), 2,
                         "the status listings are one request each for in progress and queued")
        self.assertEqual(len([p for p in hub.asked if "/comments?" in p]), gw.COMMENT_ENDPOINTS,
                         "the comment window assumes exactly this many endpoints a poll")

    def test_the_run_cap_covers_a_full_page_back_between_two_sightings(self):
        """One poll's window of NEW run ids is still the newest page; a burst read from
        deeper pages is re-read for a poll or two inside the margin, but those ids are known and
        only refreshed. What each cycle's page-back adds is up to (RUNS_MAX_PAGES - 1) pages of
        new ids, and the by-id fetches add more, all between two sightings of a run that sits
        on the first page. The cap must cover that churn, or a page-one run is evicted and
        announced again: 19 newer ids on page one after it, the deeper pages, the fetches,
        and 19 older ids on page one before it the next cycle."""
        between = ((gw.RUNS_PER_PAGE - 1) * 2 + gw.RUNS_PER_PAGE * (gw.RUNS_MAX_PAGES - 1)
                   + gw.INFLIGHT_FETCH_CAP)
        self.assertLessEqual(between, gw.SEEN_CAPS["run"], between)


class RestartKeepsTheDedupMemory(unittest.TestCase):
    """The ticket's regression tests: through the real load, poll and save, over enough
    cycles that more than 2000 ids have been seen."""

    def test_a_restart_re_emits_nothing_the_api_still_returns(self):
        """THE restart test. 600 polls of everyday load — six runs a poll, a comment
        every third — is 3,800 ids seen against the old 2,000 cap. Save, load, poll:
        the API still returns the 20 newest runs, the recent comments and every review
        on three open PRs, and none of it may be announced again."""
        hub = _Hub()
        _seed_open_prs(hub)
        poller = _Poller(self, hub)
        state = poller.load()
        self.assertEqual(poller.poll(state), [], "the first run seeds silently")
        lines = _news(_churn(hub, poller, state, cycles=600))
        runs = [l for l in lines if l.startswith("type=run")]
        self.assertEqual(len(runs), 600 * 6, "the simulation itself lost or repeated a run")
        self.assertEqual(len(set(runs)), len(runs))

        restarted = poller.load()
        again = _news(poller.poll(restarted))
        self.assertEqual(again, [], "a restart re-announced %d event(s) the API still returns: %s"
                         % (len(again), again[:3]))

        # Not vacuous: a daemon that emitted nothing at all would pass the line above.
        hub.add_run()
        hub.add_comment()
        self.assertEqual(len(_news(poller.poll(restarted))), 2, "a genuinely new event was swallowed")
        # And the second restart is as quiet as the first.
        self.assertEqual(_news(poller.poll(poller.load())), [])

    def test_a_burst_of_runs_does_not_make_a_reviews_id_forgettable(self):
        """Per-kind memory, as behaviour. Three PRs fall out of the newest-15 listing —
        their reviews are no longer asked for, so nothing refreshes them — while 4,200
        runs churn through. When the PRs come back, after a restart, their thirty
        reviews are still remembered: only another review could have pushed them out."""
        hub = _Hub()
        _seed_open_prs(hub, prs=3, reviews_each=10)
        listed = list(hub.listing[REPO])
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        hub.listing[REPO] = []
        _churn(hub, poller, state, cycles=700)
        hub.listing[REPO] = listed
        again = _news(poller.poll(poller.load()))
        self.assertEqual(again, [], "%d review(s) came back as new after a burst of runs" % len(again))

    def test_a_review_the_api_keeps_returning_is_never_evicted(self):
        """LAST-seen, not first-seen: a long-lived PR stays in the listing while 1,000
        reviews arrive elsewhere — more than the review cap. Its own four are
        re-fetched every poll, so they must never age out and be announced again."""
        hub = _Hub()
        hub.listing[REPO] = [_pr(1)]
        hub.reviews[(REPO, "1")] = [hub.new_review(body="long-lived %d" % i) for i in range(4)]
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        seen_lines = []
        for i in range(500):
            n = 100 + i
            hub.listing[REPO] = [_pr(n), _pr(1)]
            hub.reviews[(REPO, str(n))] = [hub.new_review(body="churn %d.%d" % (n, k)) for k in range(2)]
            seen_lines.extend(_news(poller.poll(state)))
        self.assertEqual([l for l in seen_lines if "long-lived" in l], [],
                         "a review on a PR that never closed was announced again")
        self.assertEqual(len([l for l in seen_lines if l.startswith("type=review")]), 1000,
                         "the churn itself must be announced once each")

    def test_the_saved_list_is_oldest_first_so_the_reload_evicts_the_right_ids(self):
        """The order in the FILE is the memory: runs that have left the page sit before the
        runs still on it, and the newest are last. Five in each group, so a hash-ordered
        list cannot line up by luck."""
        hub = _Hub()
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        gone = [hub.add_run() for _ in range(5)]
        stays = [hub.add_run() for _ in range(5)]
        poller.poll(state)
        for run in gone:
            del hub.runs[REPO][run["id"]]
        newest = [hub.add_run() for _ in range(5)]
        poller.poll(state)
        seen = poller.saved()["seen"]

        def places(runs):
            return [seen.index("run:%d:1" % r["id"]) for r in runs]
        self.assertLess(max(places(gone)), min(places(stays)), "the file is not oldest-first: %s" % seen)
        self.assertLess(max(places(stays)), min(places(newest)), "the file is not oldest-first: %s" % seen)

    def test_a_quiet_repos_runs_survive_a_busy_repos_churn(self):
        """The multi-repo case a single-repo watcher never meets. One profile has one `run:`
        budget. The quiet repo's newest 20 runs sit on its page for days, and in that time the
        busy repo pushes far more than the cap through the same budget. Refreshing an id on
        every sighting is what keeps the quiet repo's runs from being announced again."""
        busy, quiet = REPO, "acme/workstation-daemons"
        hub = _Hub(repos=[busy, quiet])
        for _ in range(20):
            hub.add_run(repo=quiet)
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)                                     # first run: baseline
        said = []
        for _ in range(300):
            for _ in range(6):
                hub.add_run(repo=busy)                         # 1,800 runs in all: more than 2 repos' run cap
            said.extend(_news(poller.poll(state)))
        self.assertEqual(len([l for l in said if l.startswith("type=run")]), 1800)
        self.assertEqual([l for l in said if "repo=workstation-daemons" in l], [],
                         "the quiet repo's runs were announced again")

    def test_a_comment_that_keeps_being_edited_is_never_announced_again(self):
        """A bot that rewrites one summary comment on every push keeps it in the `since` window
        for days, because each edit moves its `updated_at` and the API returns it again. It has
        to be refreshed like any other id: left to age by FIRST sighting it is evicted after a
        few hundred newer comments and announced as new at its next edit."""
        hub = _Hub()
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        bot = hub.add_comment(body="coverage report")
        self.assertEqual(len(_news(poller.poll(state))), 1)
        said = []
        for _ in range(300):
            bot["_first_poll"] = hub.polls + 1          # an edit: the window opens again on it
            hub.add_comment()
            hub.add_comment()                           # 600 newer comments in all: more than the cap
            said.extend(_news(poller.poll(state)))
        self.assertEqual([l for l in said if "coverage report" in l], [],
                         "an edited comment was announced as if it were new")
        self.assertEqual(len(said), 600, "every genuinely new comment is announced exactly once")

    def test_a_pr_at_the_edge_of_the_listing_keeps_its_reviews_while_it_sits_a_poll_out(self):
        """Why the review cap is TWO windows. The listing is the 15 most recently updated PRs, so a
        PR at rank 15 flickers in and out as others are updated. Take 14 stable PRs and two that
        share the last slot: every poll touches a full window (15 x 20 = 300 review ids), and the
        PR that sat this poll out is untouched. With room for one window its reviews are evicted
        the moment it is out and announced again the moment it is back, every poll, for ever."""
        hub = _Hub()
        stable = [_pr(n) for n in range(1, 15)]
        edge = [_pr(15), _pr(16)]
        for pr in stable + edge:
            n = pr["number"]
            hub.reviews[(REPO, str(n))] = [hub.new_review(body="pr %d review %d" % (n, k)) for k in range(20)]
        hub.listing[REPO] = stable + [edge[0]]
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)                                     # first run: baseline
        hub.listing[REPO] = stable + [edge[1]]
        poller.poll(state)                                     # PR 16 is seen for the first time: 20 real reviews
        for cycle in range(60):
            hub.listing[REPO] = stable + [edge[cycle % 2]]
            again = _news(poller.poll(state))
            self.assertEqual(again, [], "cycle %d: %d review(s) of the PR at the edge came back" % (cycle, len(again)))

    def test_what_is_saved_is_exactly_what_the_running_process_knows(self):
        """A restart must change nothing, so the file has to hold the process's OWN memory:
        the same ids in the same order, pruned the same way. Otherwise the running daemon
        and the restarted one dedupe differently, which is this ticket in another shape."""
        hub = _Hub()
        _seed_open_prs(hub)
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        _churn(hub, poller, state, cycles=400)                 # 2,400 runs: far past the run cap
        self.assertEqual(list(state["seen"]), poller.saved()["seen"])
        self.assertEqual(list(poller.load()["seen"]), list(state["seen"]))
        kinds = {}
        for mid in state["seen"]:
            kinds[mid.split(":", 1)[0]] = kinds.get(mid.split(":", 1)[0], 0) + 1
        for kind, cap in gw.SEEN_CAPS.items():
            self.assertLessEqual(kinds.get(kind, 0), cap, kind)
        self.assertEqual(kinds["run"], gw.SEEN_CAPS["run"], "the run kind should be at its cap by now")

    def test_no_live_id_is_evicted_at_the_worst_case_window_on_three_repos(self):
        """Sizing, exercised at the PRODUCTION parameter (a profile watching three repos, as the
        live one does) and at the API's worst-case window rather than a friendly one. Per repo:
        15 open PRs with 20 reviews each, every one returned every poll, and the entire
        20-run page replaced every poll. That is 900 live review ids and 60 new runs a poll
        against the caps; one live id evicted would be announced again the next poll."""
        repos = [REPO, "acme/agent-watchtower", "acme/workstation-daemons"]
        hub = _Hub(repos=repos)
        for repo in repos:
            for n in range(1, 16):
                hub.listing[repo].append(_pr(n))
                hub.reviews[(repo, str(n))] = [hub.new_review() for _ in range(20)]
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)                                     # first run: 900 reviews baselined
        for cycle in range(150):
            for repo in repos:
                for _ in range(20):
                    hub.add_run(repo=repo)
            lines = _news(poller.poll(state))
            self.assertEqual(len([l for l in lines if l.startswith("type=run")]), 60,
                             "cycle %d: a run was announced twice or not at all" % cycle)
            self.assertEqual([l for l in lines if l.startswith("type=review")], [],
                             "cycle %d announced a live review again" % cycle)
        self.assertEqual(_news(poller.poll(poller.load())), [], "and a restart still holds it")

    def test_restarting_before_every_single_cycle_changes_nothing(self):
        """The ticket in one property: a daemon restarted between EVERY pair of polls says
        exactly what one that never restarts says. The world is driven by a seeded generator
        so both runs see the same one: runs finish, comments land, reviews arrive, and for
        a stretch the PRs drop out of the newest-15 listing and come back."""
        def play(restart_each_cycle):
            rng = random.Random(1108)
            hub = _Hub()
            _seed_open_prs(hub, prs=3, reviews_each=4)
            listed = list(hub.listing[REPO])
            poller = _Poller(self, hub)
            state = poller.load()
            poller.poll(state)
            said = []
            for cycle in range(700):
                for _ in range(rng.randint(0, 8)):
                    hub.add_run()
                if rng.random() < 0.3:
                    hub.add_comment()
                if rng.random() < 0.1:
                    hub.reviews[(REPO, str(rng.choice([1, 2, 3])))].append(hub.new_review())
                hub.listing[REPO] = [] if 200 <= cycle < 500 else list(listed)
                if restart_each_cycle:
                    state = poller.load()
                said.extend(_news(poller.poll(state)))
            return said

        steady, restarted = play(False), play(True)
        self.assertGreater(len(steady), 2000, "the world must be busy enough to have overflowed the old cap")
        if steady != restarted:
            first = 0
            while first < min(len(steady), len(restarted)) and steady[first] == restarted[first]:
                first += 1
            self.fail("restarting changed what was said: %d lines vs %d, first difference at %d: %r vs %r"
                      % (len(steady), len(restarted), first,
                         steady[first:first + 1], restarted[first:first + 1]))


class SavedFormat(unittest.TestCase):
    """What is on disk. Nobody reads `seen` but this daemon — `just status` and the
    watchdog read `since` and `build` — so the one constraint is a rollback."""

    @staticmethod
    def _pre_td1108_loader(path):
        """``load_state`` as it stood before the checkpoint, verbatim but for its try/except: what a
        rollback to that code does with this file."""
        with open(path) as fh:
            raw = json.load(fh)
        return {"since": raw.get("since"), "seen": set(raw.get("seen", [])),
                "prs": raw.get("prs", {}), "wf_last": raw.get("wf_last", {}),
                "degraded": raw.get("degraded", {}),
                "pair_pending": raw.get("pair_pending", []),
                "runs_inflight": raw.get("runs_inflight", {}), "first_run": False}

    def _cycled(self):
        hub = _Hub()
        _seed_open_prs(hub, prs=2, reviews_each=3)
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        _churn(hub, poller, state, cycles=5)
        return poller, state

    def test_the_file_is_marked_as_the_ordered_format(self):
        poller, _ = self._cycled()
        raw = poller.saved()
        self.assertEqual(raw.get("seen_v"), 2)
        self.assertIsInstance(raw["seen"], list)
        self.assertTrue(all(isinstance(x, str) for x in raw["seen"]))

    def test_the_old_code_still_reads_the_file(self):
        """Rollback safety: `seen` stays a flat list of id strings and the marker is a new
        key the old loader ignores, so a rollback loses the ORDER (it resumes the
        arbitrary truncation) but never the ids, and never fails to start."""
        poller, _ = self._cycled()
        old = self._pre_td1108_loader(poller.state_path)
        new = poller.load()
        self.assertEqual(old["seen"], set(new["seen"]))
        for key in ("since", "prs", "wf_last", "degraded", "pair_pending", "runs_inflight"):
            self.assertEqual(old[key], new[key], key)
        self.assertGreater(len(old["seen"]), 0)

    def test_a_marked_file_is_trusted_and_gets_no_baseline(self):
        poller, _ = self._cycled()
        self.assertFalse(poller.load().get("legacy_seen"))

    def test_a_set_from_an_older_caller_is_upgraded_and_saved_ordered(self):
        """`poll_profile` upgrades a `seen` handed to it as a set (the shape every earlier
        state, and the fixtures built from it, carry) and saves the ordered form."""
        hub = _Hub()
        hub.add_run(id=42)
        poller = _Poller(self, hub)
        state = {"since": "2026-09-01T00:00:00+00:00", "seen": set(["run:42:1"]), "first_run": False,
                 "wf_last": {}, "prs": {REPO: {}}}
        self.assertEqual(_news(poller.poll(state)), [])
        self.assertNotIsInstance(state["seen"], set)
        self.assertEqual(poller.saved()["seen"], ["run:42:1"])
        self.assertEqual(poller.saved()["seen_v"], 2)


#: The old daemon's last watermark in the migration tests: the shape and the hour of the live file's.
SINCE = datetime(2026, 9, 29, 16, 10, 16, 721502, tzinfo=timezone.utc)


class LegacyStateFile(unittest.TestCase):
    """The first load of a state file written BEFORE the fix.

    Its `seen` is an arbitrary subset: the ids the old daemon dropped are already lost,
    so on the first cycle the API returns items nobody can prove were announced. Those
    must not be announced again — and an item that really is new must still be.

    The design: a file without the `seen_v` marker is legacy. On its first cycle an
    unseen item is adopted SILENTLY only on positive evidence that it pre-dates the old
    daemon's last look: its own timestamp parses and is at least one poll interval
    before the file's watermark. Anything else — later, inside that interval, or with
    no readable timestamp — is announced as it always was.
    """

    def setUp(self):
        self.hub = _Hub()
        self.poller = _Poller(self, self.hub)
        os.makedirs(self.poller.state_dir)
        self.hub.listing[REPO] = [_pr(7)]

    def _ago(self, **kw):
        return _gh_ts(SINCE - timedelta(**kw))

    def _after(self, **kw):
        return _gh_ts(SINCE + timedelta(**kw))

    def _write(self, seen, **over):
        """A state file as the old daemon left it: `seen` in no particular order and no marker."""
        raw = {"since": SINCE.isoformat(), "seen": list(seen),
               "prs": {REPO: {"7": {"head": "abc123456", "state": "open"}}},
               "wf_last": {}, "degraded": {}, "pair_pending": [], "runs_inflight": {REPO: {}},
               "build": {"src": "55bbb41ab3a4", "head": "50e866c"}}
        raw.update(over)
        with open(self.poller.state_path, "w") as fh:
            json.dump(raw, fh)

    @staticmethod
    def _arbitrary(remembered):
        """2000 ids, `remembered` among them, in no particular order (a fixed shuffle)."""
        filler = (["run:%d:1" % (100000 + i) for i in range(1400)]
                  + ["c:%d" % (100000 + i) for i in range(300)]
                  + ["review:%d" % (100000 + i) for i in range(300)])
        ids = list(remembered) + filler[:2000 - len(remembered)]
        random.Random(1108).shuffle(ids)
        return ids

    def _visible_world(self):
        """What the API returns at the first poll after the restart. Returns the ids the
        old file still remembers: half of what pre-dates the watermark, the other half
        dropped by the arbitrary truncation."""
        hub, remembered = self.hub, []
        for i, rid in enumerate(range(501, 513)):                    # 12 runs, hours before the watermark
            hub.add_run(id=rid, updated_at=self._ago(hours=3))
            if i % 2 == 0:
                remembered.append("run:%d:1" % rid)
        reviews = [hub.new_review(id=8000 + i, submitted_at=self._ago(days=2), body="review %d" % (8000 + i))
                   for i in range(6)]                                # 6 reviews on PR 7, days old
        hub.reviews[(REPO, "7")] = reviews
        remembered += ["review:%d" % r["id"] for r in reviews[::2]]
        for i in range(4):                                           # 4 comments the poller's overlap re-reads
            hub.add_comment(id=9000 + i, created_at=self._ago(minutes=4), body="comment %d" % (9000 + i))
            if i % 2 == 0:
                remembered.append("c:%d" % (9000 + i))
        return remembered

    def _new_events(self):
        """Five things that are genuinely new (or cannot be proven old)."""
        hub = self.hub
        hub.add_run(id=601, updated_at=self._after(minutes=2))       # completed after the watermark
        hub.add_run(id=602, updated_at=self._after(minutes=3))       # was in flight when the old daemon last looked
        hub.add_run(id=603, updated_at=self._ago(seconds=30))        # inside the poll interval before it
        hub.reviews[(REPO, "7")].append(hub.new_review(id=8100, submitted_at=self._after(minutes=1),
                                                       body="review 8100"))
        hub.add_comment(id=9100, created_at=self._after(minutes=1), body="comment 9100")
        return ["comment 9100", "review 8100", "run 601", "run 602", "run 603"]

    def test_a_failed_pr_listing_keeps_the_legacy_baseline_through_a_restart(self):
        """Review finding: a file written by the older code meets PR listings failing for
        twelve polls, with a restart three polls in. A failed listing raises the cycle, so
        nothing spends the legacy budget or saves a version 2 file; the first cycle that
        answers adopts what the old file lost, silently, as a clean takeover does."""
        remembered = self._visible_world()
        self._write(self._arbitrary(remembered), build={"src": "68f6389aaaaa", "head": "68f6389"})
        state = self.poller.load()
        self.hub.fail = ("/pulls?",)
        lines = []
        for i in range(12):
            if i == 3:
                state = self.poller.load()                 # the restart, three polls in
            lines.extend(self.poller.poll(state, tolerate=True))
        self.hub.fail = ()
        lines.extend(self.poller.poll(state))
        lines.extend(self.poller.poll(state))
        old = [l for l in lines if l.startswith("type=review") and re.search(r"\| review 80\d\d$", l)]
        self.assertEqual(old, [], "a version 1 file that met a failed PR listing announced old reviews")

    def test_the_first_cycle_re_emits_nothing_the_old_file_lost(self):
        """THE migration test: 6 runs, 3 reviews and 2 comments the API returns were
        dropped by the arbitrary truncation. None is announced; the five that are new
        (after the watermark, or too close to it to prove otherwise) all are."""
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered), runs_inflight={REPO: {"602": time.time()}})
        state = self.poller.load()
        self.assertFalse(state["first_run"])
        self.assertEqual(_titles(self.poller.poll(state)), expected)

    def test_the_daemon_log_says_what_the_migration_adopted(self):
        self._write(self._arbitrary(self._visible_world()))
        self.poller.poll(self.poller.load())
        said = [n for n in self.poller.notes if "adopted" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertRegex(said[0], r"adopted 3 review\(s\), 2 comment\(s\), 6 run\(s\)")

    def test_a_legacy_file_loads_with_its_watermark_and_a_cycle_budget(self):
        """The baseline rides the in-memory state: the file's own watermark (the cutoff a held
        baseline keeps using) and how many cycles it may run."""
        self._write(self._arbitrary([]))
        legacy = self.poller.load()["legacy_seen"]
        self.assertEqual(legacy["cutoff"], SINCE.isoformat())
        self.assertGreater(legacy["cycles"], 0)

    def test_main_can_tell_the_operator_the_file_was_the_old_format(self):
        """`main()` prints `legacy_note` when it loads a profile, so the daemon log says the
        migration is coming before it happens — and says nothing for a file that needs none."""
        self._write(self._arbitrary([]))
        said = gw.legacy_note("acme", self.poller.load())
        self.assertIn("the seen list on disk predates", said)
        self.assertIn("2000 ids", said)
        self.assertIn(SINCE.isoformat(), said)
        self.assertIsNone(gw.legacy_note("acme", {"legacy_seen": None, "seen": {}}))
        self.assertIsNone(gw.legacy_note("acme", {"seen": {}}))

    def test_main_starts_on_a_legacy_file_and_says_so(self):
        """`main()` has no other test, and a typo in the lines it gained would crash-loop the
        daemon at its FIRST start after the deploy. Run it: a config in a private tree
        (the real one is never read), the real load, one real cycle, and the sleep that ends
        the first pass turned into a stop."""
        self._write(self._arbitrary([]))
        config = os.path.join(os.path.dirname(self.poller.state_dir), "config.json")
        with open(config, "w") as fh:
            json.dump({"profiles": {"acme": {"github": {"repos": [REPO]}}}}, fh)

        class Stop(Exception):
            pass

        def stop(_secs):
            raise Stop()

        clock = mock.Mock(sleep=stop, time=time.time)
        with mock.patch.object(gw, "CONFIG_PATH", config), \
                mock.patch.object(gw, "GH", "/nonexistent/gh"), \
                mock.patch.object(gw, "time", clock):
            with self.assertRaises(Stop):
                gw.main()
        said = [n for n in self.poller.notes if "the seen list on disk predates" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertEqual(self.poller.saved()["seen_v"], 2, "the cycle main() ran must have written the new format")

    def test_the_boundary_is_one_poll_interval_before_the_watermark(self):
        """Pinned so it is a decision, not an accident: 61 seconds before the watermark
        is old, 59 is not. The old daemon's last look can lag the watermark by a fetch,
        and the two clocks are not the same clock; a duplicate is the safe direction and
        a swallowed event is not."""
        self.assertEqual(gw.LEGACY_GUARD, timedelta(seconds=gw.POLL_SECS))
        self.hub.add_run(id=1, updated_at=self._ago(seconds=61))
        self.hub.add_run(id=2, updated_at=self._ago(seconds=59))
        self._write(self._arbitrary([]))
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), ["run 2"])

    def test_an_item_with_no_readable_timestamp_is_announced_not_adopted(self):
        """Positive evidence only: no timestamp, or one that does not parse, is not proof
        of anything."""
        self.hub.add_run(id=1)
        del self.hub.runs[REPO][1]["updated_at"]                     # no timestamp at all
        self.hub.add_run(id=2, updated_at="last tuesday")            # one that does not parse
        self.hub.add_run(id=3, updated_at=self._ago(hours=3))        # the control: provably old
        self._write(self._arbitrary([]))
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), ["run 1", "run 2"])

    def test_no_watermark_means_no_baseline(self):
        """Nothing to compare against: announce as the old code would."""
        self.hub.add_run(id=3, updated_at=self._ago(hours=3))
        self._write(self._arbitrary([]), since=None)
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), ["run 3"])
        self.assertFalse(state["legacy_seen"], "there is nothing to hold when there was nothing to compare")

    def test_no_watermark_leaves_nothing_to_hold_even_after_a_failed_fetch(self):
        """With no watermark there is no cutoff, so nothing can ever be adopted: holding the
        baseline through a failed fetch would only keep an inert rule and a misleading note."""
        self._write(self._arbitrary([]), since=None)
        state = self.poller.load()
        self.hub.fail = ("/reviews",)
        self.poller.poll(state)
        self.assertFalse(state["legacy_seen"])
        self.assertEqual([n for n in self.poller.notes if "baseline holds" in n], [])

    def test_an_adopted_run_does_not_rewrite_the_scheduled_baseline(self):
        """`wf_last` on disk already reflects every run that completed before the
        watermark. Re-applying an older one out of order can only regress it: the dropped
        failure below would flip a passing nightly back to failing, and the next real
        success would be announced as a recovery that never happened."""
        self.hub.add_run(id=700, name="Perf nightly", event="schedule", conclusion="failure",
                         updated_at=self._ago(hours=5))
        self.hub.add_run(id=701, name="Perf nightly", event="schedule", conclusion="success",
                         updated_at=self._ago(hours=1))
        self._write(self._arbitrary(["run:701:1"]), wf_last={"Perf nightly": "success"})
        state = self.poller.load()
        self.assertEqual(_news(self.poller.poll(state)), [])
        self.assertEqual(state["wf_last"], {REPO: {"Perf nightly": "success"}})   # keyed by repo

    def test_a_run_that_completed_after_the_old_daemons_last_look_is_still_announced(self):
        """The in-flight-run case through the migration cycle: in flight when the old daemon last
        looked, finished since, and by now off the 20-run page — fetched by id, and
        announced although this is the very cycle that adopts everything old."""
        for n in range(20):
            self.hub.add_run(id=700 + n, updated_at=self._ago(hours=2))      # the runs that pushed it off
        self.hub.add_run(id=602, updated_at=self._after(minutes=5))          # completed after the watermark
        self._write(self._arbitrary([]), runs_inflight={REPO: {"602": time.time()}})
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), ["run 602"])
        self.assertIn(602, [int(p.rsplit("/", 1)[1]) for p in self.hub.asked
                            if re.search(r"actions/runs/\d+$", p)], "it was never fetched by id")

    def test_the_baseline_is_one_clean_cycle_long(self):
        """It must not swallow beyond the migration: once a cycle has completed cleanly the
        file is trusted, and an unseen item announces whatever its timestamp says."""
        self._write(self._arbitrary(self._visible_world()))
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), [])
        self.assertFalse(state["legacy_seen"])
        self.hub.add_run(id=800, updated_at=self._ago(hours=6))
        self.assertEqual(_titles(self.poller.poll(state)), ["run 800"])

    def test_a_failed_fetch_in_the_first_cycle_keeps_the_baseline_for_the_next(self):
        """A hole in the baseline cycle leaves its scope unbaselined, and holding the
        baseline is the cheap repair: without it the reviews the failed fetch never
        read would be announced next cycle. Only the review fetch failed, so only
        reviews are still adopted then."""
        self._write(self._arbitrary(self._visible_world()))
        self._new_events()
        state = self.poller.load()
        self.hub.fail = ("/reviews",)
        first = _titles(self.poller.poll(state))
        self.assertEqual(first, ["comment 9100", "run 601", "run 602", "run 603"])
        self.assertTrue(state["legacy_seen"], "a cycle with a hole consumed the baseline")
        self.hub.fail = ()
        second = _titles(self.poller.poll(state))
        self.assertEqual(second, ["review 8100"], "the old reviews were announced, or the new one lost")
        self.assertFalse(state["legacy_seen"])

    def test_a_failed_comments_fetch_in_the_first_cycle_keeps_the_baseline_for_the_next(self):
        """`comments` in SEEN_SUBS is pinned. Without it a comments hole in the
        baseline cycle dropped the baseline, and the next cycle announced the comments the old
        file had lost."""
        self._write(self._arbitrary(self._visible_world()))
        self._new_events()
        state = self.poller.load()
        self.hub.fail = ("/issues/comments?",)
        first = _titles(self.poller.poll(state))
        held = bool(state["legacy_seen"])
        self.hub.fail = ()
        second = _titles(self.poller.poll(state))
        self.assertEqual(first, ["review 8100", "run 601", "run 602", "run 603"])
        self.assertEqual(second, ["comment 9100"], "the old comments were announced, or the new one lost")
        self.assertTrue(held, "a cycle with a comments hole consumed the baseline")
        self.assertFalse(state["legacy_seen"])

    def test_a_failed_runs_listing_in_the_first_cycle_keeps_the_baseline_for_the_next(self):
        """`runs` in SEEN_SUBS is pinned, the same way."""
        self._write(self._arbitrary(self._visible_world()))
        self._new_events()
        state = self.poller.load()
        self.hub.fail = ("/actions/runs?",)
        first = _titles(self.poller.poll(state))
        held = bool(state["legacy_seen"])
        self.hub.fail = ()
        second = _titles(self.poller.poll(state))
        self.assertEqual(first, ["comment 9100", "review 8100"])
        self.assertEqual(second, ["run 601", "run 602", "run 603"], "the old runs were announced, or a new one lost")
        self.assertTrue(held, "a cycle with a runs hole consumed the baseline")
        self.assertFalse(state["legacy_seen"])

    def test_a_failed_pr_listing_spends_no_cycle_of_the_baseline(self):
        """a failed PR listing raises the cycle before `_settle_legacy`,
        so it spends none of the LEGACY_MAX_CYCLES. Twelve failed listings in one process, then a
        cycle that answers with a review hole: the baseline still holds for the next cycle, and the
        reviews the old file lost are adopted, not announced. Spent per failed listing, the budget
        was gone by then and the next cycle announced them."""
        self._write(self._arbitrary(self._visible_world()))
        state = self.poller.load()
        self.hub.fail = ("/pulls?",)
        for _ in range(gw.LEGACY_MAX_CYCLES + 2):
            self.poller.poll(state, tolerate=True)
        left = state["legacy_seen"]["cycles"]
        self.hub.fail = ("/reviews",)
        self.poller.poll(state)
        held = bool(state["legacy_seen"])
        self.hub.fail = ()
        old = [l for l in self.poller.poll(state) if l.startswith("type=review")]
        self.assertEqual(old, [], "the reviews the old file lost were announced")
        self.assertEqual(len(self.poller.raised), gw.LEGACY_MAX_CYCLES + 2)
        self.assertTrue(held, "a cycle with a review hole consumed the baseline")
        self.assertEqual(left, gw.LEGACY_MAX_CYCLES)

    def test_a_hole_that_never_heals_cannot_keep_the_baseline_forever(self):
        """Bounded: a permanently failing fetch (a renamed repo, say) must not leave a
        silent-adoption rule running for the life of the daemon."""
        self._write(self._arbitrary([]))
        state = self.poller.load()
        self.hub.fail = ("/actions/runs",)
        for _ in range(gw.LEGACY_MAX_CYCLES):
            self.poller.poll(state)
        self.assertFalse(state["legacy_seen"])

    def test_the_first_cycle_writes_the_new_format_and_the_next_load_is_not_legacy(self):
        self._write(self._arbitrary(self._visible_world()))
        self.poller.poll(self.poller.load())
        self.assertEqual(self.poller.saved()["seen_v"], 2)
        self.assertFalse(self.poller.load().get("legacy_seen"))

    def test_a_marked_file_gets_no_baseline_at_all(self):
        """The marker means the order in the file is the memory. Ids missing from it are
        missing for a reason the daemon can no longer excuse, so they announce."""
        self._visible_world()
        self._write(self._arbitrary([]), seen_v=2)
        self.assertEqual(len(_titles(self.poller.poll(self.poller.load()))), 12 + 6 + 4)

    def test_a_first_run_is_not_a_migration(self):
        state = self.poller.load()          # no file yet
        self.assertTrue(state["first_run"])
        self.assertFalse(state.get("legacy_seen"))


class EventIds(unittest.TestCase):
    """Every review, comment and run line carries the id GitHub gave it (the standalone
    watcher's fix). Without one, two distinct events can render byte-identical — an
    empty-bodied COMMENTED review, two failures of one workflow on one branch with one
    commit title — and nobody downstream can tell a repeat from a second event, which is
    exactly the question a re-emitted wake raises."""

    def setUp(self):
        self.hub = _Hub()
        self.hub.listing[REPO] = [_pr(7)]
        self.poller = _Poller(self, self.hub)
        self.state = self.poller.load()
        self.poller.poll(self.state)                 # first run: the baseline

    def _poll(self):
        return _news(self.poller.poll(self.state))

    def test_a_review_line_carries_its_id(self):
        self.hub.reviews[(REPO, "7")] = [self.hub.new_review(id=8100, body="looks good", state="APPROVED")]
        self.assertEqual(self._poll(),
                         ["type=review repo=app #7 id=8100 by=bob APPROVED | looks good"])

    def test_a_comment_line_carries_its_id(self):
        self.hub.add_comment(id=9100, body="hello")
        self.assertEqual(self._poll(), ["type=comment repo=app #7 id=9100 by=bob | hello"])

    def test_a_run_line_carries_its_id_and_attempt(self):
        self.hub.add_run(id=601, conclusion="failure", display_title="boom")
        self.assertEqual(self._poll(),
                         ["type=run repo=app run=601.1 CI@main: failure | boom"])

    def test_two_reviews_that_used_to_render_alike_are_told_apart(self):
        self.hub.reviews[(REPO, "7")] = [self.hub.new_review(id=8100, body=""),
                                         self.hub.new_review(id=8101, body="")]
        lines = self._poll()
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(set(lines)), 2, "two distinct reviews rendered byte-identical: %s" % lines)

    def test_two_failures_of_one_workflow_with_one_title_are_told_apart(self):
        self.hub.add_run(id=601, conclusion="failure", display_title="same")
        self.hub.add_run(id=602, conclusion="failure", display_title="same")
        lines = self._poll()
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(set(lines)), 2, "two distinct runs rendered byte-identical: %s" % lines)

    def test_a_rerun_is_a_second_event_with_its_own_attempt(self):
        self.hub.add_run(id=601, conclusion="failure", display_title="flaky")
        first = self._poll()
        self.hub.add_run(id=601, conclusion="success", display_title="flaky", run_attempt=2)
        second = self._poll()
        self.assertIn(" run=601.1 ", first[0])
        self.assertIn(" run=601.2 ", second[0])


class TextFromGitHubCannotRestyleALine(unittest.TestCase):
    """A PR title, a review or comment body, a run's name, branch, title and conclusion,
    and the repo string are text a person (or the API) supplies. The controls and bidi marks a
    terminal acts on but does not show must not survive into the event line, where they could
    redraw it (a forged `by=`). Same rule as in the Bitbucket and stalled daemons."""

    CRAFTED = "fix \x1b[2K\r\x1b[1G by=forged \x1b[31mred\x1b[0m \x9b2K a‮b‍c​d"
    #: U+200D is in `_fields._KEPT` (emoji sequences), so it stays; U+200B does not.
    CLEAN = "fix [2K [1G by=forged [31mred[0m 2K ab‍cd"
    HIDDEN = ("\x1b", "\x9b", "‮", "​", "\r")

    def setUp(self):
        self.hub = _Hub()
        self.hub.listing[REPO] = [_pr(7)]
        self.poller = _Poller(self, self.hub)
        self.state = self.poller.load()
        self.poller.poll(self.state)                 # first run: the baseline

    def _assert_clean(self, line):
        self.assertEqual(len(line.splitlines()), 1, repr(line))
        for ch in self.HIDDEN:
            self.assertNotIn(ch, line, repr(ch))

    def test_one_line_drops_the_controls_the_bidi_marks_and_the_zero_width_space(self):
        out = gw.one_line(self.CRAFTED)
        for ch in self.HIDDEN:
            self.assertNotIn(ch, out, repr(ch))
        self.assertEqual(out, self.CLEAN)
        self.assertEqual(gw.one_line("a  b\n c"), "a b c")

    def test_one_line_keeps_ordinary_non_ascii_text(self):
        text = "naïve café — 日本語のタイトル 🚀 ✅ 👨‍👩‍👧"
        self.assertEqual(gw.one_line(text), text)

    def test_one_line_keeps_TD_1204_handling(self):
        self.assertEqual(gw.one_line(42), "42")
        self.assertEqual(gw.one_line(None), "")
        self.assertEqual(gw.one_line(["x"]), "")
        self.assertEqual(gw.one_line("bad \ud800 text"), "bad ? text")

    def test_an_opened_line_title_is_clean_and_the_author_is_the_real_one(self):
        emitted, _ = _poll([], [_pr(72, title=self.CRAFTED)])
        self.assertEqual(emitted, ["type=pr_opened repo=app #72 by=bob | %s" % self.CLEAN])
        self._assert_clean(emitted[0])

    def test_an_ordinary_non_ascii_title_is_byte_identical(self):
        title = "[PROJ-873] naïve café — 日本語 🚀"
        emitted, _ = _poll([], [_pr(72, title=title)])
        self.assertEqual(emitted, ["type=pr_opened repo=app #72 by=bob | %s" % title])

    def test_a_merged_line_title_is_clean(self):
        emitted, _ = _poll([_pr(72)], [_pr(72, state="closed", merged_at="2026-09-06T05:24:00Z",
                                           title=self.CRAFTED)])
        self.assertEqual(emitted, ["type=pr_merged repo=app #72 | %s" % self.CLEAN])

    def test_a_pair_window_line_title_is_clean(self):
        body = "Adds it.\n\nLands-with: acme/Agent-Watchtower#8\n"
        with mock.patch.object(gw, "companion_merged", lambda repo, num: False):
            emitted, _ = _poll([_pr(72, body=body)],
                               [_pr(72, state="closed", merged_at="2026-09-06T05:24:00Z",
                                    title=self.CRAFTED, body=body)])
        window = [l for l in emitted if l.startswith("type=pair_window_open")]
        self.assertEqual(window, ["type=pair_window_open repo=app #72 "
                                  "waits-on=acme/Agent-Watchtower#8 | %s" % self.CLEAN])
        for line in emitted:
            self._assert_clean(line)

    def test_a_pair_window_line_title_held_in_old_state_is_clean_too(self):
        """The title in a `pair_pending` entry is written to the line as stored; one saved before
        this fix may still carry the controls."""
        body = "Adds it.\n\nLands-with: acme/Agent-Watchtower#8\n"
        entry = {"repo": REPO, "number": 72, "title": self.CRAFTED, "done": [], "said": []}
        out = []
        with mock.patch.object(gw, "companion_merged", lambda repo, num: False), \
                mock.patch.object(gw, "emit", lambda profile, line: out.append(line)):
            gw.settle_pair("acme", gw._degraded.Cycle(), entry, body=body)
        self.assertEqual(out, ["type=pair_window_open repo=app #72 "
                               "waits-on=acme/Agent-Watchtower#8 | %s" % self.CLEAN])

    def test_a_review_body_and_state_are_clean(self):
        self.hub.reviews[(REPO, "7")] = [self.hub.new_review(id=8100, body=self.CRAFTED,
                                                               state="APPROVED\x1b[2K‮")]
        lines = _news(self.poller.poll(self.state))
        self.assertEqual(lines, ["type=review repo=app #7 id=8100 by=bob APPROVED[2K | %s"
                                 % self.CLEAN])
        self._assert_clean(lines[0])

    def test_a_comment_body_is_clean(self):
        self.hub.add_comment(id=9100, body=self.CRAFTED)
        lines = _news(self.poller.poll(self.state))
        self.assertEqual(lines, ["type=comment repo=app #7 id=9100 by=bob | %s" % self.CLEAN])
        self._assert_clean(lines[0])

    def test_a_forged_author_login_is_clean(self):
        self.hub.add_comment(id=9100, body="hi", user={"login": "alice\x1b[2K\rby=alice"})
        lines = _news(self.poller.poll(self.state))
        self.assertEqual(lines, ["type=comment repo=app #7 id=9100 by=alice[2K by=alice | hi"])

    def test_a_run_name_branch_title_and_conclusion_are_clean(self):
        self.hub.add_run(id=601, conclusion="failure\x1b[2K‮", name=self.CRAFTED,
                         head_branch=self.CRAFTED, display_title=self.CRAFTED)
        lines = _news(self.poller.poll(self.state))
        self.assertEqual(lines, ["type=run repo=app run=601.1 %s@%s: failure[2K | %s"
                                 % (self.CLEAN, self.CLEAN, self.CLEAN)])
        self._assert_clean(lines[0])

    def test_a_run_conclusion_that_is_not_a_string_costs_no_line(self):
        self.hub.add_run(id=601, conclusion=["x"], display_title="boom")
        lines = _news(self.poller.poll(self.state))
        self.assertEqual(len(lines), 1, lines)
        self._assert_clean(lines[0])

    def test_the_repo_string_is_clean_on_a_run_line(self):
        evil = "acme/evil\x1b[2K\rtype=forged‮"
        hub = _Hub(repos=(evil,))
        hub.listing[evil] = []
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        hub.add_run(repo=evil, id=601, conclusion="failure", display_title="boom")
        lines = _news(poller.poll(state))
        self.assertEqual(lines, ["type=run repo=evil[2K type=forged run=601.1 CI@main: failure | boom"])
        self._assert_clean(lines[0])


# ---------------------------------------------------------------------------
# A run created between two polls is never lost to the page
#
# A run is tracked once a poll has seen it in flight. A run that no poll ever
# saw stayed invisible: created after one poll, pushed off the newest-20 page by
# 20 or more newer runs before the next, it never entered `runs_inflight`, and
# its completion was never emitted. Seven merges inside about a minute
# created 49 runs and lost 3 of the 7 CI@main
# completions in one profile. A lost failure would have been just as silent.
# ---------------------------------------------------------------------------

def _runs_asked(hub, since):
    """The PLAIN runs listing requests (page one and the page-back). the two status
    listings a repo a cycle are counted by `_status_asked`, so the counts that pin the page-back
    (one request in the steady state, five at the cap) keep meaning what they say."""
    return [p for p in hub.asked[since:] if "/actions/runs?" in p and "status=" not in p]


def _status_asked(hub, since):
    return [p for p in hub.asked[since:] if "/actions/runs?" in p and "status=" in p]


def _run_ids(lines):
    return sorted(int(re.search(r" run=(\d+)\.", line).group(1)) for line in lines if line.startswith("type=run "))


def _typed(lines, prefix):
    return [line for line in lines if line.startswith(prefix)]


class _ClockedRuns(unittest.TestCase):
    """A profile already past its first run, on a clock: 30 runs from three hours ago, the
    first-run baseline, one quiet poll. Each `tick` lets time pass and polls."""

    repos = (REPO,)
    start = None                                           # a clock start other than _Clock's default

    def setUp(self):
        self.clock = (_Clock(self.start) if self.start else _Clock()).install(self)
        self.hub = _Hub(repos=self.repos, clock=self.clock)
        self.poller = _Poller(self, self.hub)
        for repo in self.repos:
            for i in range(30):
                self.hub.add_run(repo=repo, name="Deploy (dev)", created_at=self.clock.at(-3 * 3600 + i))
        self.state = self.poller.load()
        self.poller.poll(self.state)                       # the first run: a silent baseline
        self.assertEqual(self.tick(), [], "a quiet poll emitted")

    def tick(self, seconds=60, state=None, tolerate=False):
        self.clock.advance(seconds)
        return self.poller.poll(self.state if state is None else state, tolerate=tolerate)

    def burst(self, n, repo=REPO, start=1, **fields):
        """``n`` runs created one second apart from ``start`` seconds from now."""
        fields.setdefault("name", "Deploy (dev)")
        stamp = fields.pop("created_at", "unset")
        return [self.hub.add_run(repo=repo, created_at=self.clock.at(start + i) if stamp == "unset" else stamp,
                                 **fields) for i in range(n)]

    def line(self, run, concl):
        return "type=run repo=%s run=%d.%d %s@main: %s | %s" % (
            REPO.split("/")[-1], run["id"], run.get("run_attempt", 1), run["name"], concl,
            run["display_title"])


class RunsCreatedBetweenPolls(_ClockedRuns):
    """Each poll pages back until it reaches a run created before the previous poll
    began (less a margin), so no run can be created and pushed off the page unseen."""

    def _wave(self, merges=7, per_merge=7, ci_done=None):
        """An incident's wave: ``merges`` merges ten seconds apart, each starting
        ``per_merge`` runs with CI first. ``ci_done`` maps a merge to the conclusion of a
        CI run that already finished. Returns the CI runs, oldest first."""
        ci_done = ci_done or {}
        ci = []
        for m in range(merges):
            for k in range(per_merge):
                created = self.clock.at(1 + m * 8 + k)
                if k:
                    self.hub.add_run(name="Deploy (dev)", created_at=created)
                    continue
                concl = ci_done.get(m)
                ci.append(self.hub.add_run(
                    name="CI", created_at=created, display_title="merge %d" % m,
                    status="completed" if concl else "in_progress", conclusion=concl))
        return ci

    def test_a_merge_wave_between_two_polls_loses_no_ci_run(self):
        """THE ticket's case. 49 runs land between two polls; five of the seven CI runs
        are deeper than the newest 20. Merge 1's CI has already FAILED by the next poll:
        before the fix that failure was announced to nobody, ever."""
        ci = self._wave(ci_done={1: "failure"})
        self.assertEqual(_ci(self.tick(65)), [self.line(ci[1], "failure")],
                         "a CI run that finished deeper than the newest page was never announced")
        self.assertEqual(set(self.state["runs_inflight"][REPO]),
                         set(str(r["id"]) for r in ci if r["status"] != "completed"),
                         "a CI run still going deeper than the newest page was never tracked")
        for run in ci:
            if run["status"] != "completed":
                run.update(status="completed", conclusion="success")
        self.burst(25)
        self.assertEqual(sorted(_ci(self.tick())), sorted(self.line(r, "success") for r in ci if r is not ci[1]))
        self.assertEqual(self.state["runs_inflight"][REPO], {})
        self.assertEqual(_ci(self.tick() + self.tick() + self.tick()), [], "a completion was announced twice")

    def test_every_run_of_a_burst_is_announced_once_and_an_ordinary_page_back_is_quiet(self):
        made = self.burst(45)
        lines = self.tick()
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))
        self.assertEqual(_typed(lines, "type=degraded") + _typed(lines, "type=unread_runs"), [], lines)
        later = self.tick() + self.tick() + self.tick()
        self.assertEqual(_news(later), [], "the margin's re-read announced a run again")

    def test_the_steady_state_is_one_runs_request_a_poll(self):
        """A page whose oldest run predates the previous poll ends the page-back, so a repo
        creating fewer than a page of runs between polls costs what it cost before. The
        load runs long enough that page one holds only the last ten minutes."""
        for i in range(15):
            self.burst(2, start=10)
            before = len(self.hub.asked)
            self.assertEqual(len(_news(self.tick())), 2)
            if i >= 10:
                self.assertEqual(len(_runs_asked(self.hub, before)), 1, "poll %d paged back" % i)

    def test_a_burst_past_the_page_cap_is_told(self):
        self.tick()
        self.tick()                                        # past the first run's floor
        self.burst(gw.RUNS_PER_PAGE * gw.RUNS_MAX_PAGES + 15, start=1)
        before = len(self.hub.asked)
        lines = self.tick()
        self.assertEqual(len(_runs_asked(self.hub, before)), gw.RUNS_MAX_PAGES)
        capped = _typed(lines, "type=unread_runs repo=app reason=page_cap ")
        self.assertEqual(len(capped), 1, lines)
        # since = the previous cycle's start less the margin; until = the oldest run on page 5,
        # the 16th of the 115 (created 16 s after the previous cycle started).
        previous = self.clock.now - timedelta(seconds=60)
        self.assertIn(" since=%s until=%s " % (_gh_ts(previous - gw.RUNS_CUTOFF_MARGIN),
                                              _gh_ts(previous + timedelta(seconds=16))), capped[0])
        self.assertNotIn(REPO, self.state["runs_from"], "a cap hit held the cutoff")
        self.assertEqual(len(_news(lines)), gw.RUNS_PER_PAGE * gw.RUNS_MAX_PAGES)
        self.assertEqual(_typed(lines, "type=degraded") + _typed(lines, "error:"), [],
                         "a cap hit is not a failed fetch")

    def test_two_cap_hits_thirty_minutes_apart_are_both_told(self):
        """Review finding: the second hit used to fall inside the degraded
        rate limit of the first and reach the profile log as nothing at all."""
        self.burst(115)
        self.assertTrue(_typed(self.tick(), "type=unread_runs"), "the first cap hit was silent")
        for _ in range(29):
            self.tick()
        self.burst(115)
        self.assertTrue(_typed(self.tick(), "type=unread_runs"), "the second cap hit was silent")

    def test_a_cap_hit_after_a_transient_failure_is_told(self):
        self.hub.fail = ("/actions/runs?",)
        self.assertTrue(_typed(self.tick(), "type=degraded sub=runs"))
        self.hub.fail = ()
        self.tick()
        self.burst(130)
        self.assertTrue(_typed(self.tick(), "type=unread_runs"), "a cap hit after a 502 was silent")

    def test_a_page_back_announces_nothing_from_before_the_previous_poll(self):
        """The last page read straddles the cutoff. What is older than the cutoff is
        history: if `seen` has lost it (an eviction, a lost file), announcing it now would
        be a replay, which the first-run baseline exists to prevent."""
        for mid in [k for k in self.state["seen"] if k.startswith("run:")]:
            del self.state["seen"][mid]
        made = self.burst(25)
        self.assertEqual(_run_ids(self.tick()), sorted(r["id"] for r in made))

    def test_a_tracked_run_found_on_a_deeper_page_is_settled_there_not_fetched_by_id(self):
        """A run that is being tracked is announced from whichever page it turns up on, even
        when it was created before the cutoff, and is not fetched by id as well."""
        ci = self.hub.add_run(name="CI", status="in_progress", conclusion=None, display_title="slow",
                              created_at=self.clock.at(-600))
        self.assertEqual(self.tick(), [])
        self.assertIn(str(ci["id"]), self.state["runs_inflight"][REPO])
        ci.update(status="completed", conclusion="success")
        self.burst(25)
        before = len(self.hub.asked)
        self.assertEqual(_ci(self.tick()), [self.line(ci, "success")])
        self.assertEqual([p for p in self.hub.asked[before:] if p.endswith("/actions/runs/%d" % ci["id"])], [])
        self.assertEqual(self.state["runs_inflight"][REPO], {})

    def test_a_re_run_still_going_on_a_deeper_page_is_tracked(self):
        """A re-run keeps its run's original `created_at`, so it sits deep in the listing,
        older than the cutoff. Still going, it is tracked from any page the page-back reads,
        and its completion is announced."""
        old = sorted(self.hub.runs[REPO].values(), key=lambda r: r["id"])[-3]
        old.update(status="in_progress", conclusion=None, run_attempt=2, name="CI")
        self.burst(25)
        self.tick()
        self.assertIn(str(old["id"]), self.state["runs_inflight"][REPO], "a re-run in flight was not tracked")
        old.update(status="completed", conclusion="failure")
        self.assertEqual(_ci(self.tick()), [self.line(old, "failure")])

    def test_a_run_listed_late_is_still_read(self):
        """The margin is for a run created just before the previous poll and listed only
        after it (clock skew, listing lag), then pushed deep by a burst."""
        late = self.hub.add_run(name="CI", created_at=self.clock.at(-30), display_title="late", _hidden=True)
        self.assertEqual(self.tick(0), [])
        late["_hidden"] = False
        self.burst(25)
        self.assertEqual(_ci(self.tick()), [self.line(late, "success")], "a run listed late was lost")

    def test_a_failed_page_back_holds_the_cutoff_until_it_is_read(self):
        """Review finding: page 2 fails three polls running at a real
        interval; the runs behind it are read when it works, because the cutoff held."""
        made = self.burst(45)
        self.hub.fail = ("&page=2",)
        first = self.tick(65)
        self.assertEqual(len(_news(first)), gw.RUNS_PER_PAGE)
        self.assertTrue(_typed(first, "type=degraded sub=runs"), first)
        self.assertIn(REPO, self.state["runs_from"])
        held = self.tick(65) + self.tick(65)
        self.assertEqual(_news(held), [])
        self.hub.fail = ()
        recovered = self.tick(65)
        self.assertEqual(len(_news(recovered)), 25, "the runs behind a failed page were never read")
        self.assertTrue(_typed(recovered, "type=degraded_recovered sub=runs"), recovered)
        self.assertNotIn(REPO, self.state["runs_from"])
        after = self.tick()
        self.assertEqual(_news(after), [])
        # every run of the burst, each announced once — not the fixture's own length.
        self.assertEqual(_run_ids(first + held + recovered + after), sorted(r["id"] for r in made))

    def test_a_failed_listing_holds_the_cutoff_too(self):
        made = self.burst(45)
        self.hub.fail = ("/actions/runs?",)
        self.tick(66)
        self.tick(66)
        self.hub.fail = ()
        self.assertEqual(_run_ids(self.tick(66)), sorted(r["id"] for r in made))

    def test_a_failed_page_three_is_held_through_three_polls_and_read(self):
        made = self.burst(50)
        self.hub.fail = ("&page=3",)
        first = self.tick(65)
        self.assertEqual(len(_news(first)), 40)
        held = self.tick(65) + self.tick(65)
        self.assertEqual(_news(held), [])
        self.hub.fail = ()
        recovered = self.tick(65)
        self.assertEqual(len(_news(recovered)), 10, "the runs behind page 3 were never read")
        self.assertEqual(_run_ids(first + held + recovered), sorted(r["id"] for r in made))
    def test_a_hold_that_outlives_its_bound_is_released_and_told(self):
        """pins the window as well as the line: ``since`` is the HELD start less the
        margin, and ``until`` this cycle's start less the margin. A mutant taking ``until`` from the
        watermark (one poll earlier) passed the old count-only assertion and left a poll's worth of
        runs outside every told window."""
        self.burst(45)
        self.hub.fail = ("&page=2",)
        lines = self.tick()
        held = gw._parse_ts(self.state["runs_from"][REPO])
        expected = None
        for _ in range(int(gw.RUNS_HOLD_MAX.total_seconds() // 60) + 1):
            got = self.tick()
            if _typed(got, "type=unread_runs") and expected is None:
                expected = (_gh_ts(held - gw.RUNS_CUTOFF_MARGIN), _gh_ts(self.clock.now - gw.RUNS_CUTOFF_MARGIN))
            lines.extend(got)
        released = _typed(lines, "type=unread_runs repo=app reason=hold_expired ")
        self.assertEqual(len(released), 1, [l for l in lines if "unread_runs" in l])
        self.assertIn(" since=%s until=%s " % expected, released[0])
        self.assertGreater(self.clock.now - held, gw.RUNS_HOLD_MAX)

    def test_runs_created_between_two_page_requests_are_not_announced_twice(self):
        """New runs created between the page-one and page-two requests push page one's
        tail onto page two. Each run is still announced once."""
        self.burst(25)
        self.hub.arrive_after_page = {1: 3}
        lines = _news(self.tick())
        ids = [re.search(r" run=(\d+)\.", line).group(1) for line in lines]
        self.assertEqual(len(ids), 25)
        self.assertEqual(len(set(ids)), 25, lines)
        self.assertEqual(len(_news(self.tick())), 3, "the late arrivals were not read next poll")

    def test_a_page_with_no_created_at_stops_the_page_back_as_a_hole(self):
        self.burst(25)
        for run in self.hub.runs[REPO].values():           # a malformed answer: no run carries one
            run["created_at"] = None
        before = len(self.hub.asked)
        lines = self.tick()
        self.assertEqual(len(_runs_asked(self.hub, before)), 1)
        self.assertTrue([n for n in self.poller.notes if "created_at" in n], self.poller.notes)
        self.assertTrue(_typed(lines, "type=degraded sub=runs"), lines)
        self.assertIn(REPO, self.state["runs_from"], "a page that cannot say where the boundary is did not hold")

    def test_a_null_page_is_a_hole_not_the_end_of_the_list(self):
        made = self.burst(45)
        self.hub.null_pages = {2}
        lines = self.tick()
        self.assertEqual(len(_news(lines)), 20)
        self.assertTrue(_typed(lines, "type=degraded sub=runs"), lines)
        self.hub.null_pages = set()
        later = self.tick()
        self.assertEqual(len(_news(later)), 25)
        self.assertEqual(_run_ids(lines + later), sorted(r["id"] for r in made))
    def test_an_unparseable_watermark_is_told(self):
        self.state["since"] = "not a timestamp"
        lines = self.tick()
        self.assertTrue(_typed(lines, "type=degraded sub=runs"), lines)
        self.assertTrue([n for n in self.poller.notes if "does not parse" in n], self.poller.notes)

    def test_a_deeper_completed_run_with_no_created_at_is_not_read(self):
        """It cannot be proven new, so it is not announced: the page-back fails closed on it."""
        made = self.burst(25)
        made[2]["_order_as"], made[2]["created_at"] = made[2]["created_at"], None   # still on page 2
        before = len(self.hub.asked)
        self.assertEqual(_run_ids(self.tick()), sorted(r["id"] for r in made if r is not made[2]))
        self.assertEqual(len(_runs_asked(self.hub, before)), 2, "the run with no created_at was not on a page read")

    def test_a_short_page_ends_the_page_back(self):
        hub = _Hub(clock=self.clock)
        poller = _Poller(self, hub)
        state = poller.load()
        poller.poll(state)
        for i in range(30):
            hub.add_run(created_at=self.clock.at(1 + i))
        self.clock.advance(60)
        before = len(hub.asked)
        self.assertEqual(len(_news(poller.poll(state))), 30)
        self.assertEqual(len(_runs_asked(hub, before)), 2)


def _attempts(lines, repo, run):
    """The attempt numbers announced for ``run`` of ``repo`` among ``lines``, in order; a repeat appears twice."""
    pattern = r"^type=run repo=%s run=%d\.(\d+) " % (re.escape(_short(repo)), run["id"])
    return [int(m.group(1)) for m in (re.match(pattern, line) for line in lines) if m]


class _ReRuns(object):
    """A later attempt of a run that is off every page the page-back reads.

    A re-run keeps its run's original ``created_at``, so it sits among the OLD runs of the
    newest-first listing, and the page-back stops at the first page older than its cutoff. Once
    an attempt is announced the run is no longer tracked, so the next attempt was never seen
    unless it happened to be on a page read. The cases run per repo: the single-repo class
    names the repo, the two-repo classes run every case with each repo as the subject."""

    def _old_run(self, repo):
        """A run of the fixture (three hours old, announced by the first run's baseline) with
        25 runs an hour old made after it, so it is deeper than page one and than any page the
        page-back would go on to read."""
        old = sorted(self.hub.runs[repo].values(), key=lambda r: r["id"])[7]
        for _ in range(25):
            self.hub.add_run(repo=repo, name="Deploy (dev)", created_at=self.clock.at(-3600))
        self.tick()
        self.tick()
        return old

    def _start(self, run, attempt, status="in_progress"):
        run.update(status=status, conclusion=None, run_attempt=attempt, name="CI", updated_at=self.clock.at())

    def _finish(self, run, conclusion="failure"):
        run.update(status="completed", conclusion=conclusion, updated_at=self.clock.at())

    def _a_re_run_off_every_page_is_announced(self, subject):
        old = self._old_run(subject)
        self._start(old, 2)
        lines = self.tick()
        self.assertIn(str(old["id"]), self.state["runs_inflight"][subject], "a re-run in flight was not tracked")
        self._finish(old)
        lines += self.tick()
        self.assertEqual(_attempts(lines, subject, old), [2], "attempt 2 of a run off every page was not announced")
        lines += self.tick() + self.tick()
        self.assertEqual(_attempts(lines, subject, old), [2], "attempt 2 was announced twice")
        self.assertNotIn(str(old["id"]), self.state["runs_inflight"][subject])

    def _attempt_three_is_announced_after_attempt_two(self, subject):
        """The ticket's shape: attempts 1 and 2 were announced, attempt 3 was not."""
        old = self._old_run(subject)
        self._start(old, 2)
        lines = self.tick()
        self._finish(old)
        lines += self.tick()
        self.assertEqual(_attempts(lines, subject, old), [2])
        self._start(old, 3)
        lines += self.tick()
        self.assertIn(str(old["id"]), self.state["runs_inflight"][subject], "attempt 3 in flight was not tracked")
        self._finish(old, "cancelled")
        lines += self.tick() + self.tick()
        self.assertEqual(_attempts(lines, subject, old), [2, 3], "attempt 3 of a run off every page was not announced")
        self.assertNotIn(str(old["id"]), self.state["runs_inflight"][subject])

    def _a_queued_re_run_is_found_too(self, subject):
        old = self._old_run(subject)
        self._start(old, 2, status="queued")
        self.tick()
        self.assertIn(str(old["id"]), self.state["runs_inflight"][subject], "a queued re-run was not tracked")
        self._finish(old)
        self.assertEqual(_attempts(self.tick(), subject, old), [2])

    def _a_failed_status_read_is_noted_and_recorded_not_held(self, subject, status):
        old = self._old_run(subject)
        self._start(old, 2, status=status)
        self.hub.fail_when = lambda path: path.startswith("repos/%s/" % subject) and "status=%s" % status in path
        lines = self.tick()
        self.assertTrue([n for n in self.poller.notes if "status=%s" % status in n and "failed" in n],
                        self.poller.notes)
        self.assertTrue(_typed(lines, "type=degraded sub=runs"), lines)
        self.assertNotIn(subject, self.state["runs_from"], "a failed status read held the runs cutoff")
        self.hub.fail_when = None
        lines += self.tick()
        self.assertTrue(_typed(lines, "type=degraded_recovered sub=runs"), lines)
        self._finish(old)
        lines += self.tick()
        self.assertEqual(_attempts(lines, subject, old), [2], "the re-run was lost after a failed status read")


class ReRunsOffEveryPage(_ReRuns, _ClockedRuns):

    def test_a_re_run_off_every_page_is_announced(self):
        self._a_re_run_off_every_page_is_announced(REPO)

    def test_attempt_three_is_announced_after_attempt_two(self):
        self._attempt_three_is_announced_after_attempt_two(REPO)

    def test_a_queued_re_run_is_found_too(self):
        self._a_queued_re_run_is_found_too(REPO)

    def test_a_failed_in_progress_listing_is_noted_and_recorded_not_held(self):
        self._a_failed_status_read_is_noted_and_recorded_not_held(REPO, "in_progress")

    def test_a_failed_queued_listing_is_noted_and_recorded_not_held(self):
        self._a_failed_status_read_is_noted_and_recorded_not_held(REPO, "queued")

    def test_a_cycle_asks_for_each_status_once_with_no_date_filter(self):
        """Two more requests a repo a cycle, in the quiet steady state too, each the newest
        page of its status and neither carrying the `created>=` filter that hides an old run."""
        for _ in range(2):
            before = len(self.hub.asked)
            self.tick()
            asked = _status_asked(self.hub, before)
            self.assertEqual(sorted(asked), sorted([
                "repos/%s/actions/runs?status=in_progress&per_page=%d" % (REPO, gw.RUNS_STATUS_PER_PAGE),
                "repos/%s/actions/runs?status=queued&per_page=%d" % (REPO, gw.RUNS_STATUS_PER_PAGE)]))
            self.assertEqual(len(_runs_asked(self.hub, before)), 1)

    def test_a_run_going_behind_page_one_at_a_first_run_is_tracked(self):
        """The first run reads page one only, so a run still going behind it was never tracked;
        the status listings find it, and its later completion is announced."""
        hub = _Hub(clock=self.clock)
        poller = _Poller(self, hub)
        going = hub.add_run(name="CI", status="in_progress", conclusion=None, created_at=self.clock.at(-7200))
        for i in range(25):
            hub.add_run(created_at=self.clock.at(-60 + i))
        state = poller.load()
        poller.poll(state)
        self.assertIn(str(going["id"]), state["runs_inflight"][REPO])
        going.update(status="completed", conclusion="success", updated_at=self.clock.at())
        self.clock.advance(60)
        self.assertEqual(_ci(poller.poll(state)), [self.line(going, "success")])

    def test_a_run_the_status_listing_returns_completed_is_not_tracked(self):
        """The listing is by status, but an answer entry that is already completed owes no tracking."""
        done = self.hub.add_run(name="CI", created_at=self.clock.at(-600))
        real = self.hub.__class__.__call__

        def answer(hub, path):
            got = real(hub, path)
            if "status=queued" in path:
                return {"workflow_runs": [done]}
            return got

        with mock.patch.object(self.hub.__class__, "__call__", answer):
            self.tick()
        self.assertNotIn(str(done["id"]), self.state["runs_inflight"][REPO])


def _status_cap_lines(lines, status=None):
    wanted = "reason=status_cap" + (" status=%s" % status if status else "")
    return [line for line in lines if line.startswith("type=unread_runs ") and wanted in line]


class StatusListingsPageSize(_ReRuns, _ClockedRuns):
    """A status listing asks for 100 runs, not for the plain
    listing's 20, because a re-run of an old run is LAST in a newest-first list. A listing that
    comes back full may have more behind it, and says so once per (repo, status) episode."""

    def _a_re_run_behind(self, newer):
        old = self._old_run(REPO)
        self.burst(newer, status="in_progress", conclusion=None, name="CI")
        self._start(old, 2)
        self.tick()
        self.assertIn(str(old["id"]), self.state["runs_inflight"][REPO],
                      "a re-run behind %d newer in-progress runs was not tracked" % newer)
        self._finish(old)
        lines = []
        for _ in range(12):                                # the by-id fetches rotate, ten a cycle
            lines += self.tick()
        self.assertEqual(_attempts(lines, REPO, old), [2],
                         "attempt 2 behind %d newer in-progress runs was not announced once" % newer)

    def test_the_status_listings_ask_for_a_hundred_runs(self):
        before = len(self.hub.asked)
        self.tick()
        self.assertEqual(sorted(_status_asked(self.hub, before)), sorted([
            "repos/%s/actions/runs?status=in_progress&per_page=100" % REPO,
            "repos/%s/actions/runs?status=queued&per_page=100" % REPO]))
        self.assertEqual(gw.RUNS_STATUS_PER_PAGE, 100)
        self.assertNotEqual(gw.RUNS_STATUS_PER_PAGE, gw.RUNS_PER_PAGE)
        self.assertEqual(len(_runs_asked(self.hub, before)), 1)
        self.assertIn("per_page=%d" % gw.RUNS_PER_PAGE, _runs_asked(self.hub, before)[0],
                      "the plain listing keeps its own page size")

    def test_a_re_run_behind_21_newer_in_progress_runs_is_tracked_and_announced(self):
        self._a_re_run_behind(21)

    def test_a_re_run_behind_99_newer_in_progress_runs_is_tracked_and_announced(self):
        self._a_re_run_behind(99)

    def test_a_full_status_answer_is_told_once_while_it_stays_full(self):
        self.burst(100, status="in_progress", conclusion=None, name="CI")
        lines = self.tick()
        told = _status_cap_lines(lines)
        self.assertEqual(len(told), 1, lines)
        self.assertTrue(told[0].startswith("type=unread_runs repo=app reason=status_cap "
                                           "status=in_progress | "), told[0])
        self.assertIn("100", told[0])
        self.assertTrue([n for n in self.poller.notes if "status=in_progress" in n and "full" in n],
                        self.poller.notes)
        self.assertEqual(_status_cap_lines(self.tick()), [], "a still-full answer was told again")
        self.assertEqual(_status_cap_lines(self.tick()), [], "a still-full answer was told again")
        self.assertEqual(_status_cap_lines(lines, "queued"), [], "an answer that was not full was told")

    def test_a_status_answer_full_again_after_it_was_not_full_is_told_again(self):
        runs = self.burst(100, status="in_progress", conclusion=None, name="CI")
        self.assertEqual(len(_status_cap_lines(self.tick())), 1)
        self.assertEqual(_status_cap_lines(self.tick()), [])
        runs[0].update(status="completed", conclusion="success", updated_at=self.clock.at())
        self.assertEqual(_status_cap_lines(self.tick()), [], "99 in a status answer was told")
        self.burst(1, status="in_progress", conclusion=None, name="CI")
        self.assertEqual(len(_status_cap_lines(self.tick(), "in_progress")), 1,
                         "an answer full again after one that was not was not told")
        self.assertEqual(_status_cap_lines(self.tick()), [])

    def test_ninety_nine_in_a_status_answer_is_not_told(self):
        self.burst(99, status="in_progress", conclusion=None, name="CI")
        self.assertEqual(_status_cap_lines(self.tick()), [])
        self.assertFalse([n for n in self.poller.notes if "listing is full" in n], self.poller.notes)

    def test_the_two_statuses_are_remembered_apart(self):
        self.burst(100, status="in_progress", conclusion=None, name="CI")
        self.assertEqual(len(_status_cap_lines(self.tick(), "in_progress")), 1)
        self.burst(100, status="queued", conclusion=None, name="CI")
        lines = self.tick()
        self.assertEqual(len(_status_cap_lines(lines, "queued")), 1, lines)
        self.assertEqual(_status_cap_lines(lines, "in_progress"), [])


class TheFirstRun(_ClockedRuns):
    """Review finding: what the first run seeded silently is history, and
    no later poll's page-back reaches behind it."""

    def test_the_first_run_reads_page_one_and_later_polls_replay_nothing_behind_it(self):
        hub = _Hub(clock=self.clock)
        poller = _Poller(self, hub)
        for i in range(45):
            hub.add_run(created_at=self.clock.at(-60 + i))
        state = poller.load()
        self.assertEqual(poller.poll(state), [])
        self.assertEqual(len(_runs_asked(hub, 0)), 1)
        for _ in range(4):
            self.clock.advance(60)
            self.assertEqual(_news(poller.poll(state)), [], "a later poll replayed pre-baseline history")

    def test_a_lost_state_file_after_a_wave_replays_nothing(self):
        made = self.burst(45)
        self.assertEqual(_run_ids(self.tick()), sorted(r["id"] for r in made))
        os.remove(self.poller.state_path)
        fresh = self.poller.load()
        self.assertTrue(fresh["first_run"])
        self.assertEqual(self.tick(state=fresh), [])
        for _ in range(3):
            self.assertEqual(_news(self.tick(state=fresh)), [], "the lost file's wave was announced twice")


class ParkedRunsDoNotStarveTheRest(_ClockedRuns):
    """Review finding: the page-back tracks every run still going in a
    burst, and the by-id fetch used to take the ten oldest every cycle, so ten parked runs
    starved every completion behind them. It rotates now, least recently checked first."""

    def test_twelve_parked_runs_in_a_forty_run_burst_starve_nothing(self):
        parked = self.burst(12, status="queued", conclusion=None, name="Deploy (prod)")
        ci = self.burst(28, start=20, status="in_progress", conclusion=None, name="CI")
        self.tick()
        self.assertEqual(len(self.state["runs_inflight"][REPO]), 40)
        self.burst(25, start=30, name="Deploy (dev)")
        self.tick(300)                                     # still inside the margin: read from pages
        for run in ci:
            run.update(status="completed", conclusion="success")
        announced = []
        for _ in range(6):
            announced.extend(_ci(self.tick(300)))
        self.assertEqual(sorted(announced), sorted(self.line(r, "success") for r in ci),
                         "parked runs starved the CI completions behind them")
        self.assertEqual(set(self.state["runs_inflight"][REPO]), set(str(r["id"]) for r in parked))


class TwoRepos(_ClockedRuns):
    """The page-back runs for every watched repo, not only the first."""

    repos = (REPO, "acme/agent-watchtower")

    def test_a_burst_in_the_second_repo_is_read_in_full(self):
        made = self.burst(30, repo=self.repos[1])
        lines = _news(self.tick())
        self.assertEqual(_run_ids([l for l in lines if " repo=agent-watchtower " in l]),
                         sorted(r["id"] for r in made), lines)
class RunsCreatedWhileTheDaemonWasDown(_ClockedRuns):
    """Across a restart: the page-back reaches back to the SAVED watermark, so a
    wave that landed while the daemon was down is read on resume — and promise
    holds, so nothing announced before the restart is announced again."""

    def test_a_wave_during_thirty_minutes_of_downtime_is_read_on_resume(self):
        ci = []
        for i in range(40):
            name = "CI" if i % 5 == 0 else "Deploy (dev)"
            run = self.hub.add_run(name=name, created_at=self.clock.at(300 + i * 30),
                                   status="in_progress" if name == "CI" else "completed",
                                   conclusion=None if name == "CI" else "success")
            if name == "CI":
                ci.append(run)
        restarted = self.poller.load()
        lines = self.tick(1800, state=restarted)
        self.assertEqual(set(restarted["runs_inflight"][REPO]), set(str(r["id"]) for r in ci),
                         "a CI run created during the downtime was never tracked")
        self.assertEqual(len(_news(lines)), 32)

    def test_a_restart_re_announces_nothing_a_page_back_reads_again(self):
        self.burst(30)
        self.assertEqual(len(_news(self.tick())), 30)
        restarted = self.poller.load()
        self.assertEqual(_news(self.tick(state=restarted)), [])

    def test_a_held_cutoff_survives_a_restart(self):
        self.burst(45)
        self.hub.fail = ("&page=2",)
        self.assertEqual(len(_news(self.tick())), 20)
        self.hub.fail = ()
        restarted = self.poller.load()
        self.assertIn(REPO, restarted["runs_from"])
        self.assertEqual(len(_news(self.tick(300, state=restarted))), 25)


# ---------------------------------------------------------------------------
# Midnight, two repos, a frozen watermark, the hold's
# own replay path, the rotation's order, and the constants.
# ---------------------------------------------------------------------------

OTHER = "acme/agent-watchtower"


class _ARetiredRunIsTold(object):
    """A run tracked for 72 hours without completing is retired. That used to be a
    daemon-log note only, so with the run's by-id fetch failing (and, in the two-repo cases, the
    other repo's runs listing dead too) nothing reached the profile log. It is written now, as a
    `type=unread_runs reason=retired` line, once per run and attempt: the status listing tracks a
    run that is still going again the next cycle, and without that memory the line repeated every
    72 hours for as long as the run stayed parked."""

    repos = (REPO,)
    other_listing_dead = False

    def _park(self):
        """A run left in progress, then pushed off the newest page."""
        run = self.hub.add_run(repo=REPO, name="Deploy (prod)", status="in_progress", conclusion=None,
                               created_at=self.clock.at(1))
        self.tick()
        self.burst(25, repo=REPO, start=5)
        self.tick(300)
        self.assertIn(str(run["id"]), self.state["runs_inflight"][REPO])
        return run

    def _fail_by_id(self, run):
        dead = self.other_listing_dead

        def fails(path):
            return (path.endswith("/actions/runs/%d" % run["id"])
                    or (dead and path.startswith("repos/%s/actions/runs?" % OTHER)))
        self.hub.fail_when = fails

    def _told(self, run):
        return ("type=unread_runs repo=%s reason=retired run=%d | tracked %d h without completing; its "
                "outcome was not announced, check it by id"
                % (REPO.split("/")[-1], run["id"], gw.INFLIGHT_MAX_SECS // 3600))

    def _hours(self, hours):
        """The `unread_runs` lines about the subject repo written over ``hours`` hourly polls (a dead
        listing in the other repo writes its own `hold_expired` line, which is not this ticket's)."""
        out = []
        for _ in range(hours):
            out.extend(l for l in _typed(self.tick(3600), "type=unread_runs") if " repo=app " in l)
        return out

    def test_a_run_whose_by_id_fetch_fails_for_73_hours_is_told_once(self):
        run = self._park()
        self._fail_by_id(run)
        self.assertEqual(self._hours(73), [self._told(run)])
        self.assertEqual(self._hours(150), [], "the retirement was told again while the run stayed parked")

    def test_a_parked_run_that_still_answers_is_told_once(self):
        run = self._park()
        self.assertEqual(self._hours(73), [self._told(run)])
        self.assertEqual(self._hours(150), [])

    def test_a_restart_in_between_repeats_nothing(self):
        run = self._park()
        self._fail_by_id(run)
        self.assertEqual(self._hours(73), [self._told(run)])
        self.state = self.poller.load()                    # the daemon restarts, as launchd does
        self.assertEqual(self._hours(72), [], "a restart told the retirement again")
        self.state = self.poller.load()
        self.assertEqual(self._hours(72), [])

    def test_a_restart_before_the_retirement_still_tells_it_once(self):
        run = self._park()
        self._fail_by_id(run)
        self.assertEqual(self._hours(40), [])
        self.state = self.poller.load()
        self.assertEqual(self._hours(40), [self._told(run)])

    def test_a_later_attempt_of_the_run_is_told_on_its_own(self):
        run = self._park()
        self._fail_by_id(run)
        self.assertEqual(self._hours(73), [self._told(run)])
        run["run_attempt"] = 2                             # a re-run that parks again
        self.assertEqual(self._hours(73), [self._told(run)])
        self.assertEqual(self._hours(150), [])

    def test_a_404_prints_nothing(self):
        run = self._park()
        del self.hub.runs[REPO][run["id"]]
        self.assertEqual(self._hours(73), [])
        self.assertTrue([n for n in self.poller.notes if "is gone (404)" in n], self.poller.notes)
        self.assertNotIn(str(run["id"]), self.state["runs_inflight"][REPO])

    def test_a_line_that_fails_to_write_is_retried_not_lost(self):
        """Recorded only once written. The run stays tracked, and the memory of having
        told it is not made, until the line is out."""
        run = self._park()
        self._fail_by_id(run)
        self.state["runs_inflight"][REPO][str(run["id"])] -= gw.INFLIGHT_MAX_SECS + 1

        def refuses(profile, line):
            raise OSError("no space left on device")
        with mock.patch.object(gw, "emit", refuses):
            self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1, self.poller.raised)
        self.assertIn(str(run["id"]), self.state["runs_inflight"][REPO], "a retirement was recorded unwritten")
        retried = [l for l in _typed(self.tick(), "type=unread_runs") if " repo=app " in l]
        self.assertEqual(retried, [self._told(run)])


class ARetiredRunIsTold(_ARetiredRunIsTold, _ClockedRuns):
    pass


class ARetiredRunIsToldWithAnotherReposListingDead(_ARetiredRunIsTold, _ClockedRuns):
    repos = (REPO, OTHER)
    other_listing_dead = True


class ARetiredRunIsToldWithAnotherReposListingDeadMirrored(_ARetiredRunIsTold, _ClockedRuns):
    repos = (OTHER, REPO)
    other_listing_dead = True


class ARetiredRunKeepsItsAttempt(_ClockedRuns):
    """The attempt in a retirement's key came from the newest page
    and the in_progress / queued listings only, so a run parked at a deployment gate (`waiting`,
    in neither listing and off the page) was keyed `retired:<id>:?`, and its next attempt's
    retirement found that key and was swallowed. It also came out `?` with a healthy by-id fetch,
    which did not keep the attempt it was answered with. The attempt is saved with the run, where it
    joins `runs_inflight` and where a by-id answer returns it."""

    def _park_waiting(self):
        run = self.hub.add_run(repo=REPO, name="Deploy (prod)", status="waiting", conclusion=None,
                               created_at=self.clock.at(1))
        self.tick()
        self.burst(25, repo=REPO, start=5)
        self.tick(300)
        self.assertIn(str(run["id"]), self.state["runs_inflight"][REPO])
        return run

    def _told(self, run):
        return ("type=unread_runs repo=%s reason=retired run=%d | tracked %d h without completing; its "
                "outcome was not announced, check it by id"
                % (REPO.split("/")[-1], run["id"], gw.INFLIGHT_MAX_SECS // 3600))

    def _hours(self, hours):
        out = []
        for _ in range(hours):
            out.extend(l for l in _typed(self.tick(3600), "type=unread_runs") if " repo=app " in l)
        return out

    def test_the_reviewers_probe_each_attempt_of_a_run_parked_at_waiting_is_told(self):
        run = self._park_waiting()
        self.assertEqual(self._hours(73), [self._told(run)], "attempt 1 was not told")
        self.assertNotIn(str(run["id"]), self.state["runs_inflight"][REPO])
        run.update(status="in_progress", run_attempt=2)    # a re-run: the status listing tracks it again
        self.tick()
        self.assertIn(str(run["id"]), self.state["runs_inflight"][REPO])
        run.update(status="waiting")                       # and it parks at the gate again
        self.assertEqual(self._hours(73), [self._told(run)], "attempt 2 was swallowed by attempt 1's key")
        self.assertEqual(self._hours(150), [], "a retirement was told again")

    def test_the_attempt_is_saved_where_the_run_joins_the_runs_in_flight(self):
        run = self._park_waiting()
        self.assertEqual(self.state["runs_attempt"][REPO][str(run["id"])], 1)
        self.assertEqual(self.poller.saved()["runs_attempt"][REPO][str(run["id"])], 1)

    def test_a_healthy_by_id_fetch_keeps_the_attempt(self):
        run = self._park_waiting()
        del self.state["runs_attempt"]                     # a state file from before the record
        run["run_attempt"] = 2                             # what only the by-id answer can say
        self.tick()
        self.assertEqual(self.state["runs_attempt"][REPO][str(run["id"])], 2)
        self.assertEqual(self._hours(73), [self._told(run)])
        self.assertIn("retired:%d:2" % run["id"], self.state["seen"])
        self.assertNotIn("retired:%d:?" % run["id"], self.state["seen"])

    def test_a_run_whose_attempt_was_never_seen_is_keyed_with_a_question_mark(self):
        run = self._park_waiting()
        del self.state["runs_attempt"]
        self.hub.fail_when = lambda path: path.endswith("/actions/runs/%d" % run["id"])
        self.assertEqual(self._hours(73), [self._told(run)])
        self.assertIn("retired:%d:?" % run["id"], self.state["seen"])

    def test_the_record_follows_the_runs_in_flight(self):
        run = self._park_waiting()
        run.update(status="completed", conclusion="success")
        self.tick()
        self.assertNotIn(str(run["id"]), self.state["runs_inflight"][REPO])
        self.assertNotIn(str(run["id"]), self.state["runs_attempt"][REPO])

    def test_a_state_file_without_the_record_loads_and_runs(self):
        run = self._park_waiting()
        raw = self.poller.saved()
        del raw["runs_attempt"]
        with open(self.poller.state_path, "w") as fh:
            json.dump(raw, fh)
        state = self.poller.load()
        self.assertFalse(state["first_run"])
        self.assertEqual(state["runs_attempt"], {})
        self.state = state
        self.tick()
        self.assertEqual(self.state["runs_attempt"][REPO][str(run["id"])], 1, "the by-id answer was not kept")
        self.assertEqual(self._hours(73), [self._told(run)])

    def test_a_malformed_record_is_dropped_and_the_cycle_runs(self):
        run = self._park_waiting()
        rid = str(run["id"])
        for bad in (None, [], ["x"], "x", 5, {REPO: None}, {REPO: []}, {REPO: {rid: "x"}}, {REPO: {rid: True}},
                    {REPO: {rid: 0}}, {REPO: {rid: None}}, {REPO: {"abc": 1}}):
            raw = self.poller.saved()
            raw["runs_attempt"] = bad
            with open(self.poller.state_path, "w") as fh:
                json.dump(raw, fh)
            state = self.poller.load()
            self.assertIsInstance(state["runs_attempt"], dict, repr(bad))
            self.assertNotIn(rid, state["runs_attempt"].get(REPO, {}), repr(bad))
            self.state = state
            self.tick()
            self.assertEqual(self.state["runs_attempt"][REPO][rid], 1, repr(bad))
        raw = self.poller.saved()
        raw["runs_attempt"] = {REPO: {rid: 3}, "x/y": {"9": 2}}
        with open(self.poller.state_path, "w") as fh:
            json.dump(raw, fh)
        self.assertEqual(self.poller.load()["runs_attempt"], {REPO: {rid: 3}, "x/y": {"9": 2}})


class MoreRunsCases(_ClockedRuns):
    """Single-repo cases the re-review found unpinned."""

    def test_a_cap_hit_inside_an_open_runs_episode_is_told(self):
        stuck = self.hub.add_run(name="CI", status="in_progress", conclusion=None, created_at=self.clock.at(1))
        self.tick()
        self.burst(25, start=5)
        self.tick(300)                                     # still read from a page inside the margin
        self.hub.fail_when = lambda path: path.endswith("/actions/runs/%d" % stuck["id"])
        self.assertTrue(_typed(self.tick(300), "type=degraded sub=runs"), "the by-id failure opened no episode")
        self.burst(130)
        self.assertTrue(_typed(self.tick(), "type=unread_runs"), "a cap hit inside an open episode was silent")

    def test_a_cap_hit_during_a_held_read_is_told_and_the_hold_ends(self):
        self.burst(45)
        self.hub.fail = ("&page=2",)
        self.tick()
        self.burst(110, start=1)
        self.hub.fail = ()
        lines = self.tick()
        self.assertTrue(_typed(lines, "type=unread_runs repo=app reason=page_cap "), lines)
        self.assertNotIn(REPO, self.state["runs_from"])

    def test_a_frozen_watermark_does_not_repeat_the_same_cap_line(self):
        """Review finding: While a cycle keeps raising, the watermark stays
        where it was and every poll re-reads the same window: the same window is told once.
        A second burst in the same streak moves `until`, and is told."""
        self.tick()
        self.tick()
        frozen = self.state["since"]
        self.burst(110)
        lines = []
        for _ in range(5):
            self.state["since"] = frozen
            lines.extend(self.tick())
        self.assertEqual(len(_typed(lines, "type=unread_runs")), 1, _typed(lines, "type=unread_runs"))
        self.burst(110)
        self.state["since"] = frozen
        self.assertEqual(len(_typed(self.tick(), "type=unread_runs")), 1, "a moved window was not told")

    def test_a_non_object_entry_on_a_deeper_page_is_a_hole(self):
        self.burst(45)
        real = self.hub.__call__

        def answer(path):
            got = real(path)
            if "&page=2" in path:
                got = {"workflow_runs": got["workflow_runs"] + ["junk"]}
            return got
        self.poller.cfg = dict(self.poller.cfg)
        with mock.patch.object(gw, "gh_api", answer):
            lines = self.tick()
        self.assertTrue(_typed(lines, "type=degraded sub=runs"), lines)
        self.assertIn(REPO, self.state["runs_from"])

    def test_a_hole_then_a_long_downtime_is_read_by_the_first_poll_back(self):
        """The hold ages on the wall clock, so after a hole and 40 minutes down it is past
        its bound; the first poll back still reads from it, and only an incomplete read
        would release it."""
        made = self.burst(45)
        self.hub.fail = ("&page=2",)
        first = self.tick(65)
        self.assertEqual(len(_news(first)), 20)
        held = self.tick(65) + self.tick(65)
        self.assertEqual(_news(held), [])                            # three failed polls: past the margin
        self.hub.fail = ()
        restarted = self.poller.load()
        lines = self.tick(40 * 60, state=restarted)
        self.assertEqual(len(_news(lines)), 25)
        self.assertEqual(_typed(lines, "type=unread_runs"), [])
        self.assertEqual(_run_ids(first + held + lines), sorted(r["id"] for r in made))
    def test_the_rotation_takes_the_least_recently_attended_first(self):
        parked = self.burst(15, status="queued", conclusion=None, name="Deploy (prod)")
        self.tick()
        self.burst(25, start=30)
        self.tick(300)
        self.tick(300)                                     # past the margin: by id only from here
        ids = [r["id"] for r in parked]
        first = list(self.hub.asked)
        self.tick()
        got = [int(p.rsplit("/", 1)[1]) for p in self.hub.asked[len(first):] if re.search(r"/actions/runs/\d+$", p)]
        self.assertEqual(sorted(got), sorted(ids[10:] + ids[:5]),
                         "the rotation skipped the least recently checked runs")
        newer = self.burst(10, start=40, status="queued", conclusion=None, name="Deploy (stage)")
        self.tick()                                        # the ten new runs are tracked from a page
        self.burst(25, start=50)
        self.tick(300)                                     # still read from pages inside the margin
        mark = len(self.hub.asked)
        self.tick(300)                                     # the first poll they are off every page
        got = [int(p.rsplit("/", 1)[1]) for p in self.hub.asked[mark:] if re.search(r"/actions/runs/\d+$", p)]
        self.assertFalse(set(got) <= set(r["id"] for r in newer),
                         "newly tracked runs pushed every re-check back")

    def test_a_restart_does_not_restart_the_rotation(self):
        parked = self.burst(15, status="queued", conclusion=None, name="Deploy (prod)")
        self.tick()
        self.burst(25, start=30)
        self.tick(300)
        self.tick(300)                                     # the first by-id round: the ten oldest
        restarted = self.poller.load()
        mark = len(self.hub.asked)
        self.tick(state=restarted)
        got = [int(p.rsplit("/", 1)[1]) for p in self.hub.asked[mark:] if re.search(r"/actions/runs/\d+$", p)]
        ids = [r["id"] for r in parked]
        self.assertEqual(sorted(got), sorted(ids[10:] + ids[:5]), "a restart restarted the rotation from the oldest")

    def test_a_capped_bursts_re_read_inside_the_margin_is_told_with_its_own_since(self):
        """the guard drops a hit only when its ``since`` AND
        its ``until`` both repeat the last line for that repo. A burst past the cap is re-read by
        the next two polls (the margin): the same newest 100 runs, so the same ``until``, but a
        later ``since``, and each of those hits is written, as the README says ("re-read, and
        re-told, on the next poll or two"). Keyed on ``until`` alone, the re-reads were silent."""
        self.tick()
        self.tick()                                        # past the first run's floor
        self.burst(gw.RUNS_PER_PAGE * gw.RUNS_MAX_PAGES + 15)
        capped, wanted = [], []
        for _ in range(4):
            previous = self.clock.now
            got = _typed(self.tick(), "type=unread_runs")
            capped += got
            if got:
                wanted.append(_gh_ts(previous - gw.RUNS_CUTOFF_MARGIN))
        windows = [re.search(r" since=(\S+) until=(\S+) ", line).groups() for line in capped]
        self.assertEqual([since for since, _ in windows], wanted)
        self.assertEqual(len(windows), 3, capped)
        self.assertEqual(len(set(until for _, until in windows)), 1, capped)

    def test_the_rotation_memory_keeps_only_runs_still_in_flight(self):
        """`runs_checked` keeps a by-id check time only for a run
        still in flight: a run that completes leaves it one cycle after it leaves `runs_inflight`,
        because the clean-up runs before the by-id fetch that finds it complete. So a save can hold
        that cycle's completions beyond `runs_inflight`; the next cycle's clean-up drops them."""
        parked = self.burst(15, status="queued", conclusion=None, name="Deploy (prod)")
        self.tick()
        self.burst(25, start=30)
        self.tick(300)
        self.tick(300)                                     # past the margin: by id only from here
        self.tick()
        self.assertEqual(set(self.state["runs_checked"][REPO]), set(str(r["id"]) for r in parked))
        for run in parked[:10]:
            run.update(status="completed", conclusion="success")
        for _ in range(3):
            self.tick()
        self.assertEqual(set(self.state["runs_inflight"][REPO]), set(str(r["id"]) for r in parked[10:]))
        self.assertEqual(set(self.state["runs_checked"][REPO]), set(self.state["runs_inflight"][REPO]))
        self.assertEqual(set(self.poller.saved()["runs_checked"][REPO]), set(str(r["id"]) for r in parked[10:]))

    def test_the_constants_are_pinned_so_a_change_is_deliberate(self):
        self.assertEqual(gw.RUNS_CUTOFF_MARGIN, timedelta(minutes=2))
        self.assertEqual(gw.RUNS_HOLD_MAX, timedelta(minutes=30))
        self.assertEqual(gw.RUNS_MAX_PAGES, 5)
        self.assertEqual(gw.INFLIGHT_FETCH_CAP, 10)


class TheHoldDoesNotReplay(_ClockedRuns):
    """Review finding: runs announced and then pushed behind a failing page
    are not refreshed in `seen` while the page fails. 150 long runs completing by id during
    the hold used to evict them, and the held cutoff then read them back as news."""

    def test_completions_by_id_during_a_sixteen_minute_hold_announce_nothing_twice(self):
        for _ in range(5):
            self.burst(30, status="in_progress", conclusion=None, name="CI")
            self.tick()
        long_runs = [r for r in self.hub.runs[REPO].values() if r["status"] == "in_progress"]
        self.assertEqual(len(long_runs), 150)
        self.assertEqual(len(self.state["runs_inflight"][REPO]), 150)
        self.tick(300)
        self.tick(300)                                     # the long runs are off every page read now
        made = self.burst(45)
        everything = list(self.tick())
        self.assertEqual(len(_news(everything)), 45)
        # A quiet poll that announces runs BEFORE the hold; the 45 above, one poll
        # earlier, are inside the margin the held read goes back by and must survive too.
        early = self.burst(10)
        before = self.tick()
        self.assertEqual(len(_news(before)), 10)
        everything.extend(before)
        self.hub.fail = ("&page=2",)
        behind = self.burst(25)
        everything.extend(self.tick())
        for run in long_runs:
            run.update(status="completed", conclusion="success")
        for _ in range(16):
            everything.extend(self.tick())
        self.hub.fail = ()
        everything.extend(self.tick())
        everything.extend(self.tick())
        ids = [re.search(r" run=(\d+)\.", l).group(1) for l in everything if l.startswith("type=run ")]
        twice = sorted(set(i for i in ids if ids.count(i) > 1))
        self.assertEqual(twice, [], "the hold announced %d runs twice" % len(twice))
        self.assertEqual(len([i for i in ids if int(i) in set(r["id"] for r in long_runs)]), 150)
        # every run made, each announced once.
        # The old tail asserted the fixture's own length (45), so a hold released with no line once
        # the run ids passed their budget lost the 5 runs behind page 2 and still passed.
        self.assertEqual(sorted(int(i) for i in ids), sorted(r["id"] for r in long_runs + made + early + behind))


class TheRunsRecordIsBounded(_ClockedRuns):
    """`runs_recent` keeps a repo's run keys only back to its cycle's start less the
    margin (or, held, the hold's start less the margin). The trim at the end of the runs read is
    what bounds it; without it the record grows by every run the repo ever announces."""

    def test_twenty_busy_ticks_keep_the_record_to_the_margin(self):
        margin = gw.RUNS_CUTOFF_MARGIN.total_seconds()
        sizes = []
        for _ in range(20):
            self.burst(30)
            self.tick()
            record = self.state["runs_recent"][REPO]
            sizes.append(len(record))
            # nothing older than the margin survives a cycle, in memory or in the saved file
            self.assertGreaterEqual(min(record.values()), self.clock.now.timestamp() - margin,
                                    "runs_recent kept a key older than the margin; sizes %s" % sizes)
            saved = self.poller.saved()["runs_recent"][REPO]
            self.assertGreaterEqual(min(saved.values()), self.clock.now.timestamp() - margin)
        # the 20 busy ticks made 600 runs; the record holds only the last margin's worth of them
        self.assertLessEqual(max(sizes), 150, "runs_recent grew past what the trim allows; sizes %s" % sizes)
        self.assertEqual(sizes[-5:], [150] * 5)


class AcrossMidnight(_ClockedRuns):
    """Review finding: the listing's `created>=` date came from the
    watermark, so a hold kept open past 00:05 UTC asked for a listing that no longer held
    the runs it was kept for, and released them unread with `degraded_recovered`."""

    start = datetime(2026, 9, 30, 23, 50, tzinfo=timezone.utc)

    def test_a_hold_open_across_midnight_is_still_read(self):
        self.clock.now = datetime(2026, 9, 30, 23, 55, 0, tzinfo=timezone.utc)
        self.tick(0)
        made = self.burst(45, start=10)
        self.hub.fail = ("&page=2",)
        lines = []
        while self.clock.now < datetime(2026, 10, 1, 0, 7, tzinfo=timezone.utc):
            lines.extend(self.tick())
        self.assertEqual(_typed(lines, "type=degraded_recovered"), [],
                         "degraded_recovered was written while page 2 was still failing")
        self.assertIn(REPO, self.state["runs_from"])
        self.hub.fail = ()
        lines.extend(self.tick())
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made), "the runs behind the hold were lost at midnight")

    def test_a_wave_at_23_55_is_read_after_a_restart_at_00_10(self):
        self.clock.now = datetime(2026, 9, 30, 23, 55, 0, tzinfo=timezone.utc)
        self.tick(0)
        made = self.burst(45, start=10)
        restarted = self.poller.load()
        self.clock.now = datetime(2026, 10, 1, 0, 10, tzinfo=timezone.utc)
        self.assertEqual(_run_ids(self.tick(0, state=restarted)), sorted(r["id"] for r in made))


def _short(repo):
    return repo.split("/")[-1]


def _ids_in(lines, repo):
    """The run ids announced for ``repo`` among ``lines``, sorted; a repeat appears twice."""
    return _run_ids([line for line in lines if " repo=%s " % _short(repo) in line])


class _TwoReposAtOnce(_ReRuns):
    """The mirror cases: every per-repo rule holds for every repo, in either order.

    each case runs with its subject
    first and with it second, and the two classes below run every case in both repo orders,
    written unconditionally. A mutant that applies a per-repo rule to the first repo only, or
    the last only, passed a suite whose two-repo cases each named one repo.

    Three cases fix their subject as the second repo, so the two orders make each repo their
    subject, but never first:
    `test_a_repo_that_leaves_the_config_leaves_the_runs_state` and
    `test_a_failed_pr_listing_raises_the_cycle_and_the_frozen_window_is_told_once` below, and
    `_AFailedListingInEitherRepo.test_main_keeps_a_raised_cycles_state_when_its_checkpoint_cannot_be_saved`."""

    def _other(self, subject):
        return [r for r in self.repos if r != subject][0]

    def _page_two_fails(self, repo):
        self.hub.fail_when = lambda path: path.startswith("repos/%s/actions/runs?" % repo) and "&page=2" in path

    def test_a_burst_in_each_repo_in_the_same_tick_is_read_in_full(self):
        mine, theirs = self.burst(45), self.burst(45, repo=OTHER)
        lines = _news(self.tick())
        self.assertEqual(_ids_in(lines, REPO), sorted(r["id"] for r in mine))
        self.assertEqual(_ids_in(lines, OTHER), sorted(r["id"] for r in theirs))

    def _one_repo_page_two_fails(self, failing):
        clean = self._other(failing)
        mine, theirs = self.burst(45, repo=failing), self.burst(45, repo=clean)
        self._page_two_fails(failing)
        first = _news(self.tick())
        self.assertEqual(len(_ids_in(first, failing)), 20)
        self.assertEqual(_ids_in(first, clean), sorted(r["id"] for r in theirs))
        self.assertEqual(list(self.state["runs_from"]), [failing], "a complete read elsewhere released the hold")
        self.hub.fail_when = None
        later = _news(self.tick())
        self.assertEqual(_ids_in(first + later, failing), sorted(r["id"] for r in mine))

    def test_page_two_failing_in_the_first_repo_while_the_second_reads_cleanly(self):
        self._one_repo_page_two_fails(self.repos[0])

    def test_page_two_failing_in_the_second_repo_while_the_first_reads_cleanly(self):
        self._one_repo_page_two_fails(self.repos[1])

    # -- the cap line ---------------------------------------------------------------------

    def _a_cap_hit_names_its_repo(self, subject):
        self.tick()
        self.tick()                                        # past the first run's floor
        made = self.burst(121, repo=subject)
        lines = self.tick()
        capped = _typed(lines, "type=unread_runs")
        self.assertEqual(len(capped), 1, capped)
        self.assertIn(" repo=%s reason=page_cap " % _short(subject), capped[0])
        newest = sorted(made, key=lambda r: r["id"])[-gw.RUNS_PER_PAGE * gw.RUNS_MAX_PAGES:]
        self.assertEqual(_ids_in(lines, subject), sorted(r["id"] for r in newest))

    def test_a_cap_hit_in_the_first_repo_names_it(self):
        self._a_cap_hit_names_its_repo(self.repos[0])

    def test_a_cap_hit_in_the_second_repo_names_it(self):
        self._a_cap_hit_names_its_repo(self.repos[1])

    def test_a_frozen_window_is_told_once_in_each_repo(self):
        """the cap guard keeps ONE window per repo. Two repos
        past the cap under one frozen watermark each tell their window once; one slot for the
        profile alternated between them and told both on every poll."""
        self.tick()
        self.tick()
        frozen = self.state["since"]
        self.burst(110, repo=self.repos[0])
        self.burst(110, repo=self.repos[1])
        lines = []
        for _ in range(5):
            self.state["since"] = frozen
            lines.extend(self.tick())
        capped = _typed(lines, "type=unread_runs")
        self.assertEqual(sorted(_repo_of(l) for l in capped), sorted(_short(r) for r in self.repos), capped)

    # -- the rotation -----------------------------------------------------------------------

    def _parked_runs_starve_nothing(self, subject):
        other = self._other(subject)
        self.burst(12, repo=subject, status="queued", conclusion=None, name="Deploy (prod)")
        ci = self.burst(28, repo=subject, start=20, status="in_progress", conclusion=None, name="CI")
        self.burst(3, repo=other, status="queued", conclusion=None, name="Deploy (prod)")
        self.tick()
        self.burst(25, repo=subject, start=30)
        self.burst(25, repo=other, start=30)
        self.tick(300)
        for run in ci:
            run.update(status="completed", conclusion="success")
        announced = []
        for _ in range(6):
            announced.extend(l for l in self.tick(300) if " CI@" in l)
        self.assertEqual(_ids_in(announced, subject), sorted(r["id"] for r in ci),
                         "parked runs in %s starved its CI runs" % subject)

    def test_twelve_parked_and_twenty_eight_ci_in_the_first_repo(self):
        self._parked_runs_starve_nothing(self.repos[0])

    def test_twelve_parked_and_twenty_eight_ci_in_the_second_repo(self):
        self._parked_runs_starve_nothing(self.repos[1])

    # -- the hold ---------------------------------------------------------------------------

    def _the_hold_replays_nothing(self, subject):
        """TheHoldDoesNotReplay in two repos. The subject's page 2 fails for 16 polls while the
        other repo creates 30 runs a poll, so the profile's run ids pass their two-repo budget
        during the hold. The run kind is pruned while a repo is held, but the run keys the held
        read can still return (the subject's `runs_recent`, back to the hold's start less the
        margin) are exempt from the cap: evicted, the runs announced behind the subject's failing
        page were read back by the held cutoff as news. Every run made, in both repos, is
        announced once."""
        other = self._other(subject)
        # 20 runs announced well before the hold. Their keys are past the margin of the
        # hold's start by the time it opens, so the record must have dropped them.
        old = self.burst(20, repo=subject)
        lines = self.tick()
        for _ in range(4):
            lines += self.tick()
        made = old + self.burst(45, repo=subject)
        lines += self.tick()
        # A quiet poll that announces runs BEFORE the hold. The held read goes back to
        # the hold's start less the margin, so the 45 announced one poll earlier (inside the
        # margin) can be read back too, and must survive the pruning as these 10 do.
        made += self.burst(10, repo=subject)
        lines += self.tick()
        self._page_two_fails(subject)
        made += self.burst(25, repo=subject)
        lines += self.tick()
        self.assertEqual(list(self.state["runs_from"]), [subject])
        cap = gw.seen_caps(len(self.repos))["run"]
        mine = len(made)                                   # all the subject's so far: 20 + 45 + 10 + 25
        window = mine - len(old)                           # those from the hold's start less the margin on
        peak = over = exempt_peak = 0
        hold_from = gw._parse_ts(self.state["runs_from"][subject]).timestamp() - gw.RUNS_CUTOFF_MARGIN.total_seconds()
        for _ in range(16):
            made += self.burst(30, repo=other)
            lines += self.tick()
            kept = [k for k in self.state["seen"] if k.startswith("run:")]
            exempt = set(self.state["runs_recent"][subject]) & set(kept)
            # the exempt set is trimmed to the hold's start less the margin on every held tick
            self.assertGreaterEqual(min(self.state["runs_recent"][subject].values()), hold_from)
            peak, exempt_peak = max(peak, len(kept)), max(exempt_peak, len(exempt))
            over = max(over, len(kept) - len(exempt))      # what the cap itself holds
        self.hub.fail_when = None
        lines += self.tick() + self.tick()
        self.assertEqual(self.state["runs_from"], {})
        twice = sorted(i for i, n in collections.Counter(_run_ids(lines)).items() if n > 1)
        self.assertEqual(twice, [], "the hold announced %d runs twice" % len(twice))
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made), "a run was lost")
        # the case is only a test of the rule if the run ids passed their two-repo budget while held
        self.assertGreater(len(made), cap + mine + 30)
        # The run kind is bounded while held: the cap, plus the exempt set (the subject's
        # run keys touched or announced since the hold's start less the margin). The ids outside the
        # exempt set never held more than the cap, and the exempt set is the subject's own runs
        # only (at most those it made in the window, and the 30 it was seeded with, which a page
        # one touched), so the 20 it made before the window are not in it.
        self.assertLessEqual(over, cap)
        self.assertLessEqual(peak, cap + exempt_peak)
        self.assertLessEqual(exempt_peak, window + 30)     # not mine + 30: the 20 made before the window are dropped

    def test_the_hold_replays_nothing_in_the_first_repo(self):
        self._the_hold_replays_nothing(self.repos[0])

    def test_the_hold_replays_nothing_in_the_second_repo(self):
        self._the_hold_replays_nothing(self.repos[1])

    def _a_hold_open_across_midnight_is_read(self, subject):
        """AcrossMidnight in two repos: the listing's `created>=` date reaches the held cutoff in
        every repo, not only one."""
        self.clock.now = datetime(2026, 9, 30, 23, 55, 0, tzinfo=timezone.utc)
        self.tick(0)
        made = self.burst(45, repo=subject, start=10)
        self._page_two_fails(subject)
        lines = []
        while self.clock.now < datetime(2026, 10, 1, 0, 7, tzinfo=timezone.utc):
            lines.extend(self.tick())
        self.assertEqual(_typed(lines, "type=degraded_recovered"), [],
                         "degraded_recovered was written while page 2 was still failing")
        self.assertEqual(list(self.state["runs_from"]), [subject])
        self.hub.fail_when = None
        lines.extend(self.tick())
        self.assertEqual(_ids_in(lines, subject), sorted(r["id"] for r in made),
                         "the runs behind the hold were lost at midnight")

    def test_a_hold_open_across_midnight_is_read_in_the_first_repo(self):
        self._a_hold_open_across_midnight_is_read(self.repos[0])

    def test_a_hold_open_across_midnight_is_read_in_the_second_repo(self):
        self._a_hold_open_across_midnight_is_read(self.repos[1])

    def _a_hole_then_a_long_downtime_is_read(self, subject):
        """MoreRunsCases' hole-then-downtime in two repos: a hold past its bound is still read
        from once, in every repo, by the first poll back."""
        made = self.burst(45, repo=subject)
        self._page_two_fails(subject)
        lines = self.tick(65) + self.tick(65) + self.tick(65)        # three failed polls: past the margin
        self.hub.fail_when = None
        restarted = self.poller.load()
        back = self.tick(40 * 60, state=restarted)
        self.assertEqual(_typed(back, "type=unread_runs"), [])
        self.assertEqual(_ids_in(lines + back, subject), sorted(r["id"] for r in made))

    def test_a_hole_then_a_long_downtime_is_read_in_the_first_repo(self):
        self._a_hole_then_a_long_downtime_is_read(self.repos[0])

    def test_a_hole_then_a_long_downtime_is_read_in_the_second_repo(self):
        self._a_hole_then_a_long_downtime_is_read(self.repos[1])

    # -- a re-run off every page ---------------------------------------------------

    def test_a_re_run_off_every_page_is_announced_in_the_first_repo(self):
        self._a_re_run_off_every_page_is_announced(self.repos[0])

    def test_a_re_run_off_every_page_is_announced_in_the_second_repo(self):
        self._a_re_run_off_every_page_is_announced(self.repos[1])

    def test_attempt_three_is_announced_after_attempt_two_in_the_first_repo(self):
        self._attempt_three_is_announced_after_attempt_two(self.repos[0])

    def test_attempt_three_is_announced_after_attempt_two_in_the_second_repo(self):
        self._attempt_three_is_announced_after_attempt_two(self.repos[1])

    def test_a_queued_re_run_is_found_in_the_first_repo(self):
        self._a_queued_re_run_is_found_too(self.repos[0])

    def test_a_queued_re_run_is_found_in_the_second_repo(self):
        self._a_queued_re_run_is_found_too(self.repos[1])

    def test_a_failed_status_read_in_the_first_repo_is_noted_and_recorded_not_held(self):
        self._a_failed_status_read_is_noted_and_recorded_not_held(self.repos[0], "in_progress")

    def test_a_failed_status_read_in_the_second_repo_is_noted_and_recorded_not_held(self):
        self._a_failed_status_read_is_noted_and_recorded_not_held(self.repos[1], "queued")

    def test_every_repo_is_asked_for_both_statuses_each_cycle(self):
        before = len(self.hub.asked)
        self.tick()
        self.assertEqual(sorted(_status_asked(self.hub, before)), sorted(
            "repos/%s/actions/runs?status=%s&per_page=%d" % (repo, status, gw.RUNS_STATUS_PER_PAGE)
            for repo in self.repos for status in ("in_progress", "queued")))

    # -- the config -------------------------------------------------------------------------

    def test_a_repo_that_leaves_the_config_leaves_the_runs_state(self):
        """a repo dropped from the config takes its hold, its
        rotation memory and its cap memory with it, in memory and in the saved file. A hold kept
        for it would page the repo back to where it stood when it left, should it come back."""
        gone = self.repos[1]
        self.burst(3, repo=gone, status="queued", conclusion=None, name="Deploy (prod)")
        self.tick()
        self.burst(25, repo=gone, start=30)
        self.tick(300)
        self.tick(300)                                     # off every page: checked by id
        self.tick()
        self.burst(121, repo=gone)
        self.assertTrue(_typed(self.tick(), "type=unread_runs"))
        self._page_two_fails(gone)
        self.burst(45, repo=gone)
        self.tick()
        for key in ("runs_from", "runs_checked", "runs_cap_told"):
            self.assertTrue(self.state[key].get(gone), "%s held nothing for %s to clean up" % (key, gone))
        self.poller.cfg = dict(self.poller.cfg, repos=[self.repos[0]])
        self.hub.fail_when = None
        self.tick()
        saved = self.poller.saved()
        for key in ("runs_from", "runs_checked", "runs_cap_told"):
            self.assertNotIn(gone, self.state[key], key)
            self.assertNotIn(gone, saved[key], key)

    # -- a failed PR listing ----------------------------------------------------------------

    def test_a_failed_pr_listing_raises_the_cycle_and_the_frozen_window_is_told_once(self):
        """A failed PR listing raises the cycle, as on main,
        so the watermark does not move. The first repo's burst of 110 is read up to the page
        cap, 100 runs, each announced once, and the same cap window, re-read every poll under
        the frozen watermark, is told once. (An earlier version said the 110 were announced.)"""
        first, failing = self.repos
        self.tick()
        self.tick()
        frozen = self.state["since"]
        made = self.burst(110, repo=first)
        self.hub.fail = ("%s/pulls?" % failing,)
        lines = []
        for _ in range(10):
            lines.extend(self.tick(tolerate=True))
        self.assertEqual(len(self.poller.raised), 10, "a failed PR listing did not raise the cycle")
        self.assertEqual(self.state["since"], frozen, "the watermark moved past a failed cycle")
        self.assertEqual(len(_typed(lines, "type=unread_runs")), 1, _typed(lines, "type=unread_runs"))
        newest = sorted(made, key=lambda r: r["id"])[-gw.RUNS_PER_PAGE * gw.RUNS_MAX_PAGES:]
        self.assertEqual(_ids_in(lines, first), sorted(r["id"] for r in newest))


class TwoReposAtOnce(_TwoReposAtOnce, _ClockedRuns):
    repos = (REPO, OTHER)


class TwoReposAtOnceMirrored(_TwoReposAtOnce, _ClockedRuns):
    repos = (OTHER, REPO)


class TheFirstRunReloaded(_ClockedRuns):
    """The baseline floor is saved and loaded: a restart between the first run and the next
    poll still replays nothing behind it."""

    def test_a_restart_right_after_a_first_run_replays_nothing(self):
        hub = _Hub(clock=self.clock)
        poller = _Poller(self, hub)
        for i in range(45):
            hub.add_run(created_at=self.clock.at(-60 + i))
        state = poller.load()
        self.assertEqual(poller.poll(state), [])
        self.clock.advance(30)
        restarted = poller.load()
        self.assertTrue(restarted.get("runs_baseline_at"), "the floor was not saved and loaded")
        for _ in range(3):
            self.clock.advance(60)
            self.assertEqual(_news(poller.poll(restarted)), [], "a restart after the first run replayed history")


class AFailedCycleLosesNothing(_ClockedRuns):
    """A failed PR listing raises the cycle,
    as on main, so the watermark does not move past what the failed cycle never read, and a
    first run keeps its baseline until a listing answers."""

    def _comment_lines(self, lines):
        return [l for l in lines if l.startswith("type=comment ")]

    def test_a_first_run_whose_pr_listing_fails_once_announces_no_history(self):
        hub = _Hub(clock=self.clock)
        poller = _Poller(self, hub)
        for n in (1, 2, 3):
            hub.listing[REPO].append(_pr(n))
            hub.reviews[(REPO, str(n))] = [hub.new_review(), hub.new_review()]
        state = poller.load()
        hub.fail = ("/pulls?",)
        lines = poller.poll(state, tolerate=True)
        kept_baseline = state["first_run"]
        hub.fail = ()
        for _ in range(3):
            self.clock.advance(60)
            lines.extend(poller.poll(state))
        self.assertEqual([l for l in lines if l.startswith(("type=pr_opened", "type=review"))], [],
                         "a first run that met a failed PR listing announced history")
        self.assertEqual(len(poller.raised), 1, "the failed PR listing did not raise the cycle")
        self.assertTrue(kept_baseline, "a first run cleared its baseline on a listing that never answered")

    def test_a_first_run_whose_pr_listing_is_refused_or_times_out_announces_no_history(self):
        """every failure the suite injected was `HTTP 502`, so a daemon
        that raised for a 5xx alone and caught the rest passed. Live, a 401 on a first run (gh not
        signed in yet) replayed every open PR, its reviews and page one's runs. A refused listing,
        a timeout and an answer that is not JSON each raise the cycle and keep the baseline."""
        errors = (lambda path: RuntimeError("gh api %s failed: HTTP 401: Bad credentials" % path.split("?")[0]),
                  lambda path: subprocess.TimeoutExpired(["gh", "api", path], 45),
                  lambda path: ValueError("Expecting value: line 1 column 1 (char 0)"))
        for error in errors:
            hub = _Hub(clock=self.clock)
            poller = _Poller(self, hub)
            for n in (1, 2, 3):
                hub.listing[REPO].append(_pr(n))
                hub.reviews[(REPO, str(n))] = [hub.new_review(), hub.new_review()]
            for i in range(30):
                hub.add_run(created_at=self.clock.at(-600 + i))
            state = poller.load()

            def refused(path, error=error, hub=hub):
                if "/pulls?" in path:
                    raise error(path)
                return hub(path)
            with mock.patch.object(gw, "gh_api", refused):
                lines = poller.poll(state, tolerate=True)
            self.assertEqual(len(poller.raised), 1, "%r did not raise the cycle" % (error(REPO),))
            self.assertTrue(state["first_run"])
            for _ in range(3):
                self.clock.advance(60)
                lines.extend(poller.poll(state))
            self.assertEqual([l for l in lines if l.startswith("type=")], [],
                             "a first run whose listing failed with %r announced history" % (error(REPO),))

    def test_a_restart_whose_first_cycle_fails_still_reads_the_comments_of_the_gap(self):
        restarted = self.poller.load()
        self.clock.advance(5 * 60)
        made = self.hub.add_comment(body="made while the daemon was down")
        self.clock.advance(35 * 60)                        # restarted 40 minutes after the last poll
        self.hub.fail_when = lambda path: True             # no network on the first poll back
        self.poller.poll(restarted, tolerate=True)
        self.hub.fail_when = None
        lines = self.tick(state=restarted)
        self.assertEqual(self._comment_lines(lines),
                         ["type=comment repo=app #7 id=%d by=bob | made while the daemon was down"
                          % made["id"]])

    def test_every_request_failing_for_ten_polls_loses_no_comment(self):
        self.hub.fail_when = lambda path: True
        lines = []
        made = None
        for i in range(10):
            if i == 2:
                made = self.hub.add_comment(body="made two minutes into the outage")
            lines.extend(self.tick(tolerate=True))
        self.hub.fail_when = None
        lines.extend(self.tick())
        self.assertEqual([l for l in self._comment_lines(lines) if " id=%d " % made["id"] in l and "outage" in l],
                         ["type=comment repo=app #7 id=%d by=bob | made two minutes into the outage"
                          % made["id"]])

    def test_pulls_and_comments_failing_while_actions_answers_loses_no_comment(self):
        self.hub.fail_when = lambda path: "/pulls?" in path or "/comments?" in path
        made, lines = [], []
        for i in range(15):
            if i < 6:
                made.append(self.hub.add_comment(body="comment %d of six" % (i + 1)))
            lines.extend(self.tick(tolerate=True))
        self.hub.fail_when = None
        lines.extend(self.tick())
        got = sorted(int(re.search(r" id=(\d+) ", l).group(1)) for l in self._comment_lines(lines))
        self.assertEqual(got, sorted(c["id"] for c in made), "comments made across the failed cycles were lost")

    def test_a_total_outage_is_a_failed_cycle_again_through_main(self):
        """Review finding: verified through the real `main()` loop rather than
        assumed: every request failing for four passes raises each cycle, the state file is
        never rewritten (its watermark goes stale, which is what the watchdog reads), and
        `main()` writes `error: github polling failing repeatedly` once, at the third."""
        before = self.poller.saved()
        mtime = os.path.getmtime(self.poller.state_path)
        config = os.path.join(os.path.dirname(self.poller.state_dir), "config.json")
        with open(config, "w") as fh:
            json.dump({"profiles": {"acme": {"github": {"repos": [REPO]}}}}, fh)

        class Stop(Exception):
            pass

        passes = []

        def sleep(secs):
            passes.append(secs)
            if len(passes) >= 4:
                raise Stop()
            self.clock.advance(secs)

        self.hub.fail_when = lambda path: True
        clock = types.SimpleNamespace(time=lambda: self.clock.now.timestamp(), sleep=sleep)
        with mock.patch.object(gw, "CONFIG_PATH", config), \
                mock.patch.object(gw, "GH", "/nonexistent/gh"), \
                mock.patch.object(gw, "time", clock):
            with self.assertRaises(Stop):
                gw.main()
        self.assertEqual(self.poller.saved(), before, "a cycle with every request failing saved the state")
        self.assertEqual(os.path.getmtime(self.poller.state_path), mtime)
        errors = [l for l in self.poller.emitted if l.startswith("error: github polling failing repeatedly")]
        self.assertEqual(len(errors), 1, self.poller.emitted)
        self.assertEqual([l for l in self.poller.emitted if l.startswith("error: sub-fetch")], [])

    def test_a_hold_then_a_total_outage_is_read_by_the_first_poll_back(self):
        """README: a total outage fails each cycle before it reaches the runs, so a hold kept
        open through 45 minutes of it is read by the first poll back, with no hold_expired.

        page 2 now fails for three polls, so the hold is older than the
        margin when the outage begins. With a hold one poll old, the margin alone re-read the
        burst, and a daemon with no hold mechanism at all passed this test."""
        made = self.burst(45)
        self.hub.fail = ("&page=2",)
        first = self.tick(65)
        self.assertEqual(len(_news(first)), 20)
        first += self.tick(65) + self.tick(65)                      # three failed polls: past the margin
        self.assertEqual(len(_news(first)), 20)
        self.hub.fail = ()
        self.hub.fail_when = lambda path: True
        for _ in range(45):
            self.tick(tolerate=True)
        self.hub.fail_when = None
        lines = self.tick()
        self.assertEqual(len(_news(lines)), 25)
        self.assertEqual(_typed(lines, "type=unread_runs"), [])
        self.assertEqual(_run_ids(first + lines), sorted(r["id"] for r in made))
class _AFailedListingInEitherRepo(object):
    """the two-repo versions of `AFailedCycleLosesNothing`,
    written unconditionally. Each case fails the PR listing of the first repo and of the second,
    and the two classes below run each in both repo orders. With only the five single-repo tests,
    a daemon that raised for the LAST repo's failed listing alone (and caught the others, as
    an earlier version did) passed: live, the same failures came back for the first repo."""

    def _other(self, repo):
        return [r for r in self.repos if r != repo][0]

    def _a_first_run_whose_listing_fails_once(self, failing):
        hub = _Hub(repos=self.repos, clock=self.clock)
        poller = _Poller(self, hub)
        for repo in self.repos:
            for n in (1, 2, 3):
                hub.listing[repo].append(_pr(n))
                hub.reviews[(repo, str(n))] = [hub.new_review(), hub.new_review()]
            for i in range(30):
                hub.add_run(repo=repo, created_at=self.clock.at(-600 + i))
        state = poller.load()
        hub.fail = ("%s/pulls?" % failing,)
        lines = poller.poll(state, tolerate=True)
        self.assertEqual(len(poller.raised), 1, "the failed PR listing did not raise the cycle")
        self.assertTrue(state["first_run"], "a first run cleared its baseline on a listing that never answered")
        hub.fail = ()
        for _ in range(3):
            self.clock.advance(60)
            lines.extend(poller.poll(state))
        self.assertEqual([l for l in lines if l.startswith("type=")], [], "a first run announced history")

    def test_a_first_run_whose_first_repos_listing_fails_once_announces_no_history(self):
        self._a_first_run_whose_listing_fails_once(self.repos[0])

    def test_a_first_run_whose_second_repos_listing_fails_once_announces_no_history(self):
        self._a_first_run_whose_listing_fails_once(self.repos[1])

    def _pulls_and_comments_failing_in_one_repo(self, failing):
        other = self._other(failing)
        self.hub.fail_when = lambda path: path.startswith(tuple(
            "repos/%s/%s" % (failing, rest) for rest in ("pulls?", "issues/comments?", "pulls/comments?")))
        made, lines = [], []
        for i in range(15):
            if i < 6:
                made.append(self.hub.add_comment(repo=failing, body="comment %d in the failing repo" % i))
                made.append(self.hub.add_comment(repo=other, body="comment %d in the other repo" % i))
            lines.extend(self.tick(tolerate=True))
        self.assertEqual(len(self.poller.raised), 15)
        self.hub.fail_when = None
        lines.extend(self.tick())
        got = sorted(int(re.search(r" id=(\d+) ", l).group(1)) for l in lines if l.startswith("type=comment "))
        self.assertEqual(got, sorted(c["id"] for c in made), "a comment made across the failed cycles was lost or repeated")

    def test_pulls_and_comments_failing_in_the_first_repo_lose_no_comment(self):
        self._pulls_and_comments_failing_in_one_repo(self.repos[0])

    def test_pulls_and_comments_failing_in_the_second_repo_lose_no_comment(self):
        self._pulls_and_comments_failing_in_one_repo(self.repos[1])

    def _a_tracked_run_that_fails_inside_a_streak(self, failing):
        """A raise keeps what the cycle tracked: a CI run already off every page, fetched by id,
        fails 20 minutes into a 40-minute streak of failed PR listings and is announced once the
        listing answers. Forgotten at the raise, it was never announced."""
        ci = self.hub.add_run(repo=failing, name="CI", status="in_progress", conclusion=None,
                              created_at=self.clock.at(1), display_title="slow")
        self.tick()
        self.assertIn(str(ci["id"]), self.state["runs_inflight"][failing])
        self.burst(25, repo=failing, start=5)
        self.tick(300)
        self.tick(300)                                     # off every page now: fetched by id
        self.hub.fail = ("%s/pulls?" % failing,)
        lines = []
        for i in range(40):
            if i == 20:
                ci.update(status="completed", conclusion="failure")
            lines.extend(self.tick(tolerate=True))
        self.assertEqual(len(self.poller.raised), 40)
        self.hub.fail = ()
        lines += self.tick() + self.tick()
        told = [l for l in lines if " run=%d." % ci["id"] in l]
        self.assertEqual(len(told), 1, told)
        self.assertIn(": failure | slow", told[0])

    def test_a_tracked_run_in_the_first_repo_that_fails_inside_a_streak_is_announced(self):
        self._a_tracked_run_that_fails_inside_a_streak(self.repos[0])

    def test_a_tracked_run_in_the_second_repo_that_fails_inside_a_streak_is_announced(self):
        self._a_tracked_run_that_fails_inside_a_streak(self.repos[1])

    def test_main_keeps_a_raised_cycles_state_when_its_checkpoint_cannot_be_saved(self):
        """`main()` carries each profile's state in memory from one pass to the next, raised or
        not. Since the checkpoint was added a restart reloads a checkpoint that matches it, so dropping the state on
        a raised cycle differs only when no checkpoint was saved: one that could not be (the
        state's disk full, the case here), or a first run or a legacy-baseline cycle, for which
        `checkpoint()` saves nothing. Then the reload is the file from before the cycle; here its
        lines are written again. Driven
        through the real `main()`: the first repo's five runs are announced, the second repo's
        listing raises twice with every checkpoint refused, then answers. Each run is told once."""
        first, failing = self.repos
        config = os.path.join(os.path.dirname(self.poller.state_dir), "config.json")
        with open(config, "w") as fh:
            json.dump({"profiles": {"acme": {"github": {"repos": list(self.repos)}}}}, fh)
        made = self.burst(5, repo=first)
        self.hub.fail = ("%s/pulls?" % failing,)
        real_replace, broken = os.replace, [True]

        def replace(src, dst, *args, **kwargs):
            if broken[0]:
                raise PermissionError(13, "Permission denied", dst)
            return real_replace(src, dst, *args, **kwargs)

        class Stop(Exception):
            pass

        passes = []

        def sleep(secs):
            passes.append(secs)
            if len(passes) == 2:
                self.hub.fail, broken[0] = (), False
            if len(passes) == 3:
                raise Stop()
            self.clock.advance(secs)

        del self.poller.emitted[:]
        clock = types.SimpleNamespace(time=lambda: self.clock.now.timestamp(), sleep=sleep)
        with mock.patch.object(gw, "CONFIG_PATH", config), \
                mock.patch.object(gw, "GH", "/nonexistent/gh"), \
                mock.patch.object(gw, "time", clock), \
                mock.patch.object(gw.os, "replace", replace):
            with self.assertRaises(Stop):
                gw.main()
        refused = [n for n in self.poller.notes if "the failed cycle's checkpoint was not saved" in n]
        self.assertEqual(len(refused), 2, self.poller.notes)
        self.assertEqual(_ids_in(self.poller.emitted, first), sorted(r["id"] for r in made))


class AFailedListingInEitherRepo(_AFailedListingInEitherRepo, _ClockedRuns):
    repos = (REPO, OTHER)


class AFailedListingInEitherRepoMirrored(_AFailedListingInEitherRepo, _ClockedRuns):
    repos = (OTHER, REPO)


# ---------------------------------------------------------------------------
# A cycle that raises after it emitted, then a restart. The state was saved only
# at the end of a COMPLETED cycle, so what a raising cycle announced lived in the running
# process alone; a restart reloaded the file from before it and announced it all again: up
# To one runs page (20) per repo before page-back, up to 100 per repo with it,
# plus the cap and hold_expired lines and every PR, pair, review and comment line. Now a
# raising cycle checkpoints what it recorded, and records each line only once it is written.
# ---------------------------------------------------------------------------

def _kind(line):
    return line.split()[0]


def _repo_of(line):
    m = re.search(r" repo=(\S+)", line)
    return m.group(1) if m else None


def _repeated(lines):
    return sorted(line for line, n in collections.Counter(lines).items() if n > 1)


def _told(lines):
    """The `degraded`, `degraded_recovered` and `error: sub-fetch` lines among ``lines``, in
    order, each without its detail (a `degraded` line keeps its ` suppressed=N`)."""
    out = []
    for line in lines:
        if line.startswith("type=degraded"):
            count = line.rsplit(" suppressed=", 1)[1] if " suppressed=" in line else None
            out.append(line.split(" detail=")[0] + (" suppressed=" + count if count else ""))
        elif line.startswith("error: sub-fetch "):
            out.append(line.split(" (")[0])
    return out


class _FailedCycleThenRestart(_ClockedRuns):
    """Two repos on a clock. ``write_fails_once`` makes ONE line's write fail, and
    ``restart`` is what a restart does: a new process whose state is ``load_state``'s."""

    repos = (REPO, OTHER)

    def write_fails_once(self, matching, nth=1):
        """The profile log's write fails ONCE, at the ``nth`` line starting with ``matching``
        (the disk filled, say): that line is NOT written and the cycle raises. The lines
        before it were written, and the write works again afterwards."""
        poller, hits = self.poller, [0]

        def emit(profile, line):
            if hits[0] < nth and line.startswith(matching):
                hits[0] += 1
                if hits[0] == nth:
                    raise OSError(28, "No space left on device")
            poller.emitted.append(line)

        patch = mock.patch.object(gw, "emit", emit)
        patch.start()
        self.addCleanup(patch.stop)

    def note_fails_once(self, matching, error=None):
        """The daemon log's ``note()`` fails ONCE, at the first note containing ``matching``:
        the cycle raises there, after the profile-log line of the same pair was written. It
        raises ``error``, or by default the full disk's ``OSError``."""
        poller, hits = self.poller, [0]

        def note(line):
            if not hits[0] and matching in line:
                hits[0] += 1
                raise error or OSError(28, "No space left on device")
            poller.notes.append(line)

        patch = mock.patch.object(gw, "note", note)
        patch.start()
        self.addCleanup(patch.stop)
        return hits

    def restart(self):
        self.state = self.poller.load()
        return self.state

    def fail_listing(self, repo):
        self.hub.fail = ("%s/pulls?" % repo,)


def _clear_stop_request(case):
    """The daemon's stop request and idle bracket are module state: clear them now and
    after the test, so that a test that leaves one pending cannot stop the next one."""
    def clear():
        gw._STOP_AT = None
        gw._IDLE = False
    clear()
    case.addCleanup(clear)


class ASigtermMidCycle(_FailedCycleThenRestart):
    """Launchd's SIGTERM mid-cycle. The handler records the request, and the cycle raises
    `Terminated` at its next safe point (`stop_point`), a BaseException so that the cycle's many
    `except Exception` around its fetches cannot swallow it; `checkpointed` saves what the cycle
    had recorded, and `main()` notes the stop and exits. No test here sends a signal: the handler
    is called, and the hub's `gh_api` raises `Terminated` where a safe point in the real one would."""

    def setUp(self):
        super(ASigtermMidCycle, self).setUp()
        _clear_stop_request(self)

    def terminate_at(self, needle):
        """Every request whose path contains ``needle`` raises `Terminated` while the returned
        list is non-empty; ``[]`` after ``.pop()`` lets the hub answer again."""
        armed, hub = [True], self.hub

        def gh_api(path):
            if armed and needle in path:
                raise gw.Terminated()
            return hub(path)

        patch = mock.patch.object(gw, "gh_api", gh_api)
        patch.start()
        self.addCleanup(patch.stop)
        return armed

    def run_main(self, sleep):
        """The real `main()` loop on the poller's private state, with its sleeps replaced by
        ``sleep`` and `signal.signal` recorded. Returns (what main returned, the recorded calls)."""
        config = os.path.join(os.path.dirname(self.poller.state_dir), "config.json")
        with open(config, "w") as fh:
            json.dump({"profiles": {"acme": {"github": {"repos": list(self.repos)}}}}, fh)
        calls = self.signal_calls = []
        fake_signal = types.SimpleNamespace(signal=lambda *a: calls.append(a), SIGTERM=signal.SIGTERM)
        clock = types.SimpleNamespace(time=lambda: self.clock.now.timestamp(), sleep=sleep)
        with mock.patch.object(gw, "CONFIG_PATH", config), \
                mock.patch.object(gw, "GH", "/nonexistent/gh"), \
                mock.patch.object(gw, "time", clock), \
                mock.patch.object(gw, "signal", fake_signal):
            return gw.main(), calls

    def test_terminated_is_not_an_exception(self):
        self.assertTrue(issubclass(gw.Terminated, BaseException))
        self.assertFalse(issubclass(gw.Terminated, Exception),
                         "an Exception subclass is swallowed by the cycle's `except Exception` fetches")

    def test_the_handler_only_records_the_request(self):
        self.assertIsNone(gw._on_sigterm(signal.SIGTERM, None), "the handler must return, not raise")
        self.assertIsNotNone(gw._STOP_AT, "the handler did not record the request")
        with self.assertRaises(gw.Terminated):
            gw.stop_point()
        self.assertIsNone(gw._STOP_AT, "the request was not cleared when it was raised")
        gw.stop_point()                      # nothing pending: returns

    def test_a_sigterm_part_way_through_a_cycle_is_checkpointed_and_a_restart_repeats_none(self):
        made = self.burst(20)
        armed = self.terminate_at("%s/pulls?" % OTHER)
        with self.assertRaises(gw.Terminated):
            self.tick()
        lines = list(self.poller.emitted)
        self.assertGreater(len(_typed(lines, "type=run ")), 0, "the cycle announced nothing before it was stopped")
        armed.pop()
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_main_registers_the_handler_for_sigterm(self):
        class Stop(Exception):
            pass

        def sleep(secs):
            raise Stop()

        with self.assertRaises(Stop):
            self.run_main(sleep)
        self.assertEqual(self.signal_calls, [(signal.SIGTERM, gw._on_sigterm)])
        self.signal_calls[0][1](signal.SIGTERM, None)      # the registered handler: a request, no raise
        self.assertIsNotNone(gw._STOP_AT)

    def test_main_notes_a_sigterm_and_exits_cleanly(self):
        def sleep(secs):
            raise gw.Terminated()

        returned, calls = self.run_main(sleep)
        self.assertIsNone(returned, "main() must return (exit 0), not raise")
        self.assertIn("stopped by SIGTERM", "\n".join(self.poller.notes))
        self.assertEqual(calls, [(signal.SIGTERM, gw._on_sigterm)])

    def test_a_sigterm_mid_cycle_through_main_exits_cleanly_and_a_restart_repeats_none(self):
        made = self.burst(20)
        armed = self.terminate_at("%s/pulls?" % OTHER)

        def sleep(secs):
            self.fail("main() slept after a SIGTERM")

        returned, _ = self.run_main(sleep)
        self.assertIsNone(returned)
        self.assertIn("stopped by SIGTERM", "\n".join(self.poller.notes))
        self.assertEqual([n for n in self.poller.notes if "cycle failed" in n], [],
                         "a SIGTERM was counted as a failed cycle")
        lines = list(self.poller.emitted)
        self.assertGreater(len(_typed(lines, "type=run ")), 0)
        armed.pop()
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))


class _FakeClock(object):
    """A monotonic clock that moves only when a fake child waits."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Pipe(object):
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeGhChild(object):
    """What `subprocess.Popen` returns for `gh api`. It follows the real object where `gh_api`
    depends on it: leaving the `with` closes both pipes and reaps the child, and reaping a child
    that is still running is an error (the real `wait()` would block)."""

    def __init__(self, world, cmd):
        self.world, self.cmd = world, cmd
        self.stdout, self.stderr = _Pipe(), _Pipe()
        self.returncode = None
        self.killed = False
        self.reaped = False
        self.slices = []

    def communicate(self, timeout=None):
        return self.world.communicate(self, timeout)

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def wait(self):
        if self.returncode is None:
            raise AssertionError("reaped a child that is still running")
        self.reaped = True
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stdout.close()
        self.stderr.close()
        self.wait()


class _FakeGh(object):
    """`gh` without a process: replaces `subprocess.Popen` and the clock `gh_api`
    reads. ``answer(path)`` returns ``(stdout, stderr, returncode)``, or ``None`` for a child
    that never finishes: each `communicate(timeout=)` on it moves the clock by ``timeout`` and
    times out. ``on_spawn(path)`` runs when a child starts and ``on_wait(now)`` after each slice
    a child waits; a test delivers its signal from either."""

    def __init__(self, case, answer=None, on_spawn=None, on_wait=None):
        self.clock = _FakeClock()
        self.answer, self.on_spawn, self.on_wait = answer, on_spawn, on_wait
        self.children = []
        for patch in (mock.patch.object(gw.subprocess, "Popen", self.spawn),
                      mock.patch.object(gw, "_monotonic", self.clock),
                      mock.patch.object(gw, "GH", "gh")):
            patch.start()
            case.addCleanup(patch.stop)

    def spawn(self, cmd, **kwargs):
        child = _FakeGhChild(self, cmd)
        self.children.append(child)
        if self.on_spawn:
            self.on_spawn(cmd[2])
        return child

    def communicate(self, child, timeout):
        result = self.answer(child.cmd[2]) if self.answer else None
        if result is not None:
            out, err, child.returncode = result
            return out, err
        self.clock.t += timeout
        child.slices.append(timeout)
        if self.on_wait:
            self.on_wait(self.clock.t)
        raise subprocess.TimeoutExpired(child.cmd, timeout)


class AGhApiWaitIsInterruptible(unittest.TestCase):
    """`gh_api` waits for its child in slices, so a SIGTERM during a slow call is acted
    on within one slice, not when the call returns (`subprocess.run` resumed its wait after the
    handler, so a stop requested at the start of a 45 s call was acted on after 45 s)."""

    def setUp(self):
        _clear_stop_request(self)

    def test_an_answer_is_returned_and_the_child_reaped(self):
        world = _FakeGh(self, answer=lambda path: ("[1, 2]", "", 0))
        self.assertEqual(gw.gh_api("repos/a/b/pulls?x=1"), [1, 2])
        (child,) = world.children
        self.assertEqual(child.cmd, ["gh", "api", "repos/a/b/pulls?x=1"])
        self.assertTrue(child.reaped and child.stdout.closed and child.stderr.closed)
        self.assertFalse(child.killed)

    def test_a_failed_call_raises_with_its_stderr(self):
        _FakeGh(self, answer=lambda path: ("", "HTTP 502: boom", 1))
        with self.assertRaises(RuntimeError) as caught:
            gw.gh_api("repos/a/b/pulls?x=1")
        self.assertEqual(str(caught.exception), "gh api repos/a/b/pulls failed: HTTP 502: boom")

    def test_a_call_that_never_answers_times_out_after_45_seconds_as_before(self):
        world = _FakeGh(self)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            gw.gh_api("repos/a/b/pulls")
        self.assertEqual(caught.exception.timeout, 45)
        (child,) = world.children
        self.assertEqual(sum(child.slices), 45)
        self.assertLessEqual(max(child.slices), 1)
        self.assertTrue(child.killed and child.reaped and child.stdout.closed and child.stderr.closed)

    def test_a_stop_requested_at_the_start_of_a_slow_call_ends_it_within_a_second(self):
        sent = []
        world = _FakeGh(self, on_spawn=lambda path: (sent.append(world.clock.t),
                                                     gw._on_sigterm(signal.SIGTERM, None)))
        with self.assertRaises(gw.Terminated):
            gw.gh_api("repos/a/b/pulls")
        (child,) = world.children
        self.assertLessEqual(world.clock.t - sent[0], 1, "the wait went on after the stop was requested")
        self.assertTrue(child.killed and child.reaped and child.stdout.closed and child.stderr.closed)
        self.assertIsNone(gw._STOP_AT, "the request was not cleared when it was raised")

    def test_a_stop_requested_late_in_a_slow_call_ends_it_within_a_second(self):
        sent = []

        def on_wait(now):
            if now >= 1030 and not sent:
                sent.append(now)
                gw._on_sigterm(signal.SIGTERM, None)

        world = _FakeGh(self, on_wait=on_wait)
        with self.assertRaises(gw.Terminated):
            gw.gh_api("repos/a/b/pulls")
        self.assertEqual(sent, [1030.0])
        self.assertLessEqual(world.clock.t - sent[0], 1)
        self.assertTrue(world.children[0].killed and world.children[0].reaped)

    def test_a_stop_requested_before_the_call_starts_no_child(self):
        world = _FakeGh(self, answer=lambda path: ("[]", "", 0))
        gw._on_sigterm(signal.SIGTERM, None)
        with self.assertRaises(gw.Terminated):
            gw.gh_api("repos/a/b/pulls")
        self.assertEqual(world.children, [])

    def test_a_stop_requested_as_the_answer_arrives_is_raised_when_the_call_returns(self):
        def answer(path):
            gw._on_sigterm(signal.SIGTERM, None)
            return "[]", "", 0

        world = _FakeGh(self, answer=answer)
        with self.assertRaises(gw.Terminated):
            gw.gh_api("repos/a/b/pulls")
        (child,) = world.children
        self.assertTrue(child.reaped and child.stdout.closed and child.stderr.closed)

    def test_the_wait_ends_at_the_deadline_not_at_the_slice(self):
        """The last slice is cut to what is left of the 45 s: an odd total must not overshoot."""
        world = _FakeGh(self)
        with mock.patch.object(gw, "GH_TIMEOUT_SECS", 2.5):
            with self.assertRaises(subprocess.TimeoutExpired):
                gw.gh_api("repos/a/b/pulls")
        self.assertEqual(world.children[0].slices, [1, 1, 0.5])


class ASleepWakesOnTheRequest(unittest.TestCase):
    """`_sleep` is `time.sleep` that ends when SIGTERM arrives: the handler raises in
    it (nothing is half recorded between cycles), because `time.sleep` resumes after a handler
    that returns."""

    def setUp(self):
        _clear_stop_request(self)
        self.slept = []

    def sleeping(self, during=None):
        def sleep(secs):
            self.assertTrue(gw._IDLE, "the sleep ran outside the idle bracket")
            self.slept.append(secs)
            if during:
                during()

        patch = mock.patch.object(gw, "time", types.SimpleNamespace(sleep=sleep))
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_sleeps_the_whole_time_when_nothing_is_requested(self):
        self.sleeping()
        gw._sleep(60)
        self.assertEqual(self.slept, [60])
        self.assertFalse(gw._IDLE)

    def test_a_request_made_before_it_starts_stops_it_without_sleeping(self):
        self.sleeping()
        gw._on_sigterm(signal.SIGTERM, None)
        with self.assertRaises(gw.Terminated):
            gw._sleep(60)
        self.assertEqual(self.slept, [], "it slept with a stop pending")
        self.assertFalse(gw._IDLE)
        self.assertIsNone(gw._STOP_AT)

    def test_a_signal_during_the_sleep_ends_it_at_once(self):
        self.sleeping(during=lambda: gw._on_sigterm(signal.SIGTERM, None))
        with self.assertRaises(gw.Terminated):
            gw._sleep(60)
        self.assertFalse(gw._IDLE, "the idle bracket stayed open")
        self.assertIsNone(gw._STOP_AT)

    def test_the_handler_itself_ends_the_sleep_call_it_arrives_in(self):
        """The check that follows `time.sleep` in `_sleep` would raise too, but only once the whole
        sleep had run: `time.sleep` resumes after a handler that returns (PEP 475), and at
        `POLL_SECS` that is past launchd's grace before SIGKILL. So the exception must come out of
        the sleep call itself, from the handler: a sleep that returns was not woken."""
        returned = []

        def sleep(secs):
            self.slept.append(secs)
            gw._on_sigterm(signal.SIGTERM, None)
            returned.append(secs)                     # reached only if the handler returned

        patch = mock.patch.object(gw, "time", types.SimpleNamespace(sleep=sleep))
        patch.start()
        self.addCleanup(patch.stop)
        with self.assertRaises(gw.Terminated):
            gw._sleep(gw.POLL_SECS)
        self.assertEqual(self.slept, [gw.POLL_SECS])
        self.assertEqual(returned, [], "the sleep ran on after the signal: the handler did not end it")
        self.assertFalse(gw._IDLE)
        self.assertIsNone(gw._STOP_AT)

    def test_a_request_that_lands_as_the_sleep_ends_is_seen_before_it_returns(self):
        def during():
            gw._IDLE = False                          # the bracket closed, then the signal came
            gw._on_sigterm(signal.SIGTERM, None)      # so the handler only records it

        self.sleeping(during=during)
        with self.assertRaises(gw.Terminated):
            gw._sleep(60)
        self.assertIsNone(gw._STOP_AT)

    def test_the_handler_does_not_raise_outside_the_sleep(self):
        self.assertFalse(gw._IDLE)
        gw._on_sigterm(signal.SIGTERM, None)
        self.assertIsNotNone(gw._STOP_AT)

    def test_the_handler_keeps_the_time_of_the_first_request(self):
        clock = _FakeClock()
        with mock.patch.object(gw, "_monotonic", clock):
            gw._on_sigterm(signal.SIGTERM, None)
            clock.t += 5
            gw._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(gw._STOP_AT, 1000.0)


class ADeferredStopLeavesNoHalfRecordedState(_FailedCycleThenRestart):
    """The handler raised at any bytecode, which broke every
    two-statement invariant of the cycle. Now it only records the request."""

    def setUp(self):
        super(ADeferredStopLeavesNoHalfRecordedState, self).setUp()
        _clear_stop_request(self)

    def through_popen(self, hub=None, **hooks):
        """The REAL `gh_api` over the hub: its safe points are the ones the cycle runs."""
        hub = hub or self.hub
        real = _REAL_GH_API

        def answer(path):
            try:
                return json.dumps(hub(path)), "", 0
            except RuntimeError as e:
                return "", str(e), 1

        patch = mock.patch.object(gw, "gh_api", real)
        patch.start()
        self.addCleanup(patch.stop)
        return _FakeGh(self, answer=answer, **hooks)

    def test_touch_seen_is_one_statement(self):
        lines = []

        def tracer(frame, event, arg):
            if frame.f_code is not gw.touch_seen.__code__:
                return None

            def local(frame, event, arg):
                if event == "line":
                    lines.append(frame.f_lineno)
                return local
            return local

        seen = gw.new_seen(["a", "b", "c"])
        sys.settrace(tracer)
        try:
            gw.touch_seen(seen, "a")
        finally:
            sys.settrace(None)
        self.assertEqual(list(seen), ["b", "c", "a"])
        self.assertEqual(len(lines), 2, "touch_seen runs more than its test and one move: %r" % lines)

    def test_a_signal_between_the_halves_of_touch_seen_loses_no_id(self):
        """The reviewer's case: a signal delivered at every point inside `touch_seen` (before
        each line and at its return), one point per run. Moving an id to the newest position was
        `del seen[mid]; seen[mid] = None`; a Terminated between the two dropped a long-announced
        id, the checkpoint persisted it, and the restart announced an old run as news."""
        with open(self.poller.state_path) as fh:
            baseline = fh.read()
        delivered = terminated = 0
        for point in range(1, 8):
            with open(self.poller.state_path, "w") as fh:
                fh.write(baseline)
            self.restart()
            gw._STOP_AT = None
            hits = []

            def tracer(frame, event, arg):
                if frame.f_code is not gw.touch_seen.__code__:
                    return None
                if hits and hits[0] is not frame:
                    return None
                if not hits and frame.f_locals["mid"] not in frame.f_locals["seen"]:
                    return None
                hits[:] = [frame]
                count = [0]

                def local(frame, event, arg):
                    if event in ("line", "return"):
                        count[0] += 1
                        if count[0] == point:
                            delivered_here.append(point)
                            gw._on_sigterm(signal.SIGTERM, frame)
                    return local
                return local

            delivered_here = []
            lines = []
            sys.settrace(tracer)
            try:
                lines += self.tick()
            except gw.Terminated:
                terminated += 1
            finally:
                sys.settrace(None)
            delivered += len(delivered_here)
            gw._STOP_AT = None
            self.restart()
            lines += self.tick() + self.tick()
            self.assertEqual(lines, [], "a signal at point %d made the restart announce: %r" % (point, lines))
        self.assertGreaterEqual(delivered, 3, "the signal was delivered at fewer points than touch_seen has")
        self.assertGreaterEqual(terminated, 1, "no run was stopped at a safe point")

    def test_a_signal_mid_cycle_stops_at_the_next_safe_point_and_is_checkpointed(self):
        made = self.burst(20)
        fired = []
        world = self.through_popen()
        inner = world.answer

        def answer(path):
            result = inner(path)
            if "%s/pulls?" % OTHER in path and not fired:
                fired.append(path)
                gw._on_sigterm(signal.SIGTERM, None)       # the signal arrives during this call
            return result

        world.answer = answer
        before = len(self.hub.asked)
        with self.assertRaises(gw.Terminated):
            self.tick()
        self.assertEqual([p for p in self.hub.asked[before:] if OTHER in p], [fired[0]],
                         "the cycle went on after the stop was requested")
        lines = list(self.poller.emitted)
        self.assertGreater(len(_typed(lines, "type=run ")), 0)
        gw._STOP_AT = None
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_a_signal_during_a_repos_fetches_stops_before_the_next_repo(self):
        made = self.burst(20)
        hub, fired = self.hub, []

        def hook(path):
            answer = hub(path)
            if "status=" in path and OTHER not in path and not fired:
                fired.append(path)
                gw._on_sigterm(signal.SIGTERM, None)       # only the repo's own safe points remain
            return answer

        patch = mock.patch.object(gw, "gh_api", hook)       # no safe point inside gh_api
        patch.start()
        self.addCleanup(patch.stop)
        before = len(hub.asked)
        with self.assertRaises(gw.Terminated):
            self.tick()
        self.assertTrue(fired)
        self.assertEqual([p for p in hub.asked[before:] if OTHER in p], [], "the next repo was fetched")
        lines = list(self.poller.emitted)
        gw._STOP_AT = None
        patch.stop()
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_a_signal_in_the_degraded_tail_is_raised_between_subs_and_repeats_and_loses_no_line(self):
        self.hub.fail = ("/issues/comments?", "%s/actions/runs?" % OTHER)
        emitted, fired = self.poller.emitted, []

        def emit(profile, line):
            emitted.append(line)
            if line.startswith("type=degraded ") and not fired:
                fired.append(line)
                gw._on_sigterm(signal.SIGTERM, None)       # the first sub's line is written

        patch = mock.patch.object(gw, "emit", emit)
        patch.start()
        self.addCleanup(patch.stop)
        with self.assertRaises(gw.Terminated):
            self.tick()
        self.assertEqual(len(_typed(emitted, "type=degraded ")), 1, "the tail went on to the next sub")
        lines = list(emitted)
        gw._STOP_AT = None
        self.restart()
        lines += self.tick() + self.tick()
        told = _typed(lines, "type=degraded ")
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(len(told), 2, "each of the two holes is told exactly once: %r" % told)
        self.assertEqual(len(set(l.split(" detail=")[0] for l in told)), 2)

    def test_the_latency_from_the_signal_to_the_checkpoint_is_bounded(self):
        """A slow call in flight when the signal arrives: the clock is a fake and the child never
        answers, so the time is the number of seconds the wait went on."""
        self.burst(20)
        sent, saved = [], []
        real_save = gw.save_state

        def save_state(profile, state, **kwargs):
            saved.append(world.clock.t)
            return real_save(profile, state, **kwargs)

        hub = self.hub

        def answer(path):
            if "%s/pulls?" % OTHER in path:
                return None                                   # this call hangs
            return json.dumps(hub(path)), "", 0

        world = self.through_popen(on_spawn=lambda path: (sent.append(world.clock.t),
                                                         gw._on_sigterm(signal.SIGTERM, None))
                                   if "%s/pulls?" % OTHER in path else None)
        world.answer = answer
        with mock.patch.object(gw, "save_state", save_state):
            with self.assertRaises(gw.Terminated):
                self.tick()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(saved), 1, "the cycle was not checkpointed")
        latency = saved[0] - sent[0]
        self.assertLess(latency, 15)
        self.assertLessEqual(latency, gw.GH_WAIT_SLICE_SECS)

    def test_serve_wakes_from_its_poll_sleep_and_main_exits_cleanly(self):
        config = os.path.join(os.path.dirname(self.poller.state_dir), "config.json")
        with open(config, "w") as fh:
            json.dump({"profiles": {"acme": {"github": {"repos": list(self.repos)}}}}, fh)
        slept = []

        def sleep(secs):
            slept.append(secs)
            if len(slept) > 1:
                self.fail("it slept again after the stop")
            gw._on_sigterm(signal.SIGTERM, None)

        clock = types.SimpleNamespace(time=lambda: self.clock.now.timestamp(), sleep=sleep)
        with mock.patch.object(gw, "CONFIG_PATH", config), \
                mock.patch.object(gw, "GH", "/nonexistent/gh"), \
                mock.patch.object(gw, "time", clock):
            self.assertIsNone(gw.main())
        self.assertEqual(slept, [gw.POLL_SECS])
        self.assertIn("stopped by SIGTERM", "\n".join(self.poller.notes))


class AStopInsideTheMergeTransition(_FailedCycleThenRestart):
    """A PR that merged this cycle is announced (`pr_merged`, and
    `pair_trailer_unparsed` for a trailer that does not parse) and THEN its companions are asked
    (`settle_pair` -> `gh_api`, which has safe points). The PR was recorded closed, and the entry
    for the companions kept, only after that, so a stop inside it was checkpointed with the PR
    still open and the restart announced the merge, and every companion window, a second time.
    The PR and its entry are now recorded before the companions are asked."""

    BODY = ("Adds the ref input.\n\nLands-with: %s#8\nLands-with: %s#9\n" % (OTHER, OTHER))
    JUNK = "Lands-with: junk\n"

    def setUp(self):
        super(AStopInsideTheMergeTransition, self).setUp()
        _clear_stop_request(self)

    def merge(self, body, stop_at):
        """#569 is open for a tick and is then merged, with companions #8 and #9 (both open) in the
        other repo. The signal arrives during the call that asks about companion ``stop_at`` (the
        flag the real handler sets, delivered from the fake), so the real `gh_api` raises
        `Terminated` at its safe point after that call. ``None`` delivers none. Returns the lines
        the stopped cycle wrote."""
        self.hub.listing[REPO].insert(0, _pr(569, body=body))
        self.assertEqual(len(_typed(self.tick(), "type=pr_opened")), 1)
        merged = _pr(569, state="closed", merged_at="2026-09-06T05:24:00Z", title="[PROJ-900] perf nightly ref",
                     body=body)
        self.hub.listing[REPO][0] = merged
        world, fired, asked = self.through_popen(), [], []
        inner = world.answer

        def answer(path):
            companion = re.match(r"repos/%s/pulls/(\d+)$" % re.escape(OTHER), path)
            if companion:
                number = int(companion.group(1))
                asked.append(number)
                if number == stop_at and not fired:
                    fired.append(number)
                    gw._on_sigterm(signal.SIGTERM, None)       # the signal arrives during this call
                return json.dumps({"number": number, "merged_at": None}), "", 0
            if path == "repos/%s/pulls/569" % REPO:             # the retry re-reads the origin
                return json.dumps(merged), "", 0
            return inner(path)

        world.answer = answer
        self.asked_companions = asked
        if stop_at is None:
            return self.tick()
        with self.assertRaises(gw.Terminated):
            self.tick()
        self.assertEqual(fired, [stop_at], "the signal was not delivered")
        return list(self.poller.emitted)

    def through_popen(self):
        """The REAL `gh_api` over the hub, so that its safe points are the ones the cycle runs."""
        real = _REAL_GH_API

        def answer(path):
            try:
                return json.dumps(self.hub(path)), "", 0
            except RuntimeError as e:
                return "", str(e), 1

        patch = mock.patch.object(gw, "gh_api", real)
        patch.start()
        self.addCleanup(patch.stop)
        return _FakeGh(self, answer=answer)

    def windows(self, lines):
        return sorted(l.split(" waits-on=")[1].split(" ")[0] for l in _typed(lines, "type=pair_window_open"))

    def stopped_then_restarted(self, body, stop_at):
        lines = self.merge(body, stop_at)
        gw._STOP_AT = None
        loaded = copy.deepcopy(self.restart())      # what the restart found, before it runs a cycle
        return lines, loaded, lines + self.tick() + self.tick()

    def test_a_stop_at_the_first_companion_repeats_no_merge_and_asks_both_after_the_restart(self):
        first, state, lines = self.stopped_then_restarted(self.BODY, stop_at=8)
        self.assertEqual(len(_typed(first, "type=pr_merged")), 1, first)
        self.assertEqual(self.windows(first), [], "a window was told before the companion's answer was seen")
        self.assertEqual(state["prs"][REPO]["569"]["state"], "closed", "the merge was not recorded")
        self.assertEqual([(e["number"], e["done"]) for e in state["pair_pending"]], [("569", [])],
                         "the entry was not kept for the restart")
        self.assertEqual(len(_typed(lines, "type=pr_merged")), 1, lines)
        self.assertEqual(self.windows(lines), ["%s#8" % OTHER, "%s#9" % OTHER], lines)
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(self.state["pair_pending"], [], "the retry did not settle")

    def test_a_stop_at_the_second_companion_does_not_tell_the_first_again(self):
        first, state, lines = self.stopped_then_restarted(self.BODY, stop_at=9)
        self.assertEqual(len(_typed(first, "type=pr_merged")), 1, first)
        self.assertEqual(self.windows(first), ["%s#8" % OTHER], first)
        self.assertEqual(state["prs"][REPO]["569"]["state"], "closed", "the merge was not recorded")
        self.assertEqual([(e["number"], e["done"]) for e in state["pair_pending"]],
                         [("569", ["%s#8" % OTHER])], "the told companion was not kept in `done`")
        self.assertEqual(len(_typed(lines, "type=pr_merged")), 1, lines)
        self.assertEqual(self.windows(lines), ["%s#8" % OTHER, "%s#9" % OTHER], lines)
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(self.state["pair_pending"], [], "the retry did not settle")

    def told_once_after_a_stop(self, stop_at):
        first, state, lines = self.stopped_then_restarted(self.BODY + self.JUNK, stop_at=stop_at)
        told = _typed(lines, "type=pair_trailer_unparsed")
        self.assertEqual(len(told), 1, "stop at %d: %r" % (stop_at, told))
        self.assertEqual(len(_typed(lines, "type=pr_merged")), 1, lines)
        self.assertEqual(self.windows(lines), ["%s#8" % OTHER, "%s#9" % OTHER], lines)
        self.assertEqual(_repeated(lines), [])

    def test_an_unreadable_trailer_told_at_the_merge_is_not_told_again_after_a_stop_at_the_first(self):
        self.told_once_after_a_stop(8)

    def test_an_unreadable_trailer_told_at_the_merge_is_not_told_again_after_a_stop_at_the_second(self):
        self.told_once_after_a_stop(9)

    def test_a_merge_that_settles_leaves_nothing_pending_and_the_pr_closed(self):
        lines = self.merge(self.BODY, stop_at=None)
        self.assertEqual(self.asked_companions, [8, 9])
        self.assertEqual(len(_typed(lines, "type=pr_merged")), 1, lines)
        self.assertEqual(self.windows(lines), ["%s#8" % OTHER, "%s#9" % OTHER], lines)
        self.assertEqual(self.state["pair_pending"], [], "a settled entry was left pending")
        self.assertEqual(self.state["prs"][REPO]["569"]["state"], "closed")
        self.assertEqual(self.poller.saved()["pair_pending"], [])
        self.assertEqual(self.poller.saved()["prs"][REPO]["569"]["state"], "closed")
        again = self.tick() + self.tick()
        self.assertEqual(_typed(again, "type=pr_merged") + _typed(again, "type=pair_window_open"), [])
        self.assertEqual(self.asked_companions, [8, 9], "a settled entry was asked about again")


class _MergeWorld(_FailedCycleThenRestart):
    """Cases beside `AStopInsideTheMergeTransition` (one merge, a stop
    at one of its companions): several merges, an older pending entry, a failed write. The REAL
    `gh_api` runs over the hub, so its safe points are the ones the cycle runs. A single-PR read
    (`repos/R/pulls/N`) is answered here: the origin from ``origins`` (the re-read of a pending
    entry), a companion as an open PR. ``failing`` makes a read answer 502, and ``stop_at`` is a
    (repo, number) whose first read has the signal arrive during it (the flag the real handler
    sets), so that the real `gh_api` raises `Terminated` at its safe point after that call.
    ``log`` is every line written from the first merge on."""

    MERGED_AT = "2026-09-06T05:24:00Z"
    BODY_A = "First half.\n\nLands-with: %s#8\nLands-with: %s#9\n" % (OTHER, OTHER)
    BODY_B = "Second half.\n\nLands-with: %s#18\nLands-with: %s#19\n" % (OTHER, OTHER)
    JUNK = "Lands-with: junk\n"
    ALL_WINDOWS = sorted("%s#%d" % (OTHER, n) for n in (8, 9, 18, 19))

    def setUp(self):
        super(_MergeWorld, self).setUp()
        _clear_stop_request(self)
        self.origins, self.failing, self.reads = {}, set(), []
        self.stop_at, self.fired, self.log = None, [], []
        patch = mock.patch.object(gw, "gh_api", _REAL_GH_API)
        patch.start()
        self.addCleanup(patch.stop)
        _FakeGh(self, answer=self.answer)

    def answer(self, path):
        one = re.match(r"repos/([^/]+/[^/?]+)/pulls/(\d+)$", path)
        if not one:
            try:
                return json.dumps(self.hub(path)), "", 0
            except RuntimeError as e:
                return "", str(e), 1
        key = (one.group(1), int(one.group(2)))
        self.reads.append(key)
        if key == self.stop_at and not self.fired:
            self.fired.append(key)
            gw._on_sigterm(signal.SIGTERM, None)           # the signal arrives during this call
        if key in self.failing:
            return "", "gh api repos/%s/pulls/%d failed: HTTP 502" % key, 1
        if key[0] == REPO:
            return json.dumps(self.origins[key[1]]), "", 0
        return json.dumps({"number": key[1], "merged_at": None}), "", 0

    def open_prs(self, *numbered):
        """The PRs ``(number, body)``, open in the listing in that order, and one tick that sees them."""
        for number, body in numbered:
            self.hub.listing[REPO].append(_pr(number, body=body))
        self.assertEqual(len(_typed(self.tick(), "type=pr_opened")), len(numbered))
        del self.log[:]

    def merge(self, number, body):
        merged = _pr(number, state="closed", merged_at=self.MERGED_AT, title="[PROJ-900] perf nightly ref",
                     body=body)
        listing = self.hub.listing[REPO]
        listing[[pr["number"] for pr in listing].index(number)] = merged
        self.origins[number] = merged

    def cycle(self, **kwargs):
        lines = self.tick(**kwargs)
        self.log += lines
        return lines

    def stopped_cycle(self, stop_at):
        """A cycle in which the signal arrives during the read of ``stop_at``; the lines it wrote."""
        self.stop_at = stop_at
        with self.assertRaises(gw.Terminated):
            self.tick()
        self.assertEqual(self.fired, [stop_at], "the signal was not delivered")
        lines = list(self.poller.emitted)
        self.log += lines
        return lines

    def restarted(self):
        """A restart after a stop, then two cycles; what the restart found, before it ran one."""
        gw._STOP_AT = None
        found = copy.deepcopy(self.restart())
        self.log += self.tick() + self.tick()
        return found

    def windows(self, lines):
        return sorted(l.split(" waits-on=")[1].split(" ")[0] for l in _typed(lines, "type=pair_window_open"))

    def merged(self, lines):
        return sorted(re.search(r" #(\d+) ", l).group(1) for l in _typed(lines, "type=pr_merged"))

    def pending(self, state):
        return [(e["number"], e["done"]) for e in state["pair_pending"]]

    def assert_everything_told_once(self, merged):
        """Over the whole log: each merge told once, each companion's window told once, nothing
        written twice, and nothing left pending, in memory or on disk."""
        self.assertEqual(self.merged(self.log), merged, self.log)
        self.assertEqual(self.windows(self.log), self.ALL_WINDOWS, self.log)
        self.assertEqual(_repeated(self.log), [])
        self.assertEqual(self.state["pair_pending"], [], "the retry did not settle")
        self.assertEqual(self.poller.saved()["pair_pending"], [])


class TwoMergesInOneListing(_MergeWorld):
    """Two PRs merged between two polls, each with its own companions, so that one cycle runs two
    merge transitions: #569 (companions #8, #9) first, then #570 (companions #18, #19) in the
    listing's order. A stop at any companion of either repeats no `pr_merged` and no told
    `pair_window_open` after the restart, and the companions not yet told are asked from
    `pair_pending` (and #570, which the stopped cycle did not reach, is announced the first time)."""

    def stopped_at(self, companion):
        self.open_prs((569, self.BODY_A), (570, self.BODY_B))
        self.merge(569, self.BODY_A)
        self.merge(570, self.BODY_B)
        first = self.stopped_cycle((OTHER, companion))
        found = self.restarted()
        return first, found

    def test_a_stop_at_the_first_merges_first_companion(self):
        first, found = self.stopped_at(8)
        self.assertEqual(self.merged(first), ["569"])
        self.assertEqual(self.windows(first), [])
        self.assertEqual(found["prs"][REPO]["569"]["state"], "closed")
        self.assertEqual(found["prs"][REPO]["570"]["state"], "open", "the merge the cycle never reached was recorded")
        self.assertEqual(self.pending(found), [("569", [])])
        self.assert_everything_told_once(["569", "570"])

    def test_a_stop_at_the_first_merges_second_companion(self):
        first, found = self.stopped_at(9)
        self.assertEqual(self.merged(first), ["569"])
        self.assertEqual(self.windows(first), ["%s#8" % OTHER])
        self.assertEqual(found["prs"][REPO]["570"]["state"], "open")
        self.assertEqual(self.pending(found), [("569", ["%s#8" % OTHER])])
        self.assert_everything_told_once(["569", "570"])

    def test_a_stop_at_the_second_merges_first_companion(self):
        first, found = self.stopped_at(18)
        self.assertEqual(self.merged(first), ["569", "570"])
        self.assertEqual(self.windows(first), ["%s#8" % OTHER, "%s#9" % OTHER])
        self.assertEqual(found["prs"][REPO]["569"]["state"], "closed")
        self.assertEqual(found["prs"][REPO]["570"]["state"], "closed")
        self.assertEqual(self.pending(found), [("570", [])], "the settled first merge was kept, or the second lost")
        self.assert_everything_told_once(["569", "570"])

    def test_a_stop_at_the_second_merges_second_companion(self):
        first, found = self.stopped_at(19)
        self.assertEqual(self.merged(first), ["569", "570"])
        self.assertEqual(self.windows(first), sorted("%s#%d" % (OTHER, n) for n in (8, 9, 18)))
        self.assertEqual(self.pending(found), [("570", ["%s#18" % OTHER])])
        self.assert_everything_told_once(["569", "570"])

    def test_no_stop_tells_every_companion_once_and_leaves_nothing_pending(self):
        self.open_prs((569, self.BODY_A), (570, self.BODY_B))
        self.merge(569, self.BODY_A)
        self.merge(570, self.BODY_B)
        self.cycle()
        self.assertEqual(self.state["pair_pending"], [], "a settled entry was left pending after the cycle that settled it")
        self.assertEqual(self.poller.saved()["pair_pending"], [], "a settled entry was saved as pending")
        self.cycle()
        self.assert_everything_told_once(["569", "570"])


class AnOlderPendingEntryAndANewMerge(_MergeWorld):
    """#569 merged in an earlier cycle and its companion #8 could not be read, so its entry
    (`done`: #9, which was told) is retried at the start of every cycle. In the next cycle #570
    merges as well. A stop in the retry (its origin re-read, or companion #8) or in the new merge's
    check (#18, #19) repeats nothing and loses no entry: every companion is told once."""

    def stopped_at(self, key):
        self.open_prs((569, self.BODY_A), (570, self.BODY_B))
        self.merge(569, self.BODY_A)
        self.failing.add((OTHER, 8))
        earlier = self.cycle()
        self.assertEqual(self.windows(earlier), ["%s#9" % OTHER], earlier)
        self.assertEqual(self.pending(self.state), [("569", ["%s#9" % OTHER])], "no older entry to retry")
        self.failing.clear()
        self.merge(570, self.BODY_B)
        first = self.stopped_cycle(key)
        found = self.restarted()
        return first, found

    def test_a_stop_in_the_origin_re_read_of_the_older_entry(self):
        first, found = self.stopped_at((REPO, 569))
        self.assertEqual(self.merged(first), [])
        self.assertEqual(self.windows(first), [])
        self.assertEqual(found["prs"][REPO]["570"]["state"], "open")
        self.assertEqual(self.pending(found), [("569", ["%s#9" % OTHER])], "the older entry was lost")
        self.assert_everything_told_once(["569", "570"])

    def test_a_stop_at_the_older_entrys_companion(self):
        first, found = self.stopped_at((OTHER, 8))
        self.assertEqual(self.merged(first), [])
        self.assertEqual(self.windows(first), [])
        self.assertEqual(found["prs"][REPO]["570"]["state"], "open")
        self.assertEqual(self.pending(found), [("569", ["%s#9" % OTHER])], "the older entry was lost")
        self.assert_everything_told_once(["569", "570"])

    def test_a_stop_at_the_new_merges_first_companion(self):
        first, found = self.stopped_at((OTHER, 18))
        self.assertEqual(self.merged(first), ["570"])
        self.assertEqual(self.windows(first), ["%s#8" % OTHER], "the older entry's companion was not told in the retry")
        self.assertEqual(found["prs"][REPO]["570"]["state"], "closed")
        self.assertEqual(self.pending(found), [("570", [])])
        self.assert_everything_told_once(["569", "570"])

    def test_a_stop_at_the_new_merges_second_companion(self):
        first, found = self.stopped_at((OTHER, 19))
        self.assertEqual(self.merged(first), ["570"])
        self.assertEqual(self.windows(first), sorted(["%s#8" % OTHER, "%s#18" % OTHER]))
        self.assertEqual(self.pending(found), [("570", ["%s#18" % OTHER])])
        self.assert_everything_told_once(["569", "570"])


class AnOSErrorInsideSettlePair(_MergeWorld):
    """A write that FAILS inside `settle_pair` (the disk is full), not a stop: the cycle raises,
    `poll_profile` checkpoints, and the next cycle (the same process, or a restart) goes on. The
    merge is recorded and its entry kept, `done` as far as it got, so no `pr_merged`, no told
    `pair_window_open` and no `pair_trailer_unparsed` is written twice."""

    def merge_then(self, body, fail):
        """#569 merges; ``fail()`` arms the failure; the cycle raises. Returns the lines it wrote."""
        self.open_prs((569, body))
        self.merge(569, body)
        fail()
        lines = self.cycle(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1, self.poller.raised)
        self.assertIn("No space left on device", self.poller.raised[0])
        self.assertEqual(self.merged(lines), ["569"], "the merge was not written before the failure")
        self.assertEqual(self.poller.saved()["prs"][REPO]["569"]["state"], "closed", "the cycle was not checkpointed")
        return lines

    def then_continue(self, restart):
        if restart:
            gw._STOP_AT = None
            self.restart()
        self.log += self.tick() + self.tick()
        self.assertEqual(self.merged(self.log), ["569"], self.log)
        self.assertEqual(self.windows(self.log), ["%s#8" % OTHER, "%s#9" % OTHER], self.log)
        self.assertEqual(_repeated(self.log), [])
        self.assertEqual(self.state["pair_pending"], [], "the retry did not settle")
        self.assertEqual(self.poller.saved()["pair_pending"], [])

    def first_window_fails(self, restart):
        lines = self.merge_then(self.BODY_A, lambda: self.write_fails_once("type=pair_window_open", 1))
        self.assertEqual(self.windows(lines), [])
        self.assertEqual(self.pending(self.poller.saved()), [("569", [])])
        self.then_continue(restart)

    def second_window_fails(self, restart):
        lines = self.merge_then(self.BODY_A, lambda: self.write_fails_once("type=pair_window_open", 2))
        self.assertEqual(self.windows(lines), ["%s#8" % OTHER])
        self.assertEqual(self.pending(self.poller.saved()), [("569", ["%s#8" % OTHER])])
        self.then_continue(restart)

    def window_fails_beside_an_unreadable_trailer(self, restart):
        lines = self.merge_then(self.BODY_A + self.JUNK, lambda: self.write_fails_once("type=pair_window_open", 1))
        self.assertEqual(len(_typed(lines, "type=pair_trailer_unparsed")), 1)
        self.assertEqual(self.poller.saved()["pair_pending"][0]["said"], ["junk"])
        self.then_continue(restart)
        self.assertEqual(len(_typed(self.log, "type=pair_trailer_unparsed")), 1,
                         "the unreadable trailer was told again by the retry")

    def note_of_a_failed_companion_fails(self, restart):
        def arm():
            self.failing.add((OTHER, 8))
            self.note_fails_once("companion fetch failed")

        lines = self.merge_then(self.BODY_A, arm)
        self.assertEqual(self.windows(lines), [])
        self.assertEqual(self.pending(self.poller.saved()), [("569", [])])
        self.failing.clear()
        self.then_continue(restart)

    def test_the_first_window_write_fails_and_the_next_cycle_tells_both(self):
        self.first_window_fails(restart=False)

    def test_the_first_window_write_fails_and_a_restart_tells_both(self):
        self.first_window_fails(restart=True)

    def test_the_second_window_write_fails_and_the_next_cycle_tells_only_it(self):
        self.second_window_fails(restart=False)

    def test_the_second_window_write_fails_and_a_restart_tells_only_it(self):
        self.second_window_fails(restart=True)

    def test_a_window_write_fails_beside_an_unreadable_trailer_which_is_not_told_again(self):
        self.window_fails_beside_an_unreadable_trailer(restart=False)

    def test_a_window_write_fails_beside_an_unreadable_trailer_which_is_not_told_again_after_a_restart(self):
        self.window_fails_beside_an_unreadable_trailer(restart=True)

    def test_the_note_of_a_failed_companion_fails_and_the_next_cycle_tells_both(self):
        self.note_of_a_failed_companion_fails(restart=False)

    def test_the_note_of_a_failed_companion_fails_and_a_restart_tells_both(self):
        self.note_of_a_failed_companion_fails(restart=True)


class AnOrderedSeenIsAlwaysWhatTheCycleGets(unittest.TestCase):
    """`_ordered_seen` is called once, by `poll_profile`; `load_state` and a fresh state
    already hold an `OrderedDict`, and a set (the fixtures built from the earlier shape) or a
    plain `dict` (the earlier container) is upgraded in place with its order kept."""

    def test_a_plain_dict_is_upgraded_with_its_order_kept(self):
        state = {"seen": {"c:3": None, "run:1:1": None, "review:2": None}}
        seen = gw._ordered_seen(state)
        self.assertIsInstance(seen, collections.OrderedDict)
        self.assertIs(state["seen"], seen, "the upgrade was not stored back in the state")
        self.assertEqual(list(seen), ["c:3", "run:1:1", "review:2"])
        gw.touch_seen(seen, "c:3")
        self.assertEqual(list(seen), ["run:1:1", "review:2", "c:3"], "the upgraded container cannot move to the end")

    def test_an_ordered_seen_is_returned_as_it_is(self):
        original = gw.new_seen(["a", "b"])
        state = {"seen": original}
        self.assertIs(gw._ordered_seen(state), original)

    def test_a_missing_seen_becomes_an_empty_ordered_one(self):
        state = {}
        self.assertIsInstance(gw._ordered_seen(state), collections.OrderedDict)
        self.assertEqual(list(state["seen"]), [])


_REAL_GH_API = gw.gh_api


class AFailedCycleThenARestart(_FailedCycleThenRestart):

    def test_a_raise_part_way_through_a_repos_run_lines_repeats_none_and_loses_none(self):
        made = self.burst(20)
        self.write_fails_once("type=run ", nth=8)
        lines = self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(len(_typed(lines, "type=run ")), 7)
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_a_restart_inside_a_streak_of_failed_cycles_repeats_none(self):
        """The ticket's case: ten failed cycles, 15 runs created in each, so the frozen
        window passes the 100-run cap and tells it as it moves; then a restart. On the old code the
        restart repeated 100 run lines and the last cap line."""
        self.fail_listing(OTHER)
        made, lines = [], []
        for _ in range(10):
            made += self.burst(15)
            lines += self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 10)
        self.assertGreater(len(_typed(lines, "type=unread_runs")), 1)
        self.restart()
        self.hub.fail = ()
        lines += self.tick()
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_the_same_raise_without_a_restart_loses_none(self):
        """The running process too: the run whose write failed was remembered before its line
        was written, so the next cycle skipped it and it was never announced at all."""
        made = self.burst(20)
        self.write_fails_once("type=run ", nth=8)
        lines = self.tick(tolerate=True) + self.tick() + self.tick()
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_a_raise_mid_page_in_the_page_back_repeats_none_and_loses_none(self):
        made = self.burst(90)
        mark = len(self.hub.asked)
        self.write_fails_once("type=run ", nth=50)
        lines = self.tick(tolerate=True)
        self.assertEqual(len([p for p in _runs_asked(self.hub, mark) if p.startswith("repos/%s/" % REPO)]), 5,
                         "the burst was not read through the page-back")
        self.assertEqual(len(_typed(lines, "type=run ")), 49)
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_a_raise_mid_page_keeps_the_hold_over_runs_it_never_announced(self):
        """The hold is that repo's runs watermark. Released before the runs were announced, a
        cycle that raised part-way through them saved a cutoff past the rest: the next poll
        paged back only to the watermark, and 16 runs created inside the held window were lost."""
        made = self.burst(45)
        self.hub.fail = ("&page=2",)
        lines = self.tick()
        self.assertEqual(len(_typed(lines, "type=run ")), 20)
        for _ in range(10):
            lines += self.tick()                           # cycles complete; the hold is kept
        self.assertIn(REPO, self.state["runs_from"])
        self.hub.fail = ()
        self.write_fails_once("type=run ", nth=10)
        failed = self.tick(tolerate=True)
        self.assertEqual(len(_typed(failed, "type=run ")), 9)
        self.restart()
        self.assertIn(REPO, self.state["runs_from"], "a failed cycle released the hold over runs it never announced")
        lines += failed + self.tick() + self.tick()
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))

    def test_a_page_cap_line_a_failed_cycle_told_is_not_told_again_after_a_restart(self):
        self.burst(110)
        self.fail_listing(OTHER)
        failed = self.tick(tolerate=True)
        self.assertEqual(len(_typed(failed, "type=unread_runs")), 1)
        self.assertEqual(len(_typed(failed, "type=run ")), 100)
        self.restart()
        self.hub.fail = ()
        after = self.tick()                                # the same frozen window, read again
        self.assertEqual(_typed(after, "type=unread_runs"), [])
        self.assertEqual(_typed(after, "type=run "), [])

    def test_a_page_cap_line_whose_write_failed_is_told_after_the_restart(self):
        self.burst(110)
        self.write_fails_once("type=unread_runs")
        self.assertEqual(self.tick(tolerate=True), [])
        self.restart()
        after = self.tick()
        self.assertEqual(len([l for l in _typed(after, "type=unread_runs") if " reason=page_cap " in l]), 1, after)
        self.assertEqual(len(_typed(after, "type=run ")), 100)

    def test_a_hold_expired_line_a_failed_cycle_told_is_not_told_again_after_a_restart(self):
        self.burst(45)
        self.hub.fail = ("&page=2",)
        self.tick()
        held = gw._parse_ts(self.state["runs_from"][REPO])
        while self.clock.now - held < timedelta(minutes=29):
            self.tick()
        self.hub.fail = ("&page=2", "%s/pulls?" % OTHER)
        told = []
        for _ in range(5):
            told = _typed(self.tick(tolerate=True), "type=unread_runs")
            if told:
                break
        self.assertEqual(len(told), 1, told)
        self.assertIn(" reason=hold_expired ", told[0])
        self.restart()
        self.hub.fail = ("&page=2",)                       # the PR listing answers; page 2 still fails
        self.assertEqual(_typed(self.tick(), "type=unread_runs"), [])

    def test_a_scheduled_recovery_whose_write_failed_is_announced_after_the_restart(self):
        """`wf_last` is a record too: moved before the line was written, the recovery's own
        success became the baseline it was judged against, and it was suppressed as noise."""
        self.hub.add_run(name="Perf nightly", event="schedule", conclusion="failure", created_at=self.clock.at(1))
        self.assertEqual(len(_typed(self.tick(), "type=run ")), 1)
        fixed = self.hub.add_run(name="Perf nightly", event="schedule", conclusion="success",
                                 created_at=self.clock.at(1))
        self.write_fails_once("type=run ")
        self.assertEqual(self.tick(tolerate=True), [])
        self.restart()
        self.assertEqual(_run_ids(self.tick()), [fixed["id"]])

    def _write_fails_at(self, matching):
        hub = self.hub
        hub.listing[REPO] = [_pr(1), _pr(2)]
        hub.reviews[(REPO, "1")] = [hub.new_review(), hub.new_review()]
        hub.reviews[(REPO, "2")] = [hub.new_review()]
        hub.add_comment()
        hub.add_comment()
        self.burst(3)
        self.write_fails_once(matching, nth=2)
        lines = self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_repeated(lines), [])
        self.assertEqual(collections.Counter(_kind(l) for l in lines if l.startswith("type=")),
                         {"type=pr_opened": 2, "type=review": 3, "type=comment": 2, "type=run": 3})

    def test_a_pr_opened_line_whose_write_failed(self):
        self._write_fails_at("type=pr_opened")

    def test_a_review_line_whose_write_failed(self):
        self._write_fails_at("type=review")

    def test_a_comment_line_whose_write_failed(self):
        self._write_fails_at("type=comment")

    def test_a_pair_window_whose_write_failed_on_the_retry_path_is_told_after_the_restart(self):
        body = "Body.\n\nLands-with: #9\n"
        self.hub.listing[REPO] = [_pr(5, body=body)]
        self.tick()
        self.hub.listing[REPO] = [_pr(5, state="closed", merged_at=self.clock.at(0), body=body)]
        answers = [RuntimeError("gh api repos/x/pulls/9 failed: HTTP 502")]

        def companion(repo, number):
            if answers:
                raise answers.pop()
            return False

        hub = self.hub

        def gh(path):                                      # the retry path re-reads the origin PR
            one = re.match(r"repos/([^/]+/[^/]+)/pulls/(\d+)$", path)
            if one:
                return [pr for pr in hub.listing[one.group(1)] if str(pr["number"]) == one.group(2)][0]
            return hub(path)

        with mock.patch.object(gw, "companion_merged", companion), mock.patch.object(gw, "gh_api", gh):
            self.assertEqual(len(_typed(self.tick(), "type=pr_merged")), 1)
            self.assertEqual(len(self.state["pair_pending"]), 1, "the unreachable companion was not kept")
            self.write_fails_once("type=pair_window_open")
            self.assertEqual(_typed(self.tick(tolerate=True), "type=pair_window_open"), [])
            self.restart()
            after = self.tick() + self.tick()
        self.assertEqual(len(_typed(after, "type=pair_window_open")), 1, after)
        self.assertEqual(_typed(after, "type=pr_merged"), [])

    def test_a_degraded_line_whose_write_failed_is_told_after_the_restart(self):
        """The cycle's tail: `reconcile` marks an episode told before its line is written, and
        the watermark used to move before it too. A checkpoint of that saved the hole as told
        and the window as read, so neither the restart nor the next cycle said anything."""
        before = self.poller.saved()["since"]
        self.hub.fail = ("%s/issues/comments?" % REPO,)
        self.write_fails_once("type=degraded")
        self.assertEqual(self.tick(tolerate=True), [])
        self.assertEqual(len(self.poller.raised), 1)
        self.restart()
        self.assertEqual(self.state["since"], before, "the watermark moved past a hole nobody was told of")
        told = _typed(self.tick(), "type=degraded")
        self.assertEqual(len(told), 1, told)
        self.assertIn(" sub=comments", told[0])

    def test_a_checkpoint_keeps_the_watermark_and_the_mtime_of_the_last_completed_cycle(self):
        """The watchdog reads the state file's mtime as the watermark's age: a checkpoint must
        not make a failing daemon look current. The access time is set to NOW and the mtime an
        hour back: with both an hour back, a checkpoint that restored the
        access time as the mtime passed too, and a read moves the access time on its own."""
        before = self.poller.saved()
        now_ns = time.time_ns()
        hour_ago = now_ns - 3600 * 10 ** 9
        os.utime(self.poller.state_path, ns=(now_ns, hour_ago))
        made = self.burst(5)
        self.fail_listing(OTHER)
        self.assertEqual(len(_typed(self.tick(tolerate=True), "type=run ")), 5)
        saved = self.poller.saved()
        self.assertEqual(os.stat(self.poller.state_path).st_mtime_ns, hour_ago,
                         "a checkpoint moved the watchdog's watermark age")
        self.assertEqual(saved["since"], before["since"], "a checkpoint moved the watermark")
        self.assertLessEqual(set("run:%d:1" % r["id"] for r in made), set(saved["seen"]))

    def test_a_failed_cycle_that_changed_nothing_leaves_the_file_alone(self):
        before = self.poller.saved()
        was = os.stat(self.poller.state_path)
        self.fail_listing(REPO)                            # the first repo: nothing is read
        self.tick(tolerate=True)
        now = os.stat(self.poller.state_path)
        self.assertEqual(self.poller.saved(), before)
        self.assertEqual((now.st_ino, now.st_mtime_ns), (was.st_ino, was.st_mtime_ns), "the file was rewritten")

    def test_a_first_run_over_an_unreadable_state_file_that_raises_leaves_it_alone(self):
        """A file that does not load is a first run too (`load_state`). Checkpointed half-done,
        it would load next time as a normal cycle and announce the unread repo's history."""
        hub = _Hub(repos=(REPO, OTHER), clock=self.clock)
        poller = _Poller(self, hub)
        os.makedirs(poller.state_dir)
        with open(poller.state_path, "w") as fh:
            fh.write("{not json")
        for repo in (REPO, OTHER):
            hub.listing[repo] = [_pr(1), _pr(2)]
            hub.reviews[(repo, "1")] = [hub.new_review()]
        state = poller.load()
        self.assertTrue(state["first_run"])
        hub.fail = ("%s/pulls?" % OTHER,)
        poller.poll(state, tolerate=True)
        with open(poller.state_path) as fh:
            self.assertEqual(fh.read(), "{not json", "a first run that raised checkpointed its half baseline")
        hub.fail = ()
        restarted = poller.load()
        lines = poller.poll(restarted)
        self.assertEqual([l for l in lines if l.startswith(("type=pr_", "type=review"))], [])

    def test_a_run_fetched_by_id_whose_write_failed_is_announced_after_the_restart(self):
        """The by-id fetch: a run tracked in flight and now off every page read. Untracked
        before its line was written, a failed write left it neither on a page nor fetched again."""
        ci = self.hub.add_run(name="CI", status="in_progress", conclusion=None,
                              created_at=self.clock.at(-4 * 3600), _order_as=self.clock.at(0))
        self.tick()
        self.assertIn(str(ci["id"]), self.state["runs_inflight"][REPO])
        del ci["_order_as"]                                # now listed four hours back: off every page read
        self.tick()
        self.assertIn(str(ci["id"]), self.state["runs_inflight"][REPO])
        ci.update(status="completed", conclusion="failure")
        mark = len(self.hub.asked)
        self.write_fails_once("type=run ")
        self.assertEqual(self.tick(tolerate=True), [])
        self.assertIn("repos/%s/actions/runs/%d" % (REPO, ci["id"]), self.hub.asked[mark:], "not fetched by id")
        self.restart()
        self.assertEqual(_run_ids(self.tick()), [ci["id"]])

    def test_a_first_run_that_raises_after_its_first_repo_writes_no_state(self):
        """A first run's baseline is silent; saved half-done, a restart would load it as a
        normal cycle and announce the other repo's whole history.

        With no state file the checkpoint would write nothing anyway (there is no mtime to keep),
        so the file's absence alone passed with the first-run skip
        removed: the skip is pinned directly by asserting that no
        checkpoint was even attempted."""
        hub = _Hub(repos=(REPO, OTHER), clock=self.clock)
        poller = _Poller(self, hub)
        for repo in (REPO, OTHER):
            hub.listing[repo] = [_pr(1), _pr(2)]
            hub.reviews[(repo, "1")] = [hub.new_review()]
            hub.add_comment(repo=repo)
            hub.add_run(repo=repo, created_at=self.clock.at(-30))
        hub.fail = ("%s/pulls?" % OTHER,)
        saves, real_save = [], gw.save_state

        def save_state(profile, state, checkpointing=False):
            saves.append(checkpointing)
            return real_save(profile, state, checkpointing=checkpointing)

        with mock.patch.object(gw, "save_state", save_state):
            self.assertEqual(poller.poll(poller.load(), tolerate=True), [])
        self.assertEqual(len(poller.raised), 1)
        self.assertEqual(saves, [], "a first run that raised attempted a checkpoint")
        self.assertEqual([n for n in poller.notes if "checkpoint" in n], [])
        self.assertFalse(os.path.exists(poller.state_path), "a first run that raised saved its state")
        hub.fail = ()
        restarted = poller.load()
        self.assertTrue(restarted["first_run"])
        lines = poller.poll(restarted)
        self.clock.advance(60)
        lines += poller.poll(restarted)
        self.assertEqual([l for l in lines if l.startswith(("type=pr_", "type=review", "type=comment", "type=run"))],
                         [], "a first run that raised announced history after the restart")

    # -- the checkpoint's own safety properties ----------------------

    def _file(self):
        st = os.stat(self.poller.state_path)
        with open(self.poller.state_path, "rb") as fh:
            return fh.read(), st.st_ino, st.st_mtime_ns

    def _raising_listing(self, raise_it):
        """``gh_api`` with OTHER's PR listing raising what ``raise_it()`` returns; the objects
        raised are kept, so a test can check that the SAME one reaches the caller."""
        raised, hub = [], self.hub

        def gh(path):
            if path.startswith("repos/%s/pulls?" % OTHER):
                raised.append(raise_it(path))
                raise raised[-1]
            return hub(path)

        patch = mock.patch.object(gw, "gh_api", gh)
        patch.start()
        self.addCleanup(patch.stop)
        return raised

    def _a_checkpoint_that_cannot_be_saved(self, breaking):
        """A failed listing raises the cycle while ``breaking`` (a context manager) is in force.
        The cycle's own error must reach `main()` as the same object, so its `error: ... last:`
        line names the failed listing and not the checkpoint; the checkpoint's failure is noted,
        never swallowed; and the state file is left exactly as it was, never written in place.
        Returns the note."""
        before = self._file()
        self.burst(5)                                      # something to checkpoint: 5 run lines
        raised = self._raising_listing(lambda path: RuntimeError("gh api %s failed: HTTP 502" % path))
        with breaking:
            with self.assertRaises(RuntimeError) as caught:
                gw.poll_profile(self.poller.profile, self.poller.cfg, self.state)
        self.assertEqual(len(raised), 1)
        self.assertIs(caught.exception, raised[0], "the checkpoint replaced the cycle's own error")
        self.assertEqual(len(_typed(self.poller.emitted, "type=run ")), 5)
        noted = [n for n in self.poller.notes if "the failed cycle's checkpoint was not saved" in n]
        self.assertEqual(len(noted), 1, self.poller.notes)
        self.assertEqual(self._file(), before, "a checkpoint that could not be saved changed the state file")
        return noted[0]

    def test_a_checkpoint_that_cannot_be_written_is_noted_and_the_cycles_own_error_stands(self):
        """The state directory refuses the write (`os.replace` raising EACCES)."""
        noted = self._a_checkpoint_that_cannot_be_saved(
            mock.patch.object(gw.os, "replace", side_effect=PermissionError(13, "Permission denied")))
        self.assertIn("Permission denied", noted)

    def test_a_checkpoint_that_cannot_read_the_state_file_is_noted_and_the_cycles_own_error_stands(self):
        """Review finding: not only an OSError. A state file that is not UTF-8 makes the
        checkpoint's comparison read raise UnicodeDecodeError, a ValueError. (The review's way
        there: `load_state` reads such a file as a first run, which ends once every repo is read,
        and then its degraded tail raises.) Caught as `OSError` only, it escaped the checkpoint
        and replaced the cycle's own error in `main()`'s `error:` line."""
        with open(self.poller.state_path, "wb") as fh:
            fh.write(b"\xff\xfe not UTF-8")
        with self.assertRaises(UnicodeDecodeError, msg="this case needs a UTF-8 or ASCII locale"):
            with open(self.poller.state_path) as fh:
                fh.read()
        noted = self._a_checkpoint_that_cannot_be_saved(contextlib.nullcontext())
        self.assertIn("can't decode", noted)

    def test_a_checkpoint_whose_state_file_cannot_be_stated_is_noted_and_the_cycles_own_error_stands(self):
        """Review finding: the checkpoint skips silently only when there is NO state file
        (N-b). A file that exists but cannot be stat'ed (EACCES here; ENOTDIR and EIO alike) is a
        checkpoint that could not be saved, and is noted. Caught as `OSError` (the mutant), it
        was skipped silently. Only the state file's own stat raises: patched by path."""
        path, real_stat = self.poller.state_path, os.stat

        def stat(p, *args, **kwargs):
            if isinstance(p, (str, bytes, os.PathLike)) and os.fsdecode(os.fspath(p)) == path:
                raise PermissionError(13, "Permission denied", path)
            return real_stat(p, *args, **kwargs)

        noted = self._a_checkpoint_that_cannot_be_saved(mock.patch.object(gw.os, "stat", stat))
        self.assertIn("Permission denied", noted)

    def test_a_keyboard_interrupt_is_not_checkpointed(self):
        """`checkpointed` catches `Exception` only: an operator's interrupt (or `SystemExit`)
        leaves the file alone even when the cycle had recorded something."""
        before = self._file()
        self.burst(5)
        self._raising_listing(lambda path: KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            gw.poll_profile(self.poller.profile, self.poller.cfg, self.state)
        self.assertEqual(len(_typed(self.poller.emitted, "type=run ")), 5, "the cycle recorded nothing to save")
        self.assertEqual(self._file(), before, "a KeyboardInterrupt was checkpointed")

    # -- the records the first round left unpinned -------------

    def _told_once_after_a_failed_write(self, matching, restart=True):
        self.write_fails_once(matching)
        failed = self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(_typed(failed, matching), [])
        if restart:
            self.restart()
        lines = failed + self.tick() + self.tick()
        self.assertEqual(len(_typed(lines, matching)), 1, lines)
        return lines

    def test_a_trailer_line_whose_write_failed_on_a_new_pr_is_told_after_the_restart(self):
        """A new PR is recorded after its WHOLE group, `pr_opened` and its trailer lines:
        recorded between them, a failed trailer write lost the trailer line for good."""
        self.hub.listing[REPO] = [_pr(4, body="Body.\n\nLands-with: not a reference\n")]
        lines = self._told_once_after_a_failed_write("type=pair_trailer_unparsed")
        # The group's earlier line repeats rather than the later one being lost (README residual).
        self.assertGreaterEqual(len(_typed(lines, "type=pr_opened")), 1)

    def test_a_trailer_line_whose_write_failed_on_a_new_pr_is_told_without_a_restart(self):
        self.hub.listing[REPO] = [_pr(4, body="Body.\n\nLands-with: not a reference\n")]
        self._told_once_after_a_failed_write("type=pair_trailer_unparsed", restart=False)

    def test_a_pr_push_line_whose_write_failed_is_told_after_the_restart(self):
        self.hub.listing[REPO] = [_pr(1, head="aaaaaaaaa")]
        self.tick()
        self.hub.listing[REPO] = [_pr(1, head="bbbbbbbbb")]
        self._told_once_after_a_failed_write("type=pr_push")

    def test_a_pr_merged_line_whose_write_failed_is_told_after_the_restart(self):
        self.hub.listing[REPO] = [_pr(1)]
        self.tick()
        self.hub.listing[REPO] = [_pr(1, state="closed", merged_at=self.clock.at(0))]
        self._told_once_after_a_failed_write("type=pr_merged")

    def test_a_hold_expired_line_whose_write_failed_is_told_after_the_restart(self):
        """The hold is released only after its `hold_expired` line: released first, a failed
        write left no hold to expire again, and the line was never written."""
        self.burst(45)
        self.hub.fail = ("&page=2",)
        self.tick()
        held = gw._parse_ts(self.state["runs_from"][REPO])
        while self.clock.now - held < timedelta(minutes=30):
            self.assertEqual(_typed(self.tick(), "type=unread_runs"), [])
        lines = self._told_once_after_a_failed_write("type=unread_runs")
        self.assertIn(" reason=hold_expired ", _typed(lines, "type=unread_runs")[0])

    # -- a told hole always gets its recovery --------------------------

    def _two_holes_and_the_second_line_fails(self):
        self.hub.listing[REPO] = [_pr(1)]
        self.tick()
        self.hub.fail = ("/issues/comments?", "/reviews?")  # sorted subs: comments, then reviews
        self.write_fails_once("type=degraded", nth=2)
        failed = self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual([l.split(" detail=")[0] for l in _typed(failed, "type=degraded")],
                         ["type=degraded sub=comments"])
        return failed

    def test_two_holes_that_heal_at_once_recover_the_one_that_was_told(self):
        failed = self._two_holes_and_the_second_line_fails()
        self.restart()
        self.hub.fail = ()
        lines = failed + self.tick() + self.tick()
        told = [l.split(" detail=")[0] for l in lines if l.startswith("type=degraded")]
        self.assertEqual(told, ["type=degraded sub=comments", "type=degraded_recovered sub=comments"])

    def test_two_holes_that_outlast_the_failed_write_repeat_nothing_and_recover_both(self):
        failed = self._two_holes_and_the_second_line_fails()
        self.restart()
        lines = failed + self.tick()                       # the holes are still holes
        self.hub.fail = ()
        lines += self.tick() + self.tick()
        told = sorted(l.split(" detail=")[0] for l in lines if l.startswith("type=degraded"))
        self.assertEqual(told, ["type=degraded sub=comments", "type=degraded sub=reviews",
                                "type=degraded_recovered sub=comments", "type=degraded_recovered sub=reviews"])

    # -- within one sub, a written line's episode is recorded before
    # -- anything after it can fail -------------------------------------------------------------

    def _a_note_fails_after_a_told_hole(self, restart, persist=False):
        """F1 (a): the note() right after the sub's first `degraded` line raises. The line was
        written, so the episode it told must be recorded: folded only after the note, the hole's
        recovery took no episode and was never told. Returns every sub line told."""
        self.hub.fail = ("/issues/comments?",)
        hits = self.note_fails_once("] note: degraded: comments not fetched")
        lines = self.tick(tolerate=True)
        self.assertEqual((len(self.poller.raised), hits[0]), (1, 1))
        self.assertEqual(_told(lines), ["type=degraded sub=comments"])
        if restart:
            self.restart()
        if persist:
            lines += self.tick()                           # the hole outlasts the failed note
        self.hub.fail = ()
        return _told(lines + self.tick() + self.tick())

    def test_a_note_that_fails_after_a_told_hole_leaves_its_recovery_to_be_told(self):
        self.assertEqual(self._a_note_fails_after_a_told_hole(restart=False),
                         ["type=degraded sub=comments", "type=degraded_recovered sub=comments"])

    def test_a_note_that_fails_after_a_told_hole_leaves_its_recovery_to_be_told_after_the_restart(self):
        self.assertEqual(self._a_note_fails_after_a_told_hole(restart=True),
                         ["type=degraded sub=comments", "type=degraded_recovered sub=comments"])

    def test_a_note_that_fails_after_a_told_hole_does_not_tell_it_again_while_it_persists(self):
        self.assertEqual(self._a_note_fails_after_a_told_hole(restart=True, persist=True),
                         ["type=degraded sub=comments", "type=degraded_recovered sub=comments"])

    def _promoted_on_the_renotify_boundary_and_the_error_write_fails(self, restart, persist):
        """F1 (b): told at T, recovered at T+1m, silenced at T+58m and T+59m. At T+60m the third
        consecutive failure falls on the renotify boundary, so the cycle writes `degraded ...
        suppressed=2` and then its `error: sub-fetch` promotion, and the `error:` write fails.
        Folded in full before the note (the plain move, mutant `foldbeforenote`), the count was
        already past the `==` that promotes, and a hole that persisted was never promoted; folded
        after the pairs, the told episode was dropped. Returns the sub lines told after that."""
        comments = ("/issues/comments?",)
        self.hub.fail = comments
        lines = self.tick()
        self.hub.fail = ()
        lines += self.tick()
        self.hub.fail = comments
        lines += self.tick(seconds=57 * 60) + self.tick()
        self.write_fails_once("error: sub-fetch")
        lines += self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(_told(lines), ["type=degraded sub=comments", "type=degraded_recovered sub=comments",
                                        "type=degraded sub=comments suppressed=2"])
        if restart:
            self.restart()
        if persist:
            lines += self.tick()                           # the hole outlasts the failed write
        self.hub.fail = ()
        return _told(lines + self.tick() + self.tick())[3:]

    def test_a_promotion_whose_write_failed_on_a_hole_that_heals_is_followed_by_one_recovery(self):
        self.assertEqual(self._promoted_on_the_renotify_boundary_and_the_error_write_fails(False, False),
                         ["type=degraded_recovered sub=comments"])

    def test_a_promotion_whose_write_failed_on_a_hole_that_heals_is_followed_by_one_recovery_after_the_restart(self):
        self.assertEqual(self._promoted_on_the_renotify_boundary_and_the_error_write_fails(True, False),
                         ["type=degraded_recovered sub=comments"])

    def test_a_promotion_whose_write_failed_on_a_hole_that_persists_is_written_once(self):
        self.assertEqual(self._promoted_on_the_renotify_boundary_and_the_error_write_fails(False, True),
                         ["error: sub-fetch comments failing repeatedly", "type=degraded_recovered sub=comments"])

    def test_a_promotion_whose_write_failed_on_a_hole_that_persists_is_written_once_after_the_restart(self):
        self.assertEqual(self._promoted_on_the_renotify_boundary_and_the_error_write_fails(True, True),
                         ["error: sub-fetch comments failing repeatedly", "type=degraded_recovered sub=comments"])

    def test_a_note_that_fails_after_a_recovery_does_not_tell_it_again(self):
        """Review finding: folded only after the note, a recovery whose note failed was
        told again by the next cycle."""
        self.hub.fail = ("/issues/comments?",)
        lines = self.tick()
        self.hub.fail = ()
        hits = self.note_fails_once("] note: recovered: comments fetching again")
        lines += self.tick(tolerate=True)
        self.assertEqual((len(self.poller.raised), hits[0]), (1, 1))
        lines += self.tick() + self.tick()
        self.assertEqual(_told(lines), ["type=degraded sub=comments", "type=degraded_recovered sub=comments"])

    def test_a_note_that_fails_after_a_recovery_past_the_window_does_not_tell_it_again(self):
        """Review finding: the same, for a hole that heals 61 minutes after its `degraded`
        line. `reconcile` drops an episode closed past the renotify window, so the fold must drop
        it too before the note: kept (the mutant), the failed note left it open and told, and
        the next cycle told the recovery again."""
        self.hub.fail = ("/issues/comments?",)
        lines = self.tick()
        self.hub.fail = ()
        hits = self.note_fails_once("] note: recovered: comments fetching again")
        lines += self.tick(seconds=61 * 60, tolerate=True)
        self.assertEqual((len(self.poller.raised), hits[0]), (1, 1))
        self.assertNotIn("comments", self.state["degraded"])
        lines += self.tick() + self.tick()
        self.assertEqual(_told(lines), ["type=degraded sub=comments", "type=degraded_recovered sub=comments"])

    # -- the hold, in both directions ---------------------

    def _told_recovered_then_silenced(self):
        """A hole told at T and recovered at T+1m, so its next failures fall inside the renotify
        window and are silenced: T+2m and T+3m. The caller arranges the fault for T+4m, the
        third consecutive failure, whose pairs are the silenced note and then the promotion."""
        comments = ("/issues/comments?",)
        self.hub.fail = comments
        lines = self.tick()
        self.hub.fail = ()
        lines += self.tick()
        self.hub.fail = comments
        lines += self.tick() + self.tick()
        self.assertEqual(_told(lines), ["type=degraded sub=comments", "type=degraded_recovered sub=comments"])
        return lines

    def _persists_then_heals(self, lines):
        self.assertEqual(len(self.poller.raised), 1)
        lines += self.tick()                               # the hole persists
        self.hub.fail = ()
        return _told(lines + self.tick() + self.tick())

    def test_a_silenced_promotion_whose_error_write_failed_is_written_once_if_the_hole_persists(self):
        """S2, the F1 (b) silenced twin. The silenced pair has no profile-log line, so the hold
        must apply to its fold too: held only after a written line (the mutant), the count was
        folded past the `==` that promotes, and a hole that persisted was never promoted. The
        recovery is silent: the episode's failures since the recovery were silenced."""
        lines = self._told_recovered_then_silenced()
        self.write_fails_once("error: sub-fetch")
        lines += self.tick(tolerate=True)
        self.assertEqual(self._persists_then_heals(lines),
                         ["type=degraded sub=comments", "type=degraded_recovered sub=comments",
                          "error: sub-fetch comments failing repeatedly"])

    def test_a_silenced_promotion_whose_silenced_note_failed_is_written_once_if_the_hole_persists(self):
        """S2, the same with the silenced note failing, before the promotion's line."""
        lines = self._told_recovered_then_silenced()
        hits = self.note_fails_once("] note: degraded (silenced inside")
        lines += self.tick(tolerate=True)
        self.assertEqual(hits[0], 1)
        self.assertEqual(self._persists_then_heals(lines),
                         ["type=degraded sub=comments", "type=degraded_recovered sub=comments",
                          "error: sub-fetch comments failing repeatedly"])

    def test_a_note_that_fails_after_a_written_promotion_does_not_write_it_again(self):
        """S4: the hold is only for an `error:` line STILL TO BE written. Held whenever the sub's
        pairs carry one (the mutant), a note that failed after the written `error:` line left the
        count one short, and the next cycle promoted the hole a second time."""
        self.hub.fail = ("/issues/comments?",)
        lines = self.tick() + self.tick()
        hits = self.note_fails_once("] note: error: sub-fetch comments")
        lines += self.tick(tolerate=True)
        self.assertEqual((len(self.poller.raised), hits[0]), (1, 1))
        self.assertEqual(_told(lines), ["type=degraded sub=comments", "error: sub-fetch comments failing repeatedly"])
        self.assertEqual(self._persists_then_heals(lines),
                         ["type=degraded sub=comments", "error: sub-fetch comments failing repeatedly",
                          "type=degraded_recovered sub=comments"])

    def test_a_silenced_note_that_fails_still_counts_toward_the_promotion(self):
        """S5: a silenced pair is folded before its note too. Not folded (the mutant), a failed
        silenced note left the episode as the recovery closed it, the failure went uncounted,
        and a hole that then failed through its third consecutive cycle and healed never got its
        `error:` line. The fold also counts the silenced failure: left to the G1 count, which
        moves only `consecutive`, the next `degraded` line carried `suppressed=2` for three."""
        comments = ("/issues/comments?",)
        self.hub.fail = comments
        lines = self.tick()                                # c0: told
        self.hub.fail = ()
        lines += self.tick()                               # c1: recovered
        self.hub.fail = comments
        hits = self.note_fails_once("] note: degraded (silenced inside")
        lines += self.tick(tolerate=True)                  # c2: silenced, and its note fails
        self.assertEqual((len(self.poller.raised), hits[0]), (1, 1))
        lines += self.tick() + self.tick()                 # c3, c4: the third consecutive failure
        self.hub.fail = ()
        lines += self.tick() + self.tick()
        self.hub.fail = comments
        lines += self.tick(seconds=55 * 60)                # 61 minutes after c0: told again
        self.assertEqual(_told(lines), ["type=degraded sub=comments", "type=degraded_recovered sub=comments",
                                        "error: sub-fetch comments failing repeatedly",
                                        "type=degraded sub=comments suppressed=3"])

    def test_a_note_that_fails_after_a_told_hole_still_promotes_it_at_its_third_failure(self):
        """S6, F1 (a) run on to a third failure. With no `error:` line to come, the fold is in
        full. Held on every failed sub's fold (the mutant), the count stayed one short, and a hole
        that failed three cycles in a row and then healed was never promoted. The F1 (a) tests
        above never reach a third failure."""
        self.hub.fail = ("/issues/comments?",)
        hits = self.note_fails_once("] note: degraded: comments not fetched")
        lines = self.tick(tolerate=True)                   # c0: told, and its note fails
        self.assertEqual((len(self.poller.raised), hits[0]), (1, 1))
        lines += self.tick() + self.tick()                 # c1, c2: c2 is the third failure
        self.hub.fail = ()
        lines += self.tick() + self.tick()
        self.assertEqual(_told(lines), ["type=degraded sub=comments", "error: sub-fetch comments failing repeatedly",
                                        "type=degraded_recovered sub=comments"])

    # -- a raise in the tail still counts every failed sub's failure -----

    def _a_third_failure_after_a_raise(self, fault, restart, c1_seconds=60):
        """G1. c0: reviews fails and is told. c1: comments and reviews fail, and ``fault()`` makes
        one write or note raise. c2: reviews fails, its third consecutive failing cycle. c3, c4:
        everything heals. Folded only after its own lines, a failed sub that the raise left
        unfolded kept its stored count: the c1 failure went uncounted, so reviews was not
        promoted at c2, and since it heals at c3, never. Returns the sub lines told per cycle."""
        self.hub.listing[REPO] = [_pr(1)]                  # a PR, so its reviews are read
        self.tick()
        comments, reviews = "/issues/comments?", "/reviews?"
        self.hub.fail = (reviews,)
        told = [_told(self.tick())]
        self.hub.fail = (comments, reviews)
        fault()
        told.append(_told(self.tick(seconds=c1_seconds, tolerate=True)))
        self.assertEqual(len(self.poller.raised), 1)
        if restart:
            self.restart()
        self.hub.fail = (reviews,)
        told.append(_told(self.tick()))
        self.hub.fail = ()
        told += [_told(self.tick()), _told(self.tick())]
        return told

    PROMOTED = "error: sub-fetch reviews failing repeatedly"

    def _the_comments_note_fails(self, restart, error=None):
        """The note after the written `degraded sub=comments` line fails: reviews, sorted after
        comments, is never reached. The lines are those of the same cycles with no fault."""
        told = self._a_third_failure_after_a_raise(
            lambda: self.note_fails_once("] note: degraded: comments not fetched", error), restart)
        self.assertEqual(told, [["type=degraded sub=reviews"], ["type=degraded sub=comments"],
                                [self.PROMOTED, "type=degraded_recovered sub=comments"],
                                ["type=degraded_recovered sub=reviews"], []])

    def test_a_failed_note_before_a_failing_sub_still_counts_its_failure(self):
        self._the_comments_note_fails(restart=False)

    def test_a_failed_note_before_a_failing_sub_still_counts_its_failure_after_the_restart(self):
        self._the_comments_note_fails(restart=True)

    def test_a_note_that_fails_with_something_other_than_oserror_still_counts_the_failure(self):
        """Review finding: every other fault here is the full disk's OSError, so the
        handler narrowed to ``except OSError`` passed them all. Whatever the write or note
        raised, the failures it hid are counted."""
        self._the_comments_note_fails(restart=False, error=ValueError("not a disk error"))

    def _the_comments_degraded_write_fails(self, restart):
        """The `degraded sub=comments` write itself fails. Reviews is counted; comments is counted
        too, as a hole NOT told: its line was never written, so it heals at c2 with no
        `degraded_recovered`, where main writes an orphan one."""
        told = self._a_third_failure_after_a_raise(
            lambda: self.write_fails_once("type=degraded sub=comments"), restart)
        self.assertEqual(told, [["type=degraded sub=reviews"], [], [self.PROMOTED],
                                ["type=degraded_recovered sub=reviews"], []])

    def test_a_failed_degraded_write_before_a_failing_sub_still_counts_its_failure(self):
        self._the_comments_degraded_write_fails(restart=False)

    def test_a_failed_degraded_write_before_a_failing_sub_still_counts_its_failure_after_the_restart(self):
        self._the_comments_degraded_write_fails(restart=True)

    def test_a_failure_whose_degraded_write_failed_is_counted_and_not_recorded_as_told(self):
        """What the raise leaves, in the running process and in the checkpoint: comments open,
        NOT told, its failure counted, and a `last` that lets its next failure be told; reviews'
        told episode kept as it was, its count advanced."""
        self.hub.listing[REPO] = [_pr(1)]
        self.tick()
        self.hub.fail = ("/reviews?",)
        self.tick()
        before = copy.deepcopy(self.state["degraded"]["reviews"])
        self.hub.fail = ("/issues/comments?", "/reviews?")
        self.write_fails_once("type=degraded sub=comments")
        self.assertEqual(self.tick(tolerate=True), [])
        window = gw._degraded.DEFAULT_RENOTIFY_MIN * 60
        for episodes in (self.state["degraded"], self.poller.saved()["degraded"]):
            comments = episodes["comments"]
            self.assertEqual((comments["open"], comments["announced"], comments["consecutive"]), (True, False, 1))
            self.assertLessEqual(comments["last"], self.clock.now.timestamp() - window)
            self.assertEqual(episodes["reviews"], dict(before, consecutive=2))

    def _the_reviews_renotify_write_fails(self, restart):
        """Reviews' own renotify `degraded` write fails, 60 minutes after c0: its episode stays
        told, with the `last` of c0, so c2 writes that `degraded` line and the promotion."""
        told = self._a_third_failure_after_a_raise(
            lambda: self.write_fails_once("type=degraded sub=reviews"), restart, c1_seconds=60 * 60)
        self.assertEqual(told, [["type=degraded sub=reviews"], ["type=degraded sub=comments"],
                                ["type=degraded sub=reviews", self.PROMOTED, "type=degraded_recovered sub=comments"],
                                ["type=degraded_recovered sub=reviews"], []])

    def test_a_failed_renotify_write_still_counts_its_failure(self):
        self._the_reviews_renotify_write_fails(restart=False)

    def test_a_failed_renotify_write_still_counts_its_failure_after_the_restart(self):
        self._the_reviews_renotify_write_fails(restart=True)

    def _a_promotion_due_on_a_sub_the_raise_left_unfolded(self, restart):
        """The raise falls on reviews' THIRD failure (c2): its `error:` line is due and not
        written, so its count is held one short, and the hole that persists is promoted at c3.
        Counted in full, it was past the `==` that promotes, and never promoted. Comments, whose
        `degraded` write failed at c2, is told at c3 with its first failure still counted."""
        self.hub.listing[REPO] = [_pr(1)]
        self.tick()
        comments, reviews = "/issues/comments?", "/reviews?"
        self.hub.fail = (reviews,)
        told = [_told(self.tick()), _told(self.tick())]    # c0 told, c1 its second failure
        self.hub.fail = (comments, reviews)
        self.write_fails_once("type=degraded sub=comments")
        told.append(_told(self.tick(tolerate=True)))       # c2: the raise, before reviews
        self.assertEqual(len(self.poller.raised), 1)
        if restart:
            self.restart()
        told.append(_told(self.tick()))                    # c3: both still failing
        self.hub.fail = ()
        told += [_told(self.tick()), _told(self.tick())]
        self.assertEqual(told, [["type=degraded sub=reviews"], [], [],
                                ["type=degraded sub=comments", self.PROMOTED],
                                ["type=degraded_recovered sub=comments", "type=degraded_recovered sub=reviews"], []])

    def test_a_promotion_due_on_a_sub_the_raise_left_unfolded_is_written_once_if_the_hole_persists(self):
        self._a_promotion_due_on_a_sub_the_raise_left_unfolded(restart=False)

    def test_a_promotion_due_on_a_sub_the_raise_left_unfolded_is_written_once_after_the_restart(self):
        self._a_promotion_due_on_a_sub_the_raise_left_unfolded(restart=True)

    # -- the recovered half of the per-sub tail -------------------------

    def _a_told_hole_heals(self):
        self.hub.fail = ("/issues/comments?",)
        self.assertEqual(_told(self.tick()), ["type=degraded sub=comments"])
        self.hub.fail = ()

    def test_a_recovery_whose_write_failed_is_told_once(self):
        """A recovered sub is folded in only after its `degraded_recovered` line: folded before
        it (mutant MV37), a failed write closed the episode, and the line was lost for good."""
        self._a_told_hole_heals()
        self._told_once_after_a_failed_write("type=degraded_recovered", restart=False)

    def test_a_recovery_whose_write_failed_is_told_once_after_the_restart(self):
        self._a_told_hole_heals()
        self._told_once_after_a_failed_write("type=degraded_recovered")

    def _two_told_holes_heal_and_the_second_recovery_fails(self):
        self.hub.listing[REPO] = [_pr(1)]
        self.tick()
        self.hub.fail = ("/issues/comments?", "/reviews?")  # sorted subs: comments, then reviews
        self.assertEqual(_told(self.tick()), ["type=degraded sub=comments", "type=degraded sub=reviews"])
        self.hub.fail = ()
        self.write_fails_once("type=degraded_recovered", nth=2)
        lines = self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(_told(lines), ["type=degraded_recovered sub=comments"])
        return lines

    def test_two_recoveries_whose_second_write_failed_are_each_told_once(self):
        """Each recovered sub is reconciled and folded on its own: reconciled in one batch
        (mutant MV7), the second sub's failed write left the first sub's episode open, and its
        recovery was told again."""
        lines = self._two_told_holes_heal_and_the_second_recovery_fails()
        lines += self.tick() + self.tick()
        self.assertEqual(_told(lines), ["type=degraded_recovered sub=comments", "type=degraded_recovered sub=reviews"])

    def test_two_recoveries_whose_second_write_failed_are_each_told_once_after_the_restart(self):
        lines = self._two_told_holes_heal_and_the_second_recovery_fails()
        self.restart()
        lines += self.tick() + self.tick()
        self.assertEqual(_told(lines), ["type=degraded_recovered sub=comments", "type=degraded_recovered sub=reviews"])

    # -- a heal the raise hid restarts the count -----
    #
    # A raise in the tail leaves every recovered sub after it unfolded: its episode stays open,
    # and told if it was, so its recovery is written at its next healthy cycle. Left with its old
    # count as well, its next failure counted on from the failures before the heal. In each case
    # below the `error:` lines are those of the same cycles with no fault (and of main in the
    # running process); only a hidden recovery moves, to its sub's next healthy cycle.

    COMMENTS, REVIEWS, BOTH = ("/issues/comments?",), ("/reviews?",), ("/issues/comments?", "/reviews?")
    D_C, D_R = "type=degraded sub=comments", "type=degraded sub=reviews"
    R_C, R_R = "type=degraded_recovered sub=comments", "type=degraded_recovered sub=reviews"

    def _cycles(self, script, restart):
        """One cycle per ``(failing, seconds, fault)`` in ``script``, with PR #1 listed so that its
        reviews are read. ``fault``, if not None, makes one write or note raise in that cycle,
        and ``restart`` restarts after each such cycle. Returns the sub lines told, per cycle."""
        if not self.hub.listing.get(REPO):
            self.hub.listing[REPO] = [_pr(1)]
            self.tick()
        told = []
        for failing, seconds, fault in script:
            self.hub.fail = failing
            raised = len(self.poller.raised)
            if fault:
                fault()
            told.append(_told(self.tick(seconds=seconds, tolerate=bool(fault))))
            self.assertEqual(len(self.poller.raised), raised + bool(fault))
            if fault and restart:
                self.restart()
        return told

    def _comments_note_fails(self):
        return lambda: self.note_fails_once("] note: degraded: comments not fetched")

    def _write_fails(self, matching):
        return lambda: self.write_fails_once(matching)

    def _both_heal_and_the_comments_recovery_write_fails(self, restart):
        """B2, the author's I-2: comments and reviews are told at c0 and heal at c1, where the
        `degraded_recovered sub=comments` write fails, so reviews' heal is hidden too. Reviews
        fails at c2, c3 and c4. Left with its count of 1, it was promoted at c3, after two
        failures. Counted from 0, it is promoted at c4, its third."""
        told = self._cycles([
            (self.BOTH, 60, None), ((), 60, self._write_fails("type=degraded_recovered sub=comments")),
            (self.REVIEWS, 60, None), (self.REVIEWS, 60, None), (self.REVIEWS, 60, None),
            ((), 60, None), ((), 60, None)], restart)
        self.assertEqual(told, [[self.D_C, self.D_R], [], [self.R_C], [], [self.PROMOTED], [self.R_R], []])

    def test_a_heal_the_raise_hid_restarts_the_count(self):
        self._both_heal_and_the_comments_recovery_write_fails(restart=False)

    def test_a_heal_the_raise_hid_restarts_the_count_after_the_restart(self):
        self._both_heal_and_the_comments_recovery_write_fails(restart=True)

    def _one_failure_after_a_hidden_heal(self, restart):
        """B2, a line invented: reviews fails at c0 and c1 and heals at c2, where the note after
        the written `degraded sub=comments` line fails. It fails once more at c3. Left with its
        count of 2, it was promoted at c3 after that one failure."""
        told = self._cycles([
            (self.REVIEWS, 60, None), (self.REVIEWS, 60, None), (self.COMMENTS, 60, self._comments_note_fails()),
            (self.REVIEWS, 60, None), ((), 60, None), ((), 60, None)], restart)
        self.assertEqual(told, [[self.D_R], [], [self.D_C], [self.R_C], [self.R_R], []])

    def test_one_failure_after_a_heal_the_raise_hid_is_not_promoted(self):
        self._one_failure_after_a_hidden_heal(restart=False)

    def test_one_failure_after_a_heal_the_raise_hid_is_not_promoted_after_the_restart(self):
        self._one_failure_after_a_hidden_heal(restart=True)

    def _a_promoted_hole_heals_hidden_and_fails_again(self, restart):
        """B2, a line lost: reviews is promoted at c2 and heals at c3 behind the same failed note.
        A new episode fails c4-c7. Left with its count of 3, the new episode counted 4 to 7,
        past the `==` that promotes, and was never promoted. Its episode stays told across the
        hidden heal, so the new failures are not told again, and its one recovery comes at c8."""
        told = self._cycles([
            (self.REVIEWS, 60, None), (self.REVIEWS, 60, None), (self.REVIEWS, 60, None),
            (self.COMMENTS, 60, self._comments_note_fails())]
            + [(self.REVIEWS, 60, None)] * 4 + [((), 60, None), ((), 60, None)], restart)
        self.assertEqual(told, [[self.D_R], [], [self.PROMOTED], [self.D_C], [self.R_C], [],
                                [self.PROMOTED], [], [self.R_R], []])

    def test_a_promoted_hole_whose_heal_the_raise_hid_is_promoted_in_its_next_episode(self):
        self._a_promoted_hole_heals_hidden_and_fails_again(restart=False)

    def test_a_promoted_hole_whose_heal_the_raise_hid_is_promoted_in_its_next_episode_after_the_restart(self):
        self._a_promoted_hole_heals_hidden_and_fails_again(restart=True)

    def _two_tail_faults_in_a_row(self, restart, persist):
        """B1: reviews is told at c0 and heals at c1 behind the failed comments note. At c2, 60
        minutes after c0, it fails and its renotify `degraded` write fails, which hides comments'
        heal; it fails at c3. Left with its count of 1, G1's count for c2 was added to it, and it
        was promoted at c3 after two failures, where main and the same cycles with no
        fault promote nothing. Counted from 0, it is promoted only at a third failure, c4
        (``persist``)."""
        told = self._cycles([
            (self.REVIEWS, 60, None), (self.COMMENTS, 60, self._comments_note_fails()),
            (self.REVIEWS, 59 * 60, self._write_fails("type=degraded sub=reviews")), (self.REVIEWS, 60, None)]
            + [(self.REVIEWS, 60, None)] * persist + [((), 60, None), ((), 60, None)], restart)
        self.assertEqual(told, [[self.D_R], [self.D_C], [], [self.D_R, self.R_C]]
                         + [[self.PROMOTED]] * persist + [[self.R_R], []])

    def test_two_tail_faults_in_a_row_do_not_promote_a_hole_that_failed_twice(self):
        self._two_tail_faults_in_a_row(restart=False, persist=False)

    def test_two_tail_faults_in_a_row_do_not_promote_a_hole_that_failed_twice_after_the_restart(self):
        self._two_tail_faults_in_a_row(restart=True, persist=False)

    def test_two_tail_faults_in_a_row_promote_a_hole_at_its_third_failure(self):
        self._two_tail_faults_in_a_row(restart=False, persist=True)

    def test_two_tail_faults_in_a_row_promote_a_hole_at_its_third_failure_after_the_restart(self):
        self._two_tail_faults_in_a_row(restart=True, persist=True)

    def _a_failure_with_no_line_after_a_hidden_heal(self, restart):
        """P4: reviews is told at c0 and heals at c1 behind the failed comments note, so its count
        restarts at 0 with its episode still told. At c2 it fails again, which writes nothing (an
        announced episode still going), and comments' recovery write fails after it. It fails at
        c3, then heals. The fold after its pairs, there being none, marks reviews folded: left
        unmarked, the raise counted its c2 failure a second time, and it was promoted at c3."""
        told = self._cycles([
            (self.REVIEWS, 60, None), (self.COMMENTS, 60, self._comments_note_fails()),
            (self.REVIEWS, 60, self._write_fails("type=degraded_recovered sub=comments")),
            (self.REVIEWS, 60, None), ((), 60, None), ((), 60, None)], restart)
        self.assertEqual(told, [[self.D_R], [self.D_C], [], [self.R_C], [self.R_R], []])

    def test_a_failure_that_writes_no_line_before_a_raise_is_counted_once(self):
        self._a_failure_with_no_line_after_a_hidden_heal(restart=False)

    def test_a_failure_that_writes_no_line_before_a_raise_is_counted_once_after_the_restart(self):
        self._a_failure_with_no_line_after_a_hidden_heal(restart=True)

    def _a_promoted_hole_fails_behind_a_raise(self, restart):
        """P1: reviews is promoted at c2, its third failure. At c3 it fails behind a raise (the
        comments note), and at c4 it fails again. G1's count is held one short only AT the
        promotion; past it (4) it is not held, so the hole is promoted once. Held at every count
        from the promotion on (the mutant at ``>=``), it went back to 2, and c4 promoted it again."""
        told = self._cycles([
            (self.REVIEWS, 60, None), (self.REVIEWS, 60, None), (self.REVIEWS, 60, None),
            (self.BOTH, 60, self._comments_note_fails()), (self.REVIEWS, 60, None),
            ((), 60, None), ((), 60, None)], restart)
        self.assertEqual(told, [[self.D_R], [], [self.PROMOTED], [self.D_C], [self.R_C], [self.R_R], []])

    def test_a_promoted_hole_that_fails_behind_a_raise_is_not_promoted_again(self):
        self._a_promoted_hole_fails_behind_a_raise(restart=False)

    def test_a_promoted_hole_that_fails_behind_a_raise_is_not_promoted_again_after_the_restart(self):
        self._a_promoted_hole_fails_behind_a_raise(restart=True)

    def test_a_closed_episode_that_fails_behind_a_raise_is_counted_from_one_and_not_told(self):
        """P2: reviews is told at c0 and recovers at c1: a closed, told episode inside the window.
        At c2 it fails behind a raise (the comments note), so its failure is counted on a record
        that is open, NOT told, with a count of 1 and the `last` of c0. Its next failures are then
        silenced as with no fault, the third (c4) is promoted, and its recovery is silent. Opened as told (mutant ``announced=old["announced"]``), its heal wrote an orphan
        `degraded_recovered`; left closed, its failure not counted, the c4 promotion was lost."""
        told = self._cycles([(self.REVIEWS, 60, None)], restart=False)
        last = self.state["degraded"]["reviews"]["last"]
        told += self._cycles([((), 60, None)], restart=False)
        closed = copy.deepcopy(self.state["degraded"]["reviews"])
        self.assertEqual((closed["open"], closed["announced"], closed["last"]), (False, True, last))
        told += self._cycles([(self.BOTH, 60, self._comments_note_fails())], restart=False)
        for episodes in (self.state["degraded"], self.poller.saved()["degraded"]):
            self.assertEqual(episodes["reviews"], dict(closed, open=True, announced=False, consecutive=1))
        told += self._cycles([(self.REVIEWS, 60, None), (self.REVIEWS, 60, None),
                              ((), 60, None), ((), 60, None)], restart=False)
        self.assertEqual(told, [[self.D_R], [self.R_R], [self.D_C], [self.R_C], [self.PROMOTED], [], []])

    def test_a_raise_counts_no_failure_for_a_closed_episode_it_did_not_reach(self):
        """P3: comments is told at c0 and recovers at c1: a closed episode inside the window. At
        c2 reviews fails and its `degraded` write fails before comments, which is fetching fine,
        is reached. The raise leaves comments' closed episode as it was: the handler counts the
        FAILED subs only. Run over every sub (the mutant), it opened comments as an untold
        failure, and comments' next healthy cycle noted a silent recovery from a failure that
        never happened. On the profile log that mutant writes what this does, because the I-2
        reset after it re-zeroes every count it adds to an open episode."""
        told = self._cycles([(self.COMMENTS, 60, None), ((), 60, None)], restart=False)
        closed = copy.deepcopy(self.state["degraded"]["comments"])
        told += self._cycles([(self.REVIEWS, 60, self._write_fails("type=degraded sub=reviews"))], restart=False)
        for episodes in (self.state["degraded"], self.poller.saved()["degraded"]):
            self.assertEqual(episodes["comments"], closed)
        first = len(self.poller.notes)
        told += self._cycles([((), 60, None)], restart=False)
        self.assertEqual([n for n in self.poller.notes[first:] if "comments" in n], [])
        self.assertEqual(told, [[self.D_C], [self.R_C], [], []])


class _BetweenRepos(object):
    """One of every line a repo can emit, in both repos; then one repo's PR listing fails,
    raising the cycle, and the daemon restarts. Each line must be written exactly once."""

    def _every_kind(self, repo):
        hub = self.hub
        hub.listing[repo] = [_pr(3, title="new"), _pr(1, head="ccccccccc"),
                             _pr(2, state="closed", merged_at=self.clock.at(0), body="B.\n\nLands-with: #9\n")]
        hub.reviews[(repo, "1")].append(hub.new_review(body="a new review"))
        hub.add_comment(repo=repo, body="a new comment")
        self.burst(20, repo=repo)

    def _raise_between_repos(self, failing):
        hub = self.hub
        for repo in self.repos:
            hub.listing[repo] = [_pr(1, head="aaaaaaaaa"), _pr(2, head="bbbbbbbbb", body="B.\n\nLands-with: #9\n")]
            hub.reviews[(repo, "1")] = [hub.new_review()]
        self.tick()
        for repo in self.repos:
            self._every_kind(repo)
        self.fail_listing(failing)
        with mock.patch.object(gw, "companion_merged", lambda repo, number: False):
            failed = self.tick(tolerate=True)
            self.assertEqual(len(self.poller.raised), 1)
            self.restart()
            self.hub.fail = ()
            after = self.tick() + self.tick()
        lines = [l for l in failed + after if l.startswith("type=")]
        self.assertEqual(_repeated(lines), [])
        per_repo = collections.Counter((_repo_of(l), _kind(l)) for l in lines)
        for repo in self.repos:
            rs = repo.split("/")[1]
            self.assertEqual(dict((k, n) for (r, k), n in per_repo.items() if r == rs),
                             {"type=pr_opened": 1, "type=pr_push": 1, "type=pr_merged": 1,
                              "type=pair_window_open": 1, "type=review": 1, "type=comment": 1, "type=run": 20},
                             "repo %s" % rs)
        return failed

    def test_the_second_repo_fails_after_the_first_emitted(self):
        failed = self._raise_between_repos(failing=self.repos[1])
        self.assertEqual(set(_repo_of(l) for l in failed if l.startswith("type=")), {self.repos[0].split("/")[1]})

    def test_the_first_repo_fails_before_anything_is_emitted(self):
        failed = self._raise_between_repos(failing=self.repos[0])
        self.assertEqual([l for l in failed if l.startswith("type=")], [])


class ARaiseBetweenRepos(_BetweenRepos, _FailedCycleThenRestart):
    repos = (REPO, OTHER)


class ARaiseBetweenReposMirrored(_BetweenRepos, _FailedCycleThenRestart):
    repos = (OTHER, REPO)


class AnOversizedCompanionNumber(_FailedCycleThenRestart):
    """The companion number was an unbounded `(\\d+)` passed to `int()`;
    past 4,300 digits that raises ValueError wherever CPython caps int-string conversion (3.14,
    and 3.9.14 on, CI's 3.9 included), so a PR body GitHub accepts made every cycle raise at that
    PR, repeat `pr_opened`, and never poll the rest of the profile. The number is now at most
    nine digits, so an oversized one is an unparsed trailer, told once. These tests do not
    depend on the interpreter: without the bound, `_companion_of` returns a pair on every one,
    and the trailer is read as a declaration rather than told."""

    HUGE = "1" * 5000

    def test_it_does_not_parse_on_any_interpreter(self):
        for value in ("a/b#" + self.HUGE, "#" + self.HUGE, "https://github.com/a/b/pull/" + self.HUGE):
            self.assertIsNone(gw._companion_of(value, "x/y"), value[:40])
        self.assertEqual(gw._companion_of("a/b#123456789", "x/y"), ("a/b", 123456789))
        self.assertEqual(gw._companion_of("#123456789", "x/y"), ("x/y", 123456789))
        body = "Body.\n\nLands-with: a/b#%s\n" % self.HUGE
        self.assertEqual(gw.pairs_of(body, repo="x/y"), [])
        self.assertEqual(gw.companions_mentioned(body, repo="x/y"), [])
        self.assertEqual(gw.unparsed_trailers(body, repo="x/y"), ["a/b#" + self.HUGE])

    def test_a_new_pr_carrying_one_is_told_and_the_other_repo_is_still_announced(self):
        self.hub.listing[REPO] = [_pr(4, body="Body.\n\nLands-with: owner/repo#%s\n" % self.HUGE)]
        made = self.burst(3, repo=OTHER, name="CI")
        comment = self.hub.add_comment(repo=OTHER, body="a comment in the other repo")
        lines = self.tick() + self.tick() + self.tick()    # a raise fails the test here
        self.assertEqual(len(_typed(lines, "type=pr_opened repo=app #4 ")), 1, lines)
        told = _typed(lines, "type=pair_trailer_unparsed repo=app #4 ")
        self.assertEqual(len(told), 1, lines)
        self.assertLess(len(told[0]), 200, "the told value was not capped")
        self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made))
        self.assertEqual(len([l for l in _typed(lines, "type=comment repo=agent-watchtower ")
                              if " id=%d " % comment["id"] in l]), 1, lines)

    def test_a_merge_carrying_one_checks_no_companion_and_completes(self):
        """The merge and pair-retry paths read the same two parsers; main raised there too."""
        body = "Body.\n\nLands-with: owner/repo#%s\n" % self.HUGE
        self.hub.listing[REPO] = [_pr(4, body=body)]
        self.tick()
        self.hub.listing[REPO] = [_pr(4, state="closed", merged_at=self.clock.at(0), body=body)]
        asked = []
        with mock.patch.object(gw, "companion_merged", lambda repo, number: asked.append((repo, number)) or False):
            lines = self.tick() + self.tick()
        self.assertEqual(asked, [], "an oversized trailer was read as a declared companion")
        self.assertEqual(len(_typed(lines, "type=pr_merged ")), 1, lines)
        self.assertEqual(len(_typed(lines, "type=pair_trailer_unparsed ")), 1, lines)
        self.assertEqual(self.state["pair_pending"], [])


class AFirstRunWhoseTailFails(_FailedCycleThenRestart):
    """A first run used to end only after its degraded lines, and the
    checkpoint skips a first run. So a first run with a hole whose `type=degraded` write failed
    raised with `first_run` still set and `since` unset, and the next cycle silently re-seeded:
    a PR, a comment and a failed run made in between were never announced. The first run now
    ends once every repo is read, before the degraded lines, as it did before the checkpoint was added."""

    def _fresh(self, unreadable=False):
        hub = _Hub(repos=(REPO,), clock=self.clock)
        poller = _Poller(self, hub)
        for i in range(30):
            hub.add_run(name="Deploy (dev)", created_at=self.clock.at(-3 * 3600 + i))
        if unreadable:
            os.makedirs(poller.state_dir)
            with open(poller.state_path, "w") as fh:
                fh.write("{not json")
        return hub, poller

    def _first_run_whose_degraded_write_fails(self, hub, poller):
        hub.fail = ("/issues/comments?",)                  # a sub-fetch hole in the first run
        hits = [0]

        def emit(profile, line):
            if not hits[0] and line.startswith("type=degraded"):
                hits[0] += 1
                raise OSError(28, "No space left on device")
            poller.emitted.append(line)

        state = poller.load()
        self.assertTrue(state["first_run"])
        start = self.clock.now.isoformat()
        with mock.patch.object(gw, "emit", emit):
            self.assertEqual(poller.poll(state, tolerate=True), [])
        self.assertEqual((len(poller.raised), hits[0]), (1, 1))
        self.assertFalse(state["first_run"], "a first run that read every repo kept its baseline open")
        self.assertEqual(state["since"], start, "a first run that read every repo set no watermark")
        hub.fail = ()
        return state

    def _made_in_between(self, hub):
        hub.listing[REPO] = [_pr(4, title="opened after the first run")]
        comment = hub.add_comment(body="made after the first run")
        run = hub.add_run(name="CI", conclusion="failure", created_at=self.clock.at(1))
        return comment, run

    def _announced_once(self, lines, comment, run):
        self.assertEqual(len(_typed(lines, "type=pr_opened repo=app #4 ")), 1, lines)
        self.assertEqual(len([l for l in _typed(lines, "type=comment ") if " id=%d " % comment["id"] in l]), 1, lines)
        self.assertEqual(_run_ids(lines), [run["id"]])
        self.assertEqual(_repeated([l for l in lines if l.startswith("type=")]), [])

    def test_a_first_run_whose_degraded_write_fails_announces_what_is_made_next(self):
        hub, poller = self._fresh()
        state = self._first_run_whose_degraded_write_fails(hub, poller)
        # Review finding: on a fresh install there is no file to checkpoint, and the
        # checkpoint writes and notes nothing (it used to note `[Errno 2]` from `os.stat`).
        self.assertFalse(os.path.exists(poller.state_path))
        self.assertEqual([n for n in poller.notes if "checkpoint" in n], [])
        comment, run = self._made_in_between(hub)
        lines = []
        for _ in range(3):
            self.clock.advance(60)
            lines += poller.poll(state)
        self._announced_once(lines, comment, run)

    def test_over_an_unreadable_state_file_the_restart_announces_what_is_made_next_and_no_history(self):
        """The file exists, so the checkpoint is saved. `first_run` and `since` move TOGETHER:
        `first_run` cleared alone checkpoints a normal state with no watermark, and the restart
        reads the hole's unseeded comments as news (the review's mutant V2)."""
        hub, poller = self._fresh(unreadable=True)
        old = hub.add_comment(body="made before the first run", created_at=self.clock.at(-8 * 60),
                              updated_at=self.clock.at(-8 * 60))
        state = self._first_run_whose_degraded_write_fails(hub, poller)
        restarted = poller.load()
        self.assertFalse(restarted["first_run"])
        self.assertEqual(restarted["since"], state["since"])
        comment, run = self._made_in_between(hub)
        lines = []
        for _ in range(3):
            self.clock.advance(60)
            lines += poller.poll(restarted)
        self._announced_once(lines, comment, run)
        self.assertEqual([l for l in lines if " id=%d " % old["id"] in l], [], "the restart announced history")


class TheCapMemoryLoadsWhateverItHolds(unittest.TestCase):
    """`_cap_told` drops every malformed entry. Without that filter a
    saved `{"repo": 5}` sent `load_state` into its bare except, which returns a FIRST RUN: a
    silent re-baseline that loses everything made while the daemon was down."""

    SINCE = "2026-09-30T20:40:00+00:00"
    SHAPES = ({"r": 5}, {"r": None}, [], "x", None, 5, {"r": ["a"]}, {"r": ["a", "b", "c"]}, {"r": []},
              {"r": {"since": "a", "until": "b"}}, {"r": {"a": {"b": 1}}})

    def _load(self, cap_told):
        poller = _Poller(self, _Hub())
        os.makedirs(poller.state_dir, exist_ok=True)
        with open(poller.state_path, "w") as fh:
            json.dump({"since": self.SINCE, "seen": ["run:1:1"], "seen_v": gw.SEEN_V, "prs": {},
                       "runs_cap_told": cap_told}, fh)
        return poller.load()

    def test_every_malformed_shape_loads_as_an_empty_memory_and_not_as_a_first_run(self):
        for shape in self.SHAPES:
            state = self._load(shape)
            self.assertFalse(state["first_run"], "runs_cap_told=%r loaded as a first run" % (shape,))
            self.assertEqual(state["runs_cap_told"], {}, "runs_cap_told=%r" % (shape,))
            self.assertEqual((state["since"], list(state["seen"])), (self.SINCE, ["run:1:1"]))

    def test_a_good_entry_beside_a_bad_one_is_kept(self):
        state = self._load({"good": ["s", "u"], "bad": 5})
        self.assertFalse(state["first_run"])
        self.assertEqual(state["runs_cap_told"], {"good": ("s", "u")})


class TheRunsStateLoadsWhateverItHolds(_ClockedRuns):
    """`runs_from` and `runs_checked` were loaded as the file held them, so a
    null or a list in either (no version writes one) made every cycle raise at `.get` or
    `.setdefault` for as long as the file stayed so: the profile was never polled again. They are
    read like `runs_cap_told` now: a value that is not the expected shape is dropped, and the cycle
    runs. Two parked runs off every page are checked by id each cycle, so the rotation memory is
    read, written and sorted on."""

    TOP = (None, [], ["x"], "x", 5)

    def setUp(self):
        _ClockedRuns.setUp(self)
        self.parked = self.burst(2, status="queued", conclusion=None, name="Deploy (prod)")
        self.tick()
        self.burst(25, start=30)
        self.tick(300)
        self.tick(300)                                     # off every page: checked by id
        self.assertEqual(set(self.state["runs_checked"][REPO]), set(str(r["id"]) for r in self.parked))
        one = str(self.parked[0]["id"])
        # a check time that is not a number beside one that is: sorting the two raised TypeError
        self.per_repo = ({REPO: None}, {REPO: []}, {REPO: [one]}, {REPO: "x"}, {REPO: 5},
                         {REPO: {one: "x"}}, {REPO: {one: [1]}}, {REPO: {one: None}}, {REPO: {one: True}})

    def _reload_with(self, key, value):
        raw = self.poller.saved()
        raw[key] = value
        with open(self.poller.state_path, "w") as fh:
            json.dump(raw, fh)
        return self.poller.load()

    def _every_cycle_still_runs(self, key, shapes):
        for shape in shapes:
            state = self._reload_with(key, shape)
            self.assertFalse(state["first_run"], "%s=%r loaded as a first run" % (key, shape))
            made = self.burst(3)
            lines = self.tick(state=state)                 # raised here, every cycle, before
            self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made), "%s=%r" % (key, shape))
            self.assertIsInstance(state[key], dict, "%s=%r" % (key, shape))
            self.assertIsInstance(state[key].get(REPO, {}), (dict, str), "%s=%r" % (key, shape))

    def test_a_malformed_runs_from_loads_as_no_hold(self):
        self._every_cycle_still_runs("runs_from", self.TOP)

    def test_a_malformed_runs_checked_loads_as_no_rotation_memory(self):
        self._every_cycle_still_runs("runs_checked", self.TOP + self.per_repo)

    def test_a_malformed_runs_recent_loads_as_no_record(self):
        """The record of run keys touched lately, `{repo: {key: epoch}}`."""
        self._every_cycle_still_runs("runs_recent", self.TOP + self.per_repo)

    def test_runs_recent_is_saved_and_a_good_entry_beside_a_bad_one_is_kept(self):
        saved = self.poller.saved()["runs_recent"]
        self.assertIn(REPO, saved)
        self.assertTrue(all(k.startswith("run:") and gw._is_time(at) for k, at in saved[REPO].items()), saved)
        good = {"run:1:1": 1234.5}
        state = self._reload_with("runs_recent", {REPO: good, OTHER: None, "x/y": {"run:9:1": "soon"}})
        self.assertEqual(state["runs_recent"], {REPO: good, "x/y": {}})
        state = self._reload_with("runs_recent", {REPO: dict(good, **{"run:7:1": True})})
        self.assertEqual(state["runs_recent"], {REPO: good})
        huge = {"run:7:1": 10 ** 400, "run:8:1": float("inf")}
        state = self._reload_with("runs_recent", {REPO: dict(good, **huge)})
        self.assertEqual(state["runs_recent"], {REPO: good})

    def test_a_file_without_runs_recent_loads_with_none(self):
        """The previous code does not write it: read with .get."""
        raw = self.poller.saved()
        del raw["runs_recent"]
        with open(self.poller.state_path, "w") as fh:
            json.dump(raw, fh)
        state = self.poller.load()
        self.assertEqual(state["runs_recent"], {})
        self.assertEqual(_run_ids(self.tick(state=state)), [])

    def test_a_good_entry_beside_a_bad_one_is_kept(self):
        good = {str(self.parked[0]["id"]): 1234.5}
        state = self._reload_with("runs_checked", {REPO: good, OTHER: None, "x/y": {"9": "soon"}})
        self.assertEqual(state["runs_checked"], {REPO: good, "x/y": {}})
        # a bool is an int to Python but not a check time
        state = self._reload_with("runs_checked", {REPO: dict(good, **{"7": True})})
        self.assertEqual(state["runs_checked"], {REPO: good})
        # nor is a number no float can hold, or an infinite one
        huge = {"7": 10 ** 400, "8": float("inf")}
        state = self._reload_with("runs_checked", {REPO: dict(good, **huge)})
        self.assertEqual(state["runs_checked"], {REPO: good})
        held = self.clock.at(-90)
        state = self._reload_with("runs_from", {REPO: held})
        self.assertEqual(state["runs_from"], {REPO: held})

    def test_a_held_read_survives_a_reload_with_a_malformed_rotation_memory(self):
        """Only the malformed key is dropped: a hold in the same file is read as before."""
        made = self.burst(45)
        self.hub.fail = ("&page=2",)
        first = self.tick()
        self.hub.fail = ()
        state = self._reload_with("runs_checked", None)
        self.assertIn(REPO, state["runs_from"])
        back = self.tick(state=state)
        self.assertEqual(_run_ids(first + back), sorted(r["id"] for r in made))


class CommentsArePaged(_ClockedRuns):
    """A cycle read one page of 30 comments per endpoint, so after downtime the rest
    went unannounced. Each endpoint is now read page after page until a short page, up to
    COMMENTS_MAX_PAGES; the steady state is still one request per endpoint."""

    def _made(self, n, step=1):
        made = []
        for _ in range(n):
            self.clock.advance(step)
            made.append(self.hub.add_comment())
        return made

    def _comment_requests(self, since_index, endpoint="issues"):
        return [p for p in self.hub.asked[since_index:] if "/%s/comments?" % endpoint in p]

    def _ids(self, lines):
        return sorted(int(re.search(r" id=(\d+) ", l).group(1)) for l in lines if l.startswith("type=comment "))

    def test_comments_made_across_an_hour_of_downtime_are_all_announced(self):
        made = self._made(45, step=80)                     # 45 comments over an hour, nobody polling
        before = len(self.hub.asked)
        lines = self.tick(seconds=1)
        self.assertEqual(self._ids(lines), sorted(c["id"] for c in made),
                         "comments past the first page of 30 went unannounced")
        self.assertEqual(len(self._comment_requests(before)), 2, "45 comments are two pages: 30, then 15")
        self.assertEqual(len(self._comment_requests(before, "pulls")), 1)
        self.assertEqual(_typed(lines, "type=unread_comments"), [], "the cap was not reached")

    def test_the_cap_is_told_once_and_the_comments_read_before_it_are_announced(self):
        made = self._made(200)
        before = len(self.hub.asked)
        lines = self.tick(seconds=1)
        read = gw.COMMENTS_PER_PAGE * gw.COMMENTS_MAX_PAGES
        self.assertEqual(len(self._comment_requests(before)), gw.COMMENTS_MAX_PAGES,
                         "more than the bound was requested, or fewer")
        self.assertEqual(self._ids(lines), sorted(c["id"] for c in made[:read]))
        capped = _typed(lines, "type=unread_comments repo=app endpoint=issues reason=page_cap ")
        self.assertEqual(len(capped), 1, lines)
        self.assertEqual(len(_typed(lines, "type=unread_comments")), 1, "only the issue comments hit the bound")
        # The episode is still going on the next poll (the same 200 are still inside the window):
        # it is not told again, and nothing already announced is announced twice.
        again = self.tick(seconds=1)
        self.assertEqual(_typed(again, "type=unread_comments"), [], "one episode told twice")
        self.assertEqual(self._ids(again), [])

    def test_an_episode_that_ended_is_told_again_when_the_cap_is_hit_anew(self):
        self._made(200)
        self.assertEqual(len(_typed(self.tick(seconds=1), "type=unread_comments")), 1)
        for _ in range(3):                                 # the 200 age out of the window: short reads
            self.tick(seconds=400)
        self._made(200)
        self.assertEqual(len(_typed(self.tick(seconds=1), "type=unread_comments")), 1, "a new episode went untold")

    def test_a_failed_second_page_keeps_the_first_and_is_recorded(self):
        made = self._made(45)
        self.hub.fail_when = lambda path: "/issues/comments?" in path and "page=2" in path
        lines = self.tick(seconds=1)
        self.assertEqual(self._ids(lines), sorted(c["id"] for c in made[:gw.COMMENTS_PER_PAGE]),
                         "page one's comments were lost with the failed page")
        self.assertTrue(any("comments page 2 fetch failed" in n for n in self.poller.notes), self.poller.notes)
        self.assertEqual(len(_typed(lines, "type=degraded sub=comments")), 1,
                         "a failed page was not recorded as a comments hole")
        self.hub.fail_when = None
        later = self.tick()
        self.assertEqual(self._ids(later), sorted(c["id"] for c in made[gw.COMMENTS_PER_PAGE:]),
                         "the comments behind the failed page were not read when it answered")

    def test_the_steady_state_asks_once_per_endpoint(self):
        self._made(gw.COMMENTS_PER_PAGE - 1)
        for _ in range(3):
            before = len(self.hub.asked)
            self.tick()
            self.assertEqual(len(self._comment_requests(before)), 1)
            self.assertEqual(len(self._comment_requests(before, "pulls")), 1)
            self.assertTrue(all("page=" not in p.replace("per_page=", "") for p in self._comment_requests(before)))

    def test_the_dedup_memory_is_sized_for_every_page(self):
        self.assertEqual(gw.SEEN_WINDOW["c"],
                         gw.COMMENT_ENDPOINTS * gw.COMMENTS_PER_PAGE * gw.COMMENTS_MAX_PAGES)
        self.assertEqual(gw.SEEN_CAPS["c"], gw.SEEN_WINDOW["c"] * gw.COMMENT_POLLS)
        made = self._made(gw.COMMENTS_PER_PAGE * gw.COMMENTS_MAX_PAGES)
        self.tick(seconds=1)
        known = [k for k in self.state["seen"] if k.startswith("c:")]
        self.assertEqual(sorted(known), sorted("c:%d" % c["id"] for c in made))


class TheTailSaysWhatOneReconcileSays(_FailedCycleThenRestart):
    """The tail reconciles each sub-resource on its own episode and folds
    it in right after its own lines, so a failed write cannot drop an earlier sub's told
    episode. On a cycle whose writes all succeed that must be exactly one `reconcile` call:
    the same lines, in the same order, and the same episodes. Checked over random hole
    sequences that cross the renotify window and the promotion threshold."""

    def test_random_hole_sequences_match_one_reconcile_call(self):
        self.hub.listing[REPO] = [_pr(1)]
        self.tick()
        made, real = [], gw._degraded.Cycle

        class Spy(real):
            def __init__(cycle):
                real.__init__(cycle)
                made.append(cycle)

        rng = random.Random(1128)
        expected = copy.deepcopy(self.state["degraded"])
        subs = ("/issues/comments?", "/reviews?", "%s/actions/runs?" % OTHER)
        said = []
        with mock.patch.object(gw._degraded, "Cycle", Spy):
            for _ in range(80):
                self.hub.fail = tuple(s for s in subs if rng.random() < 0.45)
                del made[:]
                lines = self.tick(seconds=rng.choice((60, 60, 60, 900, 3700)))
                holes = made[0]                            # `poll_profile`'s own, made before any other
                want = [e for e, _ in gw._degraded.reconcile(holes, expected, self.clock.now.timestamp()) if e]
                self.assertEqual([l for l in lines if l.startswith(("type=degraded", "error: sub-fetch"))], want)
                self.assertEqual(self.state["degraded"], expected)
                said += want
        for kind in ("type=degraded ", "type=degraded_recovered ", "error: sub-fetch ", " suppressed="):
            self.assertTrue([l for l in said if kind in l], "the sequence never produced %r" % kind)


class AnEarlyRaiseIsCounted(_FailedCycleThenRestart):
    """A cycle that raises BEFORE its degraded tail reconciled no sub-fetch, so a hole
    that failed in it was counted by nobody and a sub that fetched fine in it was never seen to
    heal: the promotion count drifted from the one an unraised cycle keeps. OTHER's PR
    listing, which raises the cycle by design, is read after REPO's PRs, reviews, comments and
    runs, so a raise there is a raise before the tail with REPO's sub-fetches already done."""

    PROMOTED = "error: sub-fetch reviews failing repeatedly"
    DEGRADED = "type=degraded sub=reviews"
    RECOVERED = "type=degraded_recovered sub=reviews"

    def setUp(self):
        super(AnEarlyRaiseIsCounted, self).setUp()
        self.hub.listing[REPO] = [_pr(1)]                  # an open PR, so its reviews are read
        self.tick()
        self.reviews_fail = ("/reviews?",)

    def raising(self, reviews_fail=True):
        """One cycle that raises at OTHER's listing, REPO's reviews failing or not. Returns the lines told."""
        self.hub.fail = (("%s/pulls?" % OTHER,) + (self.reviews_fail if reviews_fail else ()))
        before = len(self.poller.raised)
        told = _told(self.tick(tolerate=True))
        self.assertEqual(len(self.poller.raised), before + 1, "the cycle did not raise")
        return told

    def plain(self, reviews_fail=True):
        """One cycle that completes, REPO's reviews failing or not. Returns the lines told."""
        self.hub.fail = self.reviews_fail if reviews_fail else ()
        return _told(self.tick())

    def episodes(self, sub="reviews"):
        """The sub's episode in the running process and in the checkpoint file, which must agree."""
        saved = self.poller.saved()["degraded"].get(sub)
        self.assertEqual(self.state["degraded"].get(sub), saved)
        return saved

    def test_an_unraised_hole_is_told_promoted_and_recovered(self):
        """The control: what the raised cycles below are compared against."""
        self.assertEqual([self.plain(), self.plain(), self.plain(), self.plain(False), self.plain(False)],
                         [[self.DEGRADED], [], [self.PROMOTED], [self.RECOVERED], []])

    def test_a_hole_whose_first_cycles_raise_before_the_tail_is_promoted_at_its_third(self):
        """c0 and c1 raise with reviews failing and write nothing, but each counts the failure,
        marks nothing told, and checkpoints that. c2 completes with reviews still failing: its
        third consecutive failing cycle, so it is promoted. Uncounted, c2 was the first failure
        the tail saw, and a hole that then healed never got its `error:` line."""
        self.assertEqual(self.raising(), [])
        first = self.episodes()
        self.assertEqual((first["open"], first["announced"], first["consecutive"]), (True, False, 1))
        self.assertEqual(self.raising(), [])
        self.assertEqual(self.episodes()["consecutive"], 2)
        self.assertEqual(self.plain(), [self.DEGRADED, self.PROMOTED])
        self.assertEqual(self.episodes()["consecutive"], 3)
        self.assertEqual([self.plain(False), self.plain(False)], [[self.RECOVERED], []])

    def test_a_hole_whose_cycles_all_raise_keeps_its_count_and_is_promoted_by_the_next(self):
        """Three cycles raise before the tail: the third failure is held one short of the promotion,
        as in the tail (nothing was written in it), so the next cycle that completes with the hole
        still failing promotes it. Uncounted it was never promoted, since it healed at c4."""
        self.assertEqual([self.raising(), self.raising(), self.raising()], [[], [], []])
        self.assertEqual(self.episodes()["consecutive"], 2)
        self.assertEqual(self.plain(), [self.DEGRADED, self.PROMOTED])
        self.assertEqual([self.plain(False), self.plain(False)], [[self.RECOVERED], []])

    def test_a_sub_fetched_ok_in_a_raising_cycle_restarts_its_count(self):
        """c0 and c1 fail and are counted (2), c2 raises with reviews fetched fine: the hole healed,
        so its count restarts at 0 and the episode stays open, and told. Its next failures count
        from one: c5 is the third, and c3 is not (left at 2, c3 promoted it a cycle too early)."""
        self.assertEqual([self.plain(), self.plain()], [[self.DEGRADED], []])
        self.assertEqual(self.episodes()["consecutive"], 2)
        self.assertEqual(self.raising(reviews_fail=False), [])
        healed = self.episodes()
        self.assertEqual((healed["open"], healed["announced"], healed["consecutive"]), (True, True, 0))
        self.assertEqual([self.plain(), self.plain(), self.plain()], [[], [], [self.PROMOTED]])

    def test_a_sub_the_raise_came_before_is_held_as_it_was(self):
        """A raise at REPO's own listing, before any of REPO's sub-fetches: nothing was fetched, so
        an open episode is neither counted nor restarted."""
        self.assertEqual([self.plain(), self.plain()], [[self.DEGRADED], []])
        before = copy.deepcopy(self.episodes())
        self.hub.fail = ("%s/pulls?" % REPO,)
        self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(self.episodes(), before)

    def test_a_sub_fetched_ok_in_one_repo_does_not_heal_the_open_episode_of_another(self):
        """The reviewers' multi-repo probe: OTHER's reviews failed in two cycles (an episode at 2),
        then a cycle raises at OTHER's listing after REPO's reviews answered. REPO's success is not
        OTHER's: the episode is held at 2, so the next failing cycle is its third and promotes it.
        Healed by REPO's success it restarted at 0, and that cycle was only its first."""
        self.hub.listing[OTHER] = [_pr(2)]
        self.reviews_fail = ("%s/pulls/2/reviews?" % OTHER,)
        self.assertEqual([self.plain(), self.plain()], [[self.DEGRADED], []])
        self.assertEqual(self.episodes()["consecutive"], 2)
        self.assertEqual(self.raising(reviews_fail=False), [])
        held = self.episodes()
        self.assertEqual((held["open"], held["announced"], held["consecutive"]), (True, True, 2))
        self.assertEqual(self.plain(), [self.PROMOTED])

    def _only_one_sub_answers(self, sub):
        """Open the episodes of reviews, comments and runs (REPO's fetches failing for two cycles),
        then raise at OTHER's listing with only ``sub`` answering: its episode restarts at 0, and the
        others, which failed again, are counted and held one short of the promotion."""
        needles = {"reviews": ("%s/pulls/1/reviews?" % REPO,),
                   "comments": ("%s/issues/comments?" % REPO, "%s/pulls/comments?" % REPO),
                   "runs": ("%s/actions/runs?" % REPO,)}
        self.hub.fail = needles["reviews"] + needles["comments"] + needles["runs"]
        self.tick()
        self.tick()
        for each in sorted(needles):
            self.assertEqual(self.episodes(each)["consecutive"], 2, each)
        others = tuple(n for each in sorted(needles) if each != sub for n in needles[each])
        self.hub.fail = ("%s/pulls?" % OTHER,) + others
        self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1, "the cycle did not raise")
        healed = self.episodes(sub)
        self.assertEqual((healed["open"], healed["consecutive"]), (True, 0), sub)
        for each in sorted(needles):
            if each != sub:
                self.assertEqual(self.episodes(each)["consecutive"], 2, each)

    def test_reviews_that_answer_in_a_raising_cycle_heal_only_their_own_episode(self):
        self._only_one_sub_answers("reviews")

    def test_comments_that_answer_in_a_raising_cycle_heal_only_their_own_episode(self):
        self._only_one_sub_answers("comments")

    def test_runs_that_answer_in_a_raising_cycle_heal_only_their_own_episode(self):
        self._only_one_sub_answers("runs")

    PLAIN_RUNS = "%s/actions/runs?per_page" % REPO
    STATUS_RUNS = "%s/actions/runs?status=" % REPO

    def _open_a_runs_episode(self, failing):
        """REPO's runs fail (``failing``) for two completed cycles: an episode at 2."""
        self.hub.fail = failing
        self.tick()
        self.tick()
        self.assertEqual(self.episodes("runs")["consecutive"], 2)

    def _raise_at_other(self, failing):
        self.hub.fail = ("%s/pulls?" % OTHER,) + tuple(failing)
        before = len(self.poller.raised)
        self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), before + 1, "the cycle did not raise")
        return self.episodes("runs")

    def test_a_status_listing_that_answers_does_not_heal_a_plain_listing_that_failed(self):
        """`runs` is one sub fed by three requests a repo. The plain listing fails in a
        raising cycle (the hole is recorded) while a status listing answers in the same repo: the
        failure outweighs the answer, so the episode is held at 2. The control, the plain listing
        answering too, restarts it at 0."""
        self._open_a_runs_episode((self.PLAIN_RUNS,))
        held = self._raise_at_other((self.PLAIN_RUNS,))
        self.assertEqual((held["open"], held["announced"], held["consecutive"]), (True, True, 2))
        healed = self._raise_at_other(())
        self.assertEqual((healed["open"], healed["consecutive"]), (True, 0))

    def test_a_by_id_answer_does_not_heal_a_plain_listing_that_failed(self):
        """The same with a by-id fetch as the answer: a run seen in flight and no longer listed (both
        listings fail) is fetched by id and answers; the failed listings still hold the episode."""
        run = self.hub.add_run(repo=REPO, status="in_progress", conclusion=None, created_at=self.clock.at(1))
        self.tick()                                        # listed in progress: tracked in flight
        run["_hidden"] = True                              # no longer listed, so fetched by id
        both = (self.PLAIN_RUNS, self.STATUS_RUNS)
        self._open_a_runs_episode(both)
        held = self._raise_at_other(both)
        self.assertEqual((held["open"], held["announced"], held["consecutive"]), (True, True, 2))
        self.assertIn("repos/%s/actions/runs/%s" % (REPO, run["id"]), self.hub.asked)
        healed = self._raise_at_other(())
        self.assertEqual((healed["open"], healed["consecutive"]), (True, 0))

    def test_after_a_restart_a_partial_entry_does_not_shrink_the_repos_that_must_answer(self):
        """The reviewer's probe. OTHER's reviews fail for two cycles (an episode at 2) and the daemon
        restarts, which forgets where the episode failed. The first raising cycle then fails REPO's
        reviews, which must not leave an entry of REPO alone: when REPO's reviews answer in the next
        raising cycle, OTHER's have not, so the episode is held at 2, not restarted at 0."""
        self.hub.listing[OTHER] = [_pr(2)]
        self.hub.fail = ("%s/pulls/2/reviews?" % OTHER,)
        self.tick()
        self.tick()
        self.assertEqual(self.episodes()["consecutive"], 2)
        self.restart()
        self.hub.fail = ("%s/pulls?" % OTHER, "%s/pulls/1/reviews?" % REPO)
        self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(self.episodes()["consecutive"], 2)
        self.hub.fail = ("%s/pulls?" % OTHER,)
        self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 2)
        held = self.episodes()
        self.assertEqual((held["open"], held["announced"], held["consecutive"]), (True, True, 2))

    def test_a_hole_that_opened_in_raising_cycles_is_healed_by_its_own_repos_answer(self):
        """The raising cycle remembers where the episode failed (`_remember_episode_repos`, early
        raise): REPO's reviews failed in the first, and answering in the second is all it takes. With
        nothing remembered every repo would have to answer, and OTHER's listing is what raised."""
        self.assertEqual(self.raising(), [])
        self.assertEqual(self.episodes()["consecutive"], 1)
        self.assertEqual(self.raising(reviews_fail=False), [])
        healed = self.episodes()
        self.assertEqual((healed["open"], healed["consecutive"]), (True, 0))

    def test_a_hole_whose_tail_raised_is_healed_by_its_own_repos_answer(self):
        """The same when the cycle that opened the episode raised INSIDE the degraded tail (a
        failed profile-log write): the tail's handler remembers where it failed."""
        self.write_fails_once("type=degraded sub=reviews")
        self.hub.fail = self.reviews_fail
        self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(self.episodes()["consecutive"], 1)
        self.assertEqual(self.raising(reviews_fail=False), [])
        healed = self.episodes()
        self.assertEqual((healed["open"], healed["consecutive"]), (True, 0))

    def test_a_hole_from_a_completed_cycle_is_healed_by_its_own_repos_answer(self):
        """The same when the cycle that opened the episode completed: the tail remembers where it failed."""
        self.assertEqual([self.plain(), self.plain()], [[self.DEGRADED], []])
        self.assertEqual(self.raising(reviews_fail=False), [])
        healed = self.episodes()
        self.assertEqual((healed["open"], healed["announced"], healed["consecutive"]), (True, True, 0))

    def test_a_sigterm_before_the_tail_is_counted_like_any_raise(self):
        """`Terminated` is not an `Exception`: it goes through the same accounting, then stops the cycle."""
        hub = self.hub
        armed = [True]

        def gh_api(path):
            if armed and path.startswith("repos/%s/pulls?" % OTHER):
                raise gw.Terminated()
            return hub(path)

        hub.fail = self.reviews_fail
        self.clock.advance(60)
        with mock.patch.object(gw, "gh_api", gh_api):
            with self.assertRaises(gw.Terminated):
                gw.poll_profile(self.poller.profile, self.poller.cfg, self.state)
        episode = self.episodes()
        self.assertEqual((episode["open"], episode["announced"], episode["consecutive"]), (True, False, 1))

    def test_nothing_of_the_cycle_is_left_on_the_state_or_saved(self):
        """The cycle's holes are put on the state while it runs: gone after a completed cycle, after
        one that raised before the tail, and never in the file."""
        self.plain()
        self.assertNotIn(gw._OPEN_CYCLE, self.state)
        self.raising()
        self.assertNotIn(gw._OPEN_CYCLE, self.state)
        self.assertNotIn(gw._OPEN_CYCLE, self.poller.saved())

    def test_a_raise_in_the_tail_is_not_counted_twice(self):
        """The tail accounts for itself: its raise, which is not an early one, counts a failure once."""
        self.write_fails_once("type=degraded sub=reviews")
        self.hub.fail = self.reviews_fail
        self.tick(tolerate=True)
        self.assertEqual(len(self.poller.raised), 1)
        self.assertEqual(self.episodes()["consecutive"], 1)


class TheHealRuleOnItsOwn(unittest.TestCase):
    """`_answered_pairs` and `_healed_subs`, without a cycle around them."""

    def holes(self, *failures):
        """A tracked `Cycle` with a ``runs`` failure recorded in each repo of ``failures`` (``None``
        standing for one recorded outside the repo loop)."""
        holes = gw._track_repos(gw._degraded.Cycle())
        for repo in failures:
            holes.repo = repo
            holes.record("runs", "a failure")
        return holes

    def test_a_pair_that_recorded_a_failure_is_not_answered(self):
        ok = set([(REPO, "runs"), (OTHER, "runs"), (REPO, "reviews")])
        self.assertEqual(gw._answered_pairs(ok, self.holes(REPO)), set([(OTHER, "runs"), (REPO, "reviews")]))

    def test_a_failure_outside_the_repo_loop_belongs_to_every_repo(self):
        ok = set([(REPO, "runs"), (OTHER, "runs"), (REPO, "reviews")])
        self.assertEqual(gw._answered_pairs(ok, self.holes(None)), set([(REPO, "reviews")]))

    def test_with_no_failure_every_pair_is_answered(self):
        ok = set([(REPO, "runs"), (OTHER, "reviews")])
        self.assertEqual(gw._answered_pairs(ok, self.holes()), ok)

    def test_an_episode_with_no_known_repos_needs_every_repo_to_have_answered(self):
        """The `or every` fallback: no entry, or one whose repos have all left the profile, asks every
        repo of the profile. A success in one repo of two heals nothing."""
        gone = {gw._EPISODE_REPOS: {"runs": set(["acme/gone"])}}
        for state in ({}, gone):
            self.assertEqual(gw._healed_subs(state, set([(OTHER, "runs")]), [REPO, OTHER]), set())
            self.assertEqual(gw._healed_subs(state, set([(REPO, "runs"), (OTHER, "runs")]), [REPO, OTHER]),
                             set(["runs"]))

    def test_an_episode_with_known_repos_needs_only_those(self):
        state = {gw._EPISODE_REPOS: {"runs": set([REPO])}}
        self.assertEqual(gw._healed_subs(state, set([(REPO, "runs")]), [REPO, OTHER]), set(["runs"]))
        self.assertEqual(gw._healed_subs(state, set([(OTHER, "runs")]), [REPO, OTHER]), set())


class ACheckpointThatCannotBeSavedIsTold(_FailedCycleThenRestart):
    """A checkpoint that cannot be saved was only noted in the daemon log, which nothing
    tails. It is told in the profile log too, at most once per renotify window, and its recovery
    once a checkpoint saves again. It is not a fetch, so never an `error: sub-fetch` line."""

    TOLD = "type=degraded sub=checkpoint detail="
    RECOVERED = "type=degraded_recovered sub=checkpoint"

    def cycle(self, saving, seconds=60):
        """A cycle that raises with 2 runs to checkpoint, the state file refusing the write unless
        ``saving``. Returns the profile-log lines."""
        self.burst(2)
        self.fail_listing(OTHER)
        broken = (contextlib.nullcontext() if saving else
                  mock.patch.object(gw.os, "replace", side_effect=PermissionError(13, "Permission denied")))
        with broken:
            lines = self.tick(seconds=seconds, tolerate=True)
        return lines

    def checkpoint_lines(self, lines):
        return [l for l in lines if "sub=checkpoint" in l]

    def test_a_failed_checkpoint_is_told_once_a_window_and_its_recovery_once(self):
        first = self.checkpoint_lines(self.cycle(saving=False))
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0].startswith(self.TOLD), first)
        self.assertIn("Permission denied", first[0])
        # Two more inside the window: the note is written each time, the line is not.
        self.assertEqual([self.checkpoint_lines(self.cycle(saving=False)) for _ in range(2)], [[], []])
        self.assertEqual(len([n for n in self.poller.notes if "the failed cycle's checkpoint was not saved" in n]), 3)
        # A window later it is told again.
        again = self.checkpoint_lines(self.cycle(saving=False, seconds=gw._degraded.DEFAULT_RENOTIFY_MIN * 60))
        self.assertEqual(len(again), 1)
        self.assertTrue(again[0].startswith(self.TOLD), again)
        # A checkpoint that saves: recovered, once.
        self.assertEqual(self.checkpoint_lines(self.cycle(saving=True)), [self.RECOVERED])
        self.assertEqual(self.checkpoint_lines(self.cycle(saving=True)), [])

    def test_a_completed_cycle_whose_save_succeeds_tells_the_recovery(self):
        """A streak of failed cycles that ends in completed ones never checkpoints again, so the
        completed cycle's own save, which proves the disk writes, tells the recovery: once."""
        self.assertEqual(len(self.checkpoint_lines(self.cycle(saving=False))), 1)
        self.hub.fail = ()
        self.assertEqual(self.checkpoint_lines(self.tick()), [self.RECOVERED])
        self.assertNotIn(gw._CHECKPOINT_TOLD, self.state)
        self.assertEqual(self.checkpoint_lines(self.tick() + self.tick()), [])

    def test_a_completed_cycle_that_cannot_save_tells_no_recovery(self):
        """The control: where the completed cycle's save fails too, nothing recovered yet."""
        self.assertEqual(len(self.checkpoint_lines(self.cycle(saving=False))), 1)
        self.hub.fail = ()
        with mock.patch.object(gw.os, "replace", side_effect=PermissionError(13, "Permission denied")):
            lines = self.tick(tolerate=True)
        self.assertEqual(self.checkpoint_lines(lines), [])
        self.assertIn(gw._CHECKPOINT_TOLD, self.state)

    def test_a_checkpoint_that_never_failed_says_nothing(self):
        """The control: a raising cycle whose checkpoint saves tells nothing about it."""
        lines = self.cycle(saving=True) + self.cycle(saving=True)
        self.assertEqual(self.checkpoint_lines(lines), [])
        self.assertEqual([l for l in self.poller.notes if "checkpoint" in l], [])

    def test_it_is_never_promoted_to_an_error_line(self):
        lines = []
        for _ in range(4):
            lines += self.cycle(saving=False)
        self.assertEqual([l for l in lines if l.startswith("error:")], [])
        self.assertEqual(self.state.get("degraded"), {})

    def test_what_was_told_is_kept_in_memory_only(self):
        self.cycle(saving=False)
        self.assertIn(gw._CHECKPOINT_TOLD, self.state)
        self.assertNotIn(gw._CHECKPOINT_TOLD, self.poller.saved())

    def test_a_profile_log_that_cannot_be_written_leaves_only_the_note(self):
        """The log's disk is the failing one too: the line is not written, the cycle's own error
        and the note stand, and with nothing told there is no recovery to tell."""
        poller = self.poller

        def emit(profile, line):
            if "sub=checkpoint" in line:
                raise OSError(28, "No space left on device")
            poller.emitted.append(line)

        with mock.patch.object(gw, "emit", emit):
            self.assertEqual(self.checkpoint_lines(self.cycle(saving=False)), [])
            self.assertEqual(len(poller.raised), 1)
            self.assertEqual(len([n for n in poller.notes if "the failed cycle's checkpoint was not saved" in n]), 1)
            self.assertEqual(self.checkpoint_lines(self.cycle(saving=True)), [])
        self.assertNotIn(gw._CHECKPOINT_TOLD, self.state)

    def test_a_line_that_could_not_be_written_is_tried_again_by_the_next_failure(self):
        """It is marked told only once written: not written, the next failed checkpoint inside the
        window tries again."""
        self.write_fails_once("type=degraded sub=checkpoint")
        self.assertEqual(self.checkpoint_lines(self.cycle(saving=False)), [])
        written = self.checkpoint_lines(self.cycle(saving=False))
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].startswith(self.TOLD), written)


# ---------------------------------------------------------------------------
# The daemon's memory between cycles:
# what the merge path already told, whose scheduled run a baseline belongs to, what a
# first run with a hole could not seed, and a state file whose values are not the shape
# the cycle reads.
# ---------------------------------------------------------------------------

class TheMergePathSeedsWhatItTold(unittest.TestCase):
    """A merged body with a readable trailer and an unreadable one: the merge path
    tells the unreadable one, and when the companion check fails the entry it leaves for the
    retry had no ``said`` list, so the retry, which re-reads the body, told it again. One false
    wake per such merge, measured at ``{'pair_trailer_unparsed #2 | Lands-with: junk': 2}`` on
    main. The entry now carries what the merge path wrote."""

    BODY = "Adds the ref input.\n\nLands-with: acme/Agent-Watchtower#8\nLands-with: junk\n"
    CLOSED = dict(number=569, state="closed", merged_at="2026-09-06T05:24:00Z", title="[PROJ-900] perf nightly ref")
    TOLD = "type=pair_trailer_unparsed repo=app #569 | Lands-with: junk"

    def _merge_then_retry(self, restart):
        closed = _pr(body=self.BODY, **self.CLOSED)
        with mock.patch.object(gw, "companion_merged", side_effect=RuntimeError("HTTP 502")):
            merged, state = _poll([_pr(569, body=self.BODY)], [closed])
        self.assertEqual(len(state["pair_pending"]), 1, "the failed check must leave the entry")
        if restart:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gw, "STATE_DIR", tmp):
                gw.save_state("acme", state)
                state = gw.load_state("acme")
        with mock.patch.object(gw, "companion_merged", lambda repo, num: False):
            retried, state = _poll(None, [closed], state=state)
        return merged, retried, state

    def _told_once(self, restart):
        merged, retried, state = self._merge_then_retry(restart)
        said = [l for l in merged + retried if l.startswith("type=pair_trailer_unparsed")]
        self.assertEqual(said, [self.TOLD], "merge: %s / retry: %s" % (merged, retried))
        self.assertIn("type=pair_window_open", [l.split()[0] for l in retried], retried)
        self.assertEqual(state["pair_pending"], [], "the readable companion answered: settled")

    def test_an_unreadable_trailer_told_at_merge_is_not_told_again_by_the_retry(self):
        self._told_once(restart=False)

    def test_an_unreadable_trailer_told_at_merge_is_not_told_again_after_a_restart(self):
        self._told_once(restart=True)


class _ScheduledBaselinePerRepo(object):
    """`wf_last` was keyed by workflow NAME across all of a profile's repos, so a
    scheduled success in one repo moved the baseline of another repo's same-named workflow:
    the other repo's real recovery was then suppressed as cron noise, and the first success
    was told as a recovery from a failure it never had. It is keyed by repo, then name.
    Mirrored: each repo is the one that recovers, in either repo order."""

    def scheduled(self, repo, conclusion, name="Nightly"):
        return self.hub.add_run(repo=repo, name=name, event="schedule", conclusion=conclusion,
                                created_at=self.clock.at(1))

    def _recovers_beside_a_passing_twin(self, subject):
        other = [r for r in self.repos if r != subject][0]
        failed = self.scheduled(subject, "failure")
        lines = self.tick()
        self.scheduled(other, "success")                   # passing all along: cron noise, suppressed
        lines += self.tick()
        fixed = self.scheduled(subject, "success")         # the recovery
        lines += self.tick()
        self.assertEqual(_run_ids(lines), [failed["id"], fixed["id"]], _typed(lines, "type=run "))
        self.assertEqual(self.state["wf_last"], {subject: {"Nightly": "success"}, other: {"Nightly": "success"}})

    def test_a_scheduled_success_in_one_repo_does_not_suppress_the_first_repos_recovery(self):
        self._recovers_beside_a_passing_twin(self.repos[0])

    def test_a_scheduled_success_in_one_repo_does_not_suppress_the_second_repos_recovery(self):
        self._recovers_beside_a_passing_twin(self.repos[1])


class ScheduledBaselinePerRepo(_ScheduledBaselinePerRepo, _ClockedRuns):
    repos = (REPO, OTHER)


class ScheduledBaselinePerRepoMirrored(_ScheduledBaselinePerRepo, _ClockedRuns):
    repos = (OTHER, REPO)


class AScheduledBaselineKeyedByName(_ClockedRuns):
    """The `wf_last` migration. A file written before it keyed `wf_last` by name alone, and cannot
    say which repo wrote an entry: the last scheduled run of that name in ANY of the profile's
    repos. Every repo's next comparison under the old code was against that value, so the load
    copies it to every repo the file records (its `prs` keys: every repo the daemon ever
    polled), and each repo's first comparison is the one the old code would have made. A
    name-keyed entry beside per-repo ones is what the previous code wrote after a rollback, the
    newest value for that name, so it is copied over them."""

    repos = (REPO, OTHER)

    def scheduled(self, repo, conclusion, name="Nightly"):
        return self.hub.add_run(repo=repo, name=name, event="schedule", conclusion=conclusion,
                                created_at=self.clock.at(1))

    def _restart_with(self, wf_last):
        raw = self.poller.saved()
        self.assertEqual(sorted(raw["prs"]), sorted(self.repos))
        raw["wf_last"] = wf_last
        with open(self.poller.state_path, "w") as fh:
            json.dump(raw, fh)
        self.state = self.poller.load()
        return self.state

    def test_a_name_keyed_failure_is_still_recovered_from_in_every_repo(self):
        self._restart_with({"Nightly": "failure"})
        made = [self.scheduled(REPO, "success"), self.scheduled(OTHER, "success")]
        self.assertEqual(_run_ids(self.tick()), sorted(r["id"] for r in made),
                         "a recovery the old key knew of was suppressed")
        self.assertEqual(self.state["wf_last"], {REPO: {"Nightly": "success"}, OTHER: {"Nightly": "success"}})

    def test_the_load_copies_a_name_keyed_entry_to_every_repo_the_file_records(self):
        state = self._restart_with({"Nightly": "failure", "Weekly": "success",
                                    REPO: {"Nightly": "success", "Docs": "failure"}})
        self.assertEqual(state["wf_last"], {REPO: {"Nightly": "failure", "Weekly": "success", "Docs": "failure"},
                                            OTHER: {"Nightly": "failure", "Weekly": "success"}})

    def test_the_file_is_saved_keyed_by_repo_and_reloads_unchanged(self):
        self._restart_with({"Nightly": "failure"})
        self.tick()
        want = {REPO: {"Nightly": "failure"}, OTHER: {"Nightly": "failure"}}
        self.assertEqual(self.poller.saved()["wf_last"], want)
        self.assertEqual(self.poller.load()["wf_last"], want)


class AFirstRunWithAHole(unittest.TestCase):
    """A first run seeds silently what it reads, and what one of its
    sub-fetches could not read was left unseeded, so the next cycle announced it as news.

    * A PR's reviews are read with no `since`, so the next cycle announced up to 20 old
      reviews of an open PR whose reviews fetch failed. The PR is remembered
      (`reviews_unseeded`, saved with the state), and the first time its reviews answer, those
      submitted at or before the first run's start are seeded silently; a later one is news.
    * The comments window is the watermark less 5 minutes, so the cycle after a first
      run whose comments fetch failed announced the comments made in the 5 minutes before the
      first run. No comments window reaches behind the first run's start now (the floor the runs
      page-back already had, `runs_baseline_at`)."""

    def setUp(self):
        self.clock = _Clock().install(self)
        self.hub = _Hub(clock=self.clock)
        self.poller = _Poller(self, self.hub)
        self.hub.listing[REPO] = [_pr(1), _pr(2)]
        for n in ("1", "2"):
            self.hub.reviews[(REPO, n)] = [self.hub.new_review(submitted_at=self.clock.at(-86400 + i),
                                                               body="old review %s.%d" % (n, i))
                                           for i in range(3)]
        self.state = self.poller.load()
        self.start = self.clock.at(0)

    def first_run(self, fail):
        self.hub.fail = fail
        lines = self.poller.poll(self.state)
        self.hub.fail = ()
        self.assertFalse(self.state["first_run"])
        self.assertEqual(_news(lines), [], "a first run announced something")
        return lines

    def tick(self, seconds=60, fail=()):
        self.clock.advance(seconds)
        self.hub.fail = fail
        try:
            return self.poller.poll(self.state)
        finally:
            self.hub.fail = ()

    def review(self, n, seconds, body):
        made = self.hub.new_review(submitted_at=self.clock.at(seconds), body=body)
        self.hub.reviews[(REPO, n)].append(made)
        return made

    def comment(self, seconds, body):
        return self.hub.add_comment(created_at=self.clock.at(seconds), updated_at=self.clock.at(seconds), body=body)

    def restart(self):
        self.state = self.poller.load()

    @staticmethod
    def ids(lines, kind):
        return sorted(int(re.search(r" id=(\d+) ", l).group(1)) for l in _typed(lines, kind))

    # first-run reviews ------------------------------------------------------------------------------

    def _reviews_seeded_on_first_answer(self, restart=False, failing_cycles=0):
        self.first_run(("/pulls/1/reviews?",))
        new = self.review("1", 30, "made after the first run")
        lines = []
        for _ in range(failing_cycles):
            lines += self.tick(fail=("/pulls/1/reviews?",))
        if restart:
            self.restart()
        lines += self.tick()
        self.assertEqual(self.ids(lines, "type=review "), [new["id"]], _typed(lines, "type=review "))
        self.assertEqual(self.state.get("reviews_unseeded"), {}, "the PR answered: nothing left to seed")
        seeded = [n for n in self.poller.notes if "seeded 3 silently" in n]
        self.assertEqual(len(seeded), 1, self.poller.notes)
        self.assertEqual(self.ids(self.tick(), "type=review "), [])

    def test_the_old_reviews_of_a_pr_the_first_run_could_not_read_are_not_announced(self):
        self._reviews_seeded_on_first_answer()

    def test_the_old_reviews_of_a_pr_the_first_run_could_not_read_are_not_announced_after_a_restart(self):
        self._reviews_seeded_on_first_answer(restart=True)

    def test_the_old_reviews_stay_unannounced_while_the_fetch_keeps_failing(self):
        self._reviews_seeded_on_first_answer(failing_cycles=3)

    def test_the_old_reviews_stay_unannounced_across_a_restart_inside_the_failures(self):
        self._reviews_seeded_on_first_answer(restart=True, failing_cycles=2)

    def test_a_review_that_cannot_be_proven_old_is_announced(self):
        self.first_run(("/pulls/1/reviews?",))
        undated = self.review("1", -3600, "no timestamp")
        undated["submitted_at"] = None
        self.assertEqual(self.ids(self.tick(), "type=review "), [undated["id"]])

    def test_only_the_pr_whose_fetch_failed_is_remembered_and_with_the_first_runs_start(self):
        self.first_run(("/pulls/2/reviews?",))
        self.assertEqual(self.state["reviews_unseeded"], {REPO: {"2": gw._parse_ts(self.start).isoformat()}})
        self.assertEqual(self.poller.saved()["reviews_unseeded"], self.state["reviews_unseeded"])
        other = self.review("1", 30, "news on the PR that answered")
        self.assertEqual(self.ids(self.tick(), "type=review "), [other["id"]])

    # first-run comments ------------------------------------------------------------------------------

    def _comments_before_the_first_run_stay_unannounced(self, restart=False):
        self.comment(-180, "three minutes before the first run")
        self.comment(-480, "eight minutes before the first run")
        self.first_run(("/issues/comments?",))
        new = self.comment(30, "made after the first run")
        if restart:
            self.restart()
        lines = self.tick()
        self.assertEqual(self.ids(lines, "type=comment "), [new["id"]], _typed(lines, "type=comment "))

    def test_comments_made_just_before_a_first_run_whose_comments_fetch_failed_are_not_announced(self):
        self._comments_before_the_first_run_stay_unannounced()

    def test_comments_made_just_before_a_first_run_whose_comments_fetch_failed_are_not_announced_after_a_restart(self):
        self._comments_before_the_first_run_stay_unannounced(restart=True)

    def test_the_comments_window_starts_no_earlier_than_the_first_run_and_then_overlaps_as_before(self):
        self.first_run(())
        windows = []
        for _ in range(8):
            before = len(self.hub.asked)
            self.tick()
            asked = [p for p in self.hub.asked[before:] if "/issues/comments?" in p]
            windows.append(re.search(r"since=([^&]+)", asked[0]).group(1))
        since = [gw._parse_ts(w) for w in windows]
        start = gw._parse_ts(self.start)
        self.assertEqual(since[:6], [start] * 6, "the window reached behind the first run")
        self.assertEqual(since[6:], [start + timedelta(seconds=60), start + timedelta(seconds=120)],
                         "the floor must stop binding once the overlap is past it")


class TheStateFileLoadsWhateverItHolds(_ClockedRuns):
    """The same problem in the older keys. `load_state` passed `prs`, `degraded` and
    `runs_inflight` through as the file held them, and `wf_last`, `pair_pending` and `since`
    too: a value that is not the shape the cycle reads (a null or a list where an object
    belongs, an entry of the wrong type, a timestamp at the edge of the date range) raised in
    every cycle for as long as the file stayed so, and the profile was never polled again. No
    version writes one. Each is read like `runs_from` now: what is not the expected shape is
    dropped, and the cycle runs."""

    TOP = (None, [], ["x"], "x", 5)
    BODY = "Lands-with: acme/other#3\n  Lands-with: junk\n"

    def setUp(self):
        _ClockedRuns.setUp(self)
        self.hub.listing[REPO] = [_pr(1, body=self.BODY)]
        hub = self.hub

        def gh(path):                                      # the pair retry re-reads the origin PR
            one = re.match(r"repos/([^/]+/[^/]+)/pulls/(\d+)$", path)
            if one:
                return [pr for pr in hub.listing[one.group(1)] if str(pr["number"]) == one.group(2)][0]
            return hub(path)
        for patch in (mock.patch.object(gw, "gh_api", gh),
                      mock.patch.object(gw, "companion_merged", lambda repo, num: True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.tick()                                        # PR 1 is on record

    def _reload_with(self, key, value):
        raw = self.poller.saved()
        raw[key] = value
        with open(self.poller.state_path, "w") as fh:
            json.dump(raw, fh)
        return self.poller.load()

    def _every_cycle_still_runs(self, key, shapes, fail=()):
        for shape in shapes:
            state = self._reload_with(key, shape)
            self.assertFalse(state["first_run"], "%s=%r loaded as a first run" % (key, shape))
            made = self.burst(3)
            self.hub.fail = fail
            try:
                lines = self.tick(state=state)             # raised here, every cycle, before
            finally:
                self.hub.fail = ()
            self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made), "%s=%r" % (key, shape))
            self.assertIsInstance(state[key], list if key == "pair_pending" else dict, "%s=%r" % (key, shape))

    def test_a_malformed_prs_loads_as_no_memory_of_those_prs(self):
        per_repo = ({REPO: None}, {REPO: []}, {REPO: "x"}, {REPO: 5})
        per_pr = ({REPO: {"1": "x"}}, {REPO: {"1": []}}, {REPO: {"1": {}}}, {REPO: {"1": None}},
                  {REPO: {"1": {"head": "abc123456"}}}, {REPO: {"1": {"state": "open"}}})
        self._every_cycle_still_runs("prs", self.TOP + per_repo + per_pr)

    def test_a_malformed_degraded_loads_as_no_episode(self):
        inf = float("inf")
        per_sub = ({"reviews": {"last": "x", "open": True}}, {"reviews": {"last": None}},
                   {"reviews": {"last": 5, "last_silenced": "x"}}, {"reviews": {"last": 5, "consecutive": inf}},
                   {"reviews": {"last": 5, "suppressed": inf}}, {"reviews": inf}, {"reviews": [1]},
                   {"reviews": "x"}, {"reviews": None},
                   # a JSON integer too big for a float loads as an int that
                   # every cycle's arithmetic raised OverflowError on.
                   {"reviews": {"last": 10 ** 400, "open": True}},
                   {"reviews": {"last": 5, "last_silenced": -(10 ** 400)}})
        self._every_cycle_still_runs("degraded", self.TOP + per_sub, fail=("/issues/comments?",))

    def test_a_malformed_runs_inflight_loads_as_no_tracked_run(self):
        per_repo = ({REPO: None}, {REPO: []}, {REPO: "x"}, {REPO: 5})
        per_run = ({REPO: {"abc": 1.0}}, {REPO: {"5": "x"}}, {REPO: {"5": None}}, {REPO: {"5": [1]}},
                   {REPO: {"5": True}}, {REPO: {"5": 1.0, "6": "x"}},
                   {REPO: {"5": 10 ** 400}}, {REPO: {"5": -(10 ** 400)}}, {REPO: {"5": float("inf")}})
        self._every_cycle_still_runs("runs_inflight", self.TOP + per_repo + per_run)

    def test_a_malformed_wf_last_loads_as_no_scheduled_baseline(self):
        per_repo = ({REPO: None}, {REPO: []}, {REPO: 5}, {REPO: {"Nightly": 5}})
        self._every_cycle_still_runs("wf_last", self.TOP + per_repo)

    def test_a_malformed_pair_pending_loads_as_no_pending_check(self):
        shapes = ({"a": 1}, "x", 5, [5], ["x"], [None], [{"repo": REPO}],
                  [{"repo": REPO, "number": "1", "title": "t", "done": None}],
                  [{"repo": REPO, "number": "1", "title": "t", "done": [], "said": None}],
                  [{"repo": REPO, "number": "1", "title": "t", "done": "x", "said": 5}])
        self._every_cycle_still_runs("pair_pending", (None, []) + shapes)

    def test_a_malformed_watermark_is_told_and_the_cycle_runs(self):
        for since in (5, True, [], {"a": 1}, "0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00+05:00",
                      "9999-12-31T23:59:59-05:00"):
            state = self._reload_with("since", since)
            made = self.burst(3)
            before = len(self.poller.notes)
            lines = self.tick(state=state)
            self.assertEqual(_run_ids(lines), sorted(r["id"] for r in made), "since=%r" % (since,))
            self.assertTrue([n for n in self.poller.notes[before:] if "does not parse" in n], "since=%r" % (since,))
            self.assertEqual(gw._parse_ts(state["since"]), self.clock.now, "since=%r was not moved on" % (since,))

    def test_a_hold_or_a_floor_at_the_edge_of_the_date_range_is_dropped(self):
        """`_runs_from`'s docstring left this open: a held start the cycle cannot subtract
        its margin from, or one whose UTC conversion leaves the date range, raised every cycle."""
        edges = ("0001-01-01T00:00:00+00:00", "0001-01-01T00:01:59+00:00", "0001-01-01T00:00:00+05:00",
                 "9999-12-31T23:59:59-05:00")
        for edge in edges:
            state = self._reload_with("runs_from", {REPO: edge})
            made = self.burst(3)
            self.assertEqual(_run_ids(self.tick(state=state)), sorted(r["id"] for r in made), edge)
            self.assertEqual(state["runs_from"], {}, edge)
        for edge in edges:
            state = self._reload_with("runs_baseline_at", edge)
            made = self.burst(3)
            self.assertEqual(_run_ids(self.tick(state=state)), sorted(r["id"] for r in made), edge)
        self.assertIsNone(gw._parse_ts("0001-01-01T00:00:00+05:00"))
        self.assertIsNone(gw._parse_ts("9999-12-31T23:59:59-05:00"))
        self.assertEqual(gw._parse_ts("2026-09-30T20:40:00Z"), datetime(2026, 9, 30, 20, 40, tzinfo=timezone.utc))

    def test_good_entries_beside_bad_ones_are_kept(self):
        pr = {"head": "abc123456", "state": "open"}
        state = self._reload_with("prs", {REPO: {"1": pr, "2": "x"}, OTHER: None})
        self.assertEqual(state["prs"], {REPO: {"1": pr}})
        episode = {"last": 5.0, "open": True, "announced": True, "suppressed": 0, "last_silenced": 0,
                   "consecutive": 1}
        state = self._reload_with("degraded", {"reviews": episode, "comments": {"last": "x"}, "runs": 7})
        self.assertEqual(state["degraded"], {"reviews": episode, "runs": 7})
        state = self._reload_with("runs_inflight", {REPO: {"7": 1.5, "x": 2.0, "8": "y"}, OTHER: None})
        self.assertEqual(state["runs_inflight"], {REPO: {"7": 1.5}})
        state = self._reload_with("wf_last", {REPO: {"Nightly": "failure", "Bad": 5}, OTHER: None})
        self.assertEqual(state["wf_last"], {REPO: {"Nightly": "failure"}})
        good = {"repo": REPO, "number": "1", "title": "t", "done": ["x#1"], "said": ["junk"]}
        state = self._reload_with("pair_pending", [good, 5, {"repo": REPO}, dict(good, done=None)])
        self.assertEqual(state["pair_pending"], [good, dict(good, done=[])])
        state = self._reload_with("reviews_unseeded", {REPO: {"1": self.clock.at(-60), "2": "x", "3": 5}, OTHER: []})
        self.assertEqual(state["reviews_unseeded"], {REPO: {"1": self.clock.at(-60)}})


#: The daemon's own `emit` and `note`, taken at import: `_Poller` replaces both on the module while a
#: test runs, and the point here is what the real ones do with a line they cannot encode.
_REAL_EMIT, _REAL_NOTE = gw.emit, gw.note


class AnUnbuildableLineCostsNoCycle(_ClockedRuns):
    """An item whose line cannot be built used to raise inside `poll_profile`, and because
    each record follows its emit, the same item raised again every cycle. The line is now buildable:
    `one_line` coerces a non-string and replaces a lone surrogate, `_login` reads a login only from a
    dict with a string `login`, and `emit` / `note` cannot raise on a surrogate in a field that does
    not pass through `one_line`. The item is announced with `?` or an empty field where the bad one
    was, and the cycle completes."""

    SHORT = REPO.split("/")[-1]

    def _bad_pr(self, number, **fields):
        pr = _pr(number)
        pr.update(fields)
        return pr

    def test_one_line_coerces_a_non_string_and_replaces_a_lone_surrogate(self):
        self.assertEqual(gw.one_line(7), "7")
        self.assertEqual(gw.one_line(2.5), "2.5")
        for empty in (None, [], {}, ["x"], {"a": 1}, ("t",), b"bytes"):
            self.assertEqual(gw.one_line(empty), "", "%r was not turned into an empty field" % (empty,))
        self.assertEqual(gw.one_line("a\ud800b"), "a?b")
        self.assertEqual(gw.one_line("  two\n\tlines  "), "two lines")
        self.assertEqual(gw.one_line("x" * 20, 10), "x" * 9 + "…")

    def test_login_is_read_only_from_a_dict_with_a_string_login(self):
        self.assertEqual(gw._login({"login": "bob"}), "bob")
        for bad in (None, "bob", ["bob"], 7, {}, {"login": None}, {"login": 7},
                    {"login": ["x"]}, {"login": {"a": 1}}):
            self.assertEqual(gw._login(bad), "?", "%r was read as a login" % (bad,))

    def test_a_whole_cycle_with_a_bad_field_in_every_kind_of_item(self):
        before = self.state["since"]
        pr = self._bad_pr(1, title=7, user=["x"])
        self.hub.listing[REPO] = [pr]
        review = self.hub.new_review(body=7)
        self.hub.reviews[(REPO, "1")] = [review]
        comment = self.hub.add_comment(body=7)
        run = self.hub.add_run(display_title=7, created_at=self.clock.at(1))
        lines = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [], "an item whose line could not be built raised the cycle")
        self.assertEqual(sorted(lines), sorted([
            "type=pr_opened repo=%s #1 by=? | 7" % self.SHORT,
            "type=review repo=%s #1 id=%s by=bob COMMENTED | 7" % (self.SHORT, review["id"]),
            "type=comment repo=%s #7 id=%s by=bob | 7" % (self.SHORT, comment["id"]),
            "type=run repo=%s run=%d.1 CI@main: success | 7" % (self.SHORT, run["id"])]))
        self.assertNotEqual(self.state["since"], before, "the watermark did not move")
        self.assertEqual(self.poller.saved()["since"], self.state["since"])
        again = self.tick()
        self.assertEqual([l for l in again if l.startswith(("type=pr_opened", "type=review", "type=comment"))],
                         [], "an item that cost a cycle was announced again")
        self.assertEqual(_run_ids(again), [], "a run that cost a cycle was announced again")

    def test_every_bad_shape_of_a_title_and_a_user_is_announced_once(self):
        titles = [7, 2.5, [1], {"a": 1}, None]
        users = ["x", ["x"], {"login": {"a": 1}}, {"login": ["x"]}, {"login": 7}, None]
        prs = [self._bad_pr(10 + i, title=t) for i, t in enumerate(titles)]
        prs += [self._bad_pr(30 + i, user=u) for i, u in enumerate(users)]
        self.hub.listing[REPO] = prs
        lines = [l for l in self.tick(tolerate=True) if l.startswith("type=pr_opened")]
        self.assertEqual(self.poller.raised, [])
        self.assertEqual(len(lines), len(prs))
        self.assertEqual(len(set(lines)), len(prs))
        by_number = dict((int(l.split(" #")[1].split(" ")[0]), l) for l in lines)
        for i, expected in enumerate(["7", "2.5", "", "", ""]):
            self.assertEqual(by_number[10 + i], "type=pr_opened repo=%s #%d by=bob | %s" % (
                self.SHORT, 10 + i, expected))
        for i in range(len(users)):
            self.assertEqual(by_number[30 + i], "type=pr_opened repo=%s #%d by=? | t" % (self.SHORT, 30 + i))

    def test_a_review_and_a_comment_with_a_bad_user_or_body_are_announced_once(self):
        self.hub.listing[REPO] = [_pr(1)]
        users = ["x", ["x"], {"login": {"a": 1}}, None]
        reviews = [self.hub.new_review(user=u, body=[1]) for u in users]
        self.hub.reviews[(REPO, "1")] = reviews
        comments = [self.hub.add_comment(user=u, body={"a": 1}) for u in users]
        lines = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [])
        told = [l for l in lines if l.startswith(("type=review", "type=comment"))]
        self.assertEqual(len(told), 2 * len(users))
        for r in reviews:
            self.assertIn("type=review repo=%s #1 id=%s by=? COMMENTED | " % (self.SHORT, r["id"]), told)
        for c in comments:
            self.assertIn("type=comment repo=%s #7 id=%s by=? | " % (self.SHORT, c["id"]), told)
        self.assertEqual([l for l in self.tick() if l.startswith(("type=review", "type=comment"))], [])

    def test_a_lone_surrogate_in_a_run_name_or_branch_costs_no_cycle(self):
        run = self.hub.add_run(name="CI\ud800x", head_branch="br\udfffanch", created_at=self.clock.at(1))
        patch = mock.patch.object(gw, "emit", _REAL_EMIT)      # the cycle writes its profile log for real,
        patch.start()                                          # into _Poller's scratch LOG_DIR
        self.addCleanup(patch.stop)
        self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [])
        path = os.path.join(gw.LOG_DIR, "acme-github.log")
        self.assertTrue(path.startswith(tempfile.gettempdir()), path)
        with open(path, "rb") as fh:
            written = fh.read().decode("utf-8").splitlines()
        self.assertEqual(len(written), 1, written)
        self.assertTrue(written[0].startswith("type=run repo=%s run=%d.1 CI?x@br?anch: success | run " % (
            self.SHORT, run["id"])), written[0])
        self.assertEqual(self.tick(), [])

    def test_a_lone_surrogate_goes_through_the_real_emit(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        logs = os.path.join(tmp, "logs")
        with mock.patch.object(gw, "LOG_DIR", logs):
            _REAL_EMIT("scratch", "type=run repo=r run=1.1 CI\ud800x@br\udfffanch: success | ordinary")
            _REAL_EMIT("scratch", "type=comment repo=r #1 id=2 by=bob | plain")
        with open(os.path.join(logs, "scratch-github.log"), "rb") as fh:
            raw = fh.read().decode("utf-8")
        first, second = raw.splitlines()
        self.assertTrue(first.startswith("type=run repo=r run=1.1 CI?x@br?anch: success | ordinary ts="), first)
        self.assertTrue(second.startswith("type=comment repo=r #1 id=2 by=bob | plain ts="), second)

    def test_note_prints_the_repr_of_a_line_the_stream_cannot_encode(self):
        import io
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="\n")
        with mock.patch.object(sys, "stdout", out):
            _REAL_NOTE("fine")
            _REAL_NOTE("bad \ud800 line")
            out.flush()
        text = raw.getvalue().decode("utf-8")
        self.assertEqual(text.splitlines()[0], "fine")
        self.assertIn(repr("bad \ud800 line"), text)
        out.detach()

    def test_a_note_that_fails_for_another_reason_still_raises(self):
        with mock.patch("builtins.print", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                _REAL_NOTE("x")

    def test_an_ordinary_item_is_announced_exactly_as_before(self):
        self.hub.listing[REPO] = [_pr(1, title="Fix  the\nthing")]
        review = self.hub.new_review(body="LGTM  now")
        self.hub.reviews[(REPO, "1")] = [review]
        comment = self.hub.add_comment(body="a\tcomment")
        run = self.hub.add_run(display_title="run title", created_at=self.clock.at(1))
        self.assertEqual(sorted(self.tick()), sorted([
            "type=pr_opened repo=%s #1 by=bob | Fix the thing" % self.SHORT,
            "type=review repo=%s #1 id=%s by=bob COMMENTED | LGTM now" % (self.SHORT, review["id"]),
            "type=comment repo=%s #7 id=%s by=bob | a comment" % (self.SHORT, comment["id"]),
            "type=run repo=%s run=%d.1 CI@main: success | run title" % (self.SHORT, run["id"])]))

    def test_a_pr_body_that_is_not_a_string_is_announced_once_and_merged_once(self):
        bodies = {1: 7, 2: ["Lands-with: #9"], 3: {"a": 1}, 4: 2.5}
        self.hub.listing[REPO] = [self._bad_pr(n, body=b) for n, b in sorted(bodies.items())]
        opened = [l for l in self.tick(tolerate=True) if l.startswith("type=pr_opened")]
        self.assertEqual(self.poller.raised, [], "a PR whose body is not a string raised the cycle")
        self.assertEqual(sorted(opened), sorted(
            "type=pr_opened repo=%s #%d by=bob | t" % (self.SHORT, n) for n in bodies))
        again = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [])
        self.assertEqual([l for l in again if l.startswith("type=pr_")], [], "a PR was announced again")
        self.hub.listing[REPO] = [self._bad_pr(n, body=b, state="closed", merged_at=self.clock.at(0))
                                  for n, b in sorted(bodies.items())]
        merged = [l for l in self.tick(tolerate=True) if l.startswith("type=pr_")]
        self.assertEqual(self.poller.raised, [], "a merged PR whose body is not a string raised the cycle")
        self.assertEqual(sorted(merged), sorted(
            "type=pr_merged repo=%s #%d | t" % (self.SHORT, n) for n in bodies))
        self.assertEqual(self.state["pair_pending"], [])
        again = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [])
        self.assertEqual([l for l in again if l.startswith("type=pr_")], [], "a merge was announced again")

    def test_a_pr_head_that_is_not_a_dict_costs_no_cycle(self):
        heads = ["abc", ["x"], 7, {"sha": 7}, {"sha": None}, {"sha": ["a"]}]
        self.hub.listing[REPO] = [self._bad_pr(10 + i, head=h) for i, h in enumerate(heads)]
        lines = [l for l in self.tick(tolerate=True) if l.startswith("type=pr_")]
        self.assertEqual(self.poller.raised, [], "a PR whose head is not an object raised the cycle")
        self.assertEqual(sorted(lines), sorted(
            "type=pr_opened repo=%s #%d by=bob | t" % (self.SHORT, 10 + i) for i in range(len(heads))))
        again = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [])
        self.assertEqual([l for l in again if l.startswith("type=pr_")], [])
        self.hub.listing[REPO] = [self._bad_pr(10, head={"sha": "feedface1" + "0" * 31})]
        pushed = [l for l in self.tick(tolerate=True) if l.startswith("type=pr_")]
        self.assertEqual(self.poller.raised, [])
        self.assertEqual(pushed, ["type=pr_push repo=%s #10 head=feedface1 (was )" % self.SHORT])

    def test_a_state_a_run_name_a_branch_and_a_login_holding_a_newline_give_one_line(self):
        pr = self._bad_pr(1, user={"login": "a\nb"})
        self.hub.listing[REPO] = [pr]
        review = self.hub.new_review(state="COMMENTED\ntype=forged repo=x")
        other = self.hub.new_review(user={"login": "c d"})
        self.hub.reviews[(REPO, "1")] = [review, other]
        comment = self.hub.add_comment(user={"login": "e\nf"})
        run = self.hub.add_run(name="CI\nfake", head_branch="a b", created_at=self.clock.at(1))
        lines = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [])
        for line in lines:
            self.assertEqual(len(line.splitlines()), 1, "%r is more than one line" % (line,))
        self.assertEqual(sorted(lines), sorted([
            "type=pr_opened repo=%s #1 by=a b | t" % self.SHORT,
            "type=review repo=%s #1 id=%s by=bob COMMENTED type=forged repo=x | review %d" % (
                self.SHORT, review["id"], review["id"]),
            "type=review repo=%s #1 id=%s by=c d COMMENTED | review %d" % (
                self.SHORT, other["id"], other["id"]),
            "type=comment repo=%s #7 id=%s by=e f | comment %d" % (self.SHORT, comment["id"], comment["id"]),
            "type=run repo=%s run=%d.1 CI fake@a b: success | run %d" % (self.SHORT, run["id"], run["id"])]))

    def test_note_does_not_raise_on_a_stream_that_cannot_encode_a_character(self):
        import io
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="\n")
        line = "café 中 \ud800 line"
        with mock.patch.object(sys, "stdout", out):
            _REAL_NOTE("fine")
            _REAL_NOTE(line)
            out.flush()
        text = raw.getvalue().decode("ascii")
        self.assertEqual(text.splitlines()[0], "fine")
        self.assertEqual(text.splitlines()[1], ascii(line))
        out.detach()


class AWorkflowRunNameThatIsNotAStringCostsNoCycle(_ClockedRuns):
    """`wf_last` is keyed by the run's `name`, and a list or a dict is not hashable: the
    run raised the cycle before it was recorded, and every later run of the repo and every later repo
    waited behind it. The key is the name when it is a string and the empty string otherwise."""

    repos = (REPO, OTHER)
    BAD_NAMES = (["CI"], {"a": 1})

    def _lines_of(self, lines, repo):
        return [l for l in lines if l.startswith("type=run ") and " repo=%s " % repo.split("/")[-1] in l]

    def _check(self, bad_name):
        bad = self.hub.add_run(name=bad_name, created_at=self.clock.at(1))
        later = self.hub.add_run(name="CI", created_at=self.clock.at(2))
        elsewhere = self.hub.add_run(repo=OTHER, name="CI", created_at=self.clock.at(3))
        lines = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [], "a run whose name is %r raised the cycle" % (bad_name,))
        self.assertEqual(_run_ids(self._lines_of(lines, REPO)), sorted([bad["id"], later["id"]]))
        self.assertEqual(_run_ids(self._lines_of(lines, OTHER)), [elsewhere["id"]])
        self.assertEqual(self.state["since"], self.poller.saved()["since"])
        again = self.tick(tolerate=True)
        self.assertEqual(self.poller.raised, [])
        self.assertEqual(_news(again), [], "a run was announced again")
        return bad

    def test_a_list_name_costs_no_cycle(self):
        self._check(["CI"])

    def test_a_dict_name_costs_no_cycle(self):
        self._check({"a": 1})

    def test_the_scheduled_baseline_of_a_bad_name_is_the_empty_key(self):
        for bad_name in self.BAD_NAMES:
            self.hub.add_run(name=bad_name, event="schedule", conclusion="failure", created_at=self.clock.at(1))
            self.tick(tolerate=True)
            self.assertEqual(self.poller.raised, [])
            self.assertEqual(self.state["wf_last"][REPO][""], "failure")
            self.assertEqual(self.poller.saved()["wf_last"][REPO][""], "failure")
            self.assertEqual(self.poller.load()["wf_last"][REPO][""], "failure")

    def test_a_string_name_keeps_its_own_spelling_as_the_key(self):
        self.hub.add_run(name="Perf nightly", event="schedule", conclusion="failure", created_at=self.clock.at(1))
        self.tick()
        self.assertEqual(self.state["wf_last"][REPO]["Perf nightly"], "failure")
        self.assertNotIn("", self.state["wf_last"][REPO])


class AFirstRunWithARunNameThatIsNotAStringCostsNoCycle(unittest.TestCase):
    """The same, on the first run (the silent baseline) rather than a steady-state cycle."""

    def setUp(self):
        self.clock = _Clock().install(self)
        self.hub = _Hub(repos=(REPO, OTHER), clock=self.clock)
        self.poller = _Poller(self, self.hub)
        self.state = self.poller.load()

    def _check(self, bad_name):
        self.hub.add_run(name=bad_name, created_at=self.clock.at(-3600))
        self.poller.poll(self.state, tolerate=True)
        self.assertEqual(self.poller.raised, [], "a first run raised on a run whose name is %r" % (bad_name,))
        self.assertFalse(self.state["first_run"])
        self.clock.advance(60)
        later = self.hub.add_run(name="CI", created_at=self.clock.at(1))
        elsewhere = self.hub.add_run(repo=OTHER, name="CI", created_at=self.clock.at(2))
        self.clock.advance(60)
        lines = self.poller.poll(self.state, tolerate=True)
        self.assertEqual(self.poller.raised, [])
        self.assertEqual(_run_ids(lines), sorted([later["id"], elsewhere["id"]]))

    def test_a_list_name(self):
        self._check(["CI"])

    def test_a_dict_name(self):
        self._check({"a": 1})


class ASuppressedAuthorIsMatchedOnTheRawLogin(_ClockedRuns):
    """`suppress_authors` is compared with the login GitHub sent, not the copy shown on
    the line: a login that only becomes `alice` once flattened is a different login, and a
    login cut short for display still matches its own entry."""

    SHORT = REPO.split("/")[-1]

    def _review_lines(self, login):
        self.hub.listing[REPO] = [_pr(1)]
        self.hub.reviews[(REPO, "1")] = [self.hub.new_review(user={"login": login},
                                                              submitted_at=self.clock.at(30))]
        return [l for l in self.tick(tolerate=True) if l.startswith("type=review")]

    def _comment_lines(self, login):
        self.hub.add_comment(user={"login": login}, created_at=self.clock.at(30), updated_at=self.clock.at(30))
        return [l for l in self.tick(tolerate=True) if l.startswith("type=comment")]

    def test_the_exact_login_is_still_suppressed(self):
        self.assertEqual(self._review_lines("alice"), [])
        self.assertEqual(self._comment_lines("alice"), [])

    def test_a_login_with_a_newline_is_announced_flattened(self):
        review = self._review_lines("alice\n")
        self.assertEqual(self.poller.raised, [])
        self.assertEqual(len(review), 1, review)
        self.assertIn(" by=alice COMMENTED ", review[0])
        comment = self._comment_lines("alice\n")
        self.assertEqual(len(comment), 1, comment)
        self.assertIn(" by=alice | ", comment[0])

    def test_a_login_with_surrounding_spaces_is_announced_flattened(self):
        review = self._review_lines(" alice ")
        self.assertEqual(len(review), 1, review)
        self.assertIn(" by=alice COMMENTED ", review[0])
        comment = self._comment_lines(" alice ")
        self.assertEqual(len(comment), 1, comment)
        self.assertIn(" by=alice | ", comment[0])

    def test_a_login_over_the_display_cap_still_matches_its_own_entry(self):
        long_login = "x" * 200
        self.poller.cfg["suppress_authors"] = ["alice", long_login]
        self.assertEqual(self._review_lines(long_login), [])
        self.assertEqual(self._comment_lines(long_login), [])
        self.assertEqual(self.poller.raised, [])

    def test_a_long_login_not_in_the_list_is_announced_cut_short(self):
        review = self._review_lines("y" * 200)
        self.assertEqual(len(review), 1, review)
        self.assertIn(" by=%s… " % ("y" * 179), review[0])

    def test_the_display_copy_is_one_line(self):
        self.assertEqual(gw._login({"login": "a\nb"}), "a b")
        self.assertEqual(gw._login_raw({"login": "a\nb"}), "a\nb")
        self.assertEqual(gw._login_raw(["x"]), "?")
