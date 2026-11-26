#!/usr/bin/env python3
"""jira_wake's dedup memory survives a restart, and the first tests this daemon has had.

The daemon saved ``list(state["seen"])[-1500:]``. ``seen`` was a SET, so that slice is hash
order, randomised per process: past 1500 ids an arbitrary 1500 were kept and the rest dropped at
random, and whatever Jira still returned that had been dropped was announced a second time
after the next restart. The live state file was already at exactly 1500. What comes back is not
ancient history: the daemon only considers an item newer than its watermark less five minutes,
so it is the last few minutes before the restart, and it can be a "status -> Done" or a
"CLAIMED" comment that a seat then acts on as if it were new. It is the fix already made for
``github_wake``, adapted to this daemon's kinds (``created:`` ``comment:`` ``change:``) and windows.

The poller is exercised through the daemon's own load, poll and save against a miniature Jira
that serves the API's own windows (one page of 50 issues, an issue's newest 10 comments and last
15 changelog entries) on a simulated clock, so an item ages out of the query as it does in life.
No network, no real time, and nothing in the live Watchtower directories.

Run: `just test` (or `python3 -m unittest discover -s tests`).
"""
import json
import os
import random
import re
import sys
import time
import unittest
from datetime import timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "daemons"))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _degraded  # noqa: E402
import _seen  # noqa: E402
import jira_wake as jw  # noqa: E402
from wake_sim import FakeClock, Poller, T0  # noqa: E402

BOB = {"accountId": "acct-bob", "displayName": "Bob"}
ALICE = {"accountId": "acct-alice", "displayName": "Alice"}
CFG = {"site": "https://example.atlassian.net", "email": "agent@example.test",
       "api_token": "test-token-not-real", "projects": ["ACME"],
       "suppress_account_ids": [ALICE["accountId"]]}


