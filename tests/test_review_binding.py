#!/usr/bin/env python3
"""Unreadable and determinately stale bindings are distinct outcomes."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))
import _review_binding as rb  # noqa: E402


HEAD = "a3aa307c108c5a77edb5a1c6de8837bf76bebde3"
OLD = "2af295bd373e5d0d48299fc81d756259ac9a5810"
HEAD_COMMITTED = "2026-09-15T21:29:44Z"


def review(oid=HEAD, state="APPROVED", author="bob", submitted_at="2026-09-15T21:39:06Z"):
    return {
        "author": {"login": author},
        "state": state,
        "submittedAt": submitted_at,
        "commit": {"oid": oid},
    }


def commit(oid=HEAD, committed_at=HEAD_COMMITTED):
    return {"oid": oid, "committedDate": committed_at}


def pr(decision="APPROVED", reviews=None, head=HEAD, commits=None):
    return {
        "number": 21,
        "reviewDecision": decision,
        "headRefOid": head,
        "reviews": [review()] if reviews is None else reviews,
        "commits": [commit(oid=head)] if commits is None else commits,
        # The tempting field is present and empty exactly as gh 2.97.0 emits
        # it.  Production code must never consult it.
        "latestReviews": [review(oid="")],
    }


class TestGitHubReviewBinding(unittest.TestCase):
    def test_the_declared_field_set_uses_full_history_and_never_latest_reviews(self):
        self.assertIn("reviews", rb.GITHUB_PR_REVIEW_FIELDS)
        self.assertIn("headRefOid", rb.GITHUB_PR_REVIEW_FIELDS)
        self.assertIn("commits", rb.GITHUB_PR_REVIEW_FIELDS)
        self.assertNotIn("latestReviews", rb.GITHUB_PR_REVIEW_FIELDS)

    def test_full_history_wins_over_latest_reviews_empty_oid(self):
        got = rb.github_review_binding(pr())
        self.assertEqual(got["status"], rb.BOUND_AT_HEAD)
        self.assertEqual(got["bound_oid"], HEAD)
        self.assertEqual(got["reviewer"], "bob")

    def test_empty_bound_sha_is_unreadable_not_no_verdict_or_head_bound(self):
        got = rb.github_review_binding(pr(reviews=[review(oid="")]))
        self.assertEqual(got["status"], rb.BINDING_UNREADABLE)
        self.assertEqual(got["reason"], "standing review has an empty commit.oid")
        self.assertNotEqual(got["status"], rb.NO_STANDING_VERDICT)

    def test_whitespace_bound_sha_is_also_unreadable(self):
        got = rb.github_review_binding(pr(reviews=[review(oid="  ")]))
        self.assertEqual(got["status"], rb.BINDING_UNREADABLE)

    def test_a_different_nonempty_sha_is_explicitly_stale(self):
        got = rb.github_review_binding(pr(reviews=[review(oid=OLD)]))
        self.assertEqual(got["status"], rb.BOUND_TO_OTHER_HEAD)
        self.assertEqual(got["bound_oid"], OLD)
        self.assertEqual(got["head_oid"], HEAD)

    def test_the_newest_matching_full_history_review_is_the_binding(self):
        got = rb.github_review_binding(pr(reviews=[
            review(oid=HEAD, submitted_at="2026-09-15T21:39:06Z"),
            review(state="COMMENTED", submitted_at="2026-09-15T21:20:00Z"),
            review(oid=OLD, submitted_at="2026-09-15T21:05:31Z"),
        ]))
        self.assertEqual(got["status"], rb.BOUND_AT_HEAD)
        self.assertEqual(got["submitted_at"], "2026-09-15T21:39:06Z")

    def test_missing_reviews_is_unknown_when_a_standing_verdict_exists(self):
        fixture = pr()
        del fixture["reviews"]
        got = rb.github_review_binding(fixture)
        self.assertEqual(got["status"], rb.BINDING_UNREADABLE)

    def test_no_matching_history_is_unknown_not_no_approval(self):
        got = rb.github_review_binding(pr(reviews=[review(state="COMMENTED")]))
        self.assertEqual(got["status"], rb.BINDING_UNREADABLE)
        self.assertIn("no matching", got["reason"])

    def test_missing_head_author_or_timestamp_each_fail_closed(self):
        fixtures = [
            pr(head=""),
            pr(reviews=[review(author="")]),
            pr(reviews=[review(submitted_at="")]),
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.assertEqual(rb.github_review_binding(fixture)["status"],
                                 rb.BINDING_UNREADABLE)

    def test_an_invalid_or_naive_timestamp_fails_closed(self):
        for raw in ("not-a-time", "2026-09-15T21:39:06"):
            with self.subTest(raw=raw):
                got = rb.github_review_binding(pr(reviews=[review(submitted_at=raw)]))
                self.assertEqual(got["status"], rb.BINDING_UNREADABLE)

    def test_a_retargeted_review_that_predates_the_current_head_fails_closed(self):
        got = rb.github_review_binding(pr(
            reviews=[review(submitted_at="2026-09-15T19:28:12Z")],
            commits=[commit(committed_at="2026-09-16T01:27:53Z")],
        ))
        self.assertEqual(got["status"], rb.BINDING_STALE)
        self.assertNotEqual(got["status"], rb.BINDING_UNREADABLE)
        self.assertNotEqual(got["status"], rb.BOUND_TO_OTHER_HEAD)
        self.assertEqual(got["bound_oid"], HEAD)
        self.assertEqual(got["head_oid"], HEAD)
        self.assertEqual(got["reason"], "standing review predates current head commit")

    def test_a_review_at_the_same_instant_as_the_head_is_allowed(self):
        got = rb.github_review_binding(pr(
            reviews=[review(submitted_at=HEAD_COMMITTED)],
        ))
        self.assertEqual(got["status"], rb.BOUND_AT_HEAD)

    def test_missing_or_malformed_head_commit_data_fails_closed(self):
        fixtures = [
            {"commits": []},
            {"commits": ["not-a-commit"]},
            {"commits": [commit(oid=OLD)]},
            {"commits": [commit(committed_at="")]},
            {"commits": [commit(committed_at="not-a-time")]},
            {"commits": [commit(committed_at="2026-09-15T21:29:44")]},
        ]
        for kwargs in fixtures:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(rb.github_review_binding(pr(**kwargs))["status"],
                                 rb.BINDING_UNREADABLE)

        fixture = pr()
        del fixture["commits"]
        self.assertEqual(rb.github_review_binding(fixture)["status"],
                         rb.BINDING_UNREADABLE)

    def test_no_standing_verdict_is_distinct_from_unreadable_binding(self):
        fixture = pr(decision="REVIEW_REQUIRED", reviews=[])
        got = rb.github_review_binding(fixture)
        self.assertEqual(got["status"], rb.NO_STANDING_VERDICT)
        self.assertIsNone(got["bound_oid"])

    def test_changes_requested_uses_its_matching_review(self):
        got = rb.github_review_binding(pr(decision="CHANGES_REQUESTED", reviews=[
            review(state="APPROVED"),
            review(state="CHANGES_REQUESTED", author="reviewer", oid=HEAD),
        ]))
        self.assertEqual(got["status"], rb.BOUND_AT_HEAD)
        self.assertEqual(got["verdict"], "CHANGES_REQUESTED")
        self.assertEqual(got["reviewer"], "reviewer")


if __name__ == "__main__":
    unittest.main()
