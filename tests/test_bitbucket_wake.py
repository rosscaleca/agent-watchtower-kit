#!/usr/bin/env python3
"""bitbucket_wake emits approvals — the signal the standalone pr-watch existed for.

A review asked whether the standalone `pr-watch` agent is redundant with
`github_wake` + `bitbucket_wake`. Its Bitbucket half was not: pr_watch v2 was
rewritten precisely because the previous version "missed approvals", and this daemon watched lifecycle + comments only. A
cross-side approval is the wake that matters most on a team where neither
side may merge its own PR, so it is ported here rather than left as the
reason to keep a fifth, unversioned daemon alive.

The poller runs with `bkt_api` and `emit` replaced: no network, no files.

The second half of this file (from ``SeenMemory``) covers what the daemon remembers of the
comments it has announced, and that a restart reloads it intact.
Run: `just test` (or `python3 -m unittest discover -s tests`).
"""
import json
import os
import random
import re
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "daemons"))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _degraded  # noqa: E402
import _seen  # noqa: E402
import bitbucket_wake as bw  # noqa: E402
from wake_sim import FakeClock, Poller, T0  # noqa: E402

REPO = "acme/service"
BOB = {"display_name": "Bob", "uuid": "{b0b0b0b0-0000-4000-8000-00000000b0b0}"}
ALICE = {"display_name": "Alice", "uuid": "{a11ce000-0000-4000-8000-00000000a11c}"}
CFG = {"repos": [REPO], "cwd": "~", "suppress_user_ids": [ALICE["uuid"]], "suppress_users": ["Alice"]}


def _pr(number, head="cff2ea034", approved=(), title="[ACME-873] test(site): guards", changes=()):
    return {"id": number, "title": title, "author": {"display_name": "Bob"},
            "source": {"commit": {"hash": head + "a66"}},
            "participants": ([{"user": u, "role": "REVIEWER", "approved": True, "state": "approved"}
                              for u in approved]
                             + [{"user": u, "role": "REVIEWER", "approved": False,
                                 "state": "changes_requested"} for u in changes])}


class _FakeBkt:
    def __init__(self, open_prs):
        self.open_prs = open_prs

    def __call__(self, path, cwd):
        if "/pullrequests?state=OPEN" in path:
            return {"values": self.open_prs}
        if path.endswith("/comments?pagelen=50"):
            return {"values": []}
        return {"state": "MERGED", "title": "t"}


def _run(state, open_prs):
    emitted = []
    with mock.patch.object(bw, "bkt_api", _FakeBkt(open_prs)), \
            mock.patch.object(bw, "emit", lambda profile, line: emitted.append(line)), \
            mock.patch.object(bw, "save_state", lambda profile, st: None):
        bw.poll_profile("acme", CFG, state)
    return emitted


def _seeded(open_prs):
    """State after a first-run baseline over ``open_prs`` (no emits)."""
    state = {"since": None, "seen": set(), "prs": {}, "first_run": True}
    assert _run(state, open_prs) == []
    return state


class Approvals(unittest.TestCase):

    def test_a_peer_approval_emits_pr_approved(self):
        state = _seeded([_pr(72)])
        emitted = _run(state, [_pr(72, approved=[BOB])])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertTrue(emitted[0].startswith(
            'type=pr_approved repo=service #72 by="Bob" | '), emitted[0])

    def test_an_approval_is_emitted_once(self):
        state = _seeded([_pr(72)])
        _run(state, [_pr(72, approved=[BOB])])
        self.assertEqual(_run(state, [_pr(72, approved=[BOB])]), [])

    def test_own_approval_is_suppressed_by_uuid(self):
        state = _seeded([_pr(72)])
        self.assertEqual(_run(state, [_pr(72, approved=[ALICE])]), [])

    def test_withdrawn_approval_emits_pr_unapproved(self):
        state = _seeded([_pr(72, approved=[BOB])])
        emitted = _run(state, [_pr(72)])
        self.assertEqual(emitted, ['type=pr_unapproved repo=service #72 by="Bob"'])

    def test_a_push_keeps_the_approval_record(self):
        """A head change must not reset the approval set, or the same approval
        re-emits after every push (and Bitbucket keeps approvals across pushes
        unless the workspace resets them)."""
        state = _seeded([_pr(72, approved=[BOB])])
        emitted = _run(state, [_pr(72, head="f873f1b96", approved=[BOB])])
        self.assertEqual([line.split()[0] for line in emitted], ["type=pr_push"], emitted)

    def test_first_run_seeds_approvals_silently(self):
        state = _seeded([_pr(72, approved=[BOB])])
        self.assertEqual(state["prs"][REPO]["72"]["approvals"], [BOB["uuid"]])

    def test_listing_asks_for_participants(self):
        """The OPEN listing carries participants only with explicit fields; a
        refactor that drops them makes every approval invisible again."""
        for field in ("values.participants.approved", "values.participants.user.uuid",
                      "values.participants.user.display_name"):
            self.assertIn(field, bw.LIST_FIELDS)


class ADisplayNameCannotForgeALine(unittest.TestCase):
    """A display name is the account's own text: a newline or a double quote in it must not end
    the by= field or the line, on any event that carries one."""

    EVIL = {"display_name": 'Mallory"\ntype=pr_merged repo=x #1', "uuid": "{evil}"}

    def _assert_one_clean_line(self, emitted, kind):
        self.assertEqual(len(emitted), 1, emitted)
        line = emitted[0]
        self.assertTrue(line.startswith(kind + " "), line)
        self.assertNotIn("\n", line)
        self.assertIn('by="Mallory\' type=pr_merged repo=x #1"', line)

    def test_who_field_collapses_whitespace_and_quotes(self):
        self.assertEqual(bw.who_field('a"b\n c'), "a'b c")
        self.assertEqual(bw.who_field(None), "?")
        self.assertEqual(len(bw.who_field("x" * 200)), 80)

    def test_a_changes_requested_line(self):
        state = _seeded([_pr(72)])
        self._assert_one_clean_line(_run(state, [_pr(72, changes=[self.EVIL])]), "type=pr_changes_requested")

    def test_a_changes_cleared_line(self):
        state = _seeded([_pr(72, changes=[self.EVIL])])
        self._assert_one_clean_line(_run(state, [_pr(72)]), "type=pr_changes_cleared")

    def test_an_approved_line(self):
        state = _seeded([_pr(72)])
        self._assert_one_clean_line(_run(state, [_pr(72, approved=[self.EVIL])]), "type=pr_approved")

    def test_an_unapproved_line(self):
        state = _seeded([_pr(72, approved=[self.EVIL])])
        self._assert_one_clean_line(_run(state, [_pr(72)]), "type=pr_unapproved")


