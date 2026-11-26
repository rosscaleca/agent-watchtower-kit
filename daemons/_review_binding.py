"""Exact-head GitHub review bindings shared by Watchtower consumers.

``gh`` exposes ``latestReviews.commit.oid`` but, as of gh 2.97.0, fills that
OID with the empty string.  That is a particularly dangerous shape for a
defensive caller: ``if oid and oid != head`` silently treats an unreadable
binding as healthy forever.

The full ``reviews`` field has a second trap: GitHub may retarget a prior
review's exposed ``commit.oid`` onto a new head even though the review predates
that commit.  Callers must therefore also supply ``commits`` and reject that
impossible time ordering.

⚠ The trigger is ANY push, not a force-push.  Agent-Watchtower
once retargeted an approval on an ordinary merge-from-main pushed normally —
``compare`` reported ``status=ahead, behind_by=0``, so the old head was still an
ancestor and no history was rewritten.  A guard keyed on ``head_ref_force_pushed``
therefore passes the common case cleanly and wrongly, and so does a reader who
only distrusts rebased branches.  Measured alongside it: a ``CHANGES_REQUESTED``
that was the sole review on that PR sat through a push unmoved, so the retarget
tracks APPROVED state rather than recency.

Callers therefore supply the keys in :data:`GITHUB_PR_REVIEW_FIELDS` and pass
the result to :func:`github_review_binding`.  The classifier reads the
``reviews`` list it is handed — never ``latestReviews`` — and gives unreadable
data and a determinately stale binding distinct results before any SHA
comparison.  Its answer is deliberately narrow: it says whether the latest
standing verdict binds the current head.  It says nothing about draft state,
required checks, mergeability, or permission to merge.

⚠ THE ``reviews`` LIST IS THE CALLER'S, AND IN PRACTICE IT IS BOUNDED.
``verdict`` is taken from ``reviewDecision``, which GitHub computes over a PR's
ENTIRE review history; the search for a review carrying that verdict then runs
over whatever list arrived.  If a caller supplies a truncated history — every
caller does today, at 100 — and the standing verdict's review falls outside it,
the result is ``binding-unreadable``: a recorded hole, not a wrong verdict.
That is the intended direction of failure, but it is a hole rather than an
answer, so a caller wanting completeness must page the connection itself.

⚠ HOW those keys are fetched is not free, and the obvious way is
broken.  ``gh pr list --json commits`` implicitly expands each commit's
``authors(first:100)`` connection, so GitHub budgets ``--limit`` x 100 commits
x 100 authors and rejects the request on the REQUESTED LIMIT rather than on the
data: a repo with ZERO open PRs fails identically, and the cliff sits at 49.
This classifier never reads commit authors — it needs the single ``commits``
entry whose ``oid`` matches ``headRefOid``, for its ``committedDate`` alone.
Fetch accordingly (``stalled_wake.GH_PR_QUERY`` asks GraphQL for the head commit
only) and do not reach for ``--json commits`` to satisfy this contract.
"""

from datetime import datetime

#: The keys :func:`github_review_binding` reads off a PR dict.  This is a SHAPE
#: contract, not a ``gh pr list --json`` argument — passing it as one is the
#: known regression.  A caller may satisfy it however it likes provided
#: ``commits`` carries the head entry's ``oid`` and ``committedDate``.
GITHUB_PR_REVIEW_FIELDS = (
    "number",
    "title",
    "author",
    "updatedAt",
    "reviewDecision",
    "headRefOid",
    "reviews",
    "commits",
)

VERDICTS = ("APPROVED", "CHANGES_REQUESTED")

NO_STANDING_VERDICT = "no-standing-verdict"
BINDING_UNREADABLE = "binding-unreadable"
BINDING_STALE = "binding-stale"
BOUND_AT_HEAD = "bound-at-head"
BOUND_TO_OTHER_HEAD = "bound-to-other-head"


def _result(status, verdict="", reason=None, review=None, head_oid=None):
    """Return one stable, JSON-friendly result shape for every outcome."""
    review = review or {}
    author = review.get("author") if isinstance(review, dict) else None
    commit = review.get("commit") if isinstance(review, dict) else None
    return {
        "status": status,
        "verdict": verdict,
        "reviewer": author.get("login") if isinstance(author, dict) else None,
        "bound_oid": commit.get("oid") if isinstance(commit, dict) else None,
        "head_oid": head_oid,
        "submitted_at": review.get("submittedAt") if isinstance(review, dict) else None,
        "reason": reason,
    }


