#!/usr/bin/env python3
"""stalled_wake turns the ABSENCE of change into a line.

Every other daemon is pinned on what it emits when something happens. This
one is pinned on what it emits when nothing does: a PR with a standing
verdict and no activity, or a ticket whose Review/In Progress work state has
not moved. And on the two ways such a sweep lies — repeating itself
every cycle, and printing "none" when the query was the thing that failed.

The three fetches and ``emit`` are replaced: no network, no live logs; state
goes to a temp dir. Run: ``just test``.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))
import bitbucket_wake  # noqa: E402
import github_wake  # noqa: E402
import stalled_wake as sw  # noqa: E402
import watchdog_wake  # noqa: E402

NOW = datetime(2026, 9, 9, 20, 0, 0, tzinfo=timezone.utc)
GH_REPO = "acme/app"
BB_REPO = "acme/service"
PAIR_KEY = "pair:%s#569->alice/Agent-Watchtower#8" % GH_REPO   # the companion is part of the key
PCFG = {
    "github": {"repos": [GH_REPO]},
    "bitbucket": {"repos": [BB_REPO], "cwd": "~"},
    "jira": {"site": "https://x.atlassian.net", "email": "e", "api_token": "t", "projects": ["TD"]},
}
CFG = {"days": 3, "reemit_hours": 24}


def _ago(days):
    return NOW - timedelta(days=days)


def _gh_ts(days_ago):
    return _ago(days_ago).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh(number, verdict, days_ago, title="t", verdict_days_ago=None, reviews=True, draft=False):
    """``days_ago`` is the artefact's last touch; the verdict's own clock defaults to
    the same moment, or ``verdict_days_ago`` when the two differ."""
    head = ("%040d" % number)[-40:]
    when = days_ago if verdict_days_ago is None else verdict_days_ago
    pr = {"number": number, "title": title, "author": {"login": "bob"},
          "updatedAt": _gh_ts(days_ago), "reviewDecision": verdict, "headRefOid": head,
          "isDraft": draft,
          "commits": [{"oid": head, "committedDate": _gh_ts(when + 1)}]}
    if reviews and verdict in sw.VERDICTS:
        pr["reviews"] = [{"author": {"login": "alice"}, "state": verdict,
                          "submittedAt": _gh_ts(when), "commit": {"oid": head}}]
    return pr


def gql_page(prs, has_next=False, cursor=None):
    """One `gh api graphql` result, built from the flat ``gh()`` fixtures.

    ``gh_prs`` now unwraps the connections itself, so the fixtures stay
    in the flat shape the classifier reads and this re-wraps them the way
    GitHub returns them — including the ``{"commit": {...}}`` wrapper that
    PullRequestCommit nodes carry and ``gh pr list --json`` used to strip.
    """
    return (0, json.dumps({"data": {"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": [{
            "number": pr["number"], "title": pr["title"], "author": pr["author"],
            "updatedAt": pr["updatedAt"], "reviewDecision": pr["reviewDecision"],
            "headRefOid": pr["headRefOid"], "isDraft": pr.get("isDraft", False),
            "reviews": {"nodes": pr.get("reviews", [])},
            "commits": {"nodes": [{"commit": c} for c in pr.get("commits", [])]},
        } for pr in prs],
    }}}}), "")


def merged_pr(number, days_ago, lands_with=None, title="t"):
    body = "Does a thing.\n\nLands-with: %s\n" % lands_with if lands_with else "Does a thing."
    return {"number": number, "title": title, "body": body, "mergedAt": _gh_ts(days_ago)}


def origin_pr(days_ago, lands_with="alice/Agent-Watchtower#8", title="t"):
    """The same PR as `gh api repos/…/pulls/N` returns it — snake_case, body included."""
    listed = merged_pr(0, days_ago, lands_with, title)
    return {"merged_at": listed["mergedAt"], "title": title, "body": listed["body"]}


def bb(pr_id, approved, days_ago, approved_days_ago=None):
    when = days_ago if approved_days_ago is None else approved_days_ago
    return {"id": pr_id, "title": "t", "author": {"display_name": "Bob"},
            "updated_on": _ago(days_ago).isoformat(),
            "participants": [{"approved": approved, "participated_on": _ago(when).isoformat(),
                              "user": {"display_name": "Alice", "uuid": "{u}"}}]}


def bb_changes(pr_id, days_ago, requested_days_ago=None, requesters=("Alice",)):
    """A Bitbucket PR held only by changes-requested: no participant has approved."""
    when = days_ago if requested_days_ago is None else requested_days_ago
    return {"id": pr_id, "title": "t", "author": {"display_name": "Bob"},
            "updated_on": _ago(days_ago).isoformat(),
            "participants": [{"approved": False, "state": "changes_requested",
                              "participated_on": _ago(when).isoformat(),
                              "user": {"display_name": name, "uuid": "{%s}" % name}}
                             for name in requesters]}


def _jira_ts(days_ago):
    return _ago(days_ago).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def jira(key, status, days_ago, assignee=None, issue_type="Task", created_days_ago=None,
         issue_id=None, summary="s", parent=None, hierarchy_level=None):
    """A candidate issue. ``days_ago`` is Jira's broad last-touch timestamp;
    the semantic clock lives separately in the changelog fixture below."""
    created_days_ago = days_ago if created_days_ago is None else created_days_ago
    fields = {
        "summary": summary, "status": {"name": status},
        "updated": _jira_ts(days_ago), "created": _jira_ts(created_days_ago),
        "assignee": {"displayName": assignee} if assignee else None,
        "issuetype": {"name": issue_type}}
    if hierarchy_level is not None:
        fields["issuetype"]["hierarchyLevel"] = hierarchy_level
    if parent is not None:
        fields["parent"] = {"id": str(parent.get("id")), "key": parent.get("key")}
    return {"id": str(issue_id or ("id-" + key)), "key": key, "fields": fields}


def jira_change(field, days_ago, from_string=None, to_string=None, numeric=False,
                change_id=None):
    """One history entry in the bulk-changelog shape.

    Jira's bulk endpoint documents ``created`` as an epoch number, while older
    issue-changelog responses use an ISO timestamp. The daemon must accept both.
    """
    created = int(_ago(days_ago).timestamp()) if numeric else _jira_ts(days_ago)
    return {"id": str(change_id or ("change-%s-%s" % (field, days_ago))),
            "created": created,
            "items": [{"field": field, "fieldId": field,
                       "fromString": from_string, "toString": to_string}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.emitted = []
        self.notes = []
        self.gh = {GH_REPO: []}
        self.bb = {BB_REPO: []}
        self.jira = {"Review": [], "In Progress": []}
        self.jira_children = []
        self.jira_histories = {}
        self.jira_searches = []
        self.jira_changelog_batches = []
        self.merged = {GH_REPO: []}                           # merged PRs, the pool for pairs
        self.companions = {}                                  # (repo, num) -> merged? (or Exception)
        self.origins = {}                                     # (repo, num) -> the PR re-read by number (or Exception)
        self.reachable = {GH_REPO: 1}                        # gh_reachable mock
        # Every patch is NAMED. Tests used to stop/start entries by list index;
        # on 2026-09-10 an edit that inserted two patches at the front shifted the
        # indices, one test stopped the EMIT patch instead of `gh_prs`, and six
        # fixture lines were written into the live ~/Library/Logs/watchtower.
        # Belt and braces below: the live LOG_DIR / STATE_DIR are patched to the
        # temp tree too, so an unpatched writer still cannot reach them.
        self.p_gh_prs = mock.patch.object(sw, "gh_prs", self._gh_prs)
        self.p_bb_prs = mock.patch.object(sw, "bb_prs", self._bb_prs)
        self.p_jira = mock.patch.object(sw, "jira_search", self._jira_search)
        # New Jira reads get their own named seam. `create=True` keeps this test
        # fixture safe while a red test is developed before the source helper.
        self.p_jira_changelogs = mock.patch.object(
            sw, "jira_changelogs", self._jira_changelogs, create=True)
        self.p_jira_client = mock.patch.object(
            sw.jira_wake, "Jira", side_effect=AssertionError("unmocked Jira network access"))
        self.p_gh_merged = mock.patch.object(sw, "gh_merged_prs", self._gh_merged)
        self.p_companion = mock.patch.object(github_wake, "companion_merged", self._companion_merged)
        self.p_gh_pr = mock.patch.object(sw, "gh_pr", self._gh_pr)
        # Gh_reachable patch
        self.p_gh_reachable = mock.patch.object(sw, "gh_reachable", self._gh_reachable, create=True)
        self.patches = [
            mock.patch.object(sw, "LOG_DIR", os.path.join(self.tmp.name, "logs")),
            mock.patch.object(github_wake, "LOG_DIR", os.path.join(self.tmp.name, "logs")),
            mock.patch.object(sw, "STATE_DIR", os.path.join(self.tmp.name, "state")),
            mock.patch.object(sw, "emit", lambda profile, line: self.emitted.append(line)),
            mock.patch.object(sw, "note", lambda line: self.notes.append(line)),
            self.p_gh_prs,
            self.p_bb_prs,
            self.p_jira,
            self.p_jira_changelogs,
            self.p_jira_client,
            mock.patch.object(bitbucket_wake, "BKT", "/usr/bin/true"),
            # codex pass 2 on this branch: compute() checks GH before the (mocked) fetch,
            # so without this the suite passed only on hosts that happen to have gh.
            mock.patch.object(sw, "GH", "/usr/bin/gh"),
            self.p_gh_merged,
            self.p_companion,
            self.p_gh_pr,
            self.p_gh_reachable,
        ]
        for p in self.patches:
            p.start()
        self.state = sw.load_state("acme")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _gh_prs(self, repo):
        out = self.gh[repo]
        if isinstance(out, Exception):
            raise out
        return out

    def _gh_reachable(self, repo):
        out = self.reachable.get(repo, 1)
        if isinstance(out, Exception):
            raise out
        return out

    def _gh_merged(self, repo, since):
        out = self.merged.get(repo, [])
        if isinstance(out, Exception):
            raise out
        return out

    def _companion_merged(self, repo, num):
        out = self.companions.get((repo, num), False)
        if isinstance(out, Exception):
            raise out
        return out

    def _gh_pr(self, repo, num):
        out = self.origins.get((repo, num), RuntimeError("no such PR in the fixture"))
        if isinstance(out, Exception):
            raise out
        return out

    def _bb_prs(self, repo, cwd):
        out = self.bb[repo]
        if isinstance(out, Exception):
            raise out
        return out

    def _jira_search(self, cfg, jql):
        self.jira_searches.append(jql)
        low = jql.lower()
        if "parent in" in low:
            out = self.jira_children
        elif "status = review" in low and "in progress" not in low:
            # Compatibility with the older two-query source while these
            # tests are being introduced.
            out = self.jira["Review"]
        elif 'status = "in progress"' in low and "review" not in low:
            out = self.jira["In Progress"]
        else:
            out = []
            for label in ("Review", "In Progress"):
                value = self.jira[label]
                if isinstance(value, Exception):
                    raise value
                out.extend(value)
        if isinstance(out, Exception):
            raise out
        return out

    def _jira_changelogs(self, cfg, issues):
        self.jira_changelog_batches.append([str(issue.get("id")) for issue in issues])
        if isinstance(self.jira_histories, Exception):
            raise self.jira_histories
        result = {}
        for issue in issues:
            issue_id = str(issue.get("id"))
            value = self.jira_histories.get(issue_id)
            if isinstance(value, Exception):
                raise value
            if value is None:
                fields = issue.get("fields") or {}
                # Preserve all older tests: their broad `updated` timestamp was
                # also the status clock before that change. New tests set an explicit
                # history whenever the two clocks intentionally differ.
                value = [{"id": "default-" + issue_id,
                          "created": fields.get("updated"),
                          "items": [{"field": "status", "fieldId": "status",
                                     "fromString": "To Do",
                                     "toString": (fields.get("status") or {}).get("name")}]}]
            result[issue_id] = list(value)
        return result

    def history(self, issue, *entries):
        self.jira_histories[str(issue["id"])] = list(entries)
        return issue

    def poll(self, now=NOW):
        self.emitted.clear()
        self.notes.clear()
        sw.poll_profile("acme", PCFG, CFG, self.state, now=now)
        return list(self.emitted)

    def types(self, lines):
        return [line.split()[0] for line in lines]


class TestTimestamps(unittest.TestCase):
    """The daemons run under 3.9: ``fromisoformat`` takes no Z, only 3/6-digit
    fractions and no bare offset — and the three APIs send all three."""

    def test_the_three_api_formats_parse_to_the_same_instant(self):
        want = datetime(2026, 9, 4, 20, 35, 2, tzinfo=timezone.utc)
        self.assertEqual(sw.parse_ts("2026-09-04T20:35:02Z"), want)                     # GitHub
        self.assertEqual(sw.parse_ts("2026-09-04T20:35:02.123456+00:00"), want.replace(microsecond=123456))  # Bitbucket
        self.assertEqual(sw.parse_ts("2026-09-04T13:35:02.305-0700"), want.replace(microsecond=305000))      # Jira
        self.assertEqual(sw.parse_ts(int(want.timestamp())), want)                      # Jira bulk history
        self.assertEqual(sw.parse_ts(int(want.timestamp() * 1000)), want)               # defensive milliseconds
        self.assertIsNone(sw.parse_ts("not a time"))
        self.assertIsNone(sw.parse_ts(None))

    def test_idle_days_floors_and_never_goes_negative(self):
        self.assertEqual(sw.idle_days(_ago(5).strftime("%Y-%m-%dT%H:%M:%SZ"), NOW), 5)
        self.assertEqual(sw.idle_days((NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), NOW), 0)
        self.assertIsNone(sw.idle_days("garbage", NOW))


class TestWhatCountsAsStalled(Base):
    def test_a_pr_with_a_standing_verdict_and_no_activity_is_stalled(self):
        self.gh[GH_REPO] = [gh(547, "CHANGES_REQUESTED", 5, "stale verdict"),
                            gh(548, "APPROVED", 1, "fresh approval"),
                            gh(549, "REVIEW_REQUIRED", 9, "no verdict, just old"),
                            gh(550, None, 20, "never reviewed")]
        lines = self.poll()
        self.assertEqual(self.types(lines), ["type=stalled"])
        self.assertIn("kind=pr repo=app #547 CHANGES_REQUESTED reviewer=alice", lines[0])
        self.assertIn("binding=head idle=5d author=bob | stale verdict", lines[0])
        self.assertNotIn(" by=", lines[0])

    # ----------------------------------------------------------------
    # A DRAFT PR with a standing verdict is still reported (an approved
    # draft forgotten for a week is real), but the line says it is a draft, so a
    # reader can tell a parked PR from one waiting on someone. Non-draft lines
    # are byte-for-byte what they were.
    # ----------------------------------------------------------------

    def test_a_stalled_draft_is_still_reported_and_is_marked_draft(self):
        self.gh[GH_REPO] = [gh(591, "APPROVED", 7, "parked on upstream", draft=True)]
        lines = self.poll()
        self.assertEqual(self.types(lines), ["type=stalled"])
        self.assertIn("kind=pr repo=app #591 draft=true APPROVED reviewer=alice",
                      lines[0])
        self.assertIn("binding=head idle=7d author=bob | parked on upstream", lines[0])

    def test_a_stalled_non_draft_line_carries_no_draft_token(self):
        self.gh[GH_REPO] = [gh(547, "CHANGES_REQUESTED", 5, "stale verdict")]
        lines = self.poll()
        self.assertEqual(self.types(lines), ["type=stalled"])
        self.assertIn("kind=pr repo=app #547 CHANGES_REQUESTED reviewer=alice",
                      lines[0])
        self.assertNotIn("draft", lines[0])

    def test_a_draft_that_is_not_idle_is_not_stalled(self):
        self.gh[GH_REPO] = [gh(592, "APPROVED", 1, "fresh draft", draft=True)]
        self.assertEqual(self.poll(), [])

    def test_jira_review_idle_and_in_progress_unassigned_are_stalled(self):
        self.jira["Review"] = [jira("ACME-843", "Review", 11, "Bob Reviewer")]
        self.jira["In Progress"] = [jira("ACME-732", "In Progress", 7)]
        lines = sorted(self.poll())
        self.assertEqual(len(lines), 2)
        self.assertIn('kind=jira issue=ACME-732 "In Progress" unassigned idle=7d | s', lines[0])
        self.assertIn('kind=jira issue=ACME-843 "Review" assignee="Bob Reviewer" idle=11d | s', lines[1])

    def test_a_bitbucket_pr_is_stalled_only_with_an_approval(self):
        self.bb[BB_REPO] = [bb(72, True, 12), bb(73, False, 40)]
        lines = self.poll()
        self.assertEqual(len(lines), 1)
        self.assertIn('kind=bb repo=service #72 approved=1 approvers=["Alice"] '
                      'idle=12d author="Bob" | t', lines[0])
        self.assertNotIn(" by=", lines[0])

    def test_the_first_sweep_emits_what_is_already_stalled(self):
        """The existing set IS the finding — no silent baseline here."""
        self.assertIsNone(self.state["since"])
        self.gh[GH_REPO] = [gh(2, "APPROVED", 16)]
        self.assertEqual(self.types(self.poll()), ["type=stalled"])

    def test_idle_counts_from_the_verdict_not_the_last_touch(self):
        """A bot comment or an author's push resets updatedAt and
        would hide a standing verdict forever. The verdict's own clock rules."""
        self.gh[GH_REPO] = [gh(547, "APPROVED", 1, "approved 5d ago, touched yesterday", verdict_days_ago=5),
                            gh(548, "CHANGES_REQUESTED", 9, "old touch, fresh verdict", verdict_days_ago=1)]
        lines = sorted(self.poll())
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("#547 APPROVED reviewer=alice", lines[0])
        self.assertIn("binding=head idle=5d", lines[0])

    def test_a_stale_review_binding_names_both_full_shas_and_the_real_reviewer(self):
        item = gh(547, "APPROVED", 5)
        old = "f" * 40
        item["reviews"][0]["commit"]["oid"] = old
        self.gh[GH_REPO] = [item]
        line = self.poll()[0]
        self.assertIn("reviewer=alice", line)
        self.assertIn("bound=%s" % old, line)
        self.assertIn("head=%s" % item["headRefOid"], line)
        self.assertIn("binding=stale", line)
        self.assertIn("author=bob", line)

    def test_a_temporally_stale_binding_is_visible_not_a_source_hole(self):
        item = gh(547, "APPROVED", 5)
        item["commits"][0]["committedDate"] = _gh_ts(4)
        self.gh[GH_REPO] = [item]

        lines = self.poll()

        self.assertEqual(self.types(lines), ["type=stalled"])
        self.assertIn("reviewer=alice", lines[0])
        self.assertIn("binding=stale", lines[0])
        self.assertFalse(any("review binding unreadable" in note for note in self.notes))

    def test_bitbucket_idle_counts_from_the_approval(self):
        self.bb[BB_REPO] = [bb(72, True, 1, approved_days_ago=12), bb(73, True, 9, approved_days_ago=1)]
        lines = self.poll()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('#72 approved=1 approvers=["Alice"] idle=12d', lines[0])

    def test_a_bitbucket_pr_held_only_by_changes_requested_is_stalled(self):
        """A standing changes-requested verdict waits on its author just as an
        approval waits on a merge; it was invisible here."""
        self.bb[BB_REPO] = [bb_changes(75, 12)]
        lines = self.poll()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('kind=bb repo=service #75 ', lines[0])
        self.assertIn('changes_requested=1 requesters=["Alice"] idle=12d author="Bob" | t', lines[0])
        self.assertIn('approved=0 ', lines[0])

    def test_changes_requested_idle_counts_from_the_requesting_participant(self):
        self.bb[BB_REPO] = [bb_changes(75, 1, requested_days_ago=12),
                            bb_changes(76, 9, requested_days_ago=1)]
        lines = self.poll()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('#75 ', lines[0])
        self.assertIn('idle=12d', lines[0])

    def test_a_pr_with_both_verdicts_is_one_item_clocked_from_the_latest(self):
        pr = bb(77, True, 20)
        extra = bb_changes(77, 20, requested_days_ago=10, requesters=("Pat",))["participants"]
        pr["participants"].extend(extra)
        self.bb[BB_REPO] = [pr]
        lines = self.poll()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('approved=1 approvers=["Alice"]', lines[0])
        self.assertIn('changes_requested=1 requesters=["Pat"] idle=10d', lines[0])

    def test_a_bitbucket_pr_with_no_verdict_at_all_is_not_stalled(self):
        pr = bb(78, False, 40)
        pr["participants"][0]["state"] = None
        self.bb[BB_REPO] = [pr]
        self.assertEqual(self.poll(), [])

    def test_bb_fields_ask_for_the_participant_state(self):
        self.assertIn("values.participants.state", sw.BB_FIELDS)

    def test_a_profile_without_sources_stalls_nothing(self):
        items, answered, _ = sw.compute({}, CFG, NOW, sw._degraded.Cycle())
        self.assertEqual((items, answered), ({}, set()))