class ATitleCannotRestyleALine(unittest.TestCase):
    """A PR title is the author's own text: the controls and bidi marks a terminal acts
    on but does not show must not survive into the line, where they could rewrite the author."""

    CRAFTED = "fix \x1b[2K\r\x1b[1G type=x by=\"Alice\" \x9b2K a\u202eb\u200bc"
    HIDDEN = ("\x1b", "\x9b", "\u202e", "\u200b", "\r")

    def test_an_opened_line_loses_the_controls_and_the_author_is_the_real_one(self):
        state = _seeded([])
        emitted = _run(state, [_pr(72, title=self.CRAFTED)])
        self.assertEqual(len(emitted), 1, emitted)
        line = emitted[0]
        self.assertEqual(len(line.splitlines()), 1, line)
        for ch in self.HIDDEN:
            self.assertNotIn(ch, line, repr(ch))
        self.assertTrue(line.startswith('type=pr_opened repo=service #72 by="Bob" | '), line)
        self.assertIn('| fix [2K [1G type=x by="Alice" 2K abc', line)

    def test_an_ordinary_title_is_byte_identical(self):
        title = "[ACME-873] test(site): guards — naïve café"
        emitted = _run(_seeded([]), [_pr(72, title=title)])
        self.assertEqual(emitted, ['type=pr_opened repo=service #72 by="Bob" | %s' % title])

    def test_one_line_drops_them_directly(self):
        out = bw.one_line(self.CRAFTED)
        for ch in self.HIDDEN:
            self.assertNotIn(ch, out, repr(ch))
        self.assertEqual(bw.one_line("a  b\n c"), "a b c")


class ALoneSurrogateCostsNoLine(unittest.TestCase):
    """A title can hold a lone surrogate (U+D800..U+DFFF), which UTF-8 cannot encode. The
    real ``emit`` must still write the one line and raise nothing."""

    def test_emit_writes_one_line_and_raises_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = os.path.join(tmp, "logs")
            with mock.patch.object(bw, "LOG_DIR", logs):
                bw.emit("p", "type=pr_opened repo=r #1 by=\"A\" | " + bw.one_line("bad \ud800 title"))
            with open(os.path.join(logs, "p-bitbucket.log"), "rb") as fh:
                data = fh.read()
        self.assertEqual(len(data.splitlines()), 1, data)
        self.assertTrue(data.startswith(b"type=pr_opened"), data)
        self.assertIn(b"bad ? title ts=", data)


class ChangesRequested(unittest.TestCase):
    """A changes-requested verdict is announced once, and its clearing once."""

    def test_listing_asks_for_the_participant_state(self):
        self.assertIn("values.participants.state", bw.LIST_FIELDS)

    def test_changes_requested_of_reads_only_that_state(self):
        pr = _pr(72, approved=[BOB], changes=[ALICE])
        pr["participants"].append({"user": {"display_name": "Nobody"}, "state": None, "approved": False})
        self.assertEqual(bw.changes_requested_of(pr), {ALICE["uuid"]: "Alice"})
        self.assertEqual(bw.approvals_of(pr), {BOB["uuid"]: "Bob"})

    def test_a_changes_requested_verdict_emits_once(self):
        state = _seeded([_pr(72)])
        emitted = _run(state, [_pr(72, changes=[BOB])])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertTrue(emitted[0].startswith(
            'type=pr_changes_requested repo=service #72 by="Bob" | '), emitted[0])
        self.assertEqual(_run(state, [_pr(72, changes=[BOB])]), [])

    def test_a_cleared_request_emits_once(self):
        state = _seeded([_pr(72, changes=[BOB])])
        emitted = _run(state, [_pr(72)])
        self.assertEqual(emitted, ['type=pr_changes_cleared repo=service #72 by="Bob"'])
        self.assertEqual(_run(state, [_pr(72)]), [])

    def test_a_request_turned_into_an_approval_clears_and_approves(self):
        state = _seeded([_pr(72, changes=[BOB])])
        emitted = _run(state, [_pr(72, approved=[BOB])])
        self.assertEqual(sorted(line.split()[0] for line in emitted),
                         ["type=pr_approved", "type=pr_changes_cleared"], emitted)

    def test_first_run_seeds_the_request_silently(self):
        state = _seeded([_pr(72, changes=[BOB])])
        self.assertEqual(state["prs"][REPO]["72"]["changes"], [BOB["uuid"]])
        self.assertEqual(state["prs"][REPO]["72"]["changes_names"], {BOB["uuid"]: "Bob"})

    def test_a_pr_first_seen_later_with_a_request_announces_only_the_opening(self):
        state = _seeded([])
        emitted = _run(state, [_pr(72, changes=[BOB])])
        self.assertEqual([line.split()[0] for line in emitted], ["type=pr_opened"], emitted)
        self.assertEqual(state["prs"][REPO]["72"]["changes"], [BOB["uuid"]])

    def test_a_saved_state_without_changes_adopts_the_standing_request_silently(self):
        state = _seeded([_pr(72)])
        del state["prs"][REPO]["72"]["changes"]
        del state["prs"][REPO]["72"]["changes_names"]
        self.assertEqual(_run(state, [_pr(72, changes=[BOB])]), [])
        self.assertEqual(state["prs"][REPO]["72"]["changes"], [BOB["uuid"]])
        # Adopted, so its clearing is news from here on.
        self.assertEqual(_run(state, [_pr(72)]),
                         ['type=pr_changes_cleared repo=service #72 by="Bob"'])

    def test_a_push_keeps_the_request_record(self):
        state = _seeded([_pr(72, changes=[BOB])])
        emitted = _run(state, [_pr(72, head="f873f1b96", changes=[BOB])])
        self.assertEqual([line.split()[0] for line in emitted], ["type=pr_push"], emitted)

    def test_own_request_is_suppressed_by_uuid(self):
        state = _seeded([_pr(72)])
        self.assertEqual(_run(state, [_pr(72, changes=[ALICE])]), [])

    def test_own_cleared_request_is_suppressed_by_uuid(self):
        state = _seeded([_pr(72, changes=[ALICE])])
        self.assertEqual(_run(state, [_pr(72)]), [])

    def test_a_suppressed_display_name_is_suppressed_for_both_events(self):
        nameonly = {"display_name": "Alice"}
        state = _seeded([_pr(72)])
        self.assertEqual(_run(state, [_pr(72, changes=[nameonly])]), [])
        self.assertEqual(_run(state, [_pr(72)]), [])

    def test_two_requesters_are_told_apart(self):
        state = _seeded([_pr(72, changes=[BOB])])
        third = {"display_name": "Pat Q", "uuid": "{11111111-2222-3333-4444-555555555555}"}
        emitted = _run(state, [_pr(72, changes=[BOB, third])])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertIn('by="Pat Q"', emitted[0])



