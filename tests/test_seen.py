#!/usr/bin/env python3
"""The dedup memory the jira, inbox and bitbucket daemons share.

Each of the three saved ``list(state["seen"])[-CAP:]`` from a SET, the line a later fix removed
from ``github_wake``: past the cap an arbitrary subset of the ids survived, so after a restart
whatever the source still returned and the cut had dropped was announced again. The fix is an
ordered memory per kind, pruned in the running process, and a migration for the file the old
code wrote. ``_seen`` holds the parts that do not depend on which source it is.

These tests are the module's own contract. What each daemon does with it, and the numbers each
one sizes it to, are in ``test_jira_wake`` / ``test_inbox_wake`` / ``test_bitbucket_wake``.

Run: `just test` (or `python3 -m unittest discover -s tests`).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))

import _degraded  # noqa: E402
import _seen  # noqa: E402

UTC = timezone.utc


class Container(unittest.TestCase):

    def test_the_container_is_ordered_and_never_a_set(self):
        """The root cause, directly: a set would make the pruning arbitrary however
        correct the pruning itself is."""
        seen = _seen.new_seen(["comment:1", "change:2", "created:ACME-3"])
        self.assertNotIsInstance(seen, set)
        seen["comment:4"] = None
        self.assertEqual(list(seen), ["comment:1", "change:2", "created:ACME-3", "comment:4"])
        self.assertIn("change:2", seen)

    def test_touch_moves_a_known_id_to_the_newest_position(self):
        """Eviction must be LAST-seen, not first-seen: re-assigning a key in a dict keeps its
        old place, so an id inserted once and merely re-observed would drift to the front."""
        seen = _seen.new_seen(["comment:1", "change:2", "comment:3"])
        _seen.touch_seen(seen, "change:2")
        self.assertEqual(list(seen), ["comment:1", "comment:3", "change:2"])

    def test_touch_is_a_no_op_for_an_unknown_id(self):
        seen = _seen.new_seen(["comment:1"])
        _seen.touch_seen(seen, "comment:99")
        self.assertEqual(list(seen), ["comment:1"])

    def test_a_set_from_an_older_caller_is_upgraded_in_place(self):
        """Every state built before this (and the fixtures made from one) carries a set."""
        state = {"seen": set(["comment:1"])}
        seen = _seen.ordered_seen(state)
        self.assertNotIsInstance(seen, set)
        self.assertIs(state["seen"], seen, "the upgrade must be stored back on the state")
        self.assertEqual(list(seen), ["comment:1"])

    def test_an_ordered_container_is_returned_as_it_is(self):
        state = {"seen": _seen.new_seen(["a:1", "b:2"])}
        before = state["seen"]
        self.assertIs(_seen.ordered_seen(state), before)

    def test_a_state_with_no_seen_gets_an_empty_one(self):
        state = {}
        self.assertEqual(list(_seen.ordered_seen(state)), [])
        self.assertIn("seen", state)


class Pruning(unittest.TestCase):

    CAPS = {"comment": 10, "change": 20}

    def test_insertion_order_is_preserved(self):
        order = ["comment:%d" % i for i in range(7)]
        self.assertEqual(_seen.prune_seen(order, self.CAPS), order)

    def test_the_newest_ids_of_an_overflowing_kind_are_kept_in_order(self):
        order = ["comment:%d" % i for i in range(25)]
        kept = _seen.prune_seen(order, self.CAPS)
        self.assertEqual(kept, order[-10:])
        self.assertNotIn("comment:0", kept)

    def test_it_keeps_the_newest_and_not_the_oldest(self):
        """The two ways to be wrong about a cap look identical until the ids are read."""
        kept = _seen.prune_seen(["comment:%d" % i for i in range(11)], {"comment": 3})
        self.assertEqual(kept, ["comment:8", "comment:9", "comment:10"])

    def test_a_burst_of_one_kind_cannot_evict_another_kinds_ids(self):
        """One shared budget was the old bug's second half: the busiest kind won the
        eviction race against the kind that needed the longest memory."""
        order = (["comment:%d" % i for i in range(4)]
                 + ["change:%d" % i for i in range(500)]
                 + ["comment:%d" % i for i in range(100, 103)])
        kept = _seen.prune_seen(order, self.CAPS)
        self.assertEqual([m for m in kept if m.startswith("comment:")],
                         ["comment:0", "comment:1", "comment:2", "comment:3",
                          "comment:100", "comment:101", "comment:102"])
        self.assertEqual(len([m for m in kept if m.startswith("change:")]), 20)

    def test_the_kinds_stay_interleaved_as_they_arrived(self):
        order = ["comment:1", "change:1", "comment:2", "change:2"]
        self.assertEqual(_seen.prune_seen(order, self.CAPS), order)

    def test_unknown_kinds_share_one_bounded_budget(self):
        """A kind nobody sized still gets a bound, one bucket for ALL of them, so a stream of
        distinct unknown prefixes cannot grow the file without limit."""
        order = ["weird%d:%d" % (k, i) for k in range(50) for i in range(20)]
        self.assertEqual(len(_seen.prune_seen(order, self.CAPS)), _seen.SEEN_CAP_OTHER)

    def test_the_shared_bucket_size_can_be_chosen(self):
        order = ["weird%d:%d" % (k, i) for k in range(5) for i in range(20)]
        self.assertEqual(len(_seen.prune_seen(order, self.CAPS, other_cap=7)), 7)

    def test_an_id_with_no_prefix_is_an_unknown_kind_by_default(self):
        self.assertEqual(len(_seen.prune_seen(["a%d" % i for i in range(400)], self.CAPS)),
                         _seen.SEEN_CAP_OTHER)

    def test_a_daemon_whose_ids_carry_no_prefix_names_their_kind(self):
        """inbox_wake's ids are bare event ids and must stay bare (a rollback compares them
        as they are), so the kind cannot come from a prefix there."""
        order = ["ev-%d" % i for i in range(50)]
        kept = _seen.prune_seen(order, {"event": 5}, kind_of=lambda mid: "event")
        self.assertEqual(kept, order[-5:])

    def test_an_id_that_is_not_a_string_is_bounded_and_never_raises(self):
        kept = _seen.prune_seen([1, 2, 3, None, ("a", "b")], self.CAPS)
        self.assertEqual(kept, [1, 2, 3, None, ("a", "b")])

    def test_an_ordered_mapping_is_accepted_and_the_result_is_a_list(self):
        seen = _seen.new_seen(["comment:%d" % i for i in range(15)])
        kept = _seen.prune_seen(seen, self.CAPS)
        self.assertIsInstance(kept, list)
        self.assertEqual(kept, ["comment:%d" % i for i in range(5, 15)])

    def test_the_kind_is_the_prefix_before_the_first_colon(self):
        self.assertEqual(_seen.id_kind("run:123:2"), "run")
        self.assertEqual(_seen.id_kind("created:ACME-9"), "created")
        self.assertEqual(_seen.id_kind("c:860701407"), "c")
        self.assertIsNone(_seen.id_kind("aaaaaaaa-0000-4000"))
        self.assertIsNone(_seen.id_kind(17))

    def test_caps_scale_with_the_number_of_units_a_profile_watches(self):
        """A window is per repo, or per credential: a profile with three asks three times the
        questions and must remember three times the answers."""
        self.assertEqual(_seen.scaled(self.CAPS, 3), {"comment": 30, "change": 60})
        self.assertEqual(_seen.scaled(self.CAPS, 1), self.CAPS)
        self.assertEqual(_seen.scaled(self.CAPS, 0), self.CAPS, "no units is still a usable cap")
        self.assertEqual(_seen.scaled(self.CAPS, None), self.CAPS)

    def test_scaling_does_not_change_the_table_it_was_given(self):
        caps = dict(self.CAPS)
        _seen.scaled(caps, 5)
        self.assertEqual(caps, self.CAPS)

    def test_the_caps_are_a_window_times_a_number_of_windows(self):
        self.assertEqual(_seen.caps_from({"a": 50, "b": 7}, {"a": 6, "b": 2}), {"a": 300, "b": 14})


class Polls(unittest.TestCase):
    """How many consecutive polls an id of a `since`-filtered query can be returned in.

    An id is returned while the query's floor (the watermark less the overlap) is at or before it,
    and the watermark moves on one poll a time: the overlap PLUS one poll, in polls, ROUNDED UP,
    because a partial poll is still a poll the id can be returned in.
    """

    def test_a_sixty_second_poll_with_a_five_minute_overlap_is_six_polls(self):
        self.assertEqual(_seen.returnable_polls(timedelta(minutes=5), 60), 6)

    def test_a_ninety_second_poll_rounds_up_not_down(self):
        """390 s / 90 s = 4.33: an id created just before a poll can be returned in five, and
        rounding down to four is a cap that is sized for a world that does not exist."""
        self.assertEqual(_seen.returnable_polls(timedelta(minutes=5), 90), 5)

    def test_the_edges(self):
        self.assertEqual(_seen.returnable_polls(timedelta(0), 60), 1)
        self.assertEqual(_seen.returnable_polls(timedelta(minutes=5), 300), 2)
        self.assertEqual(_seen.returnable_polls(timedelta(minutes=5), 45), 8)


class Timestamps(unittest.TestCase):
    """The daemons run under /usr/bin/python3 (3.9), whose ``fromisoformat`` takes no ``Z``, no
    ``-0700`` and only a 3- or 6-digit fraction; every spelling a source uses is normalised."""

    CASES = {
        "2026-09-29T16:10:16Z": datetime(2026, 9, 29, 16, 10, 16, tzinfo=UTC),
        "2026-09-29T16:10:16.573374Z": datetime(2026, 9, 29, 16, 10, 16, 573374, tzinfo=UTC),
        "2026-09-29T16:10:16.5Z": datetime(2026, 9, 29, 16, 10, 16, 500000, tzinfo=UTC),
        "2026-09-29T16:10:16.250Z": datetime(2026, 9, 29, 16, 10, 16, 250000, tzinfo=UTC),
        "2026-09-29T16:10:16.1234567Z": datetime(2026, 9, 29, 16, 10, 16, 123456, tzinfo=UTC),
        "2026-09-29T16:10:16+00:00": datetime(2026, 9, 29, 16, 10, 16, tzinfo=UTC),
        "2026-09-29T16:10:16.721502+00:00": datetime(2026, 9, 29, 16, 10, 16, 721502, tzinfo=UTC),
        "2026-09-29T16:10:16.5+00:00": datetime(2026, 9, 29, 16, 10, 16, 500000, tzinfo=UTC),
        # Jira's own spelling: a numeric offset with no colon.
        "2026-08-06T13:31:18.975-0700": datetime(2026, 8, 6, 20, 31, 18, 975000, tzinfo=UTC),
        "2026-08-06T13:31:18-07:00": datetime(2026, 8, 6, 20, 31, 18, tzinfo=UTC),
        "2026-08-06T13:31:18.975+0530": datetime(2026, 8, 6, 8, 1, 18, 975000, tzinfo=UTC),
        # No zone at all is taken as UTC, as the inbox's `at` sort always did.
        "2026-09-29T16:10:16": datetime(2026, 9, 29, 16, 10, 16, tzinfo=UTC),
    }

    def test_every_spelling_a_source_uses_parses_to_the_same_instant(self):
        for raw, expected in self.CASES.items():
            with self.subTest(raw=raw):
                parsed = _seen.parse_ts(raw)
                self.assertEqual(parsed, expected)
                self.assertEqual(parsed.utcoffset(), timedelta(0), "the result is UTC")

    def test_what_is_not_a_timestamp_is_none_and_never_raises(self):
        for raw in (None, "", "not a time", "last tuesday", 12345, [], {}, b"2026-09-29T16:10:16Z"):
            with self.subTest(raw=raw):
                self.assertIsNone(_seen.parse_ts(raw))

    def test_predates_is_inclusive_of_the_cutoff(self):
        cutoff = datetime(2026, 9, 29, 16, 10, 16, tzinfo=UTC)
        self.assertTrue(_seen.predates("2026-09-29T16:10:16Z", cutoff))
        self.assertTrue(_seen.predates("2026-09-29T16:10:15Z", cutoff))
        self.assertFalse(_seen.predates("2026-09-29T16:10:17Z", cutoff))

    def test_predates_needs_positive_evidence(self):
        """No cutoff, no timestamp, or one that does not parse is never `old`: an item that
        cannot be proven old is announced, because a duplicate costs a turn and a swallowed
        event costs the event."""
        cutoff = datetime(2026, 9, 29, 16, 10, 16, tzinfo=UTC)
        self.assertFalse(_seen.predates("2026-09-29T16:10:15Z", None))
        self.assertFalse(_seen.predates(None, cutoff))
        self.assertFalse(_seen.predates("", cutoff))
        self.assertFalse(_seen.predates("last tuesday", cutoff))


class Baseline(unittest.TestCase):
    """The migration from a state file written before the ordered format existed."""

    SINCE = "2026-09-29T16:10:16.721502+00:00"
    #: Stands in for a daemon's old cap: what the previous code's ``list(seen)[-CAP:]`` kept.
    CAP = 3
    #: The sub-fetches of a daemon whose failure leaves a slice of the dedup memory unread.
    SUBS = ("comments", "changelog")

    def at_cap(self, held=None):
        return ["a:%d" % i for i in range(self.CAP if held is None else held)]

    def file(self, **extra):
        """An old-format state file AT its old cap, as the previous code left it, so that on its own it
        could have lost ids. Keyword arguments replace or add keys."""
        raw = {"since": self.SINCE, "seen": self.at_cap()}
        raw.update(extra)
        return raw

    @staticmethod
    def episodes(open_subs=(), closed_subs=()):
        """``degraded`` as `_degraded.reconcile` leaves it in a state file: open for the subs that failed in
        the last cycle (all recorded in ONE cycle, as a daemon does, since a cycle that does not fail a sub
        recovers its open episode), closed and still on record for the ones that then recovered."""
        out = {}
        if closed_subs:
            cycle = _degraded.Cycle()
            for sub in closed_subs:
                cycle.record(sub, "HTTP 502")
            _degraded.reconcile(cycle, out, now=1000.0)
            _degraded.reconcile(_degraded.Cycle(), out, now=1060.0)          # they recover
        if open_subs:
            cycle = _degraded.Cycle()
            for sub in open_subs:
                cycle.record(sub, "HTTP 502")
            _degraded.reconcile(cycle, out, now=1120.0)
        return out

    def test_the_constants(self):
        self.assertEqual(_seen.SEEN_V, 2)
        self.assertEqual(_seen.SEEN_CAP_OTHER, 300)
        self.assertEqual(_seen.LEGACY_MAX_CYCLES, 10)

    def test_a_marked_file_gets_no_baseline(self):
        """The marker means the order in the file is the memory. An id missing from it is
        missing for a reason nothing can excuse any more, so it announces."""
        self.assertIsNone(_seen.legacy_baseline(self.file(seen_v=_seen.SEEN_V), self.CAP))

    def test_an_unmarked_file_gets_its_own_watermark_and_a_cycle_budget(self):
        legacy = _seen.legacy_baseline(self.file(), self.CAP)
        self.assertEqual(legacy, {"cutoff": self.SINCE, "cycles": _seen.LEGACY_MAX_CYCLES})

    def test_a_marker_from_another_version_is_not_this_ones(self):
        self.assertIsNotNone(_seen.legacy_baseline(self.file(seen_v=1), self.CAP))
        self.assertIsNotNone(_seen.legacy_baseline(self.file(seen_v=3), self.CAP))

    def test_a_watermark_that_trails_the_last_look_is_moved_up_to_it(self):
        """inbox_wake saves ``cycle_start - OVERLAP`` as `since`, so the last time the old
        daemon LOOKED is five minutes later than the file says."""
        legacy = _seen.legacy_baseline(self.file(), self.CAP, lag=timedelta(minutes=5))
        self.assertEqual(_seen.parse_ts(legacy["cutoff"]),
                         datetime(2026, 9, 29, 16, 15, 16, 721502, tzinfo=UTC))

    def test_no_usable_watermark_means_no_cutoff(self):
        for raw in ({"seen": self.at_cap()}, self.file(since=None), self.file(since="not a time")):
            for lag in (None, timedelta(minutes=5)):
                with self.subTest(raw=raw, lag=lag):
                    legacy = _seen.legacy_baseline(raw, self.CAP, lag=lag)
                    self.assertIsNone(_seen.legacy_cutoff(legacy, timedelta(seconds=60)))

    def test_the_cutoff_is_the_watermark_less_the_guard(self):
        legacy = _seen.legacy_baseline(self.file(since="2026-09-29T16:10:16+00:00"), self.CAP)
        self.assertEqual(_seen.legacy_cutoff(legacy, timedelta(seconds=60)),
                         datetime(2026, 9, 29, 16, 9, 16, tzinfo=UTC))
        self.assertEqual(_seen.legacy_cutoff(legacy, timedelta(seconds=90)),
                         datetime(2026, 9, 29, 16, 8, 46, tzinfo=UTC))

    def test_no_baseline_means_no_cutoff(self):
        self.assertIsNone(_seen.legacy_cutoff(None, timedelta(seconds=60)))

    # -- Adopt only where the file says ids were LOST and the last cycle READ everything --

    def test_a_file_under_the_old_cap_gets_no_cutoff(self):
        """The previous code dropped an id only at its cut, list(seen)[-cap:], and a set only grows, so a
        file holding fewer ids than the cap has never lost one. An item it does not list was never
        announced, and adopting it would swallow it."""
        for held in (0, 1, self.CAP - 1):
            with self.subTest(held=held):
                legacy = _seen.legacy_baseline(self.file(seen=self.at_cap(held)), self.CAP)
                self.assertIsNone(legacy["cutoff"])
                self.assertIn("under the old cap of %d" % self.CAP, legacy["why"])
                self.assertIsNone(_seen.legacy_cutoff(legacy, timedelta(seconds=60)))
                self.assertEqual(legacy["cycles"], _seen.LEGACY_MAX_CYCLES)

    def test_a_file_at_or_over_the_old_cap_keeps_its_cutoff(self):
        """At the cap ids WERE being dropped, which is what the adoption exists for. (Over it cannot
        happen to a file the previous code wrote, and is treated the same.)"""
        for held in (self.CAP, self.CAP + 1, 10 * self.CAP):
            with self.subTest(held=held):
                legacy = _seen.legacy_baseline(self.file(seen=self.at_cap(held)), self.CAP)
                self.assertEqual(legacy, {"cutoff": self.SINCE, "cycles": _seen.LEGACY_MAX_CYCLES})

    def test_a_missing_seen_list_is_under_every_cap(self):
        for raw in ({"since": self.SINCE}, {"since": self.SINCE, "seen": None}, {"since": self.SINCE, "seen": []}):
            with self.subTest(raw=raw):
                self.assertIsNone(_seen.legacy_baseline(raw, self.CAP)["cutoff"])

    def test_an_open_episode_for_a_sub_that_feeds_the_memory_gets_no_cutoff(self):
        """An open degraded episode means that slice was NOT fetched in the last cycle the old daemon
        saved, and that cycle still advanced `since`: "stamped before its last look" does not mean "read"."""
        for sub in self.SUBS:
            with self.subTest(sub=sub):
                legacy = _seen.legacy_baseline(self.file(degraded=self.episodes(open_subs=[sub])), self.CAP,
                                               subs=self.SUBS)
                self.assertIsNone(legacy["cutoff"])
                self.assertIn(sub, legacy["why"])
                self.assertIn("open degraded episode", legacy["why"])
                self.assertIsNone(_seen.legacy_cutoff(legacy, timedelta(seconds=60)))

    def test_every_open_sub_is_named(self):
        legacy = _seen.legacy_baseline(self.file(degraded=self.episodes(open_subs=["comments", "changelog"])),
                                       self.CAP, subs=self.SUBS)
        self.assertIn("changelog, comments", legacy["why"])

    def test_a_closed_episode_or_one_for_another_sub_does_not_stop_the_adoption(self):
        """Closed: the slice was read again since. Another sub: it does not feed the memory."""
        closed = self.episodes(closed_subs=["comments"])
        self.assertFalse(closed["comments"]["open"], "the fixture must be a closed episode that is still on record")
        elsewhere = self.episodes(open_subs=["pr_state"])
        for degraded in (closed, elsewhere, {}, None, [], "garbage"):
            with self.subTest(degraded=degraded):
                legacy = _seen.legacy_baseline(self.file(degraded=degraded), self.CAP, subs=self.SUBS)
                self.assertEqual(legacy, {"cutoff": self.SINCE, "cycles": _seen.LEGACY_MAX_CYCLES})

    def test_a_daemon_that_names_no_subs_ignores_episodes(self):
        """inbox_wake has no per-fetch holes: a failed call raises out of the cycle before anything is saved."""
        legacy = _seen.legacy_baseline(self.file(degraded=self.episodes(open_subs=["comments"])), self.CAP)
        self.assertEqual(legacy["cutoff"], self.SINCE)

    def test_under_the_cap_is_the_reason_given_when_both_hold(self):
        legacy = _seen.legacy_baseline(self.file(seen=self.at_cap(1), degraded=self.episodes(open_subs=["comments"])),
                                       self.CAP, subs=self.SUBS)
        self.assertIsNone(legacy["cutoff"])
        self.assertIn("under the old cap", legacy["why"])

    def test_the_old_cap_cannot_be_left_out(self):
        """A baseline that never asks whether the file could have lost an id is the bug this closes, so the cap
        is a required argument and not a default that can be forgotten."""
        with self.assertRaises(TypeError):
            _seen.legacy_baseline(self.file())

    def test_the_daemon_log_says_the_file_is_the_old_format(self):
        state = {"seen": dict.fromkeys(["a:%d" % i for i in range(1500)]),
                 "legacy_seen": _seen.legacy_baseline(self.file(), self.CAP)}
        said = _seen.legacy_note("acme", state, timedelta(seconds=60))
        self.assertIn("acme", said)
        self.assertIn("predates", said)
        self.assertIn("1500 ids", said)
        self.assertIn("60s", said)
        self.assertIn(self.SINCE, said)

    def test_the_daemon_log_says_why_nothing_is_adopted(self):
        for raw, reason in ((self.file(seen=self.at_cap(2)), "under the old cap of %d" % self.CAP),
                            (self.file(degraded=self.episodes(open_subs=["changelog"])), "changelog")):
            with self.subTest(reason=reason):
                state = {"seen": dict.fromkeys(raw["seen"]),
                         "legacy_seen": _seen.legacy_baseline(raw, self.CAP, subs=self.SUBS)}
                said = _seen.legacy_note("acme", state, timedelta(seconds=60))
                self.assertIn("predates", said)
                self.assertIn("adopts nothing", said)
                self.assertIn("announces", said)
                self.assertIn(reason, said)
                self.assertNotIn("adopts silently", said)

    def test_it_says_nothing_for_a_file_that_needs_no_migration(self):
        self.assertIsNone(_seen.legacy_note("p", {"legacy_seen": None, "seen": {}}, timedelta(seconds=60)))
        self.assertIsNone(_seen.legacy_note("p", {"seen": {}}, timedelta(seconds=60)))

    LABELS = (("comment", "comment(s)"), ("change", "change(s)"))
    CUTOFF = datetime(2026, 9, 29, 16, 9, 16, tzinfo=UTC)

    def _settle(self, legacy, adopted=None, unread=(), cutoff=CUTOFF):
        notes = []
        state = {"legacy_seen": legacy}
        _seen.settle_legacy("acme", state, legacy, cutoff,
                            dict(adopted or {"comment": 0, "change": 0}), list(unread), self.LABELS,
                            notes.append)
        return state, notes

    def test_a_clean_cycle_ends_the_baseline_and_says_what_it_adopted(self):
        state, notes = self._settle(_seen.legacy_baseline(self.file(), self.CAP),
                                    {"comment": 12, "change": 3})
        self.assertIsNone(state["legacy_seen"])
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("adopted 12 comment(s), 3 change(s) silently", notes[0])
        self.assertIn("2026-09-29T16:09:16Z", notes[0])

    def test_a_cycle_that_adopted_nothing_says_nothing(self):
        state, notes = self._settle(_seen.legacy_baseline(self.file(), self.CAP))
        self.assertIsNone(state["legacy_seen"])
        self.assertEqual(notes, [])

    def test_a_fetch_that_feeds_the_memory_failing_holds_the_baseline_for_the_next_cycle(self):
        """A hole in the baseline cycle leaves its scope unbaselined; holding the baseline is
        the cheap repair. Without it what the failed fetch never read is announced next cycle."""
        legacy = _seen.legacy_baseline(self.file(), self.CAP)
        state, notes = self._settle(legacy, unread=["comments"])
        self.assertIs(state["legacy_seen"], legacy)
        self.assertEqual(legacy["cycles"], _seen.LEGACY_MAX_CYCLES - 1)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("comments not read this cycle", notes[0])
        self.assertIn("(%d left)" % (_seen.LEGACY_MAX_CYCLES - 1), notes[0])

    def test_a_hole_that_never_heals_cannot_keep_the_baseline_forever(self):
        legacy = _seen.legacy_baseline(self.file(), self.CAP)
        state = {"legacy_seen": legacy}
        notes = []
        for _ in range(_seen.LEGACY_MAX_CYCLES):
            _seen.settle_legacy("p", state, legacy, self.CUTOFF, {"comment": 0, "change": 0},
                                ["comments"], self.LABELS, notes.append)
        self.assertIsNone(state["legacy_seen"])
        self.assertIn("baseline is dropped", notes[-1])

    def test_no_cutoff_leaves_nothing_to_hold_even_after_a_failed_fetch(self):
        """With no watermark nothing can ever be adopted: holding the baseline would only keep
        an inert rule and a misleading note."""
        state, notes = self._settle(_seen.legacy_baseline(self.file(since=None), self.CAP), unread=["comments"],
                                    cutoff=None)
        self.assertIsNone(state["legacy_seen"])
        self.assertEqual(notes, [])

    def test_a_baseline_that_adopts_nothing_is_cleared_after_its_cycle_without_a_word(self):
        """The load-time note already said why; the cycle has nothing to add, and there is nothing to hold."""
        legacy = _seen.legacy_baseline(self.file(seen=self.at_cap(1)), self.CAP)
        state, notes = self._settle(legacy, unread=["comments"], cutoff=_seen.legacy_cutoff(legacy, timedelta(seconds=60)))
        self.assertIsNone(state["legacy_seen"])
        self.assertEqual(notes, [])


class FileLayout(unittest.TestCase):
    """The ``__main__`` guard sits below the last TestCase, or direct execution skips tests
    (the shape a review found in another daemon's test module)."""

    def test_main_guard_is_the_last_statement_in_this_file(self):
        with open(__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("\nclass ", src[src.index('if __name__ == "__main__":'):])


if __name__ == "__main__":
    unittest.main()