class TestJiraSemanticIdleness(Base):
    """Jira's broad `updated` is not a work-state clock."""

    def jira_result(self):
        holes = sw._degraded.Cycle()
        items, complete = sw.jira_stalled(
            PCFG["jira"], PCFG["jira"]["projects"], NOW - timedelta(days=CFG["days"]), NOW,
            holes)
        return items, complete, holes

    def test_bookkeeping_touches_do_not_reset_review_or_in_progress(self):
        """Labels and summary edits appear in history; a comment only advances
        `fields.updated`. None changes the status/assignee clock."""
        review = jira("ACME-843", "Review", .25, "Bob Reviewer", created_days_ago=20)
        progress = jira("ACME-732", "In Progress", .25, "Bob Reviewer",
                        created_days_ago=20)
        self.jira["Review"] = [review]
        self.jira["In Progress"] = [progress]
        self.history(
            review,
            jira_change("status", 8, "In Progress", "Review"),
            jira_change("labels", 2, None, "seat-claimed"),
            jira_change("summary", 1, "old", "new"),
        )
        self.history(
            progress,
            jira_change("assignee", 9, None, "Bob Reviewer"),
            jira_change("status", 8, "To Do", "In Progress"),
            jira_change("labels", 2, None, "seat-claimed"),
            jira_change("summary", 1, "old", "new"),
        )

        lines = sorted(self.poll())

        self.assertEqual(self.types(lines), ["type=stalled", "type=stalled"])
        self.assertIn('issue=ACME-732 "In Progress" assignee="Bob Reviewer" idle=8d', lines[0])
        self.assertIn('issue=ACME-843 "Review" assignee="Bob Reviewer" idle=8d', lines[1])

    def test_bookkeeping_on_a_tracked_ticket_does_not_emit_a_false_clear(self):
        review = jira("ACME-843", "Review", 8, "Bob", created_days_ago=20)
        progress = jira("ACME-732", "In Progress", 8, "Bob", created_days_ago=20)
        self.jira["Review"] = [review]
        self.jira["In Progress"] = [progress]
        review_status = jira_change("status", 8, "In Progress", "Review")
        progress_status = jira_change("status", 8, "To Do", "In Progress")
        self.history(review, review_status)
        self.history(progress, progress_status)
        self.assertEqual(self.types(self.poll()), ["type=stalled", "type=stalled"])

        # The detector's own documentation/comment can make `updated` fresh.
        # Neither standing work state changed, so both episodes remain open.
        review["fields"]["updated"] = _jira_ts(0)
        progress["fields"]["updated"] = _jira_ts(0)
        self.history(review, review_status, jira_change("labels", 0, None, "seat-claimed"))
        self.history(progress, progress_status, jira_change("summary", 0, "old", "new"))

        lines = self.poll(NOW + timedelta(hours=1))

        self.assertEqual(lines, [])
        self.assertIn("jira:ACME-732", self.state["stalled"])
        self.assertIn("jira:ACME-843", self.state["stalled"])

    def test_assigned_in_progress_is_included_and_assignee_changes_reset_its_clock(self):
        old = jira("ACME-700", "In Progress", .25, "Bob", created_days_ago=20)
        fresh = jira("ACME-701", "In Progress", .25, "Alice", created_days_ago=20)
        boundary = jira("ACME-702", "In Progress", .25, "Bob", created_days_ago=20)
        self.jira["In Progress"] = [old, fresh, boundary]
        self.history(
            old,
            jira_change("status", 9, "To Do", "In Progress"),
            jira_change("assignee", 8, None, "Bob"),
        )
        self.history(
            fresh,
            jira_change("status", 9, "To Do", "In Progress"),
            jira_change("assignee", 1, "Bob", "Alice"),
        )
        self.history(
            boundary,
            jira_change("status", 9, "To Do", "In Progress"),
            # The bulk endpoint sends an epoch number. Exactly N days is stale.
            jira_change("assignee", 3, None, "Bob", numeric=True),
        )

        lines = sorted(self.poll())

        self.assertEqual(len(lines), 2, lines)
        text = "\n".join(lines)
        self.assertIn('issue=ACME-700 "In Progress" assignee="Bob" idle=8d', text)
        self.assertIn('issue=ACME-702 "In Progress" assignee="Bob" idle=3d', text)
        self.assertNotIn("ACME-701", text, "a recent assignee change resets the semantic clock")

    def test_an_assignee_change_clears_then_reopens_the_same_in_progress_episode(self):
        issue = jira("ACME-732", "In Progress", 8, None, created_days_ago=20)
        self.jira["In Progress"] = [issue]
        entered = jira_change("status", 8, "To Do", "In Progress")
        self.history(issue, entered)
        self.assertEqual(self.types(self.poll()), ["type=stalled"])

        issue["fields"]["assignee"] = {"displayName": "Bob"}
        issue["fields"]["updated"] = _jira_ts(0)
        self.history(issue, entered, jira_change("assignee", 0, None, "Bob"))
        cleared = self.poll(NOW + timedelta(hours=1))
        self.assertEqual(self.types(cleared), ["type=stalled_cleared"])

        reopened = self.poll(NOW + timedelta(days=3))
        self.assertEqual(self.types(reopened), ["type=stalled"])
        self.assertIn('assignee="Bob" idle=3d', reopened[0])

    def test_complete_history_not_the_latest_fifteen_supplies_the_status_clock(self):
        issue = jira("ACME-703", "Review", .1, "Bob", created_days_ago=30)
        self.jira["Review"] = [issue]
        churn = [jira_change("labels" if n % 2 else "summary", 6 - n / 10.0,
                             "before", "after", change_id="churn-%d" % n)
                 for n in range(20)]
        self.history(issue, jira_change("status", 12, "In Progress", "Review"), *churn)

        lines = self.poll()

        self.assertEqual(len(lines), 1, lines)
        self.assertIn('issue=ACME-703 "Review" assignee="Bob" idle=12d', lines[0])

    def test_complete_history_without_a_transition_falls_back_to_creation(self):
        old = jira("ACME-704", "Review", .25, "Bob", created_days_ago=4)
        boundary = jira("ACME-705", "Review", .25, "Bob", created_days_ago=3)
        self.jira["Review"] = [old, boundary]
        self.history(old, jira_change("labels", 1, None, "triaged"))
        self.history(boundary, jira_change("summary", 1, "old", "new"))

        lines = sorted(self.poll())

        self.assertEqual(len(lines), 2, lines)
        self.assertIn("issue=ACME-704", lines[0])
        self.assertIn("idle=4d", lines[0])
        self.assertIn("issue=ACME-705", lines[1])
        self.assertIn("idle=3d", lines[1])

    def test_missing_history_marks_the_jira_inventory_incomplete_and_records_a_hole(self):
        issue = jira("ACME-706", "Review", 8, "Bob", created_days_ago=20)
        self.jira["Review"] = [issue]

        with mock.patch.object(sw, "jira_changelogs", return_value={}):
            items, complete, holes = self.jira_result()

        self.assertEqual(items, {})
        self.assertFalse(complete)
        self.assertIn("jira", holes.failures)
        self.assertIn("jira issue ACME-706 has no complete changelog", holes.failures["jira"])

    def test_malformed_history_entry_marks_the_jira_inventory_incomplete_and_records_a_hole(self):
        issue = jira("ACME-707", "Review", 8, "Bob", created_days_ago=20)
        self.jira["Review"] = [issue]
        self.history(issue, {"id": "broken", "created": _jira_ts(8), "items": None})

        items, complete, holes = self.jira_result()

        self.assertEqual(items, {})
        self.assertFalse(complete)
        self.assertIn("jira", holes.failures)
        self.assertIn("jira issue ACME-707 has a malformed changelog entry", holes.failures["jira"])

    def test_status_snapshot_mismatch_marks_the_jira_inventory_incomplete_and_records_a_hole(self):
        issue = jira("ACME-708", "Review", 8, "Bob", created_days_ago=20)
        self.jira["Review"] = [issue]
        self.history(issue, jira_change("status", 8, "To Do", "In Progress"))

        items, complete, holes = self.jira_result()

        self.assertEqual(items, {})
        self.assertFalse(complete)
        self.assertIn("jira", holes.failures)
        self.assertIn("jira issue ACME-708 changed while its inventory was being read",
                      holes.failures["jira"])

    def test_invalid_semantic_timestamp_marks_the_jira_inventory_incomplete_and_records_a_hole(self):
        issue = jira("ACME-709", "Review", 8, "Bob", created_days_ago=20)
        self.jira["Review"] = [issue]
        change = jira_change("status", 8, "In Progress", "Review")
        change["created"] = "not-a-timestamp"
        self.history(issue, change)

        items, complete, holes = self.jira_result()

        self.assertEqual(items, {})
        self.assertFalse(complete)
        self.assertIn("jira", holes.failures)
        self.assertIn("jira issue ACME-709 has an invalid semantic-change timestamp",
                      holes.failures["jira"])

    def test_recent_semantic_child_activity_suppresses_an_epic_stall(self):
        epic = jira("ACME-727", "In Progress", 8, issue_type="Epic", created_days_ago=30,
                    hierarchy_level=1)
        child = jira("ACME-731", "Done", 1, "Bob", parent=epic, created_days_ago=20)
        self.jira["In Progress"] = [epic]
        self.jira_children = [child]
        self.history(epic, jira_change("status", 8, "To Do", "In Progress"))
        self.history(child, jira_change("status", 1, "In Progress", "Done"))

        lines = self.poll()

        self.assertEqual(lines, [])
        self.assertTrue(any("parent in" in q.lower() and "ACME-727" in q
                            for q in self.jira_searches), self.jira_searches)

    def test_stale_child_activity_allows_an_epic_stall_and_sets_its_idle_age(self):
        epic = jira("ACME-727", "In Progress", 9, issue_type="Epic", created_days_ago=30,
                    hierarchy_level=1)
        child1 = jira("ACME-731", "In Progress", 5, "Bob", parent=epic,
                      created_days_ago=20)
        child2 = jira("ACME-732", "Done", 7, "Bob", parent=epic, created_days_ago=20)
        self.jira["In Progress"] = [epic]
        self.jira_children = [child1, child2]
        self.history(epic, jira_change("status", 9, "To Do", "In Progress"))
        self.history(child1, jira_change("status", 5, "To Do", "In Progress"))
        self.history(child2, jira_change("status", 7, "In Progress", "Done"))

        lines = self.poll()

        self.assertEqual(len(lines), 1, lines)
        self.assertIn('issue=ACME-727 "In Progress" unassigned idle=5d', lines[0])

    def test_unknown_epic_children_mark_jira_incomplete_record_a_hole_and_suppress_the_epic(self):
        epic = jira("ACME-727", "In Progress", 9, issue_type="Epic", created_days_ago=30,
                    hierarchy_level=1)
        other_epic = jira("ACME-999", "In Progress", 9, issue_type="Epic",
                          created_days_ago=30, hierarchy_level=1)
        child = jira("ACME-731", "Done", 7, "Bob", parent=other_epic,
                     created_days_ago=20)
        self.jira["In Progress"] = [epic]
        self.jira_children = [child]
        self.history(epic, jira_change("status", 9, "To Do", "In Progress"))
        self.history(child, jira_change("status", 7, "In Progress", "Done"))

        items, complete, holes = self.jira_result()

        self.assertNotIn("jira:ACME-727", items)
        self.assertFalse(complete)
        self.assertIn("jira", holes.failures)
        self.assertIn("jira child ACME-731 has no matching Epic parent", holes.failures["jira"])

    def test_jira_uses_one_candidate_query_one_child_query_and_one_history_batch(self):
        epic = jira("ACME-727", "In Progress", 9, issue_type="Epic", created_days_ago=30,
                    hierarchy_level=1)
        child = jira("ACME-731", "Done", 5, "Bob", parent=epic, created_days_ago=20)
        self.jira["In Progress"] = [epic]
        self.jira_children = [child]
        self.history(epic, jira_change("status", 9, "To Do", "In Progress"))
        self.history(child, jira_change("status", 5, "In Progress", "Done"))

        self.poll()

        self.assertEqual(sw.JIRA_FIELDS, "summary,created,status,assignee,issuetype,parent")
        self.assertEqual(self.jira_searches, [
            'project in (TD) AND status in (Review, "In Progress") ORDER BY key',
            "parent in (ACME-727) ORDER BY key",
        ])
        self.assertEqual(self.jira_changelog_batches, [[epic["id"], child["id"]]])

    def test_an_incomplete_changelog_is_a_hole_not_an_add_or_clear(self):
        tracked = jira("ACME-843", "Review", 8, "Bob", created_days_ago=20)
        unknown = jira("ACME-844", "Review", 8, "Bob", created_days_ago=20)
        self.jira["Review"] = [tracked]
        self.history(tracked, jira_change("status", 8, "In Progress", "Review"))
        self.assertEqual(self.types(self.poll()), ["type=stalled"])
        self.jira["Review"] = [tracked, unknown]
        self.jira_histories = sw.Truncated("bulk changelog page cap")

        lines = self.poll(NOW + timedelta(hours=1))

        text = "\n".join(lines)
        self.assertNotIn("type=stalled_cleared key=jira:ACME-843", text)
        self.assertNotIn("issue=ACME-844", text, "unknown history is not positive stall evidence")
        self.assertTrue(any(line.startswith("type=degraded sub=jira") for line in lines), lines)
        self.assertIn("jira:ACME-843", self.state["stalled"])

    def test_an_incomplete_epic_child_inventory_holds_jira_but_keeps_positive_findings(self):
        epic = jira("ACME-727", "In Progress", 9, issue_type="Epic", created_days_ago=30,
                    hierarchy_level=1)
        task = jira("ACME-700", "Review", 6, "Bob", created_days_ago=20)
        self.jira["Review"] = [task]
        self.jira["In Progress"] = [epic]
        self.history(epic, jira_change("status", 9, "To Do", "In Progress"))
        self.history(task, jira_change("status", 6, "In Progress", "Review"))
        self.state["stalled"]["jira:ACME-727"] = {
            "first": "2026-09-01T00:00:00Z", "last_emit": NOW.timestamp()}
        self.jira_children = sw.Truncated("child inventory page cap")

        lines = self.poll(NOW + timedelta(hours=1))

        text = "\n".join(lines)
        self.assertIn("issue=ACME-700", text, "positive evidence from the same cycle still emits")
        self.assertNotIn("type=stalled_cleared key=jira:ACME-727", text)
        self.assertTrue(any(line.startswith("type=degraded sub=jira") for line in lines), lines)
        self.assertIn("jira:ACME-727", self.state["stalled"])


