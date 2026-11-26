#!/usr/bin/env python3
"""inbox_wake credentials and labels, and the first tests this daemon has had.

The original plan was "mint per-inbox-scoped keys for both watched inboxes, then
retire the org key". That plan could not be implemented as written: this daemon
sends ONE ``cfg["api_key"]`` as Bearer (``:86``) and batches every configured
inbox into a single ``/v1/events`` call as repeated ``inbox_id`` params
(``:136``), while the API's ``scope_inbox_id`` is singular. One inbox-scoped key
therefore cannot serve two inboxes, so retiring the org key required a code
change nobody had costed.

What changes here: ``inboxes`` values may be a plain label (unchanged) or
``{"label": ..., "api_key": ...}``. Inboxes are GROUPED BY CREDENTIAL and one
request is made per group — so a profile whose inboxes all share the top-level
key still makes exactly one call, byte-for-byte as before.

⚠ THE BACKWARD-COMPATIBILITY TESTS ARE THE LOAD-BEARING ONES. The always-on host runs this
same daemon for `pm-acme` and its config uses the old shape; a change that
quietly altered its request pattern would degrade the always-on seat, which is
the host where a dropped wake costs most. Those cases are pinned first
and deliberately assert the request COUNT, not just success.

No network, no files: ``InboxAPI`` is replaced with a recorder.

The second half of this file (from ``SeenMemory``) covers what the daemon remembers of the
events it has announced, and that a restart reloads it intact.
Run: `just test` (or `python3 -m unittest discover -s tests`).
"""
import json
import os
import random
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "daemons"))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _seen  # noqa: E402
import inbox_wake as iw  # noqa: E402
from wake_sim import FakeClock, Poller, T0  # noqa: E402

ORG = "td_org_key"
K1 = "td_key_one"
K2 = "td_key_two"
A = "aaaaaaaa-0000-4000-8000-00000000000a"
B = "bbbbbbbb-0000-4000-8000-00000000000b"


class TestCredentialGrouping(unittest.TestCase):
    """Which key is used for which inbox, and how many requests that costs."""

    def test_old_shape_all_inboxes_share_the_top_level_key(self):
        """The always-on host's config. One credential, one group, therefore ONE request."""
        cfg = {"api_key": ORG, "inboxes": {A: "claude-seat", B: "legacy"}}
        groups = iw.inbox_groups(cfg)
        self.assertEqual(len(groups), 1, "old-shape config must still make a single batched call")
        key, ids = groups[0]
        self.assertEqual(key, ORG)
        self.assertEqual(sorted(ids), sorted([A, B]))

    def test_single_inbox_old_shape_is_unchanged(self):
        """pm-acme on the always-on host: one inbox, one key."""
        cfg = {"api_key": ORG, "inboxes": {A: "pm-acme"}}
        self.assertEqual(iw.inbox_groups(cfg), [(ORG, [A])])

    def test_per_inbox_keys_split_into_one_group_each(self):
        """The target state: two inbox-scoped keys, no org key anywhere."""
        cfg = {"inboxes": {A: {"label": "claude-seat", "api_key": K1},
                           B: {"label": "legacy", "api_key": K2}}}
        groups = dict(iw.inbox_groups(cfg))
        self.assertEqual(groups, {K1: [A], K2: [B]})

    def test_mixed_shape_falls_back_to_the_top_level_key(self):
        """Migration state: one inbox moved to its own key, one not yet."""
        cfg = {"api_key": ORG, "inboxes": {A: {"label": "claude-seat", "api_key": K1},
                                           B: "legacy"}}
        groups = dict(iw.inbox_groups(cfg))
        self.assertEqual(groups, {K1: [A], ORG: [B]})

    def test_two_inboxes_sharing_one_key_are_batched_not_split(self):
        """Grouping is by CREDENTIAL, not per inbox — an unnecessary second
        request would double this daemon's poll rate against the API."""
        cfg = {"inboxes": {A: {"label": "a", "api_key": K1},
                           B: {"label": "b", "api_key": K1}}}
        groups = iw.inbox_groups(cfg)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0][1]), sorted([A, B]))

    def test_an_inbox_with_no_key_anywhere_is_reported_not_silently_dropped(self):
        """A config with no usable credential for an inbox must be loud. Silently
        skipping it would mean that inbox stops producing wakes and nothing says
        so — the exact silent-drop shape this tool exists to remove."""
        cfg = {"inboxes": {A: {"label": "a"}}}          # no api_key, no default
        with self.assertRaises(iw.MissingCredential):
            iw.inbox_groups(cfg)

    def test_per_inbox_placeholder_uses_the_profile_default(self):
        """A reviewer's counterexample: a real top-level key plus one inbox
        whose per-inbox api_key is the literal placeholder. `key or default` kept
        the placeholder (it is truthy), so the daemon parked while the watchdog,
        reading the real default, reported dead/stale instead. The decision:
        LENIENT — the placeholder means "use the default", exactly as
        "" already did."""
        cfg = {"api_key": ORG, "inboxes": {A: {"label": "a", "api_key": iw.PLACEHOLDER},
                                           B: "b"}}
        self.assertEqual(dict(iw.inbox_groups(cfg)), {ORG: [A, B]},
                         "a per-inbox placeholder must fall back to the default, in ONE group")

    def test_per_inbox_placeholder_with_no_default_is_still_reported(self):
        """Lenient is not silent: with nothing to fall back to, the inbox is still
        MissingCredential, never quietly dropped."""
        cfg = {"inboxes": {A: {"label": "a", "api_key": iw.PLACEHOLDER}}}
        with self.assertRaises(iw.MissingCredential):
            iw.inbox_groups(cfg)


class TestLabels(unittest.TestCase):
    """The wake line's label must resolve under BOTH config shapes.

    ``label = inboxes.get(inbox_id, ...)`` returns the VALUE, which under the new
    shape is a dict — so an unfixed lookup would emit `inbox={'label': ...}` into
    the log the session parses.
    """

    def test_label_from_old_shape(self):
        self.assertEqual(iw.inbox_label({A: "claude-seat"}, A), "claude-seat")

    def test_label_from_new_shape(self):
        self.assertEqual(iw.inbox_label({A: {"label": "claude-seat", "api_key": K1}}, A), "claude-seat")

    def test_label_falls_back_to_the_id_prefix_when_unknown(self):
        self.assertEqual(iw.inbox_label({}, A), A[:8])

    def test_new_shape_without_a_label_falls_back_to_the_id_prefix(self):
        self.assertEqual(iw.inbox_label({A: {"api_key": K1}}, A), A[:8])