def github_review_binding(pr):
    """Classify the latest review carrying the PR's standing verdict.

    ``reviews`` is the full-history field returned by ``gh pr list`` and
    ``gh pr view``.  The newest matching ``submittedAt`` entry is the binding a
    caller should describe.  Missing or empty binding data is
    ``binding-unreadable`` — never equivalent to no verdict and never allowed
    to fall through to the SHA equality test.
    """
    verdict = pr.get("reviewDecision") if isinstance(pr, dict) else None
    if verdict not in VERDICTS:
        return _result(NO_STANDING_VERDICT, verdict=verdict or "")

    reviews = pr.get("reviews")
    if not isinstance(reviews, list):
        return _result(BINDING_UNREADABLE, verdict=verdict,
                       reason="reviews field is missing or malformed")
    if any(not isinstance(review, dict) for review in reviews):
        return _result(BINDING_UNREADABLE, verdict=verdict,
                       reason="reviews history contains a malformed entry")

    matching = [review for review in reviews if review.get("state") == verdict]
    if not matching:
        return _result(BINDING_UNREADABLE, verdict=verdict,
                       reason="standing verdict has no matching full-history review")

    dated = []
    for position, candidate in enumerate(matching):
        raw = candidate.get("submittedAt")
        if not isinstance(raw, str) or not raw.strip():
            return _result(BINDING_UNREADABLE, verdict=verdict, review=candidate,
                           reason="standing review has no submittedAt")
        try:
            submitted_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return _result(BINDING_UNREADABLE, verdict=verdict, review=candidate,
                           reason="standing review has an invalid submittedAt")
        if submitted_at.tzinfo is None:
            return _result(BINDING_UNREADABLE, verdict=verdict, review=candidate,
                           reason="standing review has an invalid submittedAt")
        dated.append((submitted_at, position, candidate))

    submitted_at, _, review = max(dated, key=lambda item: (item[0], item[1]))
    commit = review.get("commit")
    bound_oid = commit.get("oid") if isinstance(commit, dict) else None
    if not isinstance(bound_oid, str) or not bound_oid.strip():
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       reason="standing review has an empty commit.oid")

    head_oid = pr.get("headRefOid")
    if not isinstance(head_oid, str) or not head_oid.strip():
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       reason="PR has an empty headRefOid")

    commits = pr.get("commits")
    if not isinstance(commits, list):
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="commits field is missing or malformed")
    if any(not isinstance(candidate, dict) for candidate in commits):
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="commits history contains a malformed entry")

    matching_head = [candidate for candidate in commits if candidate.get("oid") == head_oid]
    if len(matching_head) != 1:
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="current head commit is missing or ambiguous")

    raw_committed_at = matching_head[0].get("committedDate")
    if not isinstance(raw_committed_at, str) or not raw_committed_at.strip():
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="current head commit has no committedDate")
    try:
        committed_at = datetime.fromisoformat(raw_committed_at.replace("Z", "+00:00"))
    except ValueError:
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="current head commit has an invalid committedDate")
    if committed_at.tzinfo is None:
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="current head commit has an invalid committedDate")

    # GitHub can rewrite reviews[].commit.oid after a force-push.  An approval
    # cannot genuinely bind a commit that did not exist when it was submitted,
    # even when the two exposed OIDs now compare equal.  This is a determinate
    # stale binding, not an unreadable-data hole: consumers must surface the PR
    # rather than suppressing it as an incomplete inventory.
    if submitted_at < committed_at:
        return _result(BINDING_STALE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="standing review predates current head commit")

    author = review.get("author")
    reviewer = author.get("login") if isinstance(author, dict) else None
    if not isinstance(reviewer, str) or not reviewer.strip():
        return _result(BINDING_UNREADABLE, verdict=verdict, review=review,
                       head_oid=head_oid, reason="standing review has no author login")

    # This comparison is intentionally below the explicit empty-value
    # and impossible-time branches.  Moving it above them turns unreadable or
    # retargeted review data into a healthy result.
    status = BOUND_AT_HEAD if bound_oid == head_oid else BOUND_TO_OTHER_HEAD
    return _result(status, verdict=verdict, review=review, head_oid=head_oid)