# ---------------------------------------------------------------------------
# The dedup memory survives a restart
#
# `save_state` wrote `list(state["seen"])[-2000:]`. `seen` was a SET, so that is hash order,
# randomised per process: past 2000 ids an arbitrary 2000 were kept and the rest dropped at
# random. The daemon ignores a comment older than its watermark less five minutes, so what could
# come back after a restart was only the last few minutes before it, but a peer's "please fix
# the migration" comment, announced again, is a request that has already been answered. The same line
# was fixed in `github_wake`; this is that fix, adapted to this daemon: one kind of id
# (`c:`, a comment) and a 90-second poll. Bitbucket PRs are tracked in `state["prs"]` and deleted
# when they close, and pipelines are not watched at all, so neither has an id in `seen`.
# ---------------------------------------------------------------------------

class _Bitbucket(object):
    """A miniature Bitbucket: what ``bkt api`` answers for the paths the daemon reads, with the
    API's own windowing.

    The windowing is the point. The OPEN listing answers the first ``pagelen`` open PRs; a PR's
    comments answer the first ``pagelen`` of its comments, oldest first, with NO ``since`` filter,
    so an open PR's whole first page comes back on every poll. ``pagelen`` is read from the
    request, so a daemon that changes what it asks for is served what Bitbucket would serve for
    that, and a request this hub does not model fails the test instead of quietly answering empty.
    Times are the clock's, in Bitbucket's spelling (six digits and ``+00:00``).
    """

    def __init__(self, clock, repos=(REPO,)):
        self.clock = clock
        self.repos = list(repos)
        self.prs = dict((repo, {}) for repo in self.repos)      # repo -> number -> PR
        self.asked = []                                         # (path, cwd) for every call
        self.fail = ()                                          # substrings of paths that answer 502
        self._cid = 860700000
        self._number = 100

    @staticmethod
    def stamp(when):
        return when.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

    # -- the world -------------------------------------------------------------------------

    def open_pr(self, repo=REPO, title="[ACME-1] fix: something", head="cff2ea034"):
        self._number += 1
        self.prs[repo][self._number] = {"id": self._number, "title": title, "state": "OPEN",
                                        "head": head, "comments": []}
        return self._number

    def comment(self, number, repo=REPO, body="a comment", author=BOB, at=None, deleted=False, raw_created=None):
        """``raw_created``: serve this text as ``created_on`` (a garbage timestamp), whatever ``at`` is."""
        self._cid += 1
        found = {"id": self._cid, "at": at or self.clock.now(), "author": author, "body": body,
                 "deleted": deleted, "raw": raw_created}
        self.prs[repo][number]["comments"].append(found)
        return found

    def merge(self, number, repo=REPO):
        self.prs[repo][number]["state"] = "MERGED"

    # -- what the daemon calls -------------------------------------------------------------

    def __call__(self, path, cwd):
        self.asked.append((path, cwd))
        for needle in self.fail:
            if needle in path:
                raise RuntimeError("bkt api %s failed: HTTP 502" % path.split("?")[0])
        found = re.match(r"^/repositories/([^/]+/[^/?]+)/pullrequests(?:/(\d+)(/comments)?)?(?:\?(.*))?$", path)
        if not found:
            raise AssertionError("the daemon asked for something this hub does not model: %s" % path)
        repo, number, comments, query = found.groups()
        pagelen = int(re.search(r"pagelen=(\d+)", query).group(1)) if query and "pagelen=" in query else 10
        prs = self.prs[repo]
        if number is None:
            if "state=OPEN" not in (query or ""):
                raise AssertionError("the hub only models the OPEN listing: %s" % path)
            opened = sorted((p for p in prs.values() if p["state"] == "OPEN"), key=lambda p: p["id"], reverse=True)
            return {"values": [{"id": p["id"], "title": p["title"], "author": {"display_name": "Bob"},
                                "source": {"commit": {"hash": p["head"] + "a66"}}, "participants": []}
                               for p in opened[:pagelen]]}
        pr = prs[int(number)]
        if comments:
            return {"values": [{"id": c["id"], "created_on": c["raw"] if c["raw"] is not None else self.stamp(c["at"]),
                                "user": c["author"],
                                "content": {"raw": c["body"]}, "deleted": c["deleted"]}
                               for c in pr["comments"][:pagelen]]}
        return {"state": pr["state"], "title": pr["title"]}


def _bb_world(case, repos=(REPO,)):
    clock = FakeClock(T0)
    hub = _Bitbucket(clock, repos)
    cfg = dict(CFG, repos=list(repos))
    poller = Poller(case, bw, cfg, {"bkt_api": hub}, clock=clock)
    return hub, poller


def _comments(lines):
    """The comment lines among ``lines``: the one kind the memory guards."""
    return [line for line in lines if line.startswith("type=comment ")]


def _bodies(lines):
    return sorted(line.split(" | ", 1)[1] for line in _comments(lines))


def _busy_repo(hub, poller, state, ticks, prs=6, repo=REPO):
    """The everyday load: six PRs open at a time, each taking a comment a poll, one merged and
    replaced every other poll, and now and then a comment by the account the daemon suppresses.
    Returns every line emitted."""
    said, opened, n = [], [], 0
    for i in range(ticks):
        if len(opened) < prs or i % 2 == 0:
            if len(opened) >= prs:
                hub.merge(opened.pop(0), repo=repo)
            opened.append(hub.open_pr(repo=repo))
        for number in opened:
            n += 1
            hub.comment(number, repo=repo, body="comment %d" % n, author=ALICE if n % 6 == 0 else BOB)
        said.extend(poller.poll(state))
    return said