class TestRequestsMade(unittest.TestCase):
    """End to end through poll_profile, with the API replaced by a recorder."""

    def setUp(self):
        self.calls = []
        recorder = self.calls

        class FakeAPI:
            def __init__(self, base, key):
                self.key = key

            def get(self, path, params=None):
                recorder.append({"key": self.key, "path": path,
                                 "inbox_ids": [v for k, v in (params or []) if k == "inbox_id"]})
                return {"items": []}

        self._real = iw.InboxAPI
        iw.InboxAPI = FakeAPI
        # codex P1: poll_profile() calls save_state(), which os.replace()s
        # ~/.local/state/watchtower/inbox/<profile>.json. Left live, `just test`
        # on a watcher host rewrites the daemon's watermark with an EMPTY seen
        # set — discarding pending backlog on the machine running the tests.
        # A test that can damage the host it runs on is not a unit test.
        self._real_save = iw.save_state
        self.saved = []
        iw.save_state = lambda profile, state: self.saved.append(profile)

    def tearDown(self):
        iw.InboxAPI = self._real
        iw.save_state = self._real_save

    def _state(self):
        return {"since": "2026-09-09T00:00:00+00:00", "seen": set()}

    def test_old_shape_makes_exactly_one_request_with_both_ids(self):
        cfg = {"api_url": "https://x", "api_key": ORG, "inboxes": {A: "a", B: "b"}}
        iw.poll_profile("acme", cfg, self._state())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["key"], ORG)
        self.assertEqual(sorted(self.calls[0]["inbox_ids"]), sorted([A, B]))

    def test_per_inbox_keys_make_one_request_each_with_its_own_id(self):
        cfg = {"api_url": "https://x",
               "inboxes": {A: {"label": "a", "api_key": K1},
                           B: {"label": "b", "api_key": K2}}}
        iw.poll_profile("acme", cfg, self._state())
        self.assertEqual(len(self.calls), 2)
        by_key = {c["key"]: c["inbox_ids"] for c in self.calls}
        self.assertEqual(by_key, {K1: [A], K2: [B]})

    def test_no_inbox_id_leaks_across_credentials(self):
        """An inbox-scoped key asked for another inbox's id would 403 the whole
        group, taking down a healthy inbox with it."""
        cfg = {"api_url": "https://x",
               "inboxes": {A: {"label": "a", "api_key": K1},
                           B: {"label": "b", "api_key": K2}}}
        iw.poll_profile("acme", cfg, self._state())
        for c in self.calls:
            self.assertEqual(len(c["inbox_ids"]), 1,
                             "each scoped key must ask only for its own inbox")


class TestStateIsNotWrittenByTests(unittest.TestCase):
    """A regression pin for codex's P1: these tests must not touch real state.

    Asserted structurally rather than by trusting the patch — if a future test
    calls poll_profile without neutralising save_state, the real one runs and
    this notices.
    """

    def test_save_state_is_patched_out_during_request_tests(self):
        case = TestRequestsMade("test_old_shape_makes_exactly_one_request_with_both_ids")
        case.setUp()
        try:
            self.assertIsNot(iw.save_state, case._real_save,
                             "save_state must be replaced before poll_profile runs")
        finally:
            case.tearDown()
        self.assertIs(iw.save_state, case._real_save, "tearDown must restore it")


class TestMainLoopGate(unittest.TestCase):
    """codex P1: the per-inbox-only config is this change's TARGET STATE.

    Gating on the top-level api_key alone parked it — zero requests, every
    inbox wake silently stopped. Testing poll_profile only would never see it:
    the unit was green and the feature was dead.
    """

    def test_per_inbox_only_config_has_credentials(self):
        cfg = {"inboxes": {A: {"label": "a", "api_key": K1},
                           B: {"label": "b", "api_key": K2}}}
        self.assertTrue(iw.has_credentials(cfg))

    def test_top_level_key_only_still_has_credentials(self):
        self.assertTrue(iw.has_credentials({"api_key": ORG, "inboxes": {A: "a"}}))

    def test_mixed_has_credentials(self):
        self.assertTrue(iw.has_credentials(
            {"api_key": ORG, "inboxes": {A: {"label": "a", "api_key": K1}, B: "b"}}))

    def test_placeholder_top_level_with_no_per_inbox_keys_does_not(self):
        self.assertFalse(iw.has_credentials({"api_key": iw.PLACEHOLDER, "inboxes": {A: "a"}}))

    def test_placeholder_in_a_per_inbox_key_with_no_default_does_not(self):
        """Nothing to fall back to: the inbox has no usable key, so the profile parks."""
        self.assertFalse(iw.has_credentials(
            {"inboxes": {A: {"label": "a", "api_key": iw.PLACEHOLDER}}}))

    def test_placeholder_in_a_per_inbox_key_with_a_real_default_does(self):
        """The #10 P3 case at the gate: the profile must POLL, not park."""
        self.assertTrue(iw.has_credentials(
            {"api_key": ORG, "inboxes": {A: {"label": "a", "api_key": iw.PLACEHOLDER}}}))

    def test_no_inboxes_configured_does_not(self):
        self.assertFalse(iw.has_credentials({"api_key": ORG, "inboxes": {}}))

    def test_an_inbox_with_no_credential_does_not(self):
        self.assertFalse(iw.has_credentials({"inboxes": {A: {"label": "a"}}}))