class TestTransitions(Base):
    def test_an_empty_bound_sha_is_a_loud_hole_and_cannot_clear_a_stall(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.poll()

        unreadable = gh(547, "APPROVED", 5)
        unreadable["reviews"][0]["commit"]["oid"] = ""
        self.gh[GH_REPO] = [unreadable]
        lines = self.poll(NOW + timedelta(hours=1))

        self.assertFalse(any(line.startswith("type=stalled_cleared") for line in lines))
        self.assertTrue(any(line.startswith("type=degraded sub=github") for line in lines))
        self.assertIn("pr:%s#547" % GH_REPO, self.state["stalled"])
        self.assertTrue(any("empty commit.oid" in line for line in self.notes))

    def test_no_repeat_inside_the_window_and_still_after_it(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.assertEqual(self.types(self.poll()), ["type=stalled"])
        self.assertEqual(self.poll(NOW + timedelta(hours=1)), [], "same set, same day: silence")
        lines = self.poll(NOW + timedelta(hours=25))
        self.assertEqual(self.types(lines), ["type=stalled_still"])
        self.assertIn("idle=6d", lines[0])
        self.assertEqual(self.poll(NOW + timedelta(hours=26)), [], "re-emitted once, then quiet again")

    def test_cleared_when_gone_from_a_source_that_answered(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.poll()
        self.gh[GH_REPO] = []                                  # merged, or someone acted
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"])
        self.assertIn("key=pr:%s#547" % GH_REPO, lines[0])
        self.assertEqual(self.state["stalled"], {})

    def test_a_source_that_did_not_answer_neither_clears_nor_adds(self):
        """The q1 lesson in code: a broken query must never read as 'nothing stalled'."""
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.jira["Review"] = [jira("ACME-843", "Review", 11, "Bob Reviewer")]
        self.poll()
        self.gh[GH_REPO] = RuntimeError("gh pr list failed: HTTP 502")
        self.jira["Review"] = []                               # the ticket really moved
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertNotIn("type=stalled_cleared key=pr:", "\n".join(lines), "no data is not no stall")
        self.assertIn("pr:%s#547" % GH_REPO, self.state["stalled"], "kept until a cycle that answers decides")
        self.assertTrue(any(line.startswith("type=stalled_cleared key=jira:ACME-843") for line in lines),
                        "the source that DID answer still clears")
        self.assertTrue(any(line.startswith("type=degraded sub=github") for line in lines),
                        "and the hole is said out loud")
        self.gh[GH_REPO] = []                                  # the fetch recovers, the PR is gone
        lines = self.poll(NOW + timedelta(hours=2))
        self.assertTrue(any(line.startswith("type=stalled_cleared key=pr:") for line in lines))

    def test_state_survives_a_restart_without_re_announcing(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.poll()
        self.state = sw.load_state("acme")               # a new process
        self.assertEqual(self.poll(NOW + timedelta(hours=1)), [])


class TestReportAndFleet(Base):
    def test_report_prints_the_set_and_the_holes_and_touches_nothing(self):
        self.gh[GH_REPO] = [gh(547, "CHANGES_REQUESTED", 5)]
        self.jira["Review"] = RuntimeError("HTTP 500")
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w") as fh:
            json.dump({"profiles": {"acme": dict(PCFG, stalled=CFG)}}, fh)
        out = io.StringIO()
        with mock.patch.object(sw, "CONFIG_PATH", cfg_path), redirect_stdout(out):
            rc = sw.report("acme")
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("kind=pr repo=app #547", text)
        self.assertIn("jira did NOT answer", text)
        self.assertIn("sources answered: bitbucket, github", text)
        self.assertEqual(self.emitted, [], "a report emits nothing")
        self.assertFalse(os.path.exists(os.path.join(sw.STATE_DIR, "acme.json")), "and saves nothing")

    def test_state_carries_the_build_marker(self):
        self.poll()
        with open(os.path.join(sw.STATE_DIR, "acme.json")) as fh:
            self.assertEqual(json.load(fh)["build"], sw.BUILD)

    def test_the_watchdog_and_the_fleet_know_the_source(self):
        self.assertEqual(watchdog_wake.SOURCES["stalled"], ("stalled", "days", None))
        self.assertIn("stalled", watchdog_wake.FLEET)


class TestInventoriesAreCompleteOrHoles(Base):
    """codex on this branch: a page that is exactly full may be truncated, and a
    truncated inventory read as 'answered' clears a tracked stall that merely fell
    off the page; a missing CLI parked the daemon (gh) or skipped a tier silently (bkt)."""

    def test_a_truncated_github_inventory_is_a_hole_not_a_clear(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.poll()
        self.gh[GH_REPO] = sw.Truncated("gh pr list returned 500 — truncated")
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertNotIn("stalled_cleared", "\n".join(lines))
        self.assertTrue(any(line.startswith("type=degraded sub=github") for line in lines))
        self.assertIn("pr:%s#547" % GH_REPO, self.state["stalled"])

    # ----------------------------------------------------------------
    # The GitHub leg went dark because `gh pr list --json commits`
    # implicitly expands each commit's `authors(first:100)` connection, so
    # GitHub budgets from the REQUESTED LIMIT and rejects the call before it
    # ever looks at the data. Two properties are pinned below: the query never
    # opens that connection, and the declared widths stay far under the
    # ceiling. Both are checked against the query text rather than the network,
    # because the cost is structural in the query and the failure was invisible
    # to every caller under limit 50 — including the review that shipped it.
    # ----------------------------------------------------------------

    def _real_gh_prs(self, pages):
        """Run the REAL gh_prs over a fake `gh api graphql` returning ``pages``."""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            page = pages[min(len(calls) - 1, len(pages) - 1)]
            if isinstance(page, Exception):
                raise page
            return mock.Mock(returncode=page[0], stdout=page[1], stderr=page[2])

        self.p_gh_prs.stop()
        try:
            with mock.patch.object(sw, "GH", "/usr/bin/gh"), \
                 mock.patch.object(sw.subprocess, "run", fake_run):
                return sw.gh_prs(GH_REPO), calls
        finally:
            self.p_gh_prs.start()

    def test_gh_pr_query_never_opens_the_commit_authors_connection(self):
        """The regression itself. `authors` is what blew the node ceiling."""
        self.assertNotIn("authors", sw.GH_PR_QUERY)
        self.assertNotIn("latestReviews", sw.GH_PR_QUERY)
        self.assertIn("reviews(last:", sw.GH_PR_QUERY)
        self.assertIn("headRefOid", sw.GH_PR_QUERY)
        # The commits connection must be BOUNDED and must not nest anything.
        # ⚠ Deliberately NOT asserting depth == 1: raising the depth does not
        # re-open the budget (measured — `commits(last:100)` without `authors`
        # returns data at page size 100), so pinning 1 here would defend a false
        # mechanism and block the legitimate remedy if a head ever fails to be
        # the last node. What must not come back is the nested connection.
        self.assertRegex(sw.GH_PR_QUERY, r"commits\(last:\d+\)")
        self.assertGreaterEqual(sw.GH_COMMIT_DEPTH, 1)

    def test_gh_pr_query_declared_widths_stay_far_under_the_node_ceiling(self):
        """Every connection in the query is explicitly bounded, and the product
        of those bounds — our own conservative model, not GitHub's published
        formula — leaves room rather than sitting on the cliff. The rejected
        alternative was capping the limit at 49, which passes this arithmetic
        with 0.01 to spare and re-breaks on the next field.

        ⚠ Measured limit of THIS test: it does not model nesting, so it is not
        the guard. Re-introducing `authors(first:100)` inside `commits`
        and raising the depth to 100 reproduces the live failure exactly and
        this test still passes; the sibling test above is what fails. Kept
        because it pins the declared widths against drift, not because it
        would have caught the regression.
        """
        ceiling = 500_000
        budget = sw.GH_PR_PAGE * (1 + sw.GH_REVIEW_DEPTH + sw.GH_COMMIT_DEPTH)
        self.assertLess(budget, ceiling // 10, "query budget has lost its headroom")
        # and the shape that broke it would not have passed this test
        broken = sw.GH_PR_PAGE * 100 * 100
        self.assertGreater(broken, ceiling)

    def test_gh_prs_flattens_graphql_into_the_shape_the_classifier_reads(self):
        """`gh pr list --json` flattened both connections for us; asking GraphQL
        directly means doing it here, and getting it wrong would turn every PR
        into a binding-unreadable hole rather than a visible error."""
        prs, _ = self._real_gh_prs([gql_page([gh(547, "APPROVED", 5)])])
        self.assertEqual(len(prs), 1)
        binding = sw._review_binding.github_review_binding(prs[0])
        self.assertEqual(binding["status"], sw._review_binding.BOUND_AT_HEAD)
        self.assertEqual(prs[0]["author"], {"login": "bob"})

    def test_gh_pr_query_asks_for_is_draft(self):
        """IsDraft is the only field that tells a draft from a ready PR.
        mergeable, mergeStateStatus and reviewDecision are identical on both."""
        self.assertRegex(sw.GH_PR_QUERY, r"\bisDraft\b")

    def test_gh_prs_passes_is_draft_through_the_flattening(self):
        prs, _ = self._real_gh_prs([gql_page([gh(591, "APPROVED", 7, draft=True),
                                              gh(547, "APPROVED", 7)])])
        self.assertIs(prs[0]["isDraft"], True)
        self.assertIs(prs[1]["isDraft"], False)

    def test_gh_prs_pages_the_connection_and_forwards_the_cursor(self):
        prs, calls = self._real_gh_prs([
            gql_page([gh(1, "APPROVED", 5)], has_next=True, cursor="CUR1"),
            gql_page([gh(2, "APPROVED", 6)]),
        ])
        self.assertEqual([pr["number"] for pr in prs], [1, 2])
        self.assertNotIn("cursor=CUR1", calls[0])
        self.assertIn("cursor=CUR1", calls[1])

    def test_gh_prs_raises_truncated_on_a_full_inventory(self):
        """Unchanged contract: filling GH_LIMIT is a hole, not an answer."""
        full = [gh(n, "APPROVED", 5) for n in range(sw.GH_LIMIT)]
        with self.assertRaises(sw.Truncated):
            self._real_gh_prs([gql_page(full, has_next=True, cursor="C")])

    def test_gh_prs_raises_truncated_when_pagination_makes_no_progress(self):
        with self.assertRaises(sw.Truncated):
            self._real_gh_prs([gql_page([gh(1, "APPROVED", 5)], has_next=True, cursor="SAME")])

    def test_gh_prs_raises_truncated_when_more_pages_are_advertised_with_no_cursor(self):
        with self.assertRaises(sw.Truncated):
            self._real_gh_prs([gql_page([gh(1, "APPROVED", 5)], has_next=True, cursor=None)])

    def test_gh_prs_raises_on_a_nonzero_exit_which_is_what_production_does(self):
        """⚠ THIS is the branch the live outage took. `gh api graphql` EXITS 1
        on a node-limit rejection — measured, not assumed — so the node-ceiling
        failure reaches the `returncode != 0` arm, and that arm formats the
        detail an operator sees in the recorded hole. It had no coverage."""
        stderr = ("gh: By the time this query traverses to the authors connection, "
                  "it is requesting up to 1,000,000 possible nodes")
        with self.assertRaises(RuntimeError) as caught:
            self._real_gh_prs([(1, "", stderr)])
        self.assertIn("authors connection", str(caught.exception))

    def test_gh_prs_raises_on_a_graphql_error_payload_that_exited_zero(self):
        """The other arm: a 200 carrying an `errors` array. Not the shape the
        outage took (see the sibling above), but a source that answers with an
        error must never read as an empty inventory either."""
        payload = json.dumps({"errors": [{"type": "MAX_NODE_LIMIT_EXCEEDED",
                                          "message": "requesting up to 1,000,000 possible nodes"}]})
        with self.assertRaises(RuntimeError):
            self._real_gh_prs([(0, payload, "")])

    def test_gh_prs_does_not_silently_drop_a_malformed_review(self):
        """⚠ THE ONE WRONG-ANSWER PATH, and it is asymmetric with `commits`.

        Dropping a malformed `commits` entry is fail-closed — nothing then
        matches `headRefOid`, so the classifier reports a hole. Dropping a
        malformed `reviews` entry is NOT: if the newest entry carrying the
        standing verdict is the one dropped and an older one survives,
        `matching` is non-empty and the classifier binds the OLDER review —
        a confident `bound-at-head` naming the wrong reviewer and the wrong
        submittedAt. A partial GitHub response (a null in `nodes`) is exactly
        how that arrives, so the flattener must pass entries through and let
        `_review_binding`'s own malformed-history guard fire.
        """
        page = gql_page([gh(1, "APPROVED", 5)])
        body = json.loads(page[1])
        body["data"]["repository"]["pullRequests"]["nodes"][0]["reviews"]["nodes"].insert(0, None)
        prs, _ = self._real_gh_prs([(0, json.dumps(body), "")])
        self.assertIn(None, prs[0]["reviews"], "a malformed review must reach the classifier")
        binding = sw._review_binding.github_review_binding(prs[0])
        self.assertEqual(binding["status"], sw._review_binding.BINDING_UNREADABLE)
        self.assertIn("malformed", binding["reason"])

    def test_gh_prs_passes_string_variables_with_lowercase_f(self):
        """⚠ `-F` type-converts. An owner or repo named `2024`, `true`, `false`
        or `null` — all legal on GitHub — would be sent as a JSON number/boolean
        /null against a `String!` variable and rejected every cycle, taking the
        leg dark: the exact symptom this fix removes. Measured against live
        GitHub: `-F owner=2024` -> "Could not coerce value 2024 to String";
        `-f owner=2024` resolves as a string."""
        _, calls = self._real_gh_prs([
            gql_page([gh(1, "APPROVED", 5)], has_next=True, cursor="CUR1"),
            gql_page([gh(2, "APPROVED", 6)]),
        ])
        for argv in calls:
            self.assertNotIn("-F", argv, "string variables must use -f, never -F")
            for var in ("owner=", "repo="):
                self.assertEqual("-f", argv[argv.index(next(a for a in argv if a.startswith(var))) - 1])
        self.assertIn("-f", calls[1])
        self.assertIn("cursor=CUR1", calls[1])

    def test_gh_prs_raises_truncated_on_a_malformed_or_headless_page(self):
        """Each of these is a source that did not answer. None may read as an
        empty inventory, which would clear every tracked GitHub stall."""
        for label, connection in (
            ("nodes is not a list", {"nodes": "nope"}),
            ("a node is not an object", {"nodes": ["nope"]}),
            ("no pageInfo", {"nodes": []}),
            ("no connection", None),
        ):
            with self.subTest(label):
                body = {"data": {"repository": {"pullRequests": connection}}}
                with self.assertRaises(sw.Truncated):
                    self._real_gh_prs([(0, json.dumps(body), "")])

    def test_bitbucket_pages_are_walked_to_the_end(self):
        pages = {1: [bb(i, True, 10) for i in range(sw.BB_PAGELEN)], 2: [bb(99, True, 10)]}
        calls = []

        def fake_api(path, cwd):
            page = int(path.split("page=")[1].split("&")[0])
            calls.append(page)
            values = pages.get(page, [])
            return {"values": values, "next": "…" if page == 1 else None}
        with mock.patch.object(bitbucket_wake, "bkt_api", fake_api):
            self.p_bb_prs.stop()                            # the real bb_prs over the fake API
            try:
                got = sw.bb_prs(BB_REPO, "~")
            finally:
                self.p_bb_prs.start()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(got), sw.BB_PAGELEN + 1)

    def test_a_short_non_final_bitbucket_page_is_followed(self):
        """codex pass 2: fewer than pagelen values with a `next` link is still a
        continuation; stopping there read a partial inventory as complete."""
        pages = {1: [bb(1, True, 10), bb(2, True, 10)], 2: [bb(3, True, 10)]}
        calls = []

        def fake_api(path, cwd):
            page = int(path.split("page=")[1].split("&")[0])
            calls.append(page)
            return {"values": pages.get(page, []), "next": "…" if page == 1 else None}
        with mock.patch.object(bitbucket_wake, "bkt_api", fake_api):
            self.p_bb_prs.stop()
            try:
                got = sw.bb_prs(BB_REPO, "~")
            finally:
                self.p_bb_prs.start()
        self.assertEqual(calls, [1, 2])
        self.assertEqual([p["id"] for p in got], [1, 2, 3])

    def test_a_placeholder_jira_token_is_waiting_not_a_hole_and_never_a_recovery(self):
        """codex pass 2: projects configured and no usable token skipped the tier
        silently, and a prior Jira failure then read as recovered with no query run.
        A placeholder is a tier WAITING on config — a fresh
        install must not escalate to `error:` fifteen minutes in. So: not answered,
        not a hole, the open episode held as-is."""
        self.jira["Review"] = RuntimeError("HTTP 500")
        lines = self.poll()
        self.assertTrue(any(line.startswith("type=degraded sub=jira") for line in lines))
        parked = dict(PCFG, jira=dict(PCFG["jira"], api_token="PASTE-TOKEN-HERE"))
        for hours in (1, 2, 3, 26, 50):                       # five cycles, well past promotion
            self.emitted.clear()
            sw.poll_profile("acme", parked, CFG, self.state, now=NOW + timedelta(hours=hours))
            self.assertEqual([l for l in self.emitted if "jira" in l], [],
                             "a waiting tier is silent on the profile log: no degraded, no error, no recovery")
        self.assertIn("jira", self.state["degraded"], "the earlier episode is held, not recovered")
        self.assertTrue(self.state["degraded"]["jira"]["open"])
        self.assertEqual(self.state["waiting"], {"jira": "api_token is a placeholder"})
        self.emitted.clear()                                  # the token arrives, the API is fine
        self.jira["Review"] = []
        sw.poll_profile("acme", PCFG, CFG, self.state, now=NOW + timedelta(hours=51))
        self.assertTrue(any(line.startswith("type=degraded_recovered sub=jira") for line in self.emitted),
                        "recovery is announced by the first cycle that actually ran the query")
        self.assertEqual(self.state["waiting"], {})

    def test_report_names_a_waiting_tier(self):
        parked = dict(PCFG, jira=dict(PCFG["jira"], api_token="PASTE-TOKEN-HERE"))
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w") as fh:
            json.dump({"profiles": {"acme": dict(parked, stalled=CFG)}}, fh)
        out = io.StringIO()
        with mock.patch.object(sw, "CONFIG_PATH", cfg_path), redirect_stdout(out):
            self.assertEqual(sw.report("acme"), 0)
        self.assertIn("jira NOT checked — api_token is a placeholder", out.getvalue())
        self.assertIn("sources answered: bitbucket, github", out.getvalue())

    def test_an_unknown_profile_fails_loudly(self):
        """A typo'd profile became an empty one and printed
        '(nothing stalled …)' with exit 0 — the q1 failure mode on the CLI itself."""
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w") as fh:
            json.dump({"profiles": {"acme": dict(PCFG, stalled=CFG)}}, fh)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sw, "CONFIG_PATH", cfg_path), redirect_stdout(out), \
             mock.patch("sys.stderr", err):
            self.assertEqual(sw.report("acmee"), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("unknown profile 'acmee'", err.getvalue())
        self.assertIn("acme", err.getvalue())

    def _real_bb_prs(self, fake_api, poll=False):
        """The real bb_prs (or a whole poll) over a fake Bitbucket API."""
        with mock.patch.object(bitbucket_wake, "bkt_api", fake_api):
            self.p_bb_prs.stop()
            try:
                return self.poll() if poll else sw.bb_prs(BB_REPO, "~")
            finally:
                self.p_bb_prs.start()

    @staticmethod
    def _bb_pages(pages, calls, **extra):
        """A fake API serving ``pages`` ({number: (values, next)}); a page not listed
        is empty with no link. ``calls`` collects (page number, request path)."""
        def fake_api(path, cwd):
            page = int(path.split("page=")[1].split("&")[0])
            calls.append((page, path))
            values, nxt = pages.get(page, ([], None))
            resp = {"values": values, "next": nxt}
            resp.update(extra)
            return resp
        return fake_api

    def test_a_full_bitbucket_page_with_no_next_link_and_a_non_empty_page_after_is_a_hole(self):
        """`next` surviving the fields= filter is documented, not
        verified; a full page without it must be loud, never a complete inventory.
        narrowed the rule: the page after a full page with no link is read,
        and only a value on it (the link was lost, the inventory has a hole) is the hole."""
        pages = {1: ([bb(i, True, 10) for i in range(sw.BB_PAGELEN)], None),
                 2: ([bb(900, True, 10)], None)}
        with self.assertRaises(sw.Truncated):
            self._real_bb_prs(self._bb_pages(pages, []))

    def test_exactly_one_full_bitbucket_page_then_an_empty_one_is_the_whole_inventory(self):
        calls = []
        pages = {1: ([bb(i, True, 10) for i in range(sw.BB_PAGELEN)], None)}
        got = self._real_bb_prs(self._bb_pages(pages, calls, size=sw.BB_PAGELEN))
        self.assertEqual(len(got), sw.BB_PAGELEN)
        self.assertEqual([c[0] for c in calls], [1, 2])
        for _page, path in calls:
            self.assertRegex(path, r"fields=[^&]*,size\b", "the request asks for `size`")

    def test_a_tracked_stall_is_kept_when_the_repo_has_exactly_one_full_page(self):
        stalled = [bb(72, True, 12)] + [bb(100 + i, False, 40) for i in range(sw.BB_PAGELEN - 1)]
        fake = self._bb_pages({1: (stalled, None)}, [], size=sw.BB_PAGELEN)
        lines = self._real_bb_prs(fake, poll=True)
        self.assertTrue(any("kind=bb repo=service #72" in line for line in lines))
        self.assertFalse(any(line.startswith("type=degraded sub=bitbucket") for line in lines))
        self.emitted.clear()
        lines = self._real_bb_prs(fake, poll=True)
        self.assertEqual([l for l in lines if "bitbucket" in l and l.startswith("type=degraded")], [])
        self.assertNotIn("stalled_cleared", "\n".join(lines))
        self.assertTrue(self.state["stalled"], "the tracked stall is still tracked")

    def test_a_non_empty_page_after_a_full_one_with_no_link_marks_the_bitbucket_leg_a_hole(self):
        pages = {1: ([bb(72, True, 12)] + [bb(100 + i, False, 40) for i in range(sw.BB_PAGELEN - 1)], None),
                 2: ([bb(900, True, 10)], None)}
        lines = self._real_bb_prs(self._bb_pages(pages, []), poll=True)
        self.assertTrue(any(line.startswith("type=degraded sub=bitbucket") for line in lines))

    def test_a_failed_confirming_fetch_is_a_hole(self):
        def fake_api(path, cwd):
            if "page=2" in path:
                raise RuntimeError("HTTP 502")
            return {"values": [bb(i, True, 10) for i in range(sw.BB_PAGELEN)]}
        with self.assertRaises(sw.Truncated):
            self._real_bb_prs(fake_api)

    def test_a_confirming_page_that_is_empty_but_links_on_is_a_hole(self):
        pages = {1: ([bb(i, True, 10) for i in range(sw.BB_PAGELEN)], None), 2: ([], "…")}
        with self.assertRaises(sw.Truncated):
            self._real_bb_prs(self._bb_pages(pages, []))

    def test_a_bitbucket_size_larger_than_what_was_read_is_a_hole(self):
        pages = {1: ([bb(1, True, 10), bb(2, True, 10)], None)}
        with self.assertRaises(sw.Truncated):
            self._real_bb_prs(self._bb_pages(pages, [], size=7))
        got = self._real_bb_prs(self._bb_pages(pages, [], size=2))
        self.assertEqual(len(got), 2)
        full = {1: ([bb(i, True, 10) for i in range(sw.BB_PAGELEN)], None)}
        with self.assertRaises(sw.Truncated):
            self._real_bb_prs(self._bb_pages(full, [], size=sw.BB_PAGELEN + 3))

    def test_the_confirming_fetch_is_made_only_for_a_full_page_with_no_next_link(self):
        full = [bb(i, True, 10) for i in range(sw.BB_PAGELEN)]
        for label, pages, expect in (
            ("a short page with no link", {1: ([bb(1, True, 10)], None)}, [1]),
            ("no PRs at all", {}, [1]),
            ("a full page with a link, then a short one",
             {1: (full, "…"), 2: ([bb(900, True, 10)], None)}, [1, 2]),
            ("a full page with a link, then a full one with none, then an empty one",
             {1: (full, "…"), 2: (full, None)}, [1, 2, 3]),
        ):
            with self.subTest(label):
                calls = []
                self._real_bb_prs(self._bb_pages(pages, calls))
                self.assertEqual([c[0] for c in calls], expect)

    def test_bitbucket_page_cap_is_a_hole(self):
        def endless(path, cwd):
            return {"values": [bb(1, True, 10)] * sw.BB_PAGELEN, "next": "…"}
        with mock.patch.object(bitbucket_wake, "bkt_api", endless):
            self.p_bb_prs.stop()
            try:
                with self.assertRaises(sw.Truncated):
                    sw.bb_prs(BB_REPO, "~")
            finally:
                self.p_bb_prs.start()

    def test_jira_pages_are_walked_by_next_page_token(self):
        class FakeJira:
            search_legacy = False

            def __init__(self, cfg):
                self.calls = []

            def search(self, jql, fields, max_results):
                return {"issues": [jira("ACME-1", "Review", 5)], "nextPageToken": "t2"}

            def get(self, path, params=None):
                self.calls.append(params.get("nextPageToken"))
                return {"issues": [jira("ACME-2", "Review", 6)]}      # last page: no token
        with mock.patch.object(sw.jira_wake, "Jira", FakeJira):
            self.p_jira.stop()
            try:
                got = sw.jira_search(PCFG["jira"], "x")
            finally:
                self.p_jira.start()
        self.assertEqual([i["key"] for i in got], ["ACME-1", "ACME-2"])

    def test_a_non_final_jira_page_without_a_token_is_a_hole(self):
        class FakeJira:
            search_legacy = False

            def __init__(self, cfg):
                pass

            def search(self, jql, fields, max_results):
                return {"issues": [jira("ACME-1", "Review", 5)], "isLast": False}

        with mock.patch.object(sw.jira_wake, "Jira", FakeJira):
            self.p_jira.stop()
            try:
                with self.assertRaises(sw.Truncated):
                    sw.jira_search(PCFG["jira"], "x")
            finally:
                self.p_jira.start()

    def test_a_jira_search_warning_is_a_hole_not_a_complete_inventory(self):
        class FakeJira:
            search_legacy = False

            def __init__(self, cfg):
                pass

            def search(self, jql, fields, max_results):
                return {"issues": [jira("ACME-1", "Review", 5)],
                        "isLast": True,
                        "warnings": ["results may be truncated"]}

        with mock.patch.object(sw.jira_wake, "Jira", FakeJira):
            self.p_jira.stop()
            try:
                with self.assertRaises(sw.Truncated):
                    sw.jira_search(PCFG["jira"], "x")
            finally:
                self.p_jira.start()

    def test_jira_legacy_pages_are_walked_by_start_at(self):
        class FakeLegacy:
            search_legacy = True

            def __init__(self, cfg):
                pass

            def search(self, jql, fields, max_results):
                return {"issues": [jira("ACME-1", "Review", 5)], "startAt": 0, "total": 2}

            def get(self, path, params=None):
                assert params["startAt"] == 1, params
                return {"issues": [jira("ACME-2", "Review", 6)], "startAt": 1, "total": 2}
        with mock.patch.object(sw.jira_wake, "Jira", FakeLegacy):
            self.p_jira.stop()
            try:
                got = sw.jira_search(PCFG["jira"], "x")
            finally:
                self.p_jira.start()
        self.assertEqual([i["key"] for i in got], ["ACME-1", "ACME-2"])

    def test_bulk_jira_changelogs_are_posted_paged_and_grouped_by_numeric_id(self):
        one = jira("ACME-1", "Review", 5, issue_id="10100")
        two = jira("ACME-2", "In Progress", 6, issue_id="10101")
        no_history = jira("ACME-3", "Review", 7, issue_id="10102")
        first = jira_change("status", 8, "In Progress", "Review", numeric=True,
                            change_id="h1")
        second = jira_change("assignee", 4, None, "Bob", numeric=True,
                             change_id="h2")
        third = jira_change("status", 6, "To Do", "In Progress", numeric=True,
                            change_id="h3")
        calls = []
        responses = [
            {"issueChangeLogs": [{"issueId": "10100", "changeHistories": [first]}],
             "nextPageToken": "page-2"},
            {"issueChangeLogs": [
                {"issueId": "10100", "changeHistories": [second]},
                {"issueId": "10101", "changeHistories": [third]},
            ]},
        ]

        class FakeJira:
            def __init__(self, cfg):
                pass

            def post(self, path, body):
                calls.append((path, dict(body)))
                return responses.pop(0)

        self.p_jira_changelogs.stop()
        try:
            with mock.patch.object(sw.jira_wake, "Jira", FakeJira):
                got = sw.jira_changelogs(PCFG["jira"], [two, no_history, one])
        finally:
            self.p_jira_changelogs.start()

        self.assertEqual(got, {"10100": [first, second], "10101": [third], "10102": []})
        base = {"issueIdsOrKeys": ["10100", "10101", "10102"],
                "fieldIds": ["status", "assignee"], "maxResults": 1000}
        self.assertEqual(calls, [
            ("/rest/api/3/changelog/bulkfetch", base),
            ("/rest/api/3/changelog/bulkfetch", dict(base, nextPageToken="page-2")),
        ])

    def test_a_repeated_bulk_changelog_token_is_a_hole(self):
        issue = jira("ACME-1", "Review", 5, issue_id="10100")

        class FakeJira:
            def __init__(self, cfg):
                pass

            def post(self, path, body):
                return {"issueChangeLogs": [], "nextPageToken": "same"}

        self.p_jira_changelogs.stop()
        try:
            with mock.patch.object(sw.jira_wake, "Jira", FakeJira):
                with self.assertRaises(sw.Truncated):
                    sw.jira_changelogs(PCFG["jira"], [issue])
        finally:
            self.p_jira_changelogs.start()

    def test_the_bulk_changelog_page_cap_is_a_hole(self):
        issue = jira("ACME-1", "Review", 5, issue_id="10100")
        calls = []

        class FakeJira:
            def __init__(self, cfg):
                pass

            def post(self, path, body):
                calls.append(dict(body))
                return {"issueChangeLogs": [], "nextPageToken": "page-%d" % len(calls)}

        self.p_jira_changelogs.stop()
        try:
            with mock.patch.object(sw.jira_wake, "Jira", FakeJira):
                with self.assertRaises(sw.Truncated):
                    sw.jira_changelogs(PCFG["jira"], [issue])
        finally:
            self.p_jira_changelogs.start()
        self.assertEqual(len(calls), sw.PAGE_CAP)

    def test_the_shared_jira_client_posts_json_without_a_command_line(self):
        captured = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b'{"ok": true}'

        def urlopen(request, timeout):
            captured.append((request, timeout))
            return Reply()

        self.p_jira_client.stop()
        try:
            with mock.patch.object(sw.jira_wake.urllib.request, "urlopen", urlopen):
                cfg = dict(PCFG["jira"], api_token="fixture-secret-not-in-url-123")
                client = sw.jira_wake.Jira(cfg)
                got = client.post("/rest/api/3/changelog/bulkfetch", {"issueIdsOrKeys": ["10100"]})
        finally:
            self.p_jira_client.start()

        self.assertEqual(got, {"ok": True})
        request, timeout = captured[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 20)
        self.assertEqual(json.loads(request.data.decode()), {"issueIdsOrKeys": ["10100"]})
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertNotIn(cfg["api_token"], request.full_url)

    def test_a_missing_gh_is_a_github_hole_and_the_other_tiers_still_answer(self):
        self.jira["Review"] = [jira("ACME-843", "Review", 11, "Bob Reviewer")]
        self.p_gh_prs.stop()                                # real gh_prs, which checks GH
        try:
            with mock.patch.object(sw, "GH", None):
                lines = self.poll()
        finally:
            self.p_gh_prs.start()
        self.assertTrue(any("kind=jira issue=ACME-843" in line for line in lines))
        self.assertTrue(any(line.startswith("type=degraded sub=github") for line in lines))

    def test_a_missing_bkt_with_repos_configured_is_a_bitbucket_hole(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        with mock.patch.object(bitbucket_wake, "BKT", None):
            lines = self.poll()
        self.assertTrue(any("kind=pr" in line for line in lines), "the other tiers still answer")
        self.assertTrue(any(line.startswith("type=degraded sub=bitbucket") for line in lines))


class TestHalfLandedPairs(Base):
    """Two changes that must land together, declared with `Lands-with:` in
    both PR bodies. A half that merged while its companion is unmerged is a live
    window — stalled from the moment it lands, until the companion merges."""

    def test_a_half_landed_pair_is_stalled_until_its_companion_merges(self):
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        lines = self.poll()
        self.assertEqual(self.types(lines), ["type=stalled"])
        self.assertIn("kind=pair repo=app #569 waits-on=alice/Agent-Watchtower#8 idle=2d | perf nightly ref input", lines[0])
        self.companions[("alice/Agent-Watchtower", 8)] = True   # the companion lands
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"])
        self.assertIn("key=" + PAIR_KEY, lines[0])

    def test_a_merged_pr_without_a_trailer_is_not_a_pair(self):
        self.merged[GH_REPO] = [merged_pr(570, 1)]
        self.assertEqual(self.poll(), [])

    def test_a_companion_that_cannot_be_fetched_is_a_hole_not_a_closed_window(self):
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8")]
        self.poll()
        self.companions[("alice/Agent-Watchtower", 8)] = RuntimeError("HTTP 502")
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertNotIn("stalled_cleared", "\n".join(lines), "unreachable is not merged")
        self.assertTrue(any(line.startswith("type=degraded sub=pairs") for line in lines))
        self.assertIn(PAIR_KEY, self.state["stalled"])

    def test_pairs_are_their_own_source(self):
        """A companion fetch failing must not un-answer the PR tier: a stalled PR
        that is genuinely gone still clears in the same cycle."""
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8")]
        self.poll()
        self.gh[GH_REPO] = []
        self.companions[("alice/Agent-Watchtower", 8)] = RuntimeError("HTTP 502")
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertTrue(any(line.startswith("type=stalled_cleared key=pr:") for line in lines))
        self.assertIn(PAIR_KEY, self.state["stalled"])

    def test_the_merged_pool_is_filtered_by_merge_date_on_the_server_not_walked_in_creation_order(self):
        """codex pass 1 (P1): `gh pr list` orders by CREATION. Agent-Watchtower#2 was
        opened 08-23 and merged 09-08 — it lists LAST. A walk that stopped at the
        first merge older than the window skipped it and called the pool answered."""
        calls = []
        page = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return mock.Mock(returncode=0, stdout=json.dumps(page), stderr="")

        # in creation order, as gh returns it: a young quick PR, an old one (which
        # the server filter would not return, but the client cut must still hold),
        # then the long-lived PR merged the day before yesterday.
        page = [merged_pr(0, 1), merged_pr(9, 40), merged_pr(2, 2)]
        self.p_gh_merged.stop()                               # the real gh_merged_prs over a fake gh
        try:
            with mock.patch.object(sw, "GH", "/usr/bin/gh"), mock.patch.object(sw.subprocess, "run", fake_run):
                got = sw.gh_merged_prs(GH_REPO, NOW - timedelta(days=14))
            self.assertEqual([p["number"] for p in got], [0, 2], "an out-of-order old merge must not END the walk")
            argv = calls[0]
            self.assertIn("--search", argv, argv)
            self.assertEqual(argv[argv.index("--search") + 1],
                             "merged:>=%s" % (NOW - timedelta(days=14)).strftime("%Y-%m-%d"),
                             "the window is the SERVER's filter, on the merge date")
            self.assertEqual(argv[argv.index("--state") + 1], "merged")
            page = [merged_pr(n, 1) for n in range(sw.GH_LIMIT)]
            with mock.patch.object(sw, "GH", "/usr/bin/gh"), mock.patch.object(sw.subprocess, "run", fake_run):
                with self.assertRaises(sw.Truncated):
                    sw.gh_merged_prs(GH_REPO, NOW - timedelta(days=14))
        finally:
            self.p_gh_merged.start()

    def test_a_tracked_pair_outlives_the_discovery_window(self):
        """codex pass 1 (P2): `pair_days` bounds discovery only. Once the merge ages
        out of the pool the pair is re-read by number; it clears when the companion
        merges, not when the calendar moves."""
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        later = NOW + timedelta(days=20)
        self.merged[GH_REPO] = []                             # aged out of the pool
        self.origins[(GH_REPO, 569)] = origin_pr(2, title="perf nightly ref input")
        lines = self.poll(later)
        self.assertEqual(self.types(lines), ["type=stalled_still"], lines)
        self.assertIn("kind=pair repo=app #569 waits-on=alice/Agent-Watchtower#8 idle=22d | perf nightly ref input", lines[0])
        self.companions[("alice/Agent-Watchtower", 8)] = True
        lines = self.poll(later + timedelta(hours=1))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"], lines)
        self.assertNotIn(PAIR_KEY, self.state["stalled"])

    def test_a_tracked_pair_that_cannot_be_re_read_is_a_hole_not_a_clearing(self):
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8")]
        self.poll()
        self.merged[GH_REPO] = []
        self.origins[(GH_REPO, 569)] = RuntimeError("HTTP 502")
        lines = self.poll(NOW + timedelta(days=20))
        self.assertNotIn("stalled_cleared", "\n".join(lines))
        self.assertTrue(any(line.startswith("type=degraded sub=pairs") for line in lines), lines)
        self.assertIn(PAIR_KEY, self.state["stalled"])

    def test_two_companions_are_two_windows(self):
        """codex pass 1 (P2): the companion is part of the key — two trailers on one
        merged PR are two items, and one closing does not overwrite the other."""
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8\nLands-with: acme/service-auth#3")]
        lines = self.poll()
        self.assertEqual(self.types(lines), ["type=stalled", "type=stalled"], lines)
        other = "pair:%s#569->acme/service-auth#3" % GH_REPO
        self.assertEqual(sorted(k for k in self.state["stalled"] if k.startswith("pair:")), sorted([PAIR_KEY, other]))
        self.companions[("alice/Agent-Watchtower", 8)] = True
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"], lines)
        self.assertIn("key=" + PAIR_KEY, lines[0])
        self.assertIn(other, self.state["stalled"])

    def test_a_discovered_pair_whose_first_check_fails_is_retried_past_the_window(self):
        """codex pass 2 (P2): the check failed on the last cycle inside the window,
        the key was on no record, and the next cycle — nothing in the pool, nothing
        tracked — announced a recovery. A question asked and not answered is kept
        until it is answered, whatever the calendar does meanwhile."""
        self.merged[GH_REPO] = [merged_pr(569, 13, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.companions[("alice/Agent-Watchtower", 8)] = RuntimeError("HTTP 502")
        lines = self.poll()
        self.assertEqual(self.types(lines), ["type=degraded"], lines)
        self.assertEqual(self.state["pair_pending"], [PAIR_KEY])
        self.assertEqual(sw.load_state("acme")["pair_pending"], [PAIR_KEY], "and it survives a restart")
        later = NOW + timedelta(days=2)                       # the merge is 15 days old now: out of the pool
        self.merged[GH_REPO] = []
        self.origins[(GH_REPO, 569)] = origin_pr(13, title="perf nightly ref input")
        self.companions[("alice/Agent-Watchtower", 8)] = False
        lines = self.poll(later)
        self.assertEqual(sorted(self.types(lines)), ["type=degraded_recovered", "type=stalled"], lines)
        self.assertIn("kind=pair repo=app #569 waits-on=alice/Agent-Watchtower#8 idle=15d | perf nightly ref input",
                      "\n".join(lines))
        self.assertEqual(self.state["pair_pending"], [], "answered: no longer pending")
        self.assertIn(PAIR_KEY, self.state["stalled"], "on record now")

    def test_a_pending_pair_whose_repo_left_the_config_is_dropped_not_retried(self):
        self.state["pair_pending"] = ["pair:alice/other#1->alice/Agent-Watchtower#8"]
        self.origins[("alice/other", 1)] = RuntimeError("must not be re-read")
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.state["pair_pending"], [])

    def test_a_corrected_trailer_retires_the_old_question_and_asks_the_new_one(self):
        """codex pass 3 (P2): a mistyped trailer 404s; the author fixes the body. The
        pending key still named the old companion and was re-asked forever —
        `pairs` degraded, no pair able to clear. The re-read origin's BODY is the
        declaration; the key is only where the question was filed."""
        self.merged[GH_REPO] = [merged_pr(569, 13, "alice/Agent-Watchtower#88", "perf nightly ref input")]  # typo: #88
        self.companions[("alice/Agent-Watchtower", 88)] = RuntimeError("HTTP 404")
        self.poll()
        typo_key = "pair:%s#569->alice/Agent-Watchtower#88" % GH_REPO
        self.assertEqual(self.state["pair_pending"], [typo_key])
        self.merged[GH_REPO] = []                             # aged out; the body now says #8
        self.origins[(GH_REPO, 569)] = origin_pr(13, "alice/Agent-Watchtower#8", "perf nightly ref input")
        self.companions[("alice/Agent-Watchtower", 8)] = False
        lines = self.poll(NOW + timedelta(days=2))            # #88 still raises: asking it again would show as degraded
        self.assertEqual(sorted(self.types(lines)), ["type=degraded_recovered", "type=stalled"], lines)
        self.assertIn("waits-on=alice/Agent-Watchtower#8 idle=15d", "\n".join(lines))
        self.assertEqual(self.state["pair_pending"], [])
        self.assertIn(PAIR_KEY, self.state["stalled"])
        self.assertNotIn(typo_key, self.state["stalled"])
        self.companions[("alice/Agent-Watchtower", 8)] = True
        lines = self.poll(NOW + timedelta(days=2, hours=1))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"], "the typo never blocks the clearing")

    def test_a_withdrawn_trailer_retires_a_window_on_record(self):
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8")]
        self.poll()
        self.merged[GH_REPO] = []
        self.origins[(GH_REPO, 569)] = origin_pr(2, lands_with=None)   # the trailer was removed from the body
        self.companions[("alice/Agent-Watchtower", 8)] = RuntimeError("must not be asked")
        lines = self.poll(NOW + timedelta(days=20))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"], lines)

    def test_one_unfetchable_companion_holds_only_its_own_pair(self):
        """A companion that 404s forever — a mistyped or
        Bitbucket-shaped trailer — used to un-answer the whole `pairs` source, and
        the neither-adds-nor-clears rule then held EVERY pair on the profile for
        good. Now it holds its own key, keeps its degraded line, and the others
        reconcile."""
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8"),
                                merged_pr(570, 1, "acme/service#12")]   # Bitbucket-shaped: parses, then 404s
        self.companions[("acme/service", 12)] = RuntimeError("HTTP 404")
        lines = self.poll()
        self.assertEqual(self.types(lines), ["type=stalled", "type=degraded"], lines)
        bad = "pair:%s#570->acme/service#12" % GH_REPO
        self.assertIn(PAIR_KEY, self.state["stalled"])
        self.assertNotIn(bad, self.state["stalled"])
        self.assertEqual(self.state["pair_pending"], [bad])
        items, answered, unresolved = sw.compute(PCFG, CFG, NOW, sw._degraded.Cycle(),
                                                 tracked=self.state["stalled"], pending=self.state["pair_pending"])
        self.assertIn("pairs", answered, "the inventory is complete; one key is held, the source answered")
        self.assertEqual(unresolved, {bad})
        self.companions[("alice/Agent-Watchtower", 8)] = True            # 569's companion lands
        lines = self.poll(NOW + timedelta(hours=1))
        self.assertIn("type=stalled_cleared key=" + PAIR_KEY, "\n".join(lines),
                      "the good pair must clear while the bad one is held")
        self.assertEqual(self.state["pair_pending"], [bad], "the unanswered one is still held")

    def test_the_parser_reads_a_pull_url_and_the_same_repo_shorthand_and_ignores_fences(self):
        body = ("Lands-with: https://github.com/alice/Agent-Watchtower/pull/8\n"
                "Lands-with: #583\n"
                "```\nLands-with: example/repo#1\n```\n"
                "Lands with: Agent-Watchtower#9\n")                          # owner missing: unparsed
        self.assertEqual(github_wake.pairs_of(body, repo=GH_REPO),
                         [("alice/Agent-Watchtower", 8), (GH_REPO, 583)])
        self.assertEqual(github_wake.unparsed_trailers(body, repo=GH_REPO), ["Agent-Watchtower#9"])
        self.assertEqual(github_wake.pairs_of("Lands-with: #583"), [], "the same-repo form needs the repo")
        self.assertEqual(github_wake.unparsed_trailers("Lands-with: #583"), ["#583"])
        many = "".join("Lands-with: o/r#%d\n" % n for n in range(12))
        self.assertEqual(len(github_wake.pairs_of(many)), github_wake.MAX_PAIRS_PER_BODY)

    def test_a_crlf_body_holds_a_tracked_pair_instead_of_silently_clearing_it(self):
        """P1 (post-merge) — the silent one.

        the declaration regex is anchored and cannot absorb a `\r`, so a CRLF
        body parsed to NOTHING. On a tracked pair that meant: the re-read saw no
        declared companion, retired the question, and `reconcile_set` emitted
        `stalled_cleared` for a window that was still OPEN — a broken read
        presenting as "nothing stalled", which is the one outcome this daemon
        exists to make impossible. The trigger is ordinary: GitHub's web form
        normalises textareas to CRLF, so EDITING a tracked pair's body produces
        exactly this.
        """
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.assertIn(PAIR_KEY, self.state["stalled"], "precondition: the pair is on record")
        self.merged[GH_REPO] = []                                  # aged out -> re-read by number
        origin = origin_pr(2, title="perf nightly ref input")
        origin["body"] = origin["body"].replace("\n", "\r\n")       # someone edited it in the web form
        self.assertIn("\r\n", origin["body"])
        self.origins[(GH_REPO, 569)] = origin
        lines = self.poll(NOW + timedelta(days=20))
        self.assertNotIn("stalled_cleared", "\n".join(lines),
                         "a CRLF body must never clear a window whose companion is still open")
        self.assertIn(PAIR_KEY, self.state["stalled"])
        self.assertEqual(self.types(lines), ["type=stalled_still"], lines)
        self.companions[("alice/Agent-Watchtower", 8)] = True  # and it still clears for the RIGHT reason
        self.assertEqual(self.types(self.poll(NOW + timedelta(days=20, hours=1))), ["type=stalled_cleared"])

    def test_the_parser_normalises_line_endings_and_tracks_real_fences(self):
        """The parser cases, at the level the two daemons share."""
        pair = [("acme/app", 583)]
        for label, body in (
            ("LF", "Lands-with: acme/app#583\n"),
            ("CRLF", "Lands-with: acme/app#583\r\n"),
            ("bare CR", "Lands-with: acme/app#583\r"),
        ):
            self.assertEqual(github_wake.pairs_of(body, repo=GH_REPO), pair, label)
            self.assertEqual(github_wake.unparsed_trailers(body, repo=GH_REPO), [], label)
        for label, body in (
            ("triple backtick", "x\n```\nLands-with: example/repo#1\n```\n"),
            ("tilde", "x\n~~~\nLands-with: example/repo#1\n~~~\n"),
            ("four backticks holding three", "x\n````\n```\nLands-with: example/repo#1\n```\n````\n"),
            ("indented three spaces", "x\n   ```\n   Lands-with: example/repo#1\n   ```\n"),
            # codex on the fix: a fenced example under a list item validly carries
            # FOUR+ spaces. A three-space rule missed both fences while
            # the parser read the indented line between them, so documentation
            # declared a real pair — a REGRESSION against the regex this replaced.
            ("indented four, under a list", "1. Example:\n\n    ```\n    Lands-with: example/repo#1\n    ```\n"),
            ("indented eight", "x\n        ```\n        Lands-with: example/repo#1\n        ```\n"),
        ):
            self.assertEqual(github_wake.pairs_of(body, repo=GH_REPO), [], label)
        # `unparsed_trailers` is scoped to UNFENCED lines, so a TOP-LEVEL fenced
        # example is silent — reporting it would fire an event on every PR that
        # documents the syntax.
        self.assertEqual(
            github_wake.unparsed_trailers("x\n```\nLands-with: example/repo#1\n```\n", repo=GH_REPO), [])
        # A fence indented 0–3 spaces IS a top-level fence (CommonMark), so its
        # example is stripped and stays silent — no noise on a PR that documents
        # the syntax with a slightly indented block.
        self.assertEqual(
            github_wake.unparsed_trailers("x\n   ```\n   Lands-with: example/repo#1\n   ```\n", repo=GH_REPO),
            [])
        # At FOUR+ spaces it is inside a list or an indented block, not a fence,
        # and the example is reported instead. Accepted rather than filtered: the
        # line names itself, it is report-only, and any rule silencing it would
        # also silence someone who indents a REAL trailer inside a bullet list.
        self.assertEqual(
            github_wake.unparsed_trailers("x\n    ```\n    Lands-with: example/repo#1\n    ```\n", repo=GH_REPO),
            ["example/repo#1"])
        # ⚠ codex round six, the P1 this design claimed to have ruled out: an
        # opener indented ONE space with an unindented closer. Anchoring at column
        # zero missed the opener, promoted the closer, and let a later bare fence
        # close the phantom block — stripping a real declaration in between, and
        # stripping it SILENTLY. Neither pairs_of nor unparsed_trailers saw it.
        silent = (" ```\nexample\n```\nLands-with: alice/x#7\n```\nanother\n```\n")
        self.assertEqual(github_wake.pairs_of(silent, repo=GH_REPO), [("alice/x", 7)])
        # a CLOSED fence must not swallow what follows it, in either line ending
        self.assertEqual(
            github_wake.pairs_of("```\nLands-with: example/repo#1\n```\nLands-with: alice/x#7\n", repo=GH_REPO),
            [("alice/x", 7)])
        self.assertEqual(
            github_wake.pairs_of("~~~\r\nLands-with: example/repo#1\r\n~~~\r\nLands-with: alice/y#9\r\n", repo=GH_REPO),
            [("alice/y", 9)])
        # ⚠ An UNCLOSED fence strips NOTHING, and that is the design rather than a
        # gap. It used to swallow the body to EOF, which is
        # how a real declaration after a half-written block went missing and a new
        # pair was never discovered — silently, with no tracked key for the
        # retirement predicate to protect. Discovery's failure has to be a FALSE
        # WINDOW, which is loud and correctable. So both of these now READ, and
        # the surplus is accepted:
        self.assertEqual(github_wake.pairs_of("x\n```\nLands-with: example/repo#1\n", repo=GH_REPO),
                         [("example/repo", 1)], "unclosed backtick fence strips nothing")
        self.assertEqual(github_wake.pairs_of("```\nLands-with: example/repo#1\n~~~\n", repo=GH_REPO),
                         [("example/repo", 1)], "a tilde does not close a backtick fence, so it stays unclosed")
        # and the declaration that FOLLOWS an unclosed fence survives, which is
        # the whole point of the trade
        self.assertEqual(
            github_wake.pairs_of("```\nhalf-written example\n\nLands-with: alice/x#7\n", repo=GH_REPO),
            [("alice/x", 7)])
        # a DECLARATION is at column zero; indented or quoted is an example, and
        # is reported to its author rather than silently dropped
        self.assertEqual(github_wake.pairs_of("  Lands-with: alice/x#7\n", repo=GH_REPO), [])
        self.assertEqual(github_wake.pairs_of("> Lands-with: alice/x#7\n", repo=GH_REPO), [])
        self.assertEqual(github_wake.unparsed_trailers("  Lands-with: alice/x#7\n", repo=GH_REPO),
                         ["alice/x#7"])
        # nested list markers at any depth: the example inside is indented, so the
        # column-zero rule retires the whole family without a fence rule for it
        for depth in ("- ", "1. - ", "- - - "):
            body = depth + "```sh\n" + " " * 6 + "Lands-with: example/repo#1\n" + " " * 6 + "```\n\nLands-with: alice/x#7\n"
            self.assertEqual(github_wake.pairs_of(body, repo=GH_REPO), [("alice/x", 7)], depth)
        # codex on the fix: a line that is an INLINE CODE SPAN is not a fence
        # opener — CommonMark forbids a backtick in a backtick fence's info
        # string. Without that rule it opened a fence that never closed, ate the
        # rest of the body, and put a tracked pair back on the silent-clear path.
        self.assertEqual(
            github_wake.pairs_of("```example```\n\nLands-with: acme/app#583\n", repo=GH_REPO),
            [("acme/app", 583)])
        # but a REAL opener carrying an ordinary info string still opens
        self.assertEqual(
            github_wake.pairs_of("```python\nLands-with: example/repo#1\n```\nLands-with: alice/x#7\n", repo=GH_REPO),
            [("alice/x", 7)])

    def test_a_body_that_parses_to_NOTHING_holds_the_pair_instead_of_retiring_it(self):
        """The predicate — and the reason three review rounds inside the
        parser were all one bug.

        An empty parse has two causes that look identical to the re-read: the
        author removed the trailer, or the parser missed it. Every fence variant
        codex found produced an EMPTY parse on a body that still declared the
        pair, and treating that as a withdrawal turned each one into a silent
        `stalled_cleared` on a still-open window. Positive evidence is required
        to retire: a NON-EMPTY parse that omits this companion. An empty parse
        holds, visibly, and is retried.

        Asked about THIS COMPANION, not about the count: an earlier version of
        this predicate guarded only the EMPTY parse, and codex found the hole —
        a PARTIAL parse (one trailer read, another hidden) walked past it. The
        mirror case, a body that names the companion nowhere, is a genuine
        withdrawal and still retires; see the withdrawn-trailer test.
        """
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.assertIn(PAIR_KEY, self.state["stalled"])
        self.merged[GH_REPO] = []
        origin = origin_pr(2, title="perf nightly ref input")
        # The companion is NAMED but sits where the scanner reads a fence — which
        # is exactly what a scanner bug looks like from inside the code, and is
        # indistinguishable from a genuine example naming the same companion. The
        # predicate holds either way rather than guessing.
        origin["body"] = "Does a thing.\n\n```\nLands-with: alice/Agent-Watchtower#8\n```\n"
        self.origins[(GH_REPO, 569)] = origin
        lines = self.poll(NOW + timedelta(days=20))
        self.assertNotIn("stalled_cleared", "\n".join(lines), "an empty parse must never clear a tracked pair")
        self.assertIn(PAIR_KEY, self.state["stalled"], "held on record")
        self.assertTrue(any(line.startswith("type=degraded sub=pairs") for line in lines),
                        "the hold is VISIBLE, not silent")
        # `pair_pending` carries only keys NOT already on record — a key on record
        # is retried through the `tracked` path instead, which is what happens
        # here. Assert the RETRY rather than the bookkeeping: make the body
        # readable again and the pair resolves normally.
        self.assertEqual(self.state["pair_pending"], [])
        self.origins[(GH_REPO, 569)] = origin_pr(2, title="perf nightly ref input")
        self.companions[("alice/Agent-Watchtower", 8)] = True
        lines = self.poll(NOW + timedelta(days=20, hours=1))
        self.assertEqual(self.types(lines), ["type=stalled_cleared", "type=degraded_recovered"], lines)
        self.assertNotIn(PAIR_KEY, self.state["stalled"], "and it clears for the RIGHT reason")

    def test_a_held_pair_whose_companion_MERGES_closes_even_while_unreadable(self):
        """codex, round four: holding on an unreadable body kept a FULLY LANDED
        pair on record for ever, with `pairs` degraded, until a human repaired the
        PR body. A confirmed merge is positive evidence that the window closed,
        whatever the declaration looks like — so ask before holding."""
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.merged[GH_REPO] = []
        origin = origin_pr(2, title="perf nightly ref input")
        origin["body"] = "Does a thing.\n\n```\nLands-with: alice/Agent-Watchtower#8\n```\n"
        self.origins[(GH_REPO, 569)] = origin
        self.companions[("alice/Agent-Watchtower", 8)] = True      # it landed
        lines = self.poll(NOW + timedelta(days=20))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"], lines)
        self.assertNotIn(PAIR_KEY, self.state["stalled"])

    def test_a_PARTIAL_parse_does_not_retire_the_companion_it_missed(self):
        """codex, round four — the hole in the first predicate. One trailer reads,
        another is hidden by a fence shape the scanner gets wrong, so `declared` is
        NON-empty and an empty-parse guard never fires. Asked about the companion
        rather than the count, the hidden one is held."""
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.merged[GH_REPO] = []
        origin = origin_pr(2, title="perf nightly ref input")
        origin["body"] = ("Lands-with: acme/service-auth#3\n\n"
                          "```\nLands-with: alice/Agent-Watchtower#8\n```\n")
        self.origins[(GH_REPO, 569)] = origin
        self.companions[("acme/service-auth", 3)] = True
        lines = self.poll(NOW + timedelta(days=20))
        self.assertNotIn("stalled_cleared key=" + PAIR_KEY, "\n".join(lines),
                         "a partial parse must not retire the companion it missed")
        self.assertIn(PAIR_KEY, self.state["stalled"])

    def test_a_NON_EMPTY_parse_that_omits_the_companion_still_retires_it(self):
        """The other half of the predicate: a body that declares something ELSE is
        positive evidence the author changed the trailer, and must still retire."""
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.merged[GH_REPO] = []
        self.origins[(GH_REPO, 569)] = origin_pr(2, "acme/service-auth#3", "perf nightly ref input")
        self.companions[("acme/service-auth", 3)] = True      # the new companion already landed
        lines = self.poll(NOW + timedelta(days=20))
        self.assertEqual(self.types(lines), ["type=stalled_cleared"], lines)
        self.assertNotIn(PAIR_KEY, self.state["stalled"])

    def test_holding_one_companion_does_not_blind_the_origin_to_its_others(self):
        """codex, round five: the hold branch used `continue`, which skipped the
        loop over everything else the origin declared. An origin carrying an
        unreadable mention of the tracked companion AND a new, perfectly readable
        trailer would hold the old key and never check or record the new one —
        every poll repeating the omission while both windows stood open.

        Pinned because the mutation restoring `continue` passed the whole suite:
        I fixed it and did not write it down, for the second time today.
        """
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.assertIn(PAIR_KEY, self.state["stalled"])
        self.merged[GH_REPO] = []
        origin = origin_pr(2, title="perf nightly ref input")
        origin["body"] = ("```\nLands-with: alice/Agent-Watchtower#8\n```\n"
                          "Lands-with: acme/service-auth#3\n")
        self.origins[(GH_REPO, 569)] = origin
        self.companions[("acme/service-auth", 3)] = False   # the NEW one is open too
        lines = self.poll(NOW + timedelta(days=20))
        other = "pair:%s#569->acme/service-auth#3" % GH_REPO
        self.assertIn(PAIR_KEY, self.state["stalled"], "the unreadable one is held")
        self.assertIn(other, self.state["stalled"], "and the readable one is still discovered")
        self.assertTrue(any("waits-on=acme/service-auth#3" in line for line in lines), lines)

    def test_an_UNREACHABLE_companion_does_not_blind_the_origin_to_its_others(self):
        """codex, round six — the twin of the round-five finding, four lines below
        it. Round five took the `continue` off the not-merged branch and left the
        one on the EXCEPTION branch, so a companion whose lookup keeps 404ing kept
        hiding every other declaration on the same origin, on every poll.

        Pinned because the mutation restoring that `continue` passed the whole
        suite. Third time today a fix of mine survived its own mutation.
        """
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.merged[GH_REPO] = []
        origin = origin_pr(2, title="perf nightly ref input")
        origin["body"] = ("```\nLands-with: alice/Agent-Watchtower#8\n```\n"
                          "Lands-with: acme/service-auth#3\n")
        self.origins[(GH_REPO, 569)] = origin
        self.companions[("alice/Agent-Watchtower", 8)] = RuntimeError("HTTP 404")   # keeps failing
        self.companions[("acme/service-auth", 3)] = False                   # readable, open
        lines = self.poll(NOW + timedelta(days=20))
        other = "pair:%s#569->acme/service-auth#3" % GH_REPO
        self.assertIn(PAIR_KEY, self.state["stalled"], "the unreachable one is held")
        self.assertIn(other, self.state["stalled"], "and the readable one is STILL discovered")
        self.assertTrue(any("waits-on=acme/service-auth#3" in line for line in lines), lines)

    def test_an_UNREACHABLE_companion_is_reported_as_unreachable_not_as_unreadable(self):
        """One failure, two notes, both wrong.

        The `except` emitted "could not be re-read and its companion could not be
        reached" — but the origin WAS re-read; that is how we knew the companion
        was mentioned. It then set `settled = False` and fell through, so the
        not-settled branch fired too and blamed "a shape this parser cannot read"
        for what was a 404 on the companion lookup. The daemon's DECISION was
        right both times; the operator's log was wrong twice for one event.

        The hole detail happened to survive it, because `_degraded.Cycle.record`
        is `setdefault` and the first (correct) call won — right by accident of
        ordering, which is not a property to rely on. One reason now, built once
        and used by both the note and the hole.
        """
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.assertIn(PAIR_KEY, self.state["stalled"])
        self.merged[GH_REPO] = []
        origin = origin_pr(2, title="perf nightly ref input")
        origin["body"] = "```\nLands-with: alice/Agent-Watchtower#8\n```\n"   # mentioned, not declared
        self.origins[(GH_REPO, 569)] = origin
        self.companions[("alice/Agent-Watchtower", 8)] = RuntimeError("HTTP 404")
        self.poll(NOW + timedelta(days=20))

        mine = [n for n in self.notes if n.startswith("note: [stalled]") and PAIR_KEY in n]
        self.assertEqual(len(mine), 1, "one failure must produce one stalled note: %s" % mine)
        self.assertIn("could not be reached", mine[0])
        self.assertIn("HTTP 404", mine[0])
        self.assertNotIn("shape this parser cannot read", mine[0])
        # The degraded subsystem announces the same hole; that line is a separate
        # concern and SHOULD exist — what matters is that it carries the same
        # reason, which is what building the reason once buys.
        degraded = [n for n in self.notes if "degraded: pairs" in n and PAIR_KEY in n]
        self.assertEqual(len(degraded), 1, degraded)
        self.assertIn("could not be reached", degraded[0])
        self.assertNotIn("shape this parser cannot read", degraded[0])
        self.assertIn(PAIR_KEY, self.state["stalled"], "and it is still held")

    def test_an_unreadable_shape_is_still_reported_as_a_shape(self):
        """The other side of the same branch — the reason must track the CAUSE,
        not merely stop naming the wrong one."""
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()
        self.merged[GH_REPO] = []
        origin = origin_pr(2, title="perf nightly ref input")
        origin["body"] = "```\nLands-with: alice/Agent-Watchtower#8\n```\n"
        self.origins[(GH_REPO, 569)] = origin
        self.companions[("alice/Agent-Watchtower", 8)] = False      # reachable, still open
        self.poll(NOW + timedelta(days=20))

        mine = [n for n in self.notes if n.startswith("note: [stalled]") and PAIR_KEY in n]
        self.assertEqual(len(mine), 1, mine)
        self.assertIn("shape this parser cannot read", mine[0])
        self.assertNotIn("could not be reached", mine[0])
        degraded = [n for n in self.notes if "degraded: pairs" in n and PAIR_KEY in n]
        self.assertEqual(len(degraded), 1, degraded)
        self.assertIn("shape this parser cannot read", degraded[0])
        self.assertIn(PAIR_KEY, self.state["stalled"])

    def test_a_fence_opened_on_a_list_marker_line_does_not_eat_the_real_trailer(self):
        """codex, third round: `- ```sh` missed the opener while its indented
        CLOSING fence read as a new unclosed block, discarding everything after
        it — including a real declaration."""
        body = "- ```sh\n  Lands-with: example/repo#1\n  ```\n\nLands-with: alice/Agent-Watchtower#8\n"
        self.assertEqual(github_wake.pairs_of(body, repo=GH_REPO), [("alice/Agent-Watchtower", 8)])
        numbered = "1. ```sh\n   Lands-with: example/repo#1\n   ```\n\nLands-with: alice/Agent-Watchtower#8\n"
        self.assertEqual(github_wake.pairs_of(numbered, repo=GH_REPO), [("alice/Agent-Watchtower", 8)])
        # A NESTED marker opens too (codex, round four); its closing fence is
        # indented five spaces and must not read as a fresh, unterminated block.
        nested = "1. - ```sh\n     Lands-with: example/repo#1\n     ```\n\nLands-with: alice/Agent-Watchtower#8\n"
        self.assertEqual(github_wake.pairs_of(nested, repo=GH_REPO), [("alice/Agent-Watchtower", 8)])
        # ⚠ But a marker may NOT CLOSE one: inside a fenced `diff` block a line
        # like "- ```" is CONTENT. Closing on it makes the real closing fence
        # open an unterminated block that swallows the trailer below. Pinned
        # because the mutation allowing markers on the closer survived the whole
        # suite — I had verified this case by hand and never written it down.
        diffblock = "```diff\n- ```\n```\nLands-with: alice/Agent-Watchtower#8\n"
        self.assertEqual(github_wake.pairs_of(diffblock, repo=GH_REPO), [("alice/Agent-Watchtower", 8)])
        # ⚠ and the SAME body with another fenced block after the declaration.
        # Without this, the property is not pinned: a mutation letting a marker
        # line close survived the whole suite, because the phantom it opened was
        # left UNCLOSED and an unclosed fence strips nothing. The trailing block
        # closes the phantom and the declaration between them disappears — the
        # unclosed-strips-nothing rule was masking the defect, exactly as it
        # masked the round-seven one until codex added a trailing block.
        diff_then_block = diffblock + "\n```\nunrelated\n```\n"
        self.assertEqual(github_wake.pairs_of(diff_then_block, repo=GH_REPO),
                         [("alice/Agent-Watchtower", 8)])
        # ⚠ codex, ROUND SEVEN — the same phantom-fence bug as round six, from the
        # opposite side, and the case that finally named the family. Add ANOTHER
        # fenced block after the declaration and the earlier shapes break: the
        # list fence's two-space CLOSER opened a phantom, the later bare fence
        # closed it, and the real declaration between them was stripped SILENTLY.
        # Measured before the fix: pairs_of -> [] and unparsed_trailers -> [] where
        # the base found the pair, so neither the merge notification nor stalled
        # discovery would ever have checked the companion.
        # An opener the scanner cannot SEE leaves a closer it CAN see. So OPENING
        # accepts marker prefixes and CLOSING does not.
        after = ("- ```sh\n  Lands-with: example/repo#1\n  ```\n\n"
                 "Lands-with: alice/Agent-Watchtower#8\n\n```\nunrelated\n```\n")
        self.assertEqual(github_wake.pairs_of(after, repo=GH_REPO), [("alice/Agent-Watchtower", 8)])
        numbered_after = ("1. ```sh\n   Lands-with: example/repo#1\n   ```\n\n"
                          "Lands-with: alice/Agent-Watchtower#8\n\n```\nunrelated\n```\n")
        self.assertEqual(github_wake.pairs_of(numbered_after, repo=GH_REPO),
                         [("alice/Agent-Watchtower", 8)])
        # ⚠ codex, ROUND EIGHT — the same family once more, and the case that
        # named the closer as the side to fix. A fence under `10. ` has its
        # content AND its closer at FOUR spaces, which a 0-3 closing rule
        # rejects; the fence then stayed open until a later real fence closed
        # it, taking the declaration between them. A phantom is always an
        # OPENER, so the CLOSER is the permissive side: it is only consulted
        # while a fence is already open and therefore cannot invent a span.
        ten = ("10. ```sh\n    Lands-with: example/repo#1\n    ```\n\n"
               "Lands-with: alice/Agent-Watchtower#8\n\n```\nunrelated\n```\n")
        self.assertEqual(github_wake.pairs_of(ten, repo=GH_REPO), [("alice/Agent-Watchtower", 8)])
        self.assertEqual(github_wake.unparsed_trailers(ten, repo=GH_REPO), [],
                         "and the documented example under the marker stays silent")
        # ⚠ codex, ROUND NINE — the flaw in round eight's permissive closer. A
        # line like `    ~~~` sitting as CONTENT inside a top-level tilde fence
        # closed it early; the REAL closer then opened a phantom, and the next
        # tilde block closed that, taking the declaration between them. A closer
        # is now bounded by its opener: at most three past the opener's prefix,
        # which still lets a fence under `10. ` close at four and refuses this
        # content line under a prefix of zero.
        tilde_content = ("~~~\nexample\n    ~~~\n~~~\n"
                         "Lands-with: alice/Agent-Watchtower#8\n~~~\nanother\n~~~\n")
        self.assertEqual(github_wake.pairs_of(tilde_content, repo=GH_REPO),
                         [("alice/Agent-Watchtower", 8)])
        # round six's side of the same family, kept green: an opener indented one
        # space, its closer at column zero, a declaration after both.
        r6 = " ```sh\nLands-with: example/repo#1\n```\n\nLands-with: alice/Agent-Watchtower#8\n"
        self.assertEqual(github_wake.pairs_of(r6, repo=GH_REPO), [("alice/Agent-Watchtower", 8)])
        # and the whitespace-only line that used to go quadratic still parses FAST.
        # This assertion had no time bound, so the
        # PR's word "pinned" was false — with the quadratic regex restored it still
        # passes, in 2.737 s. The bound is what pins it. Measured here at 0.00002-
        # 0.00003 s over five runs, so 0.5 s is a ~15,000x margin: a catastrophe
        # detector, not a performance test, and not a flake risk on a loaded box.
        started = time.monotonic()
        self.assertEqual(github_wake.pairs_of(" " * 20000 + "\n", repo=GH_REPO), [])
        self.assertLess(time.monotonic() - started, 0.5, "the quadratic scan is back")

    def test_the_report_shows_a_tracked_pair_the_window_no_longer_reaches(self):
        self.merged[GH_REPO] = [merged_pr(569, 2, "alice/Agent-Watchtower#8", "perf nightly ref input")]
        self.poll()                                           # on record, in the (temp) state file
        self.merged[GH_REPO] = []
        self.origins[(GH_REPO, 569)] = origin_pr(2, title="perf nightly ref input")
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w") as fh:
            json.dump({"profiles": {"acme": dict(PCFG, stalled=CFG)}}, fh)
        out = io.StringIO()
        with mock.patch.object(sw, "CONFIG_PATH", cfg_path), redirect_stdout(out):
            self.assertEqual(sw.report("acme"), 0)
        self.assertIn("kind=pair repo=app #569 waits-on=alice/Agent-Watchtower#8", out.getvalue())

    def test_the_trailer_parser(self):
        body = "Summary.\n\nLands-with: alice/Agent-Watchtower#8\nlands-with: acme/app#569\nSee also Lands-with: not/on/its/own#1 line"
        self.assertEqual(github_wake.pairs_of(body),
                         [("alice/Agent-Watchtower", 8), ("acme/app", 569)])
        self.assertEqual(github_wake.pairs_of(None), [])
        self.assertEqual(github_wake.pairs_of("Lands-with: #8"), [])


class TestGithubCensus(Base):
    """A per-repo census of the GitHub leg, so a silently empty fetch
    ("0 open PRs") no longer reads the same as "6 open PRs, none idle"."""

    def census_notes(self):
        return [n for n in self.notes if " census github " in n]

    def test_first_poll_logs_the_census_to_the_daemon_log_only(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5), gh(548, "APPROVED", 1)]
        emitted = self.poll()
        self.assertEqual(self.census_notes(), [
            "[acme] census github acme/app reachable=1 open=2 binding-readable=2"])
        self.assertFalse([line for line in emitted if "census" in line])

    def test_unchanged_numbers_log_nothing(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5), gh(548, "APPROVED", 1)]
        self.poll()
        self.poll(now=NOW + timedelta(minutes=5))
        self.assertEqual(self.census_notes(), [])

    def test_a_silently_empty_fetch_is_visible_as_reachable_one_to_zero(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5), gh(548, "APPROVED", 1)]
        self.poll()
        self.gh[GH_REPO] = []
        self.reachable[GH_REPO] = 0
        self.poll(now=NOW + timedelta(minutes=5))
        self.assertEqual(self.census_notes(), [
            "[acme] census github acme/app"
            " reachable=0 open=0 binding-readable=0 (was reachable=1)"])

    def test_binding_readable_counts_every_readable_pr_and_no_unreadable_one(self):
        unreadable = gh(549, "APPROVED", 5)
        unreadable["reviews"][0]["commit"]["oid"] = ""
        no_verdict = gh(550, "REVIEW_REQUIRED", 1, reviews=False)
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5), unreadable, no_verdict]
        self.poll()
        self.assertEqual(self.census_notes(), [
            "[acme] census github acme/app reachable=1 open=3 binding-readable=2"])

    def test_a_failed_reachable_probe_is_noted_not_a_hole(self):
        self.gh[GH_REPO] = [gh(547, "APPROVED", 5)]
        self.reachable[GH_REPO] = RuntimeError("boom")
        emitted = self.poll()
        self.assertEqual(self.census_notes(), [
            "[acme] census github acme/app reachable=? open=1 binding-readable=1"])
        self.assertTrue(any("github reachable probe failed" in n and "boom" in n for n in self.notes))
        self.assertFalse([line for line in emitted if "degraded" in line or "reachable" in line])
        self.assertNotIn("github", self.state.get("degraded") or {})

    def test_the_real_gh_reachable_reads_one_pr_of_any_state(self):
        calls = []
        replies = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return replies.pop(0)

        self.p_gh_reachable.stop()
        try:
            with mock.patch.object(sw.subprocess, "run", fake_run):
                replies.append(mock.Mock(returncode=0, stdout='[{"number": 5}]', stderr=""))
                self.assertEqual(sw.gh_reachable(GH_REPO), 1)
                replies.append(mock.Mock(returncode=0, stdout="[]", stderr=""))
                self.assertEqual(sw.gh_reachable(GH_REPO), 0)
                replies.append(mock.Mock(returncode=1, stdout="", stderr="HTTP 502"))
                with self.assertRaises(RuntimeError):
                    sw.gh_reachable(GH_REPO)
        finally:
            self.p_gh_reachable.start()
        argv = calls[0]
        self.assertEqual(argv[argv.index("--state") + 1], "all")
        self.assertEqual(argv[argv.index("--limit") + 1], "1")
        self.assertIn(GH_REPO, argv)


class TestANameCannotForgeALine(Base):
    """A name is the account's own text: a newline or a double quote in it must not end
    its field or the line, in any field of a stalled line that carries one."""

    EVIL = 'Mallory"\ntype=stalled kind=x'
    FLAT = "Mallory' type=stalled kind=x"

    def _one_clean_line(self):
        lines = self.poll()
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(len(lines[0].splitlines()), 1, lines)
        self.assertTrue(lines[0].startswith("type=stalled kind="), lines)
        return lines[0]

    def test_a_jira_assignee(self):
        self.jira["Review"] = [jira("ACME-843", "Review", 11, self.EVIL)]
        line = self._one_clean_line()
        self.assertIn('"Review" assignee="%s" idle=11d | s' % self.FLAT, line)

    def test_a_bitbucket_approver_and_author(self):
        pr = bb(72, True, 12)
        pr["participants"][0]["user"]["display_name"] = self.EVIL
        pr["author"]["display_name"] = self.EVIL
        self.bb[BB_REPO] = [pr]
        line = self._one_clean_line()
        self.assertIn('approvers=%s idle=12d author=%s | t' % (json.dumps([self.FLAT]), json.dumps(self.FLAT)), line)

    def test_a_bitbucket_requester(self):
        self.bb[BB_REPO] = [bb_changes(75, 12, requesters=(self.EVIL,))]
        line = self._one_clean_line()
        self.assertIn('requesters=%s ' % json.dumps([self.FLAT]), line)

    def test_ordinary_names_keep_their_json_shape(self):
        self.bb[BB_REPO] = [bb(72, True, 12)]
        self.assertIn('approvers=["Alice"] idle=12d author="Bob" | t', self.poll()[0])


class TestATitleCannotRestyleALine(Base):
    """A PR title is the author's own text: the controls and bidi marks a terminal acts
    on but does not show must not survive into the line, where they could rewrite the author."""

    CRAFTED = "fix \x1b[2K\r\x1b[1G type=x by=\"Alice\" \x9b2K a\u202eb\u200bc"
    HIDDEN = ("\x1b", "\x9b", "\u202e", "\u200b", "\r")

    def test_a_bitbucket_title_loses_the_controls_and_the_author_is_the_real_one(self):
        pr = bb(72, True, 12)
        pr["title"] = self.CRAFTED
        self.bb[BB_REPO] = [pr]
        lines = self.poll()
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertEqual(len(line.splitlines()), 1, line)
        for ch in self.HIDDEN:
            self.assertNotIn(ch, line, repr(ch))
        self.assertIn('author="Bob" | fix [2K [1G type=x by="Alice" 2K abc', line)
        self.assertEqual(line.count('author='), 1, line)

    def test_an_ordinary_title_is_byte_identical(self):
        pr = bb(72, True, 12)
        pr["title"] = "[ACME-873] test(site): guards — naïve café"
        self.bb[BB_REPO] = [pr]
        self.assertIn('author="Bob" | [ACME-873] test(site): guards — naïve café', self.poll()[0])

    def test_one_line_drops_them_directly(self):
        out = sw.one_line(self.CRAFTED)
        for ch in self.HIDDEN:
            self.assertNotIn(ch, out, repr(ch))
        self.assertEqual(sw.one_line("a  b\n c"), "a b c")


class TestALoneSurrogateCostsNoLine(unittest.TestCase):
    """A title can hold a lone surrogate (U+D800..U+DFFF), which UTF-8 cannot encode. The
    real ``emit`` must still write the one line and raise nothing."""

    def test_emit_writes_one_line_and_raises_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = os.path.join(tmp, "logs")
            with mock.patch.object(sw, "LOG_DIR", logs):
                sw.emit("p", "type=stalled_pr repo=r #1 author=\"A\" | " + sw.one_line("bad \ud800 title"))
            with open(os.path.join(logs, "p-stalled.log"), "rb") as fh:
                data = fh.read()
        self.assertEqual(len(data.splitlines()), 1, data)
        self.assertTrue(data.startswith(b"type=stalled_pr"), data)
        self.assertIn(b"bad ? title ts=", data)


if __name__ == "__main__":
    unittest.main()