class SeenMemory(unittest.TestCase):
    """The sizing: derived from what one poll asks Bitbucket for, not guessed."""

    def test_the_comment_line_is_unchanged(self):
        """Pinned BEFORE the memory changed: this passes on the old code and the new. The PM's
        patterns, `just unactioned` and the seats' Monitor filters parse it."""
        hub, poller = _bb_world(self)
        number = hub.open_pr()
        state = poller.load()
        poller.poll(state)                                         # first run: a silent baseline
        hub.comment(number, body="please  fix\nthe migration")
        self.assertEqual(poller.poll(state),
                         ['type=comment repo=service #%d by="Bob" | please fix the migration'
                          % number])

    def test_the_ids_written_are_the_ones_the_old_code_wrote(self):
        hub, poller = _bb_world(self)
        number = hub.open_pr()
        state = poller.load()
        poller.poll(state)
        found = hub.comment(number)
        poller.poll(state)
        self.assertEqual(poller.saved()["seen"], ["c:%d" % found["id"]])

    def test_a_comment_by_a_suppressed_user_is_remembered_but_not_announced(self):
        hub, poller = _bb_world(self)
        number = hub.open_pr()
        state = poller.load()
        poller.poll(state)
        mine = hub.comment(number, author=ALICE)
        self.assertEqual(_comments(poller.poll(state)), [])
        self.assertIn("c:%d" % mine["id"], state["seen"])

    def test_a_deleted_comment_is_ignored_as_before(self):
        hub, poller = _bb_world(self)
        number = hub.open_pr()
        state = poller.load()
        poller.poll(state)
        hub.comment(number, deleted=True)
        self.assertEqual(_comments(poller.poll(state)), [])
        self.assertEqual(list(state["seen"]), [])

    def test_the_old_cap_is_what_the_previous_code_saved(self):
        """38ffbbd saved list(seen)[-2000:], and had since the daemon was written. The migration reads it: a
        file holding fewer ids than this never lost one."""
        self.assertEqual(bw.OLD_SEEN_CAP, 2000)
        self.assertEqual(bw.SEEN_SUBS, ("comments",))

    def test_the_only_kind_in_seen_is_the_comment(self):
        """The brief for this change guessed PR, comment and pipeline ids. Read at source: a PR is
        tracked in `state["prs"]` and deleted when it merges or is declined, and pipelines are not
        watched at all (the bkt token lacks the scope), so `seen` holds comments and nothing else."""
        hub, poller = _bb_world(self)
        state = poller.load()
        _busy_repo(hub, poller, state, ticks=30)
        self.assertEqual(set(mid.split(":", 1)[0] for mid in state["seen"]), set(["c"]))
        self.assertEqual(sorted(bw.SEEN_CAPS), ["c"])

    def test_the_windows_are_what_the_poller_really_asks_for(self):
        """The window constants BUILD the request URLs, and this reads the URLs back."""
        hub, poller = _bb_world(self)
        number = hub.open_pr()
        hub.comment(number)
        poller.poll(poller.load())
        listing = [p for p, _ in hub.asked if "/pullrequests?state=OPEN" in p]
        comments = [p for p, _ in hub.asked if p.endswith("/comments?pagelen=%d" % bw.COMMENTS_PER_PAGE)]
        self.assertEqual(len(listing), 1)
        self.assertIn("pagelen=%d&" % bw.PRS_PER_PAGE, listing[0])
        self.assertEqual(len(comments), 1)
        self.assertEqual((bw.PRS_PER_PAGE, bw.COMMENTS_PER_PAGE), (25, 50))
        self.assertEqual(bw.SEEN_WINDOW, {"c": bw.PRS_PER_PAGE * bw.COMMENTS_PER_PAGE})

    def test_the_window_constants_drive_the_requests(self):
        """Reading the URLs back proves the constants and the requests agree TODAY, not that one
        drives the other: a number typed into a URL again would still agree. So the constants are
        moved, and the URLs follow."""
        hub, poller = _bb_world(self)
        number = hub.open_pr()
        hub.comment(number)
        with mock.patch.object(bw, "PRS_PER_PAGE", 9), mock.patch.object(bw, "COMMENTS_PER_PAGE", 11):
            poller.poll(poller.load())
        paths = [p for p, _ in hub.asked]
        self.assertEqual(len([p for p in paths if "/pullrequests?state=OPEN&pagelen=9&" in p]), 1, paths)
        self.assertEqual(len([p for p in paths if p.endswith("/comments?pagelen=11")]), 1, paths)

    def test_the_cap_is_pinned_so_a_change_is_deliberate(self):
        """25 PRs x 50 comments = 1,250 ids per repo, every one returned on every poll while its PR
        is open (there is no `since`), x 2 = 2,500. One window is what an id that keeps being
        returned needs; the second is room for a PR at the edge of the 25-PR listing that flickers
        in and out, and for how long a PR that leaves it keeps its comments remembered. The
        reasoning is in the comment above `SEEN_KEEP`; the behaviour it buys is tested below."""
        self.assertEqual(bw.SEEN_KEEP, {"c": 2})
        self.assertEqual(bw.SEEN_CAPS, {"c": 2500})
        self.assertGreaterEqual(bw.SEEN_CAPS["c"], 2 * bw.SEEN_WINDOW["c"])

    def test_the_cap_is_ample_for_the_volume_measured_on_the_live_log(self):
        """Measured on this host's profile log, 2026-09-03..27: 9 comment wakes in 26 days, 3 on the
        busiest day, and the live state file holds 34 ids. A cap of 2,500 is 70 times that: it is not
        sized by volume but by the size of one poll's answer, which is what a cap below would evict."""
        self.assertGreater(bw.SEEN_CAPS["c"], 50 * 34)

    def test_caps_scale_with_the_number_of_repos_a_profile_watches(self):
        """Every window is per repo: a profile watching three asks three times the questions, so it
        must remember three times the answers."""
        repos = [REPO, "acme/service-auth", "acme/svc-platform"]
        with mock.patch.object(bw, "SEEN_CAPS", {"c": 2}):
            hub, poller = _bb_world(self, repos)
            for repo in repos:
                number = hub.open_pr(repo=repo)
                for _ in range(5):
                    hub.comment(number, repo=repo)
            state = poller.load()
            poller.poll(state)                       # first run: silent, but remembered
            self.assertEqual(len(state["seen"]), 6)
            solo_hub, solo = _bb_world(self)
            number = solo_hub.open_pr()
            for _ in range(5):
                solo_hub.comment(number)
            solo_state = solo.load()
            solo.poll(solo_state)
            self.assertEqual(len(solo_state["seen"]), 2)