class TestWatchdogRecognisesPerInboxKeys(unittest.TestCase):
    """codex P2: the watchdog's credential check is a SECOND copy of the same
    assumption, in another file.

    `classify()` returns "parked" before the liveness and watermark checks, so a
    migrated profile would lose watchdog alerts entirely — appearing merely
    unconfigured while its daemon could be unloaded or stale. Fixing inbox_wake
    alone would have shipped that.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))
        import watchdog_wake
        self.wd = watchdog_wake

    def _cfg(self, inbox_block):
        return {"inbox": inbox_block}

    def test_per_inbox_only_config_is_not_parked(self):
        cfg = self._cfg({"inboxes": {A: {"label": "a", "api_key": K1}}})
        status, _ = self.wd.classify("acme", "inbox", cfg, set(), 600, 10**9)
        self.assertNotEqual(status, "parked",
                            "a migrated profile must not read as unconfigured")

    def test_top_level_key_still_not_parked(self):
        cfg = self._cfg({"api_key": ORG, "inboxes": {A: "a"}})
        status, _ = self.wd.classify("acme", "inbox", cfg, set(), 600, 10**9)
        self.assertNotEqual(status, "parked")

    def test_placeholder_everywhere_is_still_parked(self):
        cfg = self._cfg({"api_key": "PASTE-KEY-HERE", "inboxes": {A: "a"}})
        status, detail = self.wd.classify("acme", "inbox", cfg, set(), 600, 10**9)
        self.assertEqual(status, "parked")

    def test_a_per_inbox_placeholder_is_still_parked(self):
        cfg = self._cfg({"inboxes": {A: {"label": "a", "api_key": "PASTE-KEY-HERE"}}})
        status, _ = self.wd.classify("acme", "inbox", cfg, set(), 600, 10**9)
        self.assertEqual(status, "parked")


class TestEventTime(unittest.TestCase):
    """The sort key for emission order. The daemons run under /usr/bin/python3
    (3.9.6 on this host), whose ``datetime.fromisoformat`` rejects a ``Z`` suffix —
    and the live API emits BOTH ``2026-09-09T14:31:57.573374Z`` (an event's ``at``)
    and ``…+00:00`` (other fields) — so the parser normalises before parsing."""

    def test_z_suffix_parses(self):
        t = iw.event_time({"at": "2026-09-09T14:31:57.573374Z"})
        self.assertEqual(t.isoformat(), "2026-09-09T14:31:57.573374+00:00")

    def test_explicit_offset_parses(self):
        t = iw.event_time({"at": "2026-09-09T14:31:57.519444+00:00"})
        self.assertEqual(t.isoformat(), "2026-09-09T14:31:57.519444+00:00")

    def test_fractional_precision_is_normalised_for_3_9(self):
        """3.9's fromisoformat takes exactly 3 or 6 fractional digits. The API's
        6-digit form is fine today; ".5" / ".250" / ".1234567" must not silently
        sort to the epoch — the first run of this suite caught exactly that."""
        cases = {"2026-09-09T03:10:16.5Z": 500000,
                 "2026-09-09T03:10:16.250Z": 250000,
                 "2026-09-09T03:10:16.1234567Z": 123456,
                 "2026-09-09T03:10:16.573374Z": 573374,
                 "2026-09-09T03:10:16Z": 0}
        for raw, micro in cases.items():
            with self.subTest(raw=raw):
                t = iw.event_time({"at": raw})
                self.assertNotEqual(t, iw._EPOCH, "a legitimate timestamp sorted to the epoch")
                self.assertEqual(t.microsecond, micro)

    def test_whole_seconds_and_fractional_compare_correctly(self):
        """Lexicographic order gets this WRONG ('.' sorts before 'Z'); parsed order is right."""
        whole = iw.event_time({"at": "2026-09-09T03:10:16Z"})
        frac = iw.event_time({"at": "2026-09-09T03:10:16.5Z"})
        self.assertLess(whole, frac)

    def test_naive_timestamp_is_taken_as_utc_so_it_still_sorts(self):
        t = iw.event_time({"at": "2026-09-09T03:10:16"})
        self.assertIsNotNone(t.tzinfo)
        self.assertLess(iw._EPOCH, t)

    def test_missing_or_garbage_sorts_first_and_never_raises(self):
        for ev in ({}, {"at": None}, {"at": ""}, {"at": "not a time"}, {"at": 12345}):
            with self.subTest(ev=ev):
                self.assertEqual(iw.event_time(ev), iw._EPOCH)
        self.assertLess(iw._EPOCH, iw.event_time({"at": "2026-09-09T03:10:16Z"}))


class TestEmissionOrder(unittest.TestCase):
    """With two credential groups, ``reversed(items)`` was
    forward WITHIN a group and backward ACROSS them — group 2's events, then
    group 1's. The log a session parses must read forward regardless of how many
    keys are in use or what order the API returns them in."""

    def setUp(self):
        self.lines = []
        by_key = self.by_key = {}

        class FakeAPI:
            def __init__(self, base, key):
                self.key = key

            def get(self, path, params=None):
                return {"items": list(by_key.get(self.key, []))}

        self._real_api, iw.InboxAPI = iw.InboxAPI, FakeAPI
        # This test DOES emit. Left live, emit() appends to
        # ~/Library/Logs/watchtower/<profile>-inbox.log — fake wakes on the host
        # running the tests, surfaced to a real session by its Monitor. Same
        # class as the save_state P1 on #10, one file over.
        self._real_emit, iw.emit = iw.emit, lambda profile, line: self.lines.append(line)
        self._real_save, iw.save_state = iw.save_state, lambda profile, state: None

    def tearDown(self):
        iw.InboxAPI = self._real_api
        iw.emit = self._real_emit
        iw.save_state = self._real_save

    @staticmethod
    def _ev(mid, at):
        return {"id": "id-" + mid, "at": at, "event_type": "message.received",
                "inbox_id": A, "payload": {"message_id": mid}}

    @staticmethod
    def _state():
        return {"since": "2026-09-09T00:00:00+00:00", "seen": set()}

    def _emitted_message_ids(self):
        return [line.split("message_id=")[1] for line in self.lines]

    def test_two_groups_emit_in_chronological_order(self):
        # Each group newest-first, exactly as the API returns it.
        self.by_key[K1] = [self._ev("m03", "2026-09-09T03:00:00Z"),
                           self._ev("m01", "2026-09-09T01:00:00Z")]
        self.by_key[K2] = [self._ev("m04", "2026-09-09T04:00:00Z"),
                           self._ev("m02", "2026-09-09T02:00:00Z")]
        cfg = {"api_url": "https://x", "inboxes": {A: {"label": "a", "api_key": K1},
                                                    B: {"label": "b", "api_key": K2}}}
        iw.poll_profile("t", cfg, self._state())
        self.assertEqual(self._emitted_message_ids(), ["m01", "m02", "m03", "m04"])

    def test_one_group_is_unchanged_oldest_first(self):
        """The old-shape single-call case must read exactly as it did before."""
        self.by_key[ORG] = [self._ev("m02", "2026-09-09T02:00:00Z"),
                            self._ev("m01", "2026-09-09T01:00:00Z")]
        cfg = {"api_url": "https://x", "api_key": ORG, "inboxes": {A: "a"}}
        iw.poll_profile("t", cfg, self._state())
        self.assertEqual(self._emitted_message_ids(), ["m01", "m02"])

    def test_mixed_timestamp_formats_sort_together(self):
        # Deliberately NOT newest-first (`reversed()` would misorder it), and the
        # 1-digit fraction is on m02, not m01: a parser that collapsed it to the
        # epoch would sort it FIRST and fail here — the first version of this test
        # put it on m01 and passed for exactly that wrong reason.
        self.by_key[ORG] = [self._ev("m02", "2026-09-09T02:00:00.5Z"),
                            self._ev("m03", "2026-09-09T03:00:00.250+00:00"),
                            self._ev("m01", "2026-09-09T01:00:00Z")]
        cfg = {"api_url": "https://x", "api_key": ORG, "inboxes": {A: "a"}}
        iw.poll_profile("t", cfg, self._state())
        self.assertEqual(self._emitted_message_ids(), ["m01", "m02", "m03"])


class TestDaemonAndWatchdogAgree(unittest.TestCase):
    """The two credential gates, checked against each other by ENUMERATION.

    A PR body claimed the daemon and the watchdog "agree on every
    configuration" — asserted from prose, and false (a review found: a real
    top-level key plus a per-inbox placeholder). This is the check that would
    have caught it: every top-level key state x every per-inbox value shape, for
    one and for two inboxes, and the two functions must return the same answer.

    Scope: configs with at least one inbox. With none, both callers stop before
    the credential question — ``classify()`` returns "unconfigured" on its
    ``required`` check and ``main()`` skips the profile on ``cfg.get("inboxes")``
    — so agreement there is neither required nor meaningful.
    """

    TOP = {"absent": None, "empty": "", "placeholder": iw.PLACEHOLDER, "real": ORG}
    PER_INBOX = {
        "label": "a",
        "dict-no-key": {"label": "a"},
        "dict-empty-key": {"label": "a", "api_key": ""},
        "dict-placeholder": {"label": "a", "api_key": iw.PLACEHOLDER},
        "dict-real": {"label": "a", "api_key": K1},
    }

    def setUp(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))
        import watchdog_wake
        self.wd = watchdog_wake

    def _cfg(self, top, values):
        cfg = {"inboxes": {iid: v for iid, v in zip((A, B), values)}}
        if top is not None:
            cfg["api_key"] = top
        return cfg

    def _both(self, cfg):
        return (iw.has_credentials(cfg),
                self.wd._has_credential("inbox", cfg, "api_key"))

    def test_every_single_inbox_configuration_agrees(self):
        outcomes = set()
        for tname, top in self.TOP.items():
            for vname, value in self.PER_INBOX.items():
                cfg = self._cfg(top, [value])
                with self.subTest(top=tname, inbox=vname):
                    daemon, watchdog = self._both(cfg)
                    self.assertEqual(daemon, watchdog,
                                     "daemon %r vs watchdog %r for %r" % (daemon, watchdog, cfg))
                    outcomes.add(daemon)
        self.assertEqual(outcomes, {True, False}, "the matrix must exercise both answers")

    def test_every_two_inbox_configuration_agrees(self):
        outcomes = set()
        for tname, top in self.TOP.items():
            for aname, av in self.PER_INBOX.items():
                for bname, bv in self.PER_INBOX.items():
                    cfg = self._cfg(top, [av, bv])
                    with self.subTest(top=tname, a=aname, b=bname):
                        daemon, watchdog = self._both(cfg)
                        self.assertEqual(daemon, watchdog,
                                         "daemon %r vs watchdog %r for %r" % (daemon, watchdog, cfg))
                        outcomes.add(daemon)
        self.assertEqual(outcomes, {True, False}, "the matrix must exercise both answers")

    def test_the_counterexample_itself(self):
        """Named, so a regression reads as what it is."""
        cfg = {"api_key": ORG, "inboxes": {A: "a", B: {"label": "b", "api_key": iw.PLACEHOLDER}}}
        self.assertEqual(self._both(cfg), (True, True))


# ---------------------------------------------------------------------------
# The dedup memory survives a restart
#
# `save_state` wrote `list(state["seen"])[-1500:]`. `seen` was a SET, so that is hash order,
# randomised per process: past 1500 ids an arbitrary 1500 were kept and the rest dropped at
# random. The events API is asked for everything since the watermark less five minutes and this
# daemon has no other filter, so whatever it still returned and the cut had dropped was announced
# a second time after the next restart: a `mail_received` line for a message a seat had already
# answered, which the intake playbook may act on again. The same line was fixed in
# `github_wake`; this is that fix, adapted to a daemon whose ids are bare event ids in ONE kind.
# ---------------------------------------------------------------------------

class _Events(object):
    """A miniature events API: what ``GET /v1/events`` answers, with its own windowing.

    The windowing is the point. The answer is the ``limit`` NEWEST events whose ``at`` is at or
    after ``since`` (inclusive), for the inboxes and event types asked for, and a key scoped to
    one inbox reads only that inbox. Everything is read from the request, so a daemon that
    changes what it asks for is served what the API would serve for that. Times are the clock's,
    in the API's own spelling, and a request this hub does not model fails the test.
    """

    def __init__(self, clock):
        self.clock = clock
        self.events = []
        self.asked = []                  # (key, path, params) for every call
        self.scopes = {}                 # api key -> the inbox ids it may read (absent: any)
        self.fail = False
        self._seq = 0

    def add(self, inbox=A, at=None, etype="message.received", raw_at=False):
        """A new event, stamped now unless told otherwise. ``raw_at`` serves ``at`` as given
        (a garbage string, or None) and lets the event through every ``since`` filter."""
        self._seq += 1
        event = {"id": "evt-%06d" % self._seq, "at": at if at is not None or raw_at else self.clock.now(),
                 "event_type": etype, "inbox_id": inbox, "message_id": "msg-%06d" % self._seq,
                 "raw": raw_at}
        self.events.append(event)
        return event

    def api(self, base, key):
        """``inbox_wake.InboxAPI(base, key)``: the daemon builds one per credential group."""
        hub = self

        class View(object):
            def get(self, path, params=None):
                return hub.serve(key, path, params)
        return View()

    def serve(self, key, path, params):
        self.asked.append((key, path, list(params or [])))
        if self.fail:
            raise RuntimeError("HTTP 502 on %s" % path)
        if path != "/v1/events":
            raise AssertionError("the daemon asked for something this hub does not model: %s" % path)
        want = {}
        for name, value in params:
            want.setdefault(name, []).append(value)
        since = datetime.fromisoformat(want["since"][0])
        readable = self.scopes.get(key)
        hits = [e for e in self.events
                if e["inbox_id"] in want["inbox_id"] and e["event_type"] in want["event_type"]
                and (readable is None or e["inbox_id"] in readable)
                and (e["raw"] is not False or e["at"] >= since)]
        hits.sort(key=lambda e: e["at"] if isinstance(e["at"], datetime) else _EPOCH_FOR_SORT, reverse=True)
        return {"items": [{"id": e["id"], "at": (e["at"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                                                 if isinstance(e["at"], datetime) else e["at"]),
                           "event_type": e["event_type"], "inbox_id": e["inbox_id"],
                           "payload": {"message_id": e["message_id"]}}
                          for e in hits[:int(want["limit"][0])]]}


_EPOCH_FOR_SORT = datetime(1970, 1, 1, tzinfo=timezone.utc)
PM_CFG = {"api_url": "https://api.example.test", "api_key": ORG, "inboxes": {A: "pm-acme"}}


def _inbox_world(case, cfg=None):
    clock = FakeClock(T0)
    hub = _Events(clock)
    poller = Poller(case, iw, dict(cfg or PM_CFG), {"InboxAPI": hub.api}, clock=clock)
    return hub, poller


def _mail(lines):
    """The wake lines among ``lines``: the one kind this daemon emits and its memory guards."""
    return [line for line in lines if line.startswith("type=mail_received ")]


def _message_ids(lines):
    return sorted(line.split("message_id=")[1] for line in _mail(lines))


def _arrive(hub, poller, state, ticks, per_tick=5):
    """The everyday load: ``per_tick`` messages a poll, one in seven failing authentication.
    Returns every line emitted."""
    said = []
    for i in range(ticks):
        for k in range(per_tick):
            hub.add(etype="message.received.unauthenticated" if (i + k) % 7 == 0 else "message.received")
        said.extend(poller.poll(state))
    return said


class SeenMemory(unittest.TestCase):
    """The sizing: derived from what one poll asks the API for, not guessed."""

    def test_the_wake_line_is_unchanged(self):
        """Pinned BEFORE the memory changed: this passes on the old code and the new. The PM's
        patterns, `just unactioned` and the seats' Monitor filters parse it."""
        hub, poller = _inbox_world(self)
        first = hub.add()
        second = hub.add(etype="message.received.unauthenticated")
        self.assertEqual(poller.poll(poller.load()), [
            "type=mail_received inbox=pm-acme auth=pass message_id=" + first["message_id"],
            "type=mail_received inbox=pm-acme auth=FAIL message_id=" + second["message_id"]])

    def test_the_ids_written_are_the_bare_event_ids_the_old_code_wrote(self):
        """The kind cannot come from a prefix here, and adding one would break a rollback: the old
        code compares the bare id, so a prefixed id would never match and every event still in the
        window would be announced again."""
        hub, poller = _inbox_world(self)
        events = [hub.add() for _ in range(3)]
        poller.poll(poller.load())
        self.assertEqual(set(poller.saved()["seen"]), set(e["id"] for e in events))

    def test_the_window_is_what_the_poller_really_asks_for(self):
        """The window constant BUILDS the request, and this reads the request back."""
        hub, poller = _inbox_world(self)
        hub.add()
        poller.poll(poller.load())
        limits = [dict(params)["limit"] for _, _, params in hub.asked]
        self.assertEqual(limits, [iw.EVENTS_PER_PAGE])
        self.assertEqual(iw.EVENTS_PER_PAGE, 50)
        self.assertEqual(iw.SEEN_WINDOW, {"event": iw.EVENTS_PER_PAGE})

    def test_the_old_cap_is_what_the_previous_code_saved(self):
        """38ffbbd saved list(seen)[-1500:], and had since the daemon was written. The migration reads it: a
        file holding fewer ids than this never lost one."""
        self.assertEqual(iw.OLD_SEEN_CAP, 1500)

    def test_the_window_constant_drives_the_request(self):
        """Reading the request back proves the constant and the request agree TODAY, not that one
        drives the other: a number typed into the request again would still agree. So the constant is
        moved, and the request follows."""
        hub, poller = _inbox_world(self)
        hub.add()
        with mock.patch.object(iw, "EVENTS_PER_PAGE", 7):
            poller.poll(poller.load())
        self.assertEqual([dict(params)["limit"] for _, _, params in hub.asked], [7])

    def test_an_event_is_returned_for_the_polls_the_cap_is_sized_for(self):
        """`SEEN_POLLS` is a claim about the world, so it is COUNTED: how many polls look at an event
        (once to see it, then once for each refresh), with the event stamped at four points of the poll
        interval. Six, however it falls, and seven in the one case that is not really a case: an event
        stamped exactly at the instant a cycle starts, against a `since` that is inclusive."""
        for before_poll in (0, 1, 30, 59):
            hub, poller = _inbox_world(self)
            state = poller.load()
            poller.poll(state)
            found = hub.add(at=poller.clock.now() - timedelta(seconds=before_poll))
            touched, real = [], _seen.touch_seen
            with mock.patch.object(_seen, "touch_seen", lambda seen, mid: (touched.append(mid), real(seen, mid))[1]):
                for _ in range(12):
                    poller.poll(state)
            polls = 1 + touched.count(found["id"])
            self.assertEqual(polls, iw.SEEN_POLLS + (1 if before_poll == 0 else 0),
                             "stamped %d s before the poll that first sees it" % before_poll)

    def test_an_id_is_returnable_for_six_polls(self):
        """`since` is the last cycle's start less OVERLAP and moves a poll at a time: OVERLAP plus
        one poll, six of them (seven in the one case of an event stamped exactly at a cycle
        start, against an inclusive `since`)."""
        self.assertEqual(iw.SEEN_POLLS, 6)
        self.assertEqual(iw.SEEN_POLLS, _seen.returnable_polls(iw.OVERLAP, iw.POLL_SECS))
        self.assertEqual(iw.SEEN_KEEP, {"event": 6})

    def test_the_cap_is_pinned_so_a_change_is_deliberate(self):
        """50 events x 6 polls = 300 per credential group. Even with no refresh at all, an id that
        is returned in the last of its polls has at most six windows of newer ids behind it, and
        each is refreshed anyway whenever it is returned. Reasoned in the comment above `SEEN_KEEP`."""
        self.assertEqual(iw.SEEN_CAPS, {"event": 300})
        self.assertGreaterEqual(iw.SEEN_CAPS["event"], 2 * iw.SEEN_WINDOW["event"])

    def test_the_cap_is_ample_for_the_volume_measured_on_the_live_log(self):
        """Measured on this host's profile log, 2026-09-03..30: 28 wakes on the median day, 69 on
        the busiest. The window is 50 events in ONE poll, so it takes a burst, not a busy day, to
        fill it, and the cap is six of those."""
        self.assertGreater(iw.SEEN_CAPS["event"], 4 * 69)

    def test_caps_scale_with_the_number_of_credential_groups(self):
        """One `/v1/events` call is made per distinct credential, each with its own `limit`, so a
        profile with three asks three times the questions and must remember three times the answers."""
        keys = ("k1", "k2", "k3")
        inboxes = ("mbx-1", "mbx-2", "mbx-3")
        cfg = {"api_url": "https://x", "inboxes": dict((i, {"label": i, "api_key": k})
                                                       for i, k in zip(inboxes, keys))}
        with mock.patch.object(iw, "SEEN_CAPS", {"event": 2}):
            hub, poller = _inbox_world(self, cfg)
            for inbox in inboxes:
                for _ in range(5):
                    hub.add(inbox=inbox)
            state = poller.load()
            poller.poll(state)
            self.assertEqual(len(state["seen"]), 6)
            solo_hub, solo = _inbox_world(self)
            for _ in range(5):
                solo_hub.add()
            solo_state = solo.load()
            solo.poll(solo_state)
            self.assertEqual(len(solo_state["seen"]), 2)