class _Jira(object):
    """A miniature Jira: the reads the daemon makes, served with the API's own windows.

    The windowing is the point. The search answers ONE page of the ``maxResults`` most recently
    updated issues inside the JQL's ``updated >= -Nm`` window; an issue's comments answer the
    ``maxResults`` newest; its changelog answers ``total`` and the slice asked for. Everything is
    read from the request, so a daemon that changes what it asks for is served what Jira would
    serve for that, and a request this hub does not model fails the test instead of quietly
    answering empty. Times are the clock's, in Jira's own spelling (a numeric offset, no colon).
    """

    ZONE = timezone(timedelta(hours=-7))

    def __init__(self, clock):
        self.clock = clock
        self.issues = {}
        self.asked = []                      # every request the daemon made, in order
        self.fail = ()                       # substrings of paths that answer 502
        self._seq = 20000
        self._arrival = 0

    @classmethod
    def stamp(cls, when):
        local = when.astimezone(cls.ZONE)
        return local.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (local.microsecond // 1000) + "-0700"

    def _next(self):
        self._seq += 1
        return str(self._seq)

    # -- the world -------------------------------------------------------------------------

    def add_issue(self, key, summary=None, creator=BOB, at=None):
        at = at or self.clock.now()
        self._arrival += 1
        self.issues[key] = {"key": key, "summary": summary or "issue %s" % key, "creator": creator,
                            "created": at, "updated": at, "arrival": self._arrival,
                            "comments": [], "changes": []}
        return self.issues[key]

    def comment(self, key, body="a comment", author=BOB, at=None):
        issue, at = self.issues[key], at or self.clock.now()
        found = {"id": self._next(), "at": at, "author": author, "body": body}
        issue["comments"].append(found)
        issue["updated"] = max(issue["updated"], at)
        return found

    def change(self, key, field="status", to="In Review", author=BOB, at=None):
        issue, at = self.issues[key], at or self.clock.now()
        found = {"id": self._next(), "at": at, "author": author, "field": field, "to": to}
        issue["changes"].append(found)
        issue["updated"] = max(issue["updated"], at)
        return found

    # -- what the daemon calls -------------------------------------------------------------

    def __call__(self, cfg):
        """``jira_wake.Jira(cfg)``: the daemon builds a client every cycle; each is this hub."""
        return self

    def search(self, jql, fields, max_results):
        self.asked.append(("search", jql, fields, max_results))
        window = int(re.search(r"updated >= -(\d+)m", jql).group(1))
        floor = self.clock.now() - timedelta(minutes=window)
        hits = sorted((i for i in self.issues.values() if i["updated"] >= floor),
                      key=lambda i: (i["updated"], i["arrival"]), reverse=True)
        return {"issues": [{"key": i["key"], "fields": {
            "summary": i["summary"], "created": self.stamp(i["created"]),
            "updated": self.stamp(i["updated"]), "creator": i["creator"]}} for i in hits[:max_results]]}

    def get(self, path, params=None):
        params = dict(params or {})
        self.asked.append(("get", path, params))
        for needle in self.fail:
            if needle in path:
                raise RuntimeError("HTTP 502 on %s" % path)
        found = re.match(r"^/rest/api/3/issue/([A-Z]+-\d+)/(comment|changelog)$", path)
        if not found:
            raise AssertionError("the daemon asked for something this hub does not model: %s" % path)
        issue = self.issues[found.group(1)]
        if found.group(2) == "comment":
            if params.get("orderBy") != "-created":
                raise AssertionError("the hub only models the newest-first comment order: %r" % params)
            newest = sorted(issue["comments"], key=lambda c: (c["at"], int(c["id"])), reverse=True)
            return {"comments": [{"id": c["id"], "created": self.stamp(c["at"]), "author": c["author"],
                                  "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                                      {"type": "text", "text": c["body"]}]}]}}
                                 for c in newest[:int(params["maxResults"])]]}
        entries = issue["changes"]                                   # oldest first, as Jira serves them
        start = int(params.get("startAt", 0))
        return {"total": len(entries), "values": [
            {"id": e["id"], "created": self.stamp(e["at"]), "author": e["author"],
             "items": [{"field": e["field"], "toString": e["to"]}]}
            for e in entries[start:start + int(params["maxResults"])]]}


def _news(lines):
    """The created, comment and change lines among ``lines``: the three kinds the memory guards."""
    return [line for line in lines if line.startswith(("type=created ", "type=comment ", "type=change "))]


def _titles(lines):
    """What each of those lines says after its `` | ``: the summary, the comment or the change."""
    return sorted(line.split(" | ", 1)[1] for line in _news(lines))


POOL = ["ACME-%d" % n for n in range(101, 131)]


def _world(case):
    clock = FakeClock(T0)
    hub = _Jira(clock)
    poller = Poller(case, jw, dict(CFG), {"Jira": hub}, clock=clock)
    return hub, poller


def _seed_pool(hub):
    """Thirty issues, created and last touched a day before anyone looked."""
    for key in POOL:
        hub.add_issue(key, at=T0 - timedelta(days=1))


def _churn(hub, poller, state, ticks, comments=4, changes=4, new_issue_every=3):
    """The everyday load: a few comments and status changes a poll, a new issue now and then, and
    now and again an event by the account the daemon suppresses. Returns every line emitted."""
    said = []
    for i in range(ticks):
        for k in range(comments):
            n = i * comments + k
            hub.comment(POOL[n % len(POOL)], body="comment %d" % n, author=ALICE if n % 5 == 0 else BOB)
        for k in range(changes):
            n = i * changes + k
            hub.change(POOL[(n + 7) % len(POOL)], to="status %d" % n, author=ALICE if n % 7 == 0 else BOB)
        if new_issue_every and i % new_issue_every == 0:
            hub.add_issue("ACME-%d" % (5000 + i), summary="new issue %d" % i)
        said.extend(poller.poll(state))
    return said


def _kinds(seen):
    counts = {}
    for mid in seen:
        counts[mid.split(":", 1)[0]] = counts.get(mid.split(":", 1)[0], 0) + 1
    return counts


class LinesAndIds(unittest.TestCase):
    """What the daemon says and what it remembers, pinned BEFORE the memory changed.

    These pass on the old code and on the new: the fix is the saved state, its pruning and its
    migration, and the lines the PM's patterns, `just unactioned` and the seats' Monitor filters
    parse must come out byte for byte as they did. So must the ids, because a rollback compares
    the ids the new code saved with the ids the old code makes.
    """

    def test_the_three_line_formats_are_unchanged(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", summary="Ship the thing")
        hub.comment("ACME-1", body="please review", author=BOB)
        hub.change("ACME-1", field="status", to="In Review", author=BOB)
        self.assertEqual(poller.poll(poller.load()), [
            'type=created issue=ACME-1 by="Bob" | Ship the thing',
            'type=comment issue=ACME-1 by="Bob" | please review',
            'type=change issue=ACME-1 by="Bob" | status->"In Review"'])

    def test_a_long_comment_is_cut_and_a_comment_on_two_lines_is_folded_as_before(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-2", at=T0 - timedelta(days=1))
        hub.comment("ACME-2", body="x" * 400)
        hub.comment("ACME-2", body="line one\n\n   line two")
        lines = poller.poll(poller.load())
        self.assertEqual(len(lines), 2, lines)
        cut = [l for l in lines if " | xxxx" in l][0]
        self.assertEqual(cut, 'type=comment issue=ACME-2 by="Bob" | ' + "x" * 179 + "…")
        self.assertIn('type=comment issue=ACME-2 by="Bob" | line one line two', lines)

    def test_the_ids_written_are_the_ones_the_old_code_wrote(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1")
        comment = hub.comment("ACME-1")
        change = hub.change("ACME-1")
        poller.poll(poller.load())
        self.assertEqual(set(poller.saved()["seen"]),
                         set(["created:ACME-1", "comment:" + comment["id"], "change:" + change["id"]]))

    def test_an_event_by_a_suppressed_account_is_remembered_but_not_announced(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", at=T0 - timedelta(days=1))
        mine = hub.comment("ACME-1", author=ALICE)
        state = poller.load()
        self.assertEqual(_news(poller.poll(state)), [])
        self.assertIn("comment:" + mine["id"], state["seen"])

    def test_an_event_announced_once_is_not_announced_again_in_the_same_process(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", at=T0 - timedelta(days=1))
        hub.comment("ACME-1", body="once")
        state = poller.load()
        self.assertEqual(len(_news(poller.poll(state))), 1)
        for _ in range(8):
            self.assertEqual(_news(poller.poll(state)), [])


class JqlWindow(unittest.TestCase):
    """The stretched JQL window is not part of this change and must stay as it was:
    max(30 minutes, downtime + 10 minutes), capped at seven days."""

    def _jql_after(self, downtime):
        hub, poller = _world(self)
        poller.write({"since": (T0 - downtime).isoformat(), "seen": [], "seen_v": 2})
        poller.poll(poller.load())
        return [a[1] for a in hub.asked if a[0] == "search"][0]

    def test_a_quiet_poll_asks_for_thirty_minutes(self):
        self.assertIn("updated >= -30m ", self._jql_after(timedelta(seconds=60)))

    def test_a_nap_stretches_it_by_the_downtime_plus_ten_minutes(self):
        self.assertIn("updated >= -190m ", self._jql_after(timedelta(hours=3)))

    def test_it_is_capped_at_seven_days(self):
        self.assertIn("updated >= -10080m ", self._jql_after(timedelta(days=30)))


class SeenMemory(unittest.TestCase):
    """The sizing: derived from what one poll asks Jira for, not guessed."""

    def test_the_windows_are_what_the_poller_really_asks_for(self):
        """The window constants BUILD the requests, and this reads the requests back. A cap
        sized from a number the query does not use is a cap sized from a guess."""
        hub, poller = _world(self)
        hub.add_issue("ACME-1")
        hub.comment("ACME-1")
        hub.change("ACME-1")
        poller.poll(poller.load())
        searches = [a for a in hub.asked if a[0] == "search"]
        self.assertEqual(len(searches), 1, "the search is one page and is never followed")
        self.assertEqual(searches[0][3], jw.SEARCH_MAX_ISSUES)
        comment_asks = [a[2] for a in hub.asked if a[0] == "get" and a[1].endswith("/comment")]
        self.assertTrue(comment_asks, "the daemon never asked for comments")
        for params in comment_asks:
            self.assertEqual(params["maxResults"], jw.COMMENTS_PER_ISSUE)
        pages = [a[2] for a in hub.asked if a[0] == "get" and a[1].endswith("/changelog")
                 and "startAt" in a[2]]
        self.assertTrue(pages, "the daemon never read a changelog page")
        for params in pages:
            self.assertEqual(params["maxResults"], jw.CHANGES_PER_ISSUE)

    def test_the_windows_are_pinned(self):
        """What one poll can hand the daemon, in ids of each kind."""
        self.assertEqual((jw.SEARCH_MAX_ISSUES, jw.COMMENTS_PER_ISSUE, jw.CHANGES_PER_ISSUE), (50, 10, 15))
        self.assertEqual(jw.SEEN_WINDOW, {"created": 50, "comment": 500, "change": 750})
        self.assertEqual(jw.SEEN_WINDOW["created"], jw.SEARCH_MAX_ISSUES)
        self.assertEqual(jw.SEEN_WINDOW["comment"], jw.SEARCH_MAX_ISSUES * jw.COMMENTS_PER_ISSUE)
        self.assertEqual(jw.SEEN_WINDOW["change"], jw.SEARCH_MAX_ISSUES * jw.CHANGES_PER_ISSUE)

    def test_an_id_is_returnable_for_six_polls(self):
        """Only an item newer than the watermark less OVERLAP is considered at all, and the
        watermark moves a poll at a time: OVERLAP plus one poll, six of them, however wide the
        stretched JQL window is. The stretch widens the time range, not the one page of 50 issues."""
        self.assertEqual(jw.SEEN_POLLS, 6)
        self.assertEqual(jw.SEEN_POLLS, _seen.returnable_polls(jw.OVERLAP, jw.POLL_SECS))
        self.assertEqual(jw.SEEN_KEEP, {"created": 6, "comment": 6, "change": 6})

    def test_the_caps_are_pinned_so_a_change_is_deliberate(self):
        """created 50 x 6 = 300, comment 500 x 6 = 3,000, change 750 x 6 = 4,500: every id that
        one poll can return, for every poll it can still be returned in. Even with no refresh at
        all, an id that is returned in the last of its six polls has at most five windows of
        newer ids of its kind behind it. The reasoning is in the comment above `SEEN_KEEP`; the
        behaviour it buys is tested in `RestartKeepsTheDedupMemory`."""
        self.assertEqual(jw.SEEN_CAPS, {"created": 300, "comment": 3000, "change": 4500})

    def test_every_cap_covers_its_window_at_least_twice(self):
        for kind, window in jw.SEEN_WINDOW.items():
            self.assertGreaterEqual(jw.SEEN_CAPS[kind], 2 * window, kind)

    def test_the_kinds_are_the_prefixes_the_daemon_writes(self):
        self.assertEqual(sorted(jw.SEEN_CAPS), ["change", "comment", "created"])

    def test_the_old_cap_is_what_the_previous_code_saved(self):
        """38ffbbd saved list(seen)[-1500:], and had since the daemon was written. The migration reads it:
        a file holding fewer ids than this never lost one."""
        self.assertEqual(jw.OLD_SEEN_CAP, 1500)
        self.assertEqual(jw.SEEN_SUBS, ("comments", "changelog"))

    def test_the_window_constants_drive_the_requests(self):
        """Reading a request back proves the constant and the request agree TODAY, not that one
        drives the other: a number typed into the request again would still agree. So the constants
        are moved, and the requests follow."""
        hub, poller = _world(self)
        hub.add_issue("ACME-1")
        hub.comment("ACME-1")
        hub.change("ACME-1")
        with mock.patch.object(jw, "SEARCH_MAX_ISSUES", 7), mock.patch.object(jw, "COMMENTS_PER_ISSUE", 3), \
                mock.patch.object(jw, "CHANGES_PER_ISSUE", 4):
            poller.poll(poller.load())
        self.assertEqual([a[3] for a in hub.asked if a[0] == "search"], [7])
        self.assertEqual([a[2]["maxResults"] for a in hub.asked if a[0] == "get" and a[1].endswith("/comment")], [3])
        self.assertEqual([a[2]["maxResults"] for a in hub.asked
                          if a[0] == "get" and a[1].endswith("/changelog") and "startAt" in a[2]], [4])

    def test_a_comment_is_considered_for_exactly_the_polls_the_cap_is_sized_for(self):
        """`SEEN_POLLS` is a claim about the world, so it is COUNTED: how many polls look at a
        comment (once to see it, then once for each refresh), with the comment written at four points
        of the poll interval. Six, however it falls, as the comment above `SEEN_KEEP` says."""
        for before_poll in (0, 1, 30, 59):
            hub, poller = _world(self)
            hub.add_issue("ACME-1", at=T0 - timedelta(days=1))
            state = poller.load()
            poller.poll(state)
            found = hub.comment("ACME-1", at=poller.clock.now() - timedelta(seconds=before_poll))
            touched, real = [], _seen.touch_seen
            with mock.patch.object(_seen, "touch_seen", lambda seen, mid: (touched.append(mid), real(seen, mid))[1]):
                for _ in range(12):
                    poller.poll(state)
            polls = 1 + touched.count("comment:" + found["id"])
            self.assertEqual(polls, jw.SEEN_POLLS, "written %d s before the poll that first sees it" % before_poll)

    def test_the_caps_hold_many_of_the_busiest_days_measured_on_the_live_log(self):
        """Measured on this host's profile log, 2026-09-03..29 (26 days): 32 comments, 31 changes
        and 7 created issues on the median day, 145, 92 and 27 on the busiest. The caps hold
        11, 20 and 48 of those busiest days. The window is the floor; this is the headroom above
        it, and a cap cut below what was measured fails here."""
        self.assertGreaterEqual(jw.SEEN_CAPS["created"], 10 * 27)
        self.assertGreaterEqual(jw.SEEN_CAPS["comment"], 20 * 145)
        self.assertGreaterEqual(jw.SEEN_CAPS["change"], 40 * 92)


class RestartKeepsTheDedupMemory(unittest.TestCase):
    """The ticket's regression tests: through the real load, poll and save, over enough cycles
    that more than 1500 ids have been seen."""

    def test_a_restart_re_emits_nothing_the_source_still_returns(self):
        """THE restart test. 500 polls of everyday load: 4,000 comments and changes and 167 new
        issues, past the old 1,500 cap. Save, load, poll: Jira still returns the last few
        minutes of every busy issue, and none of it may be announced again."""
        hub, poller = _world(self)
        _seed_pool(hub)
        state = poller.load()
        said = _news(_churn(hub, poller, state, ticks=500))
        expected = (sum(1 for n in range(2000) if n % 5) + sum(1 for n in range(2000) if n % 7)
                    + len(range(0, 500, 3)))
        self.assertEqual(len(said), expected, "the simulation itself lost or repeated an event")
        self.assertEqual(len(set(said)), len(said))
        self.assertGreater(len(poller.saved()["seen"]), 0)

        restarted = poller.load()
        again = _news(poller.poll(restarted))
        self.assertEqual(again, [], "a restart re-announced %d event(s) Jira still returns: %s"
                         % (len(again), again[:3]))

        # Not vacuous: a daemon that said nothing at all would pass the line above.
        hub.comment("ACME-105", body="a genuinely new comment")
        hub.change("ACME-106", to="a genuinely new status")
        self.assertEqual(len(_news(poller.poll(restarted))), 2, "a genuinely new event was swallowed")
        # And the second restart is as quiet as the first.
        self.assertEqual(_news(poller.poll(poller.load())), [])

    def test_a_burst_of_changes_does_not_make_a_comments_id_forgettable(self):
        """Per-kind memory, as behaviour. An issue gets ten comments and then drops off the page
        of 50 while fifty others take 750 status changes a poll for four polls (3,000 change ids,
        double the old cap of everything). Its comments are still inside the query window when
        it is touched again, after a restart, and only another comment could have pushed their
        ids out of memory."""
        hub, poller = _world(self)
        quiet = "ACME-900"
        busy = ["ACME-%d" % n for n in range(901, 951)]
        for key in [quiet] + busy:
            hub.add_issue(key, at=T0 - timedelta(days=1))
        state = poller.load()
        poller.poll(state)
        for n in range(10):
            hub.comment(quiet, body="quiet %d" % n)
        self.assertEqual(len(_news(poller.poll(state))), 10)
        for tick in range(4):
            for key in busy:
                for n in range(15):
                    hub.change(key, to="%s tick %d.%d" % (key, tick, n))
            self.assertEqual(len(_news(poller.poll(state))), 750, "tick %d" % tick)
        restarted = poller.load()
        hub.comment(quiet, body="quiet again")
        self.assertEqual(_titles(poller.poll(restarted)), ["quiet again"],
                         "a comment came back as new after a burst of changes")

    def test_an_id_the_source_keeps_returning_is_refreshed_not_aged_by_first_sighting(self):
        """LAST-seen, not first-seen. With room for 20 comment ids, a comment that stays in the
        answer for five polls while 25 newer comments arrive elsewhere must be refreshed each
        time it is returned. Left to age by its FIRST sighting it is evicted after four polls
        and announced as new on the fifth, still inside its query window."""
        hub, poller = _world(self)
        for key in ("ACME-1", "ACME-2"):
            hub.add_issue(key, at=T0 - timedelta(days=1))
        state = poller.load()
        poller.poll(state)
        hub.comment("ACME-1", body="the one to keep")
        with mock.patch.object(jw, "SEEN_CAPS", {"created": 20, "comment": 20, "change": 20}):
            self.assertEqual(_titles(poller.poll(state)), ["the one to keep"])
            for tick in range(5):
                for n in range(5):
                    hub.comment("ACME-2", body="other %d.%d" % (tick, n))
                said = _titles(poller.poll(state))
                self.assertEqual(len(said), 5, "tick %d: %s" % (tick, said))
                self.assertNotIn("the one to keep", said, "tick %d announced it again" % tick)

    def test_a_change_the_source_keeps_returning_is_refreshed_too(self):
        """The same property for the other kind that comes from a per-issue page. A busy issue's
        changelog answer holds 15, so five new entries a poll push its older ones out of the answer
        while a quiet issue's stays in it; left to age by its first sighting, the quiet issue's entry
        is evicted after four polls and announced as new on the fifth."""
        hub, poller = _world(self)
        for key in ("ACME-1", "ACME-2"):
            hub.add_issue(key, at=T0 - timedelta(days=1))
        state = poller.load()
        poller.poll(state)
        hub.change("ACME-1", to="the one to keep")
        with mock.patch.object(jw, "SEEN_CAPS", {"created": 20, "comment": 20, "change": 20}):
            self.assertEqual(_titles(poller.poll(state)), ['status->"the one to keep"'])
            for tick in range(5):
                for n in range(5):
                    hub.change("ACME-2", to="other %d.%d" % (tick, n))
                said = _titles(poller.poll(state))
                self.assertEqual(len(said), 5, "tick %d: %s" % (tick, said))
                self.assertNotIn('status->"the one to keep"', said, "tick %d announced it again" % tick)

    def test_a_created_id_the_source_keeps_returning_is_refreshed_too(self):
        """The one kind with no per-issue page, so the page itself is shrunk to six issues and one
        issue is kept at the top of it by an update every poll. Four new issues arrive each poll behind
        it; left to age by its first sighting its `created:` id is evicted on the third poll and
        announced as new on the fourth, still inside the window in which a creation is news."""
        hub, poller = _world(self)
        state = poller.load()
        poller.poll(state)
        hub.add_issue("ACME-1", summary="the one to keep")
        created = lambda lines: sorted(l.split(" | ", 1)[1] for l in lines if l.startswith("type=created "))
        with mock.patch.object(jw, "SEEN_CAPS", {"created": 10, "comment": 50, "change": 50}), \
                mock.patch.object(jw, "SEARCH_MAX_ISSUES", 6):
            self.assertEqual(created(poller.poll(state)), ["the one to keep"])
            for tick in range(4):
                for n in range(4):
                    hub.add_issue("ACME-%d" % (100 + tick * 4 + n), summary="other %d.%d" % (tick, n),
                                  at=poller.clock.now() - timedelta(seconds=1))
                hub.comment("ACME-1", body="still busy %d" % tick)             # keeps it first on the page
                said = created(poller.poll(state))
                self.assertEqual(len(said), 4, "tick %d: %s" % (tick, said))
                self.assertNotIn("the one to keep", said, "tick %d announced it again" % tick)

    def test_the_saved_list_is_least_recently_seen_first_so_the_reload_evicts_the_right_ids(self):
        """The order in the FILE is the memory: ids that have left the query window sit before
        the ids still inside it. Five in each group, so a hash-ordered list cannot line up by
        luck."""
        hub, poller = _world(self)
        for key in ("ACME-1", "ACME-2", "ACME-3"):
            hub.add_issue(key, at=T0 - timedelta(days=1))
        state = poller.load()
        poller.poll(state)
        gone = [hub.comment("ACME-1", body="gone %d" % n) for n in range(5)]
        poller.poll(state)
        for _ in range(7):
            poller.poll(state)              # six polls after it was written a comment is no longer read
        alive = [hub.comment("ACME-2", body="alive %d" % n) for n in range(5)]
        poller.poll(state)
        newest = [hub.comment("ACME-3", body="newest %d" % n) for n in range(5)]
        poller.poll(state)
        seen = poller.saved()["seen"]

        def places(comments):
            return [seen.index("comment:" + c["id"]) for c in comments]
        self.assertLess(max(places(gone)), min(places(alive)), "the file is not oldest-first: %s" % seen)
        self.assertLess(max(places(gone)), min(places(newest)), "the file is not oldest-first: %s" % seen)

    def test_what_is_saved_is_exactly_what_the_running_process_knows(self):
        """A restart must change nothing, so the file has to hold the process's OWN memory: the
        same ids in the same order, pruned the same way. Otherwise the running daemon and the
        restarted one dedupe differently, which is this ticket in another shape.

        The churn is past EVERY cap at its production size: 4,800 comments against a cap of 3,000,
        4,800 changes against 4,500 and 400 creations against 300. It has to be, because a save that
        ran BEFORE the prune would write the excess, and a file that only ever gains ids under its caps
        cannot tell the two orders apart (the first version of this test churned about a fifth of that
        and could not). And it reads the FILE: the memory after the prune is right either way."""
        hub, poller = _world(self)
        _seed_pool(hub)
        state = poller.load()
        _churn(hub, poller, state, ticks=400, comments=12, changes=12, new_issue_every=1)
        saved = poller.saved()["seen"]
        self.assertEqual(list(state["seen"]), saved)
        self.assertEqual(_kinds(saved), dict(jw.SEEN_CAPS), "every kind should be exactly at its cap IN THE FILE")
        self.assertEqual(list(poller.load()["seen"]), saved)

    def test_the_memory_is_bounded_in_the_running_process_not_only_at_save(self):
        """The old in-process set grew for as long as the daemon ran. It is pruned at the end of
        every cycle now, at the caps, and never below what the query can still return. The file
        is checked as well as the memory: it is what a restart reloads."""
        hub, poller = _world(self)
        _seed_pool(hub)
        state = poller.load()
        _churn(hub, poller, state, ticks=300, comments=12, changes=0, new_issue_every=0)
        self.assertEqual(_kinds(state["seen"]).get("comment"), jw.SEEN_CAPS["comment"],
                         "3,600 comments were seen: the memory should be exactly at its cap")
        self.assertEqual(_kinds(poller.saved()["seen"]).get("comment"), jw.SEEN_CAPS["comment"],
                         "and so should the FILE, after every cycle")
        self.assertEqual(poller.saved()["seen"], list(state["seen"]))

    def test_no_live_id_is_evicted_at_the_worst_case_window(self):
        """Sizing, exercised at the API's worst case rather than a friendly one: fifty issues
        every poll, each taking two comments and three status changes, so every poll returns
        500 comment ids and 750 change ids (a whole window of each) and every id is returned in
        five polls running. 60 polls run the memory well past its cap. One live id evicted
        would be announced again the next poll."""
        hub, poller = _world(self)
        keys = ["ACME-%d" % n for n in range(700, 750)]
        for key in keys:
            hub.add_issue(key, at=T0 - timedelta(days=1))
        state = poller.load()
        poller.poll(state)
        for tick in range(60):
            for key in keys:
                for n in range(2):
                    hub.comment(key, body="%s comment %d.%d" % (key, tick, n))
                for n in range(3):
                    hub.change(key, to="%s status %d.%d" % (key, tick, n))
            said = _news(poller.poll(state))
            self.assertEqual(len(said), 50 * 5, "tick %d: an event was announced twice or not at all" % tick)
            self.assertEqual(len(set(said)), len(said))
        kinds = _kinds(state["seen"])
        self.assertEqual(kinds["comment"], jw.SEEN_CAPS["comment"], "the comment memory should be at its cap")
        self.assertEqual(_news(poller.poll(poller.load())), [], "and a restart still holds it")

    def test_restarting_before_every_single_cycle_changes_nothing(self):
        """The ticket in one property: a daemon restarted between EVERY pair of polls says exactly
        what one that never restarts says. The world is driven by a seeded generator so both runs
        see the same one: comments and changes land on a pool of issues, new issues appear, and
        for a stretch the whole pool goes quiet and comes back."""
        def play(restart_each_cycle):
            rng = random.Random(1109)
            hub, poller = _world(self)
            _seed_pool(hub)
            state = poller.load()
            said = []
            for cycle in range(700):
                if not 200 <= cycle < 500:
                    for _ in range(rng.randint(0, 6)):
                        hub.comment(rng.choice(POOL), body="c%d.%d" % (cycle, rng.randint(0, 10 ** 6)),
                                    author=ALICE if rng.random() < 0.15 else BOB)
                    for _ in range(rng.randint(0, 6)):
                        hub.change(rng.choice(POOL), to="s%d.%d" % (cycle, rng.randint(0, 10 ** 6)),
                                   author=ALICE if rng.random() < 0.15 else BOB)
                    if rng.random() < 0.3:
                        hub.add_issue("ACME-%d" % (6000 + cycle), summary="issue %d" % cycle)
                if restart_each_cycle:
                    state = poller.load()
                said.extend(_news(poller.poll(state)))
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
        return {"since": raw.get("since"), "seen": set(raw.get("seen", [])),
                "degraded": raw.get("degraded", {})}

    def _cycled(self):
        hub, poller = _world(self)
        _seed_pool(hub)
        state = poller.load()
        _churn(hub, poller, state, ticks=6)
        return poller

    def test_the_file_is_marked_as_the_ordered_format(self):
        raw = self._cycled().saved()
        self.assertEqual(raw.get("seen_v"), 2)
        self.assertIsInstance(raw["seen"], list)
        self.assertTrue(all(isinstance(x, str) for x in raw["seen"]))
        self.assertGreater(len(raw["seen"]), 0)

    def test_the_old_code_still_reads_the_file(self):
        """Rollback safety: `seen` stays a flat list of id strings and the marker is a new key the
        old loader ignores, so a rollback loses the ORDER (it resumes the arbitrary truncation)
        but never the ids, and never fails to start."""
        poller = self._cycled()
        old = self._pre_seen_memory_loader(poller.state_path)
        new = poller.load()
        self.assertEqual(old["seen"], set(new["seen"]))
        self.assertEqual(old["since"], new["since"])
        self.assertEqual(old["degraded"], new["degraded"])
        self.assertGreater(len(old["seen"]), 0)

    def test_a_marked_file_is_trusted_and_gets_no_baseline(self):
        self.assertFalse(self._cycled().load().get("legacy_seen"))

    def test_a_set_from_an_older_caller_is_upgraded_and_saved_ordered(self):
        """`poll_profile` upgrades a `seen` handed to it as a set (the shape every earlier state,
        and the fixtures built from it, carry) and saves the ordered form."""
        hub, poller = _world(self)
        hub.add_issue("ACME-1", at=T0 - timedelta(days=1))
        found = hub.comment("ACME-1", body="old news", at=T0 - timedelta(seconds=30))
        state = {"since": (T0 - timedelta(seconds=90)).isoformat(),
                 "seen": set(["comment:" + found["id"]]), "degraded": {}}
        self.assertEqual(_news(poller.poll(state)), [])
        self.assertNotIsInstance(state["seen"], set)
        self.assertEqual(poller.saved()["seen"], ["comment:" + found["id"]])
        self.assertEqual(poller.saved()["seen_v"], 2)

    def test_save_state_takes_the_smallest_state_it_always_did(self):
        """tests/test_build_marker hands it a bare set and no other key: it must still write."""
        hub, poller = _world(self)
        jw.save_state("p", {"since": "t", "seen": set()})
        with open(os.path.join(poller.state_dir, "p.json")) as fh:
            raw = json.load(fh)
        self.assertEqual((raw["since"], raw["seen"], raw["seen_v"]), ("t", [], 2))


class LegacyStateFile(unittest.TestCase):
    """The first load of a state file written BEFORE the fix.

    Its `seen` is an arbitrary subset: the ids the old daemon dropped are already lost, so on the
    first cycle Jira returns items nobody can prove were announced. Those must not be announced
    again, and an item that really is new must still be.

    The design: a file without the `seen_v` marker is legacy. On its first cycle an unseen item
    is adopted SILENTLY only on positive evidence that it pre-dates the old daemon's last look:
    its own timestamp parses and is at least one poll interval before the file's watermark.
    Anything else (later, inside that interval) is announced as it always was. Items older than
    the watermark less the overlap are not considered by this daemon at all, as before.

    And only where the file says ids were LOST and that the last cycle READ everything (a review
    found this): a file under the old cap has never lost an id, and one with an open `comments` or
    `changelog` episode was not fully read, so for those nothing is adopted and everything announces.
    """

    def setUp(self):
        self.hub, self.poller = _world(self)
        self.since = T0 - timedelta(seconds=120)        # the old daemon's last cycle started 2 minutes ago
        self.hub.add_issue("ACME-7", at=T0 - timedelta(days=30))

    def _ago(self, **kw):
        return self.since - timedelta(**kw)

    def _after(self, **kw):
        return self.since + timedelta(**kw)

    def _write(self, seen, **over):
        """A state file as the old daemon left it: `seen` in no particular order and no marker."""
        raw = {"since": self.since.isoformat(), "seen": list(seen), "degraded": {},
               "build": {"src": "7ed8d8fc3652", "head": "38ffbbd"}}
        raw.update(over)
        self.poller.write(raw)

    @staticmethod
    def _arbitrary(remembered, size=1500):
        """`size` ids (1500 is the old cap), `remembered` among them, in no particular order (a fixed shuffle)."""
        filler = (["comment:%d" % (100000 + i) for i in range(750)]
                  + ["change:%d" % (100000 + i) for i in range(650)]
                  + ["created:ACME-%d" % (100000 + i) for i in range(100)])
        ids = list(remembered) + filler[:size - len(remembered)]
        random.Random(1109).shuffle(ids)
        return ids

    @staticmethod
    def _episode(sub, open_=True):
        """``degraded[sub]`` exactly as `_degraded.reconcile` leaves it in the state file: open after a cycle
        in which the sub-fetch failed, closed (and still on record) after one in which it recovered."""
        episodes = {}
        _degraded.reconcile(_degraded.Cycle().record(sub, "HTTP 502"), episodes, now=1000.0)
        if not open_:
            _degraded.reconcile(_degraded.Cycle(), episodes, now=1060.0)
        return episodes

    def _visible_world(self):
        """What Jira returns at the first poll after the restart that pre-dates the old daemon's
        last look and is still inside the daemon's query floor (the watermark less five minutes).
        Returns the ids the old file still remembers: half of them, the other half dropped by the
        arbitrary truncation."""
        hub, remembered = self.hub, []
        for seconds, kept in ((170, False), (140, True), (120, False), (100, True)):
            found = hub.comment("ACME-7", body="old comment %d" % seconds, at=self._ago(seconds=seconds))
            if kept:
                remembered.append("comment:" + found["id"])
        for seconds, kept in ((160, False), (150, True), (130, True), (110, False)):
            found = hub.change("ACME-7", to="old status %d" % seconds, at=self._ago(seconds=seconds))
            if kept:
                remembered.append("change:" + found["id"])
        hub.add_issue("ACME-8", summary="lost creation", at=self._ago(seconds=170))       # created:ACME-8 was dropped
        hub.add_issue("ACME-6", summary="remembered creation", at=self._ago(seconds=200))
        remembered.append("created:ACME-6")
        # Before the query floor (the watermark less five minutes): never considered, before or after.
        hub.comment("ACME-7", body="beyond the floor", at=self._ago(seconds=400))
        hub.comment("ACME-7", body="ancient", at=self._ago(hours=5))
        return remembered

    def _new_events(self):
        """Five things that are genuinely new, or cannot be proven old. Returns what they say."""
        hub = self.hub
        hub.comment("ACME-7", body="new while down", at=self._after(seconds=30))
        hub.comment("ACME-7", body="inside the guard", at=self._ago(seconds=30))
        hub.change("ACME-7", to="new status", at=self._after(seconds=45))
        hub.change("ACME-7", to="guarded status", at=self._ago(seconds=20))
        hub.add_issue("ACME-9", summary="new issue", at=self._after(seconds=60))
        return sorted(["inside the guard", "new while down", 'status->"guarded status"',
                       'status->"new status"', "new issue"])

    def test_the_first_cycle_re_emits_nothing_the_old_file_lost(self):
        """THE migration test: 2 comments, 2 changes and 1 issue creation that Jira returns were
        dropped by the arbitrary truncation. None is announced; the five that are new (after
        the watermark, or too close to it to prove otherwise) all are."""
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered))
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), expected)

    # -- a review's finding: adopt only where the file says ids were LOST and the last cycle READ everything --

    def test_a_file_under_the_old_cap_adopts_nothing(self):
        """The old code dropped an id only at its cut, list(seen)[-1500:], and a set only grows, so a file
        holding fewer than 1,500 ids has never lost one: an item it does not hold was never announced. The
        five the big test above treats as lost are, under the cap, simply unread, so they announce with the
        new ones; adopting them would swallow them."""
        remembered = self._visible_world()
        expected = sorted(self._new_events() + ["old comment 170", "old comment 120", 'status->"old status 160"',
                                                'status->"old status 110"', "lost creation"])
        self._write(self._arbitrary(remembered, size=1499))
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), expected)
        self.assertEqual([n for n in self.poller.notes if "adopted" in n], [])
        self.assertFalse(state["legacy_seen"])

    def test_a_file_exactly_at_the_old_cap_with_no_open_episode_still_adopts(self):
        """The other side of the boundary, and exactly what the migration did before: at the cap ids WERE being
        dropped, and nothing on disk says the last cycle left a slice unread."""
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered, size=1500), degraded={})
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), expected)

    def _old_file_after_failing_fetches(self, fail, add_item):
        """The file the OLD daemon leaves when a sub-fetch is failing in its last cycles, made by running a daemon
        through them (so the `degraded` block is the real one) and then made legacy: no marker, `seen` in arbitrary
        order AT the old cap, so that the cap rule cannot be what saves the item. Cycle 1 reads ACME-7 fine; the item
        lands 5 s later; cycles 2 and 3 get a 502 on `fail`, and cycle 3 is the last the old daemon saves."""
        hub, poller = self.hub, self.poller
        poller.clock.current = T0 - timedelta(seconds=300)
        first = poller.clock.now()
        state = poller.load()
        poller.poll(state)
        add_item(first + timedelta(seconds=5))
        hub.fail = (fail,)
        poller.poll(state)
        poller.poll(state)
        hub.fail = ()
        raw = poller.saved()
        sub = "comments" if fail == "/comment" else "changelog"
        self.assertTrue(raw["degraded"][sub]["open"], "the fixture must leave the episode open")
        del raw["seen_v"]
        raw["seen"] = self._arbitrary([], size=1500)
        poller.write(raw)

    def test_a_comment_the_old_daemon_never_read_is_announced_after_its_fetch_was_failing(self):
        """A reviewer's reproduction, on the real scenario. The old daemon's last cycles got a 502 on the comment
        fetch: the holes were recorded and `since` advanced anyway, so a comment stamped before the last
        look was never read. The five-minute overlap used to catch it on the next cycle; adopting it silently
        loses it for good. The file says so: an open `comments` episode."""
        self._old_file_after_failing_fetches(
            "/comment", lambda at: self.hub.comment("ACME-7", body="the unread comment", at=at))
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), ["the unread comment"])
        self.assertEqual([n for n in self.poller.notes if "adopted" in n], [])

    def test_a_change_the_old_daemon_never_read_is_announced_after_its_fetch_was_failing(self):
        """The same for the changelog: `changelog` is the other sub-fetch that feeds the memory."""
        self._old_file_after_failing_fetches(
            "/changelog", lambda at: self.hub.change("ACME-7", to="the unread change", at=at))
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), ['status->"the unread change"'])
        self.assertEqual([n for n in self.poller.notes if "adopted" in n], [])

    def test_a_closed_episode_does_not_stop_the_adoption(self):
        """Closed: the slice was read again in the cycle after the failure, inside the overlap. (A closed record
        outlives its recovery by the renotify window, so it is the `open` flag that matters.)"""
        remembered = self._visible_world()
        expected = self._new_events()
        closed = self._episode("comments", open_=False)
        self.assertFalse(closed["comments"]["open"])
        self._write(self._arbitrary(remembered), degraded=closed)
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), expected)

    def test_an_open_episode_for_a_sub_that_does_not_feed_the_memory_does_not_stop_the_adoption(self):
        remembered = self._visible_world()
        expected = self._new_events()
        self._write(self._arbitrary(remembered), degraded=self._episode("other"))
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), expected)

    def test_the_daemon_log_says_why_nothing_is_adopted(self):
        self._write(self._arbitrary([], size=10))
        said = _seen.legacy_note("acme", self.poller.load(), jw.LEGACY_GUARD)
        self.assertIn("adopts nothing", said)
        self.assertIn("under the old cap of 1500", said)
        self._write(self._arbitrary([]), degraded=self._episode("changelog"))
        said = _seen.legacy_note("acme", self.poller.load(), jw.LEGACY_GUARD)
        self.assertIn("adopts nothing", said)
        self.assertIn("changelog", said)

    def test_the_daemon_log_says_what_the_migration_adopted(self):
        self._write(self._arbitrary(self._visible_world()))
        self.poller.poll(self.poller.load())
        said = [n for n in self.poller.notes if "adopted" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertRegex(said[0], r"adopted 1 created issue\(s\), 2 comment\(s\), 2 change\(s\) silently")

    def test_a_legacy_file_loads_with_its_watermark_and_a_cycle_budget(self):
        """The baseline rides the in-memory state: the file's own watermark (the cutoff a held
        baseline keeps using) and how many cycles it may run."""
        self._write(self._arbitrary([]))
        legacy = self.poller.load()["legacy_seen"]
        self.assertEqual(legacy["cutoff"], self.since.isoformat())
        self.assertGreater(legacy["cycles"], 0)

    def test_main_starts_on_a_legacy_file_and_says_so(self):
        """`main()` has no other test, and a typo in the lines it gained would crash-loop the
        daemon at its FIRST start after the deploy. Run it: a config in a private tree (the real
        one is never read), the real load, one real cycle, and the sleep that ends the first
        pass turned into a stop."""
        self._write(self._arbitrary([]))
        config = self.poller.write_config({"profiles": {"acme": {"jira": dict(CFG)}}})

        class Stop(Exception):
            pass

        def stop(_secs):
            raise Stop()

        clock = mock.Mock(sleep=stop, time=time.time)
        with mock.patch.object(jw, "CONFIG_PATH", config), mock.patch.object(jw, "time", clock):
            with self.assertRaises(Stop):
                jw.main()
        said = [n for n in self.poller.notes if "predates" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertEqual(self.poller.saved()["seen_v"], 2, "the cycle main() ran must have written the new format")

    def test_the_boundary_is_one_poll_interval_before_the_watermark(self):
        """Pinned so it is a decision, not an accident: 61 seconds before the watermark is old,
        59 is not. The old daemon's last look can lag the watermark by a fetch, and the two clocks
        are not the same clock; a duplicate is the safe direction and a swallowed event is not."""
        self.assertEqual(jw.LEGACY_GUARD, timedelta(seconds=jw.POLL_SECS))
        self.hub.comment("ACME-7", body="61 s before", at=self._ago(seconds=61))
        self.hub.comment("ACME-7", body="59 s before", at=self._ago(seconds=59))
        self._write(self._arbitrary([]))
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), ["59 s before"])

    def test_no_watermark_means_no_baseline(self):
        """Nothing to compare against: announce as the old code would (within its first-run lookback)."""
        self.hub.comment("ACME-7", body="two minutes ago", at=T0 - timedelta(minutes=2))
        self._write(self._arbitrary([]), since=None)
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), ["two minutes ago"])
        self.assertFalse(state["legacy_seen"], "there is nothing to hold when there was nothing to compare")

    def test_no_watermark_leaves_nothing_to_hold_even_after_a_failed_fetch(self):
        """With no watermark there is no cutoff, so nothing can ever be adopted: holding the baseline
        would only keep an inert rule and a misleading note."""
        self.hub.comment("ACME-7", body="recent, so its issue is in the window and its comments are fetched",
                         at=T0 - timedelta(minutes=2))
        self._write(self._arbitrary([]), since=None)
        state = self.poller.load()
        self.hub.fail = ("/comment",)
        self.poller.poll(state)
        self.assertTrue([a for a in self.hub.asked if a[0] == "get" and a[1].endswith("/comment")],
                        "the fetch that fails must actually be attempted, or this proves nothing")
        self.assertFalse(state["legacy_seen"])
        self.assertEqual([n for n in self.poller.notes if "baseline holds" in n], [])

    def test_the_baseline_is_one_clean_cycle_long(self):
        """It must not swallow beyond the migration: once a cycle has completed cleanly the file is
        trusted, and an unseen item announces whatever its timestamp says."""
        self._write(self._arbitrary(self._visible_world()))
        state = self.poller.load()
        self.assertEqual(_titles(self.poller.poll(state)), [])
        self.assertFalse(state["legacy_seen"])
        self.hub.comment("ACME-7", body="stamped long ago, seen late", at=self._ago(seconds=100))
        self.assertEqual(_titles(self.poller.poll(state)), ["stamped long ago, seen late"])

    def test_a_failed_fetch_in_the_first_cycle_keeps_the_baseline_for_the_next(self):
        """A hole in the baseline cycle leaves its scope unbaselined, and holding the baseline is
        the cheap repair: without it the comments the failed fetch never read would be announced
        next cycle. Only the comment fetch failed, so only comments are still adopted then."""
        self._write(self._arbitrary(self._visible_world()))
        self._new_events()
        state = self.poller.load()
        self.hub.fail = ("/comment",)
        first = _titles(self.poller.poll(state))
        self.assertEqual(first, sorted(['status->"guarded status"', 'status->"new status"', "new issue"]))
        self.assertTrue(state["legacy_seen"], "a cycle with a hole consumed the baseline")
        self.hub.fail = ()
        second = _titles(self.poller.poll(state))
        self.assertEqual(second, ["inside the guard", "new while down"],
                         "the old comments were announced, or a new one lost")
        self.assertFalse(state["legacy_seen"])

    def test_a_hole_that_never_heals_cannot_keep_the_baseline_forever(self):
        """Bounded: a permanently failing fetch must not leave a silent-adoption rule running for
        the life of the daemon."""
        self._write(self._arbitrary([]))
        state = self.poller.load()
        self.hub.fail = ("/changelog",)
        for cycle in range(_seen.LEGACY_MAX_CYCLES):
            # A fetch that keeps failing keeps being ATTEMPTED only while the issue keeps being updated
            # (an issue that has gone quiet leaves the window and is not fetched), so it is.
            self.hub.comment("ACME-7", body="still busy %d" % cycle)
            self.poller.poll(state)
            if cycle < _seen.LEGACY_MAX_CYCLES - 1:
                self.assertTrue(state["legacy_seen"], "cycle %d dropped the baseline early" % cycle)
        self.assertTrue([a for a in self.hub.asked if a[0] == "get" and a[1].endswith("/changelog")],
                        "the fetch that fails must actually be attempted, or this proves nothing")
        self.assertFalse(state["legacy_seen"])

    def test_the_first_cycle_writes_the_new_format_and_the_next_load_is_not_legacy(self):
        self._write(self._arbitrary(self._visible_world()))
        self.poller.poll(self.poller.load())
        self.assertEqual(self.poller.saved()["seen_v"], 2)
        self.assertFalse(self.poller.load().get("legacy_seen"))

    def test_a_marked_file_gets_no_baseline_at_all(self):
        """The marker means the order in the file is the memory. Ids missing from it are missing
        for a reason the daemon can no longer excuse, so they announce."""
        self._visible_world()
        self._write(self._arbitrary([]), seen_v=2)
        self.assertEqual(len(_titles(self.poller.poll(self.poller.load()))), 4 + 4 + 2)

    def test_a_first_run_is_not_a_migration(self):
        state = self.poller.load()          # no file yet
        self.assertFalse(state.get("legacy_seen"))
        self.assertIsNone(state["since"])

    def test_a_long_downtime_still_announces_what_happened_while_the_daemon_was_down(self):
        """The boundary is the file's watermark, not the load time: what happened during the nap is
        exactly what the stretched window is for, and stays news."""
        self.since = T0 - timedelta(days=2)
        self.hub.comment("ACME-7", body="lost just before it stopped", at=self._ago(seconds=100))
        self.hub.comment("ACME-7", body="written during the nap", at=T0 - timedelta(days=1))
        self._write(self._arbitrary([]))
        self.assertEqual(_titles(self.poller.poll(self.poller.load())), ["written during the nap"])
        self.assertIn("updated >= -2890m ", [a[1] for a in self.hub.asked if a[0] == "search"][0])