class RestartKeepsTheDedupMemory(unittest.TestCase):
    """The ticket's regression tests: through the real load, poll and save, over enough cycles
    that more than 2000 ids have been seen."""

    def test_a_restart_re_emits_nothing_the_api_still_returns(self):
        """THE restart test. 700 polls of a busy repo, six open PRs taking a comment a poll each,
        is about 4,200 comment ids against the old 2,000 cap. Save, load, poll: Bitbucket still returns
        every open PR's whole first page, and none of the last few minutes of it may be announced
        again."""
        hub, poller = _bb_world(self)
        state = poller.load()
        poller.poll(state)                                          # first run: a silent baseline
        said = _comments(_busy_repo(hub, poller, state, ticks=700))
        expected = sum(1 for pr in hub.prs[REPO].values() for c in pr["comments"] if c["author"] is not ALICE)
        self.assertGreater(expected, 3000)
        self.assertEqual(len(said), expected, "the simulation itself lost or repeated a comment")
        self.assertEqual(len(set(said)), len(said))

        restarted = poller.load()
        again = _comments(poller.poll(restarted))
        self.assertEqual(again, [], "a restart re-announced %d comment(s) Bitbucket still returns: %s"
                         % (len(again), again[:3]))

        # Not vacuous: a daemon that said nothing at all would pass the line above.
        newest = max(number for number, pr in hub.prs[REPO].items() if pr["state"] == "OPEN")
        hub.comment(newest, body="a genuinely new comment")
        self.assertEqual(_bodies(poller.poll(restarted)), ["a genuinely new comment"], "a new comment was swallowed")
        # And the second restart is as quiet as the first.
        self.assertEqual(_comments(poller.poll(poller.load())), [])

    def test_what_is_saved_is_exactly_what_the_running_process_knows(self):
        """A restart must change nothing, so the file has to hold the process's OWN memory: the
        same ids in the same order, pruned the same way.

        The churn is past the cap at its production size (about 2,700 comment ids against 2,500 for the one
        repo), because a save that ran BEFORE the prune would write the excess, and a file that only ever
        gains ids under its cap cannot tell the two orders apart (the first version of this test reached
        2,400 and could not). And it reads the FILE: the memory after the prune is right either way."""
        hub, poller = _bb_world(self)
        state = poller.load()
        poller.poll(state)
        _busy_repo(hub, poller, state, ticks=450)
        total = sum(len(pr["comments"]) for pr in hub.prs[REPO].values())
        self.assertGreater(total, bw.SEEN_CAPS["c"], "the world must have overflowed the cap")
        saved = poller.saved()["seen"]
        self.assertEqual(list(state["seen"]), saved)
        self.assertEqual(len(saved), bw.SEEN_CAPS["c"], "the FILE should be exactly at its cap")
        self.assertEqual(list(poller.load()["seen"]), saved)
        with mock.patch.object(bw, "SEEN_CAPS", {"c": 100}):
            poller.poll(state)
        self.assertEqual(len(state["seen"]), 100, "the memory is bounded in the running process, not only at save")
        self.assertEqual(poller.saved()["seen"], list(state["seen"]), "and the file is what a restart reloads")

    def test_the_saved_list_is_oldest_first_so_the_reload_evicts_the_right_ids(self):
        """The order in the FILE is the memory: the comments of a PR that has closed sit before the
        comments of one still open (which are returned, so refreshed, on every poll)."""
        hub, poller = _bb_world(self)
        gone_pr, stays_pr = hub.open_pr(), hub.open_pr()
        state = poller.load()
        poller.poll(state)
        gone = [hub.comment(gone_pr, body="gone %d" % n) for n in range(5)]
        stays = [hub.comment(stays_pr, body="stays %d" % n) for n in range(5)]
        poller.poll(state)
        hub.merge(gone_pr)
        poller.poll(state)
        newest_pr = hub.open_pr()
        newest = [hub.comment(newest_pr, body="newest %d" % n) for n in range(5)]
        poller.poll(state)
        seen = poller.saved()["seen"]

        def places(comments):
            return [seen.index("c:%d" % c["id"]) for c in comments]
        self.assertLess(max(places(gone)), min(places(stays)), "the file is not oldest-first: %s" % seen)
        self.assertLess(max(places(gone)), min(places(newest)), "the file is not oldest-first: %s" % seen)

    def test_an_id_the_api_keeps_returning_is_refreshed_not_aged_by_first_sighting(self):
        """LAST-seen, not first-seen. With room for 20 ids, a comment on a PR that stays open is
        returned on every poll and must be refreshed each time. Left to age by its FIRST sighting it
        is evicted once 20 newer ids have arrived, which is two polls when a poll brings 12, and it is
        announced as new on the fourth, still inside the window in which a comment is news."""
        hub, poller = _bb_world(self)
        long_lived = hub.open_pr()
        state = poller.load()
        poller.poll(state)
        with mock.patch.object(bw, "SEEN_CAPS", {"c": 20}):
            keep = hub.comment(long_lived, body="the one to keep")
            self.assertEqual(_bodies(poller.poll(state)), ["the one to keep"])
            for tick in range(3):
                passing = hub.open_pr()                             # a PR that opens, is commented on, and merges
                for n in range(12):
                    hub.comment(passing, body="passing %d.%d" % (tick, n))
                said = _bodies(poller.poll(state))
                self.assertEqual(len(said), 12, "tick %d: %s" % (tick, said))
                self.assertNotIn("the one to keep", said, "tick %d announced it again" % tick)
                hub.merge(passing)
        self.assertIn("c:%d" % keep["id"], state["seen"])

    def test_no_live_id_is_evicted_at_the_worst_case_window(self):
        """Sizing, exercised at the API's worst case rather than a friendly one: 25 open PRs, each
        with a whole first page of 50 comments, so every poll returns 1,250 comment ids (a full
        window) and touches every one. Five new comments arrive each poll, and each is returned on
        every later poll. One live id evicted would be announced again the next poll."""
        hub, poller = _bb_world(self)
        numbers = [hub.open_pr() for _ in range(25)]
        old = T0 - timedelta(days=3)
        for number in numbers:
            for n in range(45):
                hub.comment(number, body="old %d.%d" % (number, n), at=old)
        state = poller.load()
        poller.poll(state)                                          # first run: 1,125 old comments remembered
        self.assertEqual(len(state["seen"]), 25 * 45)
        for tick in range(25):
            for number in numbers[tick * 5 % 25:tick * 5 % 25 + 5]:
                hub.comment(number, body="new %d.%d" % (number, tick))
            said = _comments(poller.poll(state))
            self.assertEqual(len(said), 5, "tick %d: a comment was announced twice or not at all" % tick)
        self.assertEqual(len(state["seen"]), 25 * 50)
        self.assertEqual(_comments(poller.poll(poller.load())), [], "and a restart still holds it")

    def test_restarting_before_every_single_cycle_changes_nothing(self):
        """The ticket in one property: a daemon restarted between EVERY pair of polls says exactly
        what one that never restarts says. The world is driven by a seeded generator so both runs
        see the same one: PRs open and merge, comments land on the open ones, and for a stretch
        nothing happens at all."""
        def play(restart_each_cycle):
            rng = random.Random(1109)
            hub, poller = _bb_world(self)
            state = poller.load()
            poller.poll(state)
            opened, said = [], []
            for cycle in range(800):
                if not 300 <= cycle < 500:
                    if len(opened) < 5 or rng.random() < 0.3:
                        if len(opened) >= 5:
                            hub.merge(opened.pop(rng.randrange(len(opened))))
                        opened.append(hub.open_pr())
                    for _ in range(rng.randint(0, 9)):
                        hub.comment(rng.choice(opened), body="c%d.%d" % (cycle, rng.randint(0, 10 ** 6)),
                                    author=ALICE if rng.random() < 0.15 else BOB)
                if restart_each_cycle:
                    state = poller.load()
                said.extend(_comments(poller.poll(state)))
            return said

        steady, restarted = play(False), play(True)
        self.assertGreater(len(steady), 1200, "the world must be busy enough to have overflowed the old cap")
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
                "prs": raw.get("prs", {}), "degraded": raw.get("degraded", {}),
                "first_run": False}

    def _cycled(self):
        hub, poller = _bb_world(self)
        state = poller.load()
        poller.poll(state)
        _busy_repo(hub, poller, state, ticks=8)
        return poller

    def test_the_file_is_marked_as_the_ordered_format(self):
        raw = self._cycled().saved()
        self.assertEqual(raw.get("seen_v"), 2)
        self.assertIsInstance(raw["seen"], list)
        self.assertTrue(all(isinstance(x, str) for x in raw["seen"]))
        self.assertGreater(len(raw["seen"]), 0)

    def test_the_old_code_still_reads_the_file(self):
        """Rollback safety: `seen` stays a flat list of id strings and the marker is a new key the
        old loader ignores, so a rollback loses the ORDER (it resumes the arbitrary truncation) but
        never the ids, and never fails to start."""
        poller = self._cycled()
        old = self._pre_seen_memory_loader(poller.state_path)
        new = poller.load()
        self.assertEqual(old["seen"], set(new["seen"]))
        for key in ("since", "prs", "degraded"):
            self.assertEqual(old[key], new[key], key)
        self.assertGreater(len(old["seen"]), 0)

    def test_a_marked_file_is_trusted_and_gets_no_baseline(self):
        self.assertFalse(self._cycled().load().get("legacy_seen"))

    def test_a_set_from_an_older_caller_is_upgraded_and_saved_ordered(self):
        """`poll_profile` upgrades a `seen` handed to it as a set (the shape every earlier state, and
        the fixtures built from it, carry) and saves the ordered form."""
        hub, poller = _bb_world(self)
        number = hub.open_pr()
        found = hub.comment(number, body="old news", at=T0 - timedelta(minutes=30))
        state = {"since": (T0 - timedelta(seconds=90)).isoformat(), "seen": set(["c:%d" % found["id"]]),
                 "prs": {REPO: {}}, "first_run": False}
        poller.poll(state)
        self.assertNotIsInstance(state["seen"], set)
        self.assertEqual(poller.saved()["seen"], ["c:%d" % found["id"]])
        self.assertEqual(poller.saved()["seen_v"], 2)

    def test_save_state_takes_the_smallest_state_it_always_did(self):
        """tests/test_build_marker hands it a bare set and no other key: it must still write."""
        hub, poller = _bb_world(self)
        bw.save_state("p", {"since": "t", "seen": set(), "prs": {}})
        with open(os.path.join(poller.state_dir, "p.json")) as fh:
            raw = json.load(fh)
        self.assertEqual((raw["since"], raw["seen"], raw["seen_v"]), ("t", [], 2))