class RestartKeepsTheDedupMemory(unittest.TestCase):
    """The ticket's regression tests: through the real load, poll and save, over enough cycles
    that more than 1500 ids have been seen."""

    def test_a_restart_re_emits_nothing_the_api_still_returns(self):
        """THE restart test. 700 polls of everyday load, five messages a poll, is 3,500 event ids
        against the old 1,500 cap. Save, load, poll: the API still returns everything since the
        watermark less five minutes, and none of it may be announced again."""
        hub, poller = _inbox_world(self)
        state = poller.load()
        said = _mail(_arrive(hub, poller, state, ticks=700))
        self.assertEqual(len(said), 700 * 5, "the simulation itself lost or repeated a message")
        self.assertEqual(len(set(said)), len(said))

        restarted = poller.load()
        again = _mail(poller.poll(restarted))
        self.assertEqual(again, [], "a restart re-announced %d wake(s) the API still returns: %s"
                         % (len(again), again[:3]))

        # Not vacuous: a daemon that said nothing at all would pass the line above.
        hub.add()
        self.assertEqual(len(_mail(poller.poll(restarted))), 1, "a genuinely new message was swallowed")
        # And the second restart is as quiet as the first.
        self.assertEqual(_mail(poller.poll(poller.load())), [])

    def test_what_is_saved_is_exactly_what_the_running_process_knows(self):
        """A restart must change nothing, so the file has to hold the process's OWN memory: the same
        ids in the same order, pruned the same way."""
        hub, poller = _inbox_world(self)
        state = poller.load()
        _arrive(hub, poller, state, ticks=400)                         # 2,000 ids: past the old cap
        self.assertEqual(list(state["seen"]), poller.saved()["seen"])
        self.assertEqual(list(poller.load()["seen"]), list(state["seen"]))
        self.assertEqual(len(state["seen"]), iw.SEEN_CAPS["event"], "the memory should be at its cap by now")

    def test_the_saved_list_is_oldest_first_so_the_reload_evicts_the_right_ids(self):
        """The order in the FILE is the memory: events that have left the query window sit before
        the ones still inside it, and the newest are last. Five in each group, so a hash-ordered
        list cannot line up by luck. (Events are handled oldest first within a poll.)"""
        hub, poller = _inbox_world(self)
        state = poller.load()
        gone = [hub.add() for _ in range(5)]
        poller.poll(state)
        for _ in range(8):
            poller.poll(state)              # `gone` is no longer inside the query window
        stays = [hub.add() for _ in range(5)]
        poller.poll(state)
        newest = [hub.add() for _ in range(5)]
        poller.poll(state)
        seen = poller.saved()["seen"]

        def places(events):
            return [seen.index(e["id"]) for e in events]
        self.assertLess(max(places(gone)), min(places(stays)), "the file is not oldest-first: %s" % seen)
        self.assertLess(max(places(stays)), min(places(newest)), "the file is not oldest-first: %s" % seen)

    def test_an_id_the_api_returns_again_moves_to_the_newest_position(self):
        """LAST-seen, not first-seen, as a mechanism. Here the oldest event is always the first to
        leave the answer, so arrival order and recency order coincide and no window can tell the two
        apart (which is why the eviction tests above do not); what can is the order the poll leaves
        the memory in. Two ids are remembered, the older one is returned again, the other is not:
        the one returned is now the newest."""
        hub, poller = _inbox_world(self)
        returned = hub.add(at=T0 - timedelta(minutes=1))
        elsewhere = hub.add(at=T0 - timedelta(days=1))               # far before `since`: never returned
        state = {"since": (T0 - timedelta(minutes=5)).isoformat(),
                 "seen": _seen.new_seen([returned["id"], elsewhere["id"]])}
        self.assertEqual(_mail(poller.poll(state)), [])
        self.assertEqual(list(state["seen"]), [elsewhere["id"], returned["id"]])
        self.assertEqual(poller.saved()["seen"], [elsewhere["id"], returned["id"]])

    def test_no_live_id_is_evicted_at_the_worst_case_window_on_two_credentials(self):
        """Sizing, exercised at the API's worst case rather than a friendly one. Two credentials,
        so two calls a poll, each answering a FULL page of 50: ten new events a poll per credential
        and every one returned in five polls running. 100 polls run the memory well past its cap.
        One live id evicted would be announced again the next poll."""
        cfg = {"api_url": "https://x", "inboxes": {A: {"label": "a", "api_key": K1},
                                                    B: {"label": "b", "api_key": K2}}}
        hub, poller = _inbox_world(self, cfg)
        hub.scopes = {K1: set([A]), K2: set([B])}
        state = poller.load()
        for tick in range(100):
            for _ in range(10):
                hub.add(inbox=A)
                hub.add(inbox=B)
            said = _mail(poller.poll(state))
            self.assertEqual(len(said), 20, "tick %d: a wake was announced twice or not at all" % tick)
        full = hub.serve(K1, "/v1/events", [("since", state["since"]), ("limit", iw.EVENTS_PER_PAGE), ("inbox_id", A),
                                            ("event_type", "message.received")])
        self.assertEqual(len(full["items"]), 50, "the world must keep the page full")
        self.assertEqual(len(state["seen"]), 2 * iw.SEEN_CAPS["event"])
        self.assertEqual(_mail(poller.poll(poller.load())), [], "and a restart still holds it")

    def test_restarting_before_every_single_cycle_changes_nothing(self):
        """The ticket in one property: a daemon restarted between EVERY pair of polls says exactly
        what one that never restarts says. The world is driven by a seeded generator so both runs
        see the same one: bursts, quiet stretches, and messages of both authentication outcomes."""
        def play(restart_each_cycle):
            rng = random.Random(1109)
            hub, poller = _inbox_world(self)
            state = poller.load()
            said = []
            for cycle in range(700):
                if not 250 <= cycle < 450:
                    for _ in range(rng.choice([0, 1, 2, 3, 5, 8, 12] if cycle % 60 else [30])):
                        hub.add(etype=rng.choice(["message.received"] * 4 + ["message.received.unauthenticated"]))
                if restart_each_cycle:
                    state = poller.load()
                said.extend(_mail(poller.poll(state)))
            return said

        steady, restarted = play(False), play(True)
        self.assertGreater(len(steady), 1500, "the world must be busy enough to have overflowed the old cap")
        if steady != restarted:
            first = 0
            while first < min(len(steady), len(restarted)) and steady[first] == restarted[first]:
                first += 1
            self.fail("restarting changed what was said: %d lines vs %d, first difference at %d: %r vs %r"
                      % (len(steady), len(restarted), first, steady[first:first + 1], restarted[first:first + 1]))