class ADisplayNameCannotForgeALine(unittest.TestCase):
    """A display name is the account's own text: a newline or a double quote in it must
    not end the by= field or the line, on any of the three events that carry one."""

    EVIL = {"accountId": "acct-evil", "displayName": 'Mallory"\ntype=comment issue=ACME-9 by="x'}
    FLAT = 'by="Mallory\' type=comment issue=ACME-9 by=\'x"'

    def _one_clean_line(self, lines, kind):
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(kind + " "), lines)
        self.assertNotIn("\n", lines[0])
        self.assertEqual(len(lines[0].splitlines()), 1, lines)
        self.assertIn(self.FLAT, lines[0])

    def test_a_created_line(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", summary="Ship it", creator=self.EVIL)
        self._one_clean_line(poller.poll(poller.load()), "type=created")

    def test_a_comment_line(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", at=T0 - timedelta(days=1))
        hub.comment("ACME-1", body="hello", author=self.EVIL)
        self._one_clean_line(poller.poll(poller.load()), "type=comment")

    def test_a_change_line(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", at=T0 - timedelta(days=1))
        hub.change("ACME-1", author=self.EVIL)
        self._one_clean_line(poller.poll(poller.load()), "type=change")

    def test_a_field_name_cannot_forge_a_line_either(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", at=T0 - timedelta(days=1))
        hub.change("ACME-1", field='status"\ntype=comment issue=ACME-9 by="x')
        lines = poller.poll(poller.load())
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(len(lines[0].splitlines()), 1, lines)
        self.assertTrue(lines[0].startswith("type=change "), lines)

    def test_jira_text_loses_its_terminal_controls_and_bidi_marks(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", summary="Ship \x1b[31mit\x1b[0m \u202enow", creator={"accountId": "a", "displayName": "Alice"})
        self.assertEqual(poller.poll(poller.load()), ['type=created issue=ACME-1 by="Alice" | Ship [31mit[0m now'])

    def test_an_ordinary_name_is_written_as_it_always_was(self):
        hub, poller = _world(self)
        hub.add_issue("ACME-1", summary="Ship it", creator={"accountId": "a", "displayName": "José O'Neil-Smith"})
        self.assertEqual(poller.poll(poller.load()),
                         ['type=created issue=ACME-1 by="José O\'Neil-Smith" | Ship it'])


class FileLayout(unittest.TestCase):
    """The ``__main__`` guard sits below the last TestCase, or direct execution skips tests
    (a review found ``python3 tests/test_inbox_wake.py`` ran 21 of 25 and discovery hid it)."""

    def test_main_guard_is_the_last_statement_in_this_file(self):
        with open(__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("\nclass ", src[src.index('if __name__ == "__main__":'):])


if __name__ == "__main__":
    unittest.main()