class LegacyStateFile(unittest.TestCase):
    """The first load of a state file written BEFORE the fix.

    Its `seen` is an arbitrary subset: the ids the old daemon dropped are already lost, so on the
    first cycle Bitbucket returns comments nobody can prove were announced. Those must not be
    announced again, and a comment that really is new must still be.

    The design: a file without the `seen_v` marker is legacy. On its first cycle an unseen comment
    is adopted SILENTLY only on positive evidence that it pre-dates the old daemon's last look: its
    own `created_on` parses and is at least one poll interval before the file's watermark. The poll
    here is 90 seconds, so the interval is 90, where the other daemons' is 60. Anything else is
    announced as it always was. A comment older than the watermark less five minutes was already
    adopted silently, by this daemon's existing rule, and stays so.

    And only where the file says ids were LOST and that the last cycle READ everything (a review
    found this): a file under the old cap has never lost an id, and one with an open `comments`
    episode was not fully read, so for those nothing is adopted and everything announces.
    """

    def setUp(self):
        self.hub, self.poller = _bb_world(self)
        self.since = T0 - timedelta(seconds=180)            # the old daemon's last cycle started 3 minutes ago
        self.pr = self.hub.open_pr()

    def _ago(self, **kw):
        return self.since - timedelta(**kw)

    def _after(self, **kw):
        return self.since + timedelta(**kw)

    def _write(self, seen, **over):
        """A state file as the old daemon left it: `seen` in no particular order and no marker, and
        the PR already known (so it is not announced as opened)."""
        raw = {"since": self.since.isoformat(), "seen": list(seen),
               "prs": {REPO: {str(self.pr): {"head": "cff2ea034", "approvals": [], "approver_names": {}}}},
               "degraded": {}, "build": {"src": "f776569169d3", "head": "38ffbbd"}}
        raw.update(over)
        self.poller.write(raw)

    @staticmethod
    def _arbitrary(remembered, size=2000):
        """`size` ids (2000 is the old cap), `remembered` among them, in no particular order (a fixed shuffle)."""
        filler = ["c:%d" % (100000 + i) for i in range(2000)]
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
        """What Bitbucket returns at the first poll after the restart that pre-dates the old daemon's
        last look and is inside the daemon's query floor (the watermark less five minutes). Returns
        the ids the old file still remembers: half of them, the other half dropped by the truncation."""
        remembered = []
        for seconds, kept in ((250, False), (200, True), (150, False), (100, True)):
            found = self.hub.comment(self.pr, body="old comment %d" % seconds, at=self._ago(seconds=seconds))
            if kept:
                remembered.append("c:%d" % found["id"])
        # Before the floor (the watermark less five minutes): adopted silently by the daemon's own rule.
        self.hub.comment(self.pr, body="beyond the floor", at=self._ago(seconds=400))
        self.hub.comment(self.pr, body="ancient", at=self._ago(hours=9))
        return remembered

    def _new_comments(self):
        """Three comments that are genuinely new, or cannot be proven old. Returns what they say."""
        self.hub.comment(self.pr, body="new while down", at=self._after(seconds=30))
        self.hub.comment(self.pr, body="inside the guard", at=self._ago(seconds=60))
        self.hub.comment(self.pr, body="just before it looked", at=self._ago(seconds=10))
        return sorted(["inside the guard", "just before it looked", "new while down"])

    def test_the_first_cycle_re_emits_nothing_the_old_file_lost(self):
        """THE migration test: 2 comments Bitbucket returns were dropped by the arbitrary truncation.
        None is announced; the three that are new (after the watermark, or too close to it to prove
        otherwise) all are."""
        remembered = self._visible_world()
        expected = self._new_comments()
        self._write(self._arbitrary(remembered))
        state = self.poller.load()
        self.assertFalse(state["first_run"])
        self.assertEqual(_bodies(self.poller.poll(state)), expected)

    # -- a review's finding: adopt only where the file says ids were LOST and the last cycle READ everything --

    def test_a_file_under_the_old_cap_adopts_nothing(self):
        """The old code dropped an id only at its cut, list(seen)[-2000:], and a set only grows, so a file
        holding fewer than 2,000 ids has never lost one: a comment it does not hold was never announced. (The
        live file held 34 on 2026-09-29.) The two the big test above treats as lost are, under the cap, simply unread, so
        they announce with the new ones; adopting them would swallow them."""
        remembered = self._visible_world()
        expected = sorted(self._new_comments() + ["old comment 250", "old comment 150"])
        self._write(self._arbitrary(remembered, size=1999))
        state = self.poller.load()
        self.assertEqual(_bodies(self.poller.poll(state)), expected)
        self.assertEqual([n for n in self.poller.notes if "adopted" in n], [])
        self.assertFalse(state["legacy_seen"])

    def test_a_file_exactly_at_the_old_cap_with_no_open_episode_still_adopts(self):
        """The other side of the boundary, and exactly what the migration did before: at the cap ids WERE being
        dropped, and nothing on disk says the last cycle left a slice unread."""
        remembered = self._visible_world()
        expected = self._new_comments()
        self._write(self._arbitrary(remembered, size=2000), degraded={})
        self.assertEqual(_bodies(self.poller.poll(self.poller.load())), expected)

    def test_a_comment_the_old_daemon_never_read_is_announced_after_its_fetch_was_failing(self):
        """A reviewer's reproduction, on the real scenario. The old daemon's last cycles got a 502 on the comment
        fetch: the holes were recorded and `since` advanced anyway, so a comment stamped before the last
        look was never read. The five-minute overlap used to catch it on the next cycle; adopting it silently
        loses it for good. The file says so: an open `comments` episode. Made by running a daemon through it
        (so the `degraded` block is the real one), then made legacy with `seen` AT the old cap, so that the cap
        rule cannot be what saves the comment."""
        hub, poller = self.hub, self.poller
        poller.clock.current = T0 - timedelta(seconds=270)
        first = poller.clock.now()
        state = poller.load()
        poller.poll(state)                                   # cycle 1: the PR is there, no comments yet
        hub.comment(self.pr, body="the unread comment", at=first + timedelta(seconds=5))
        hub.fail = ("/comments",)
        poller.poll(state)                                   # cycle 2: a 502 on the comment fetch
        poller.poll(state)                                   # cycle 3: again; the last cycle the old daemon saves
        hub.fail = ()
        raw = poller.saved()
        self.assertTrue(raw["degraded"]["comments"]["open"], "the fixture must leave the episode open")
        del raw["seen_v"]
        raw["seen"] = self._arbitrary([], size=2000)
        poller.write(raw)
        state = poller.load()
        self.assertEqual(_bodies(poller.poll(state)), ["the unread comment"])
        self.assertEqual([n for n in poller.notes if "adopted" in n], [])

    def test_a_closed_episode_does_not_stop_the_adoption(self):
        """Closed: the slice was read again in the cycle after the failure, inside the overlap. (A closed record
        outlives its recovery by the renotify window, so it is the `open` flag that matters.)"""
        remembered = self._visible_world()
        expected = self._new_comments()
        closed = self._episode("comments", open_=False)
        self.assertFalse(closed["comments"]["open"])
        self._write(self._arbitrary(remembered), degraded=closed)
        self.assertEqual(_bodies(self.poller.poll(self.poller.load())), expected)

    def test_an_open_episode_for_a_sub_that_does_not_feed_the_memory_does_not_stop_the_adoption(self):
        """`pr_state` is a real sub of this daemon (a closed PR's state fetch), and it feeds `prs`, not `seen`."""
        remembered = self._visible_world()
        expected = self._new_comments()
        self._write(self._arbitrary(remembered), degraded=self._episode("pr_state"))
        self.assertEqual(_bodies(self.poller.poll(self.poller.load())), expected)

    def test_the_daemon_log_says_why_nothing_is_adopted(self):
        self._write(self._arbitrary([], size=34))
        said = _seen.legacy_note("acme", self.poller.load(), bw.LEGACY_GUARD)
        self.assertIn("adopts nothing", said)
        self.assertIn("under the old cap of 2000", said)
        self._write(self._arbitrary([]), degraded=self._episode("comments"))
        said = _seen.legacy_note("acme", self.poller.load(), bw.LEGACY_GUARD)
        self.assertIn("adopts nothing", said)
        self.assertIn("comments", said)

    def test_the_daemon_log_says_what_the_migration_adopted(self):
        self._write(self._arbitrary(self._visible_world()))
        self.poller.poll(self.poller.load())
        said = [n for n in self.poller.notes if "adopted" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertRegex(said[0], r"adopted 2 comment\(s\) silently")

    def test_a_legacy_file_loads_with_its_watermark_and_a_cycle_budget(self):
        self._write(self._arbitrary([]))
        legacy = self.poller.load()["legacy_seen"]
        self.assertEqual(legacy["cutoff"], self.since.isoformat())
        self.assertGreater(legacy["cycles"], 0)

    def test_main_starts_on_a_legacy_file_and_says_so(self):
        """`main()` prints the legacy note when it loads a profile. A typo in the lines it gained
        would crash-loop the daemon at its FIRST start after the deploy, so run it: a config in a
        private tree (the real one is never read), the real load, one real cycle, and the sleep that
        ends the first pass turned into a stop."""
        self._write(self._arbitrary([]))
        config = self.poller.write_config({"profiles": {"acme": {"bitbucket": dict(CFG)}}})

        class Stop(Exception):
            pass

        def stop(_secs):
            raise Stop()

        clock = mock.Mock(sleep=stop, time=time.time)
        with mock.patch.object(bw, "CONFIG_PATH", config), mock.patch.object(bw, "BKT", "/nonexistent/bkt"), \
                mock.patch.object(bw, "time", clock):
            with self.assertRaises(Stop):
                bw.main()
        said = [n for n in self.poller.notes if "predates" in n]
        self.assertEqual(len(said), 1, self.poller.notes)
        self.assertIn("2000 ids", said[0])
        self.assertEqual(self.poller.saved()["seen_v"], 2, "the cycle main() ran must have written the new format")

    def test_the_boundary_is_one_poll_interval_before_the_watermark(self):
        """Pinned so it is a decision, not an accident. This daemon polls every 90 seconds, so its
        guard is 90, not the 60 the other daemons use: 91 seconds before the watermark is old, 89 is
        not. The old daemon's last look can lag the watermark by a fetch and the two clocks are not
        the same clock; a duplicate is the safe direction and a swallowed event is not."""
        self.assertEqual(bw.POLL_SECS, 90)
        self.assertEqual(bw.LEGACY_GUARD, timedelta(seconds=bw.POLL_SECS))
        self.hub.comment(self.pr, body="91 s before", at=self._ago(seconds=91))
        self.hub.comment(self.pr, body="89 s before", at=self._ago(seconds=89))
        self._write(self._arbitrary([]))
        self.assertEqual(_bodies(self.poller.poll(self.poller.load())), ["89 s before"])

    def test_an_unreadable_timestamp_is_still_adopted_by_the_rule_the_daemon_always_had(self):
        """Not a new rule: a comment whose `created_on` does not parse has always been remembered
        without a wake here (`ts is None`). The migration neither adds to it nor takes it away."""
        found = self.hub.comment(self.pr, body="no date", raw_created="last tuesday")
        self._write(self._arbitrary([]))
        state = self.poller.load()
        self.assertEqual(_comments(self.poller.poll(state)), [])
        self.assertIn("c:%d" % found["id"], state["seen"])

    def test_no_watermark_means_no_baseline(self):
        """Nothing to compare against: announce as the old code would (within its first-run lookback)."""
        self.hub.comment(self.pr, body="two minutes ago", at=T0 - timedelta(minutes=2))
        self._write(self._arbitrary([]), since=None)
        state = self.poller.load()
        self.assertEqual(_bodies(self.poller.poll(state)), ["two minutes ago"])
        self.assertFalse(state["legacy_seen"], "there is nothing to hold when there was nothing to compare")

    def test_no_watermark_leaves_nothing_to_hold_even_after_a_failed_fetch(self):
        self._write(self._arbitrary([]), since=None)
        state = self.poller.load()
        self.hub.fail = ("/comments",)
        self.poller.poll(state)
        self.assertFalse(state["legacy_seen"])
        self.assertEqual([n for n in self.poller.notes if "baseline holds" in n], [])

    def test_the_baseline_is_one_clean_cycle_long(self):
        """It must not swallow beyond the migration: once a cycle has completed cleanly the file is
        trusted, and an unseen comment inside the query floor announces whatever its timestamp says."""
        self._write(self._arbitrary(self._visible_world()))
        state = self.poller.load()
        self.assertEqual(_comments(self.poller.poll(state)), [])
        self.assertFalse(state["legacy_seen"])
        self.hub.comment(self.pr, body="stamped long ago, seen late", at=self._ago(seconds=100))
        self.assertEqual(_bodies(self.poller.poll(state)), ["stamped long ago, seen late"])

    def test_a_failed_fetch_in_the_first_cycle_keeps_the_baseline_for_the_next(self):
        """A hole in the baseline cycle leaves its scope unbaselined, and holding the baseline is the
        cheap repair: without it the comments the failed fetch never read would be announced next
        cycle as if they were new."""
        self.hub.comment(self.pr, body="lost", at=self._ago(seconds=100))
        self.hub.comment(self.pr, body="new while down", at=self._after(seconds=30))
        self.hub.comment(self.pr, body="inside the guard", at=self._ago(seconds=30))
        self._write(self._arbitrary([]))
        state = self.poller.load()
        self.hub.fail = ("/comments",)
        self.assertEqual(_comments(self.poller.poll(state)), [])
        self.assertTrue(state["legacy_seen"], "a cycle with a hole consumed the baseline")
        self.hub.fail = ()
        self.assertEqual(_bodies(self.poller.poll(state)), ["inside the guard", "new while down"],
                         "the old comment was announced, or a new one lost")
        self.assertFalse(state["legacy_seen"])

    def test_a_failed_state_fetch_does_not_hold_the_baseline(self):
        """Only a fetch that feeds the memory holds it. A closed PR's state fetch does not."""
        gone = self.hub.open_pr()
        self.hub.merge(gone)
        self._write(self._arbitrary([]), prs={REPO: {str(self.pr): {"head": "cff2ea034", "approvals": [],
                                                                    "approver_names": {}},
                                                     str(gone): {"head": "cff2ea034", "approvals": [],
                                                                 "approver_names": {}}}})
        state = self.poller.load()
        self.hub.fail = ("/pullrequests/%d" % gone,)
        self.poller.poll(state)
        self.assertFalse(state["legacy_seen"])

    def test_a_hole_that_never_heals_cannot_keep_the_baseline_forever(self):
        """Bounded: a permanently failing fetch must not leave a silent-adoption rule running for
        the life of the daemon."""
        self._write(self._arbitrary([]))
        state = self.poller.load()
        self.hub.fail = ("/comments",)
        for _ in range(_seen.LEGACY_MAX_CYCLES):
            self.poller.poll(state)
        self.assertFalse(state["legacy_seen"])

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
        self.assertEqual(len(_comments(self.poller.poll(self.poller.load()))), 4)

    def test_a_first_run_is_not_a_migration(self):
        state = self.poller.load()          # no file yet
        self.assertTrue(state["first_run"])
        self.assertFalse(state.get("legacy_seen"))

    def test_a_long_downtime_still_announces_what_was_said_while_the_daemon_was_down(self):
        """The boundary is the file's watermark, not the load time: what was written during the nap
        is exactly what resume coverage is for, and stays news."""
        self.since = T0 - timedelta(days=2)
        self.hub.comment(self.pr, body="lost just before it stopped", at=self._ago(seconds=150))
        self.hub.comment(self.pr, body="written during the nap", at=T0 - timedelta(days=1))
        self._write(self._arbitrary([]))
        self.assertEqual(_bodies(self.poller.poll(self.poller.load())), ["written during the nap"])


if __name__ == "__main__":
    unittest.main()