class SavedFormat(unittest.TestCase):
    """What is on disk. Nothing reads `seen` but this daemon (`just status` and the watchdog read
    `since` and `build`), so the one constraint is a rollback."""

    @staticmethod
    def _pre_seen_memory_loader(path):
        """``load_state`` as it stood at 38ffbbd, verbatim but for its try/except: what a
        rollback to that code does with this file."""
        with open(path) as fh:
            raw = json.load(fh)
        return {"since": raw.get("since"), "seen": set(raw.get("seen", []))}

    def _cycled(self):
        hub, poller = _inbox_world(self)
        state = poller.load()
        _arrive(hub, poller, state, ticks=6)
        return poller

    def test_the_file_is_marked_as_the_ordered_format(self):
        raw = self._cycled().saved()
        self.assertEqual(raw.get("seen_v"), 2)
        self.assertIsInstance(raw["seen"], list)
        self.assertTrue(all(isinstance(x, str) for x in raw["seen"]))
        self.assertEqual(len(raw["seen"]), 30)

    def test_the_old_code_still_reads_the_file(self):
        """Rollback safety: `seen` stays a flat list of id strings and the marker is a new key the
        old loader ignores, so a rollback loses the ORDER (it resumes the arbitrary truncation) but
        never the ids, and never fails to start."""
        poller = self._cycled()
        old = self._pre_seen_memory_loader(poller.state_path)
        new = poller.load()
        self.assertEqual(old["seen"], set(new["seen"]))
        self.assertEqual(old["since"], new["since"])
        self.assertGreater(len(old["seen"]), 0)

    def test_the_watermark_is_still_the_cycle_start_less_the_overlap(self):
        """`since` is the server-side filter and what `just status` and the watchdog read; the
        migration reasons about it and must not move it."""
        poller = self._cycled()
        started = poller.clock.current - timedelta(seconds=iw.POLL_SECS)
        self.assertEqual(poller.saved()["since"], (started - iw.OVERLAP).isoformat())

    def test_a_marked_file_is_trusted_and_gets_no_baseline(self):
        self.assertFalse(self._cycled().load().get("legacy_seen"))

    def test_a_set_from_an_older_caller_is_upgraded_and_saved_ordered(self):
        """`poll_profile` upgrades a `seen` handed to it as a set (the shape every earlier state,
        and the fixtures built from it, carry) and saves the ordered form."""
        hub, poller = _inbox_world(self)
        event = hub.add()
        state = {"since": (T0 - timedelta(minutes=5)).isoformat(), "seen": set([event["id"]])}
        self.assertEqual(_mail(poller.poll(state)), [])
        self.assertNotIsInstance(state["seen"], set)
        self.assertEqual(poller.saved()["seen"], [event["id"]])
        self.assertEqual(poller.saved()["seen_v"], 2)

    def test_save_state_takes_the_smallest_state_it_always_did(self):
        """tests/test_build_marker hands it a bare set and no other key: it must still write."""
        hub, poller = _inbox_world(self)
        iw.save_state("p", {"since": "t", "seen": set()})
        with open(os.path.join(poller.state_dir, "p.json")) as fh:
            raw = json.load(fh)
        self.assertEqual((raw["since"], raw["seen"], raw["seen_v"]), ("t", [], 2))


class LegacyStateFile(unittest.TestCase):
    """The first load of a state file written BEFORE the fix.

    Its `seen` is an arbitrary subset: the ids the old daemon dropped are already lost, so on the
    first cycle the API returns events nobody can prove were announced. Those must not be
    announced again, and an event that really is new must still be.

    The design: a file without the `seen_v` marker is legacy. On its first cycle an unseen event
    is adopted SILENTLY only on positive evidence that it pre-dates the old daemon's last look:
    its own `at` parses and is at least one poll interval before it. Anything else (later, inside
    that interval, or with no readable `at`) is announced as it always was.

    THE LAST LOOK IS NOT THE FILE'S WATERMARK HERE. This daemon saves ``cycle_start - OVERLAP`` as
    `since`, and asks the API for everything from there, so the API never returns an event older
    than `since`: measured from `since`, the rule would adopt nothing and the migration would be
    the bug it exists to fix. The old daemon last looked at `since` plus the overlap.

    And only where the file says ids were LOST (a review found this): a file under the old cap
    has never lost one, so for it nothing is adopted and everything announces.
    """

    def setUp(self):
        self.hub, self.poller = _inbox_world(self)
        self.looked = T0 - timedelta(seconds=120)          # the old daemon's last cycle started 2 minutes ago
        self.since = self.looked - iw.OVERLAP              # ... and it saved that less the overlap

    def _ago(self, **kw):
        return self.looked - timedelta(**kw)

    def _after(self, **kw):
        return self.looked + timedelta(**kw)

    def _write(self, seen, **over):
        """A state file as the old daemon left it: `seen` in no particular order and no marker."""
        raw = {"since": self.since.isoformat(), "seen": list(seen),
               "build": {"src": "934141964609", "head": "38ffbbd"}}
        raw.update(over)
        self.poller.write(raw)

    @staticmethod
    def _arbitrary(remembered, size=1500):
        """`size` ids (1500 is the old cap), `remembered` among them, in no particular order (a fixed shuffle)."""
        filler = ["evt-old-%06d" % i for i in range(1500)]
        ids = list(remembered) + filler[:size - len(remembered)]
        random.Random(1109).shuffle(ids)
        return ids

    def _visible_world(self):
        """What the API returns at the first poll after the restart that pre-dates the old daemon's
        last look. Returns the ids the old file still remembers: 3 of 7, the rest dropped by the
        arbitrary truncation."""
        remembered = []
        for seconds, kept in ((290, False), (250, True), (200, False), (160, True), (130, False),
                              (90, True), (70, False)):
            event = self.hub.add(at=self._ago(seconds=seconds))
            if kept:
                remembered.append(event["id"])
        return remembered

    def _new_events(self):
        """Four wakes that are genuinely new, or cannot be proven old. Returns their message ids."""
        hub = self.hub
        return sorted([hub.add(at=self._after(seconds=30))["message_id"],       # arrived while it was down
                       hub.add(at=self._after(seconds=90), etype="message.received.unauthenticated")["message_id"],
                       hub.add(at=self._ago(seconds=30))["message_id"],         # inside the poll interval before its look
                       hub.add(at=self._ago(seconds=5))["message_id"]])

    def test_the_first_cycle_re_emits_nothing_the_old_file_lost(self):
        """THE migration test: 4 events the API returns were dropped by the arbitrary truncation.
        None is announced; the four that are new (after the last look, or too close to it to prove
        otherwise) all are."""
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered))
        state = self.poller.load()
        self.assertEqual(_message_ids(self.poller.poll(state)), expected)

    def test_the_last_look_is_the_watermark_plus_the_overlap_not_the_watermark(self):
        """The regression a literal port would ship: an event 100 s after `since` is 200 s before
        the old daemon's last look, so it is old; one 250 s after `since` is 50 s before it, so it
        is not provably old. Measured from `since` neither could ever be adopted."""
        self.hub.add(at=self.since + timedelta(seconds=100))
        close = self.hub.add(at=self.since + timedelta(seconds=250))
        self._write(self._arbitrary([]))
        self.assertEqual(_message_ids(self.poller.poll(self.poller.load())), [close["message_id"]])

    # -- a review's finding: adopt only where the file says ids were LOST --
    # (This daemon has no per-fetch holes, so the open-episode half does not apply: a failed call raises
    # out of the cycle before anything is saved, and a file is only ever written by a cycle that read it all.)

    def test_a_file_under_the_old_cap_adopts_nothing(self):
        """The old code dropped an id only at its cut, list(seen)[-1500:], and a set only grows, so a file
        holding fewer than 1,500 ids has never lost one: an event it does not hold was never announced. The
        four the big test above treats as lost are, under the cap, simply unread, so they announce with the
        new ones; adopting them would swallow them."""
        remembered = self._visible_world()
        self._new_events()
        expected = sorted(e["message_id"] for e in self.hub.events if e["id"] not in remembered)
        self.assertEqual(len(expected), 8)
        self._write(self._arbitrary(remembered, size=1499))
        state = self.poller.load()
        self.assertEqual(_message_ids(self.poller.poll(state)), expected)
        self.assertEqual([n for n in self.poller.notes if "adopted" in n], [])
        self.assertFalse(state["legacy_seen"])

    def test_a_file_exactly_at_the_old_cap_still_adopts(self):
        """The other side of the boundary, and exactly what the migration did before: at the cap ids WERE being
        dropped."""
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered, size=1500))
        self.assertEqual(_message_ids(self.poller.poll(self.poller.load())), expected)

    def test_a_degraded_block_means_nothing_here(self):
        """No daemon-level hole can leave this file: nothing is saved by a cycle whose call failed. A `degraded`
        key in a file would not be this daemon's, and is not read."""
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered), degraded={"comments": {"last": 1, "open": True}})
        self.assertEqual(_message_ids(self.poller.poll(self.poller.load())), expected)

    def test_the_daemon_log_says_why_nothing_is_adopted(self):
        self._write(self._arbitrary([], size=10))
        said = _seen.legacy_note("acme", self.poller.load(), iw.LEGACY_GUARD)
        self.assertIn("adopts nothing", said)
        self.assertIn("under the old cap of 1500", said)

    def test_the_daemon_log_says_what_the_migration_adopted(self):
        self._write(self._arbitrary(self._visible_world()))
        self.poller.poll(self.poller.load())
        said = [n for n in self.poller.notes if "adopted" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertRegex(said[0], r"adopted 4 event\(s\) silently")

    def test_a_legacy_file_loads_with_its_last_look_and_a_cycle_budget(self):
        """The baseline rides the in-memory state: when the old daemon last looked (`since` plus the
        overlap, not `since`) and how many cycles it may run."""
        self._write(self._arbitrary([]))
        legacy = self.poller.load()["legacy_seen"]
        self.assertEqual(_seen.parse_ts(legacy["cutoff"]), self.looked)
        self.assertGreater(legacy["cycles"], 0)

    def test_main_starts_on_a_legacy_file_and_says_so(self):
        """`main()` prints the legacy note when it loads a profile, so the daemon log says the
        migration is coming before it happens. A typo in the lines it gained would crash-loop the
        daemon at its FIRST start after the deploy, so run it: a config in a private tree (the real
        one is never read), the real load, one real cycle, and the sleep that ends the first pass
        turned into a stop."""
        self._write(self._arbitrary([]))
        config = self.poller.write_config({"profiles": {"acme": {"inbox": dict(PM_CFG)}}})

        class Stop(Exception):
            pass

        def stop(_secs):
            raise Stop()

        clock = mock.Mock(sleep=stop, time=time.time)
        with mock.patch.object(iw, "CONFIG_PATH", config), mock.patch.object(iw, "time", clock):
            with self.assertRaises(Stop):
                iw.main()
        said = [n for n in self.poller.notes if "predates" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertIn("1500 ids", said[0])
        self.assertEqual(self.poller.saved()["seen_v"], 2, "the cycle main() ran must have written the new format")

    def test_the_boundary_is_one_poll_interval_before_the_last_look(self):
        """Pinned so it is a decision, not an accident: 61 seconds before the last look is old, 59
        is not. The old daemon's last look can lag its watermark by a fetch, and the two clocks are
        not the same clock; a duplicate is the safe direction and a swallowed event is not."""
        self.assertEqual(iw.LEGACY_GUARD, timedelta(seconds=iw.POLL_SECS))
        self.hub.add(at=self._ago(seconds=61))
        late = self.hub.add(at=self._ago(seconds=59))
        self._write(self._arbitrary([]))
        self.assertEqual(_message_ids(self.poller.poll(self.poller.load())), [late["message_id"]])

    def test_an_event_with_no_readable_timestamp_is_announced_not_adopted(self):
        """Positive evidence only: no timestamp, or one that does not parse, is not proof of anything."""
        none = self.hub.add(at=None, raw_at=True)
        junk = self.hub.add(at="last tuesday", raw_at=True)
        self.hub.add(at=self._ago(seconds=200))                        # the control: provably old
        self._write(self._arbitrary([]))
        self.assertEqual(_message_ids(self.poller.poll(self.poller.load())),
                         sorted([none["message_id"], junk["message_id"]]))

    def test_no_watermark_means_no_baseline(self):
        """Nothing to compare against: announce as the old code would (within its first-run lookback)."""
        self.hub.add(at=T0 - timedelta(minutes=2))
        self._write(self._arbitrary([]), since=None)
        state = self.poller.load()
        self.assertEqual(len(_mail(self.poller.poll(state))), 1)
        self.assertFalse(state["legacy_seen"], "there is nothing to hold when there was nothing to compare")

    def test_a_cycle_that_fails_does_not_consume_the_baseline(self):
        """A failed call raises out of the cycle before anything is adopted or saved, and the baseline
        is only settled at the end of a cycle that completed, so it is still there for the retry."""
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered))
        state = self.poller.load()
        self.hub.fail = True
        with self.assertRaises(RuntimeError):
            self.poller.poll(state)
        self.assertTrue(state["legacy_seen"])
        self.assertEqual(state["legacy_seen"]["cycles"], _seen.LEGACY_MAX_CYCLES)
        self.hub.fail = False
        self.assertEqual(_message_ids(self.poller.poll(state)), expected)

    def test_the_baseline_is_one_clean_cycle_long(self):
        """It must not swallow beyond the migration: once a cycle has completed cleanly the file is
        trusted, and an unseen event announces whatever its timestamp says."""
        self._write(self._arbitrary(self._visible_world()))
        state = self.poller.load()
        self.assertEqual(_mail(self.poller.poll(state)), [])
        self.assertFalse(state["legacy_seen"])
        # Inside the SECOND cycle's window (the API's floor is that cycle's `since`), and old enough
        # that the baseline would have adopted it.
        late = self.hub.add(at=self._ago(seconds=130))
        self.assertEqual(_message_ids(self.poller.poll(state)), [late["message_id"]])

    def test_the_first_cycle_writes_the_new_format_and_the_next_load_is_not_legacy(self):
        self._write(self._arbitrary(self._visible_world()))
        self.poller.poll(self.poller.load())
        self.assertEqual(self.poller.saved()["seen_v"], 2)
        self.assertFalse(self.poller.load().get("legacy_seen"))

    def test_a_marked_file_gets_no_baseline_at_all(self):
        """The marker means the order in the file is the memory. Ids missing from it are missing for a
        reason the daemon can no longer excuse, so they announce."""
        self._visible_world()
        self._write(self._arbitrary([]), seen_v=2)
        self.assertEqual(len(_mail(self.poller.poll(self.poller.load()))), 7)

    def test_a_first_run_is_not_a_migration(self):
        state = self.poller.load()          # no file yet
        self.assertFalse(state.get("legacy_seen"))
        self.assertIsNone(state["since"])

    def test_a_long_downtime_still_announces_what_arrived_while_the_daemon_was_down(self):
        """The boundary is the file's watermark, not the load time: what arrived during the nap is
        exactly what resume coverage is for, and stays news."""
        self.looked = T0 - timedelta(days=2)
        self.since = self.looked - iw.OVERLAP
        self.hub.add(at=self._ago(seconds=100))
        during = self.hub.add(at=T0 - timedelta(days=1))
        self._write(self._arbitrary([]))
        self.assertEqual(_message_ids(self.poller.poll(self.poller.load())), [during["message_id"]])


class TestFileLayout(unittest.TestCase):
    """A review found the ``if __name__ == "__main__"`` guard sat ABOVE the
    last TestCase, so ``python3 tests/test_inbox_wake.py`` ran 21 of 25 — and the
    four it skipped were the watchdog pins. Discovery (``just test``) imports the
    module and was unaffected, which is exactly why nothing noticed."""

    def test_main_guard_is_the_last_statement_in_this_file(self):
        with open(__file__, encoding="utf-8") as fh:
            src = fh.read()
        guard = src.index('if __name__ == "__main__":')
        self.assertNotIn("\nclass ", src[guard:],
                         "a TestCase defined below the __main__ guard never runs under direct execution")


if __name__ == "__main__":
    unittest.main()
