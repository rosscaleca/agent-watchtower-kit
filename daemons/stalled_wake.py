#!/usr/bin/env python3
"""Watchtower stalled daemon — polls for the ABSENCE of change.

Every other source here emits on an EVENT. An artefact that is complete,
correct and waiting emits nothing, so it falls out of every round until a
human happens to ask: one PR sat finished and unmerged for 16
days; another carried a stale CHANGES_REQUESTED for five; both
were found by a manual sweep, not by any watcher. You cannot
read an instrument that never writes a line — so this daemon writes one.

Per profile it computes the STALLED set, on a slow cadence:

  pr    an open GitHub PR carrying a standing verdict (APPROVED or
        CHANGES_REQUESTED) with no activity for ``days`` days — the verdict is
        the discriminator between "stalled" and "genuinely in progress";
  bb    an open Bitbucket PR with at least one approval or one changes-requested
        verdict, idle ``days`` days;
  jira  a ticket in Review or In Progress whose work-state clock has not moved
        for ``days`` days. Review starts at the transition into Review; In
        Progress starts at the transition into that state and is reset by a
        later assignee change. An Epic also takes the latest such activity from
        each direct child;
  pair  a PR merged within ``pair_days`` whose body declares ``Lands-with:
        owner/repo#N`` and whose companion is still unmerged — a live window
        where a feature exists and the thing that makes it safe does not
       . No idle threshold: a half-landed pair is stalled from the
        moment it lands, and ``idle`` counts from that merge.

It emits ``type=stalled`` ONLY when an item enters the set, ``type=stalled_cleared``
when it leaves, and ``type=stalled_still`` once per ``reemit_hours`` for anything
still in it — so the log is never silent about a stall for more than a day, and
never noisy about one either. A fresh install's first cycle emits everything
already stalled: that set is the finding, not history to be seeded away. A
restart with state on disk re-announces nothing.

``idle`` is measured from the VERDICT for a PR — the latest APPROVED /
CHANGES_REQUESTED review on GitHub, the latest approval or changes-requested
participant on Bitbucket — not
from the artefact's last touch: a bot comment or an author's push would
otherwise reset the clock and hide a standing verdict forever. Jira follows the same rule: comments, labels, links, summary edits and
other bookkeeping never reset a ticket's semantic clock.

Report-only by construction: nothing here nudges, transitions or comments —
a mechanism that acts on this list inherits every failure listed here. The
seat decides. The drain surfaces each line once at turn-end and
``just unactioned`` lists it until acted; ``just fleet`` covers this daemon's
liveness like any other.

A broken fetch is a ``degraded`` line and NEVER an empty set: items
from a source that did not answer this cycle are neither added nor cleared.
That is the lesson in code — a sweep that prints "none" identically
for a clean board and a broken query is not a sweep.

Per profile config::

    "stalled": {"days": 3, "reemit_hours": 24}

The repos, projects and credentials come from the profile's own ``github`` /
``jira`` / ``bitbucket`` blocks — nothing is repeated here, and a profile with
no ``stalled`` block is not swept. ``--report PROFILE`` prints the current set
and exits, touching nothing (the on-demand form of the same sweep).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import _build  # sibling module; the daemons dir is sys.path[0] when run as a script
import _degraded  # a cycle that completes with a hole in it must say so
import _fields  # a person's name is flattened before it reaches an event line
import _review_binding  # exact-head verdicts from full review history
import bitbucket_wake  # the Bitbucket client and the approvals rule, shared
import github_wake  # the Lands-with trailer parser and the companion check, shared
import jira_wake  # the Jira client, shared

#: What bytes this process is running, logged on start and saved with the state.
BUILD = _build.describe(__file__)

CONFIG_PATH = os.path.expanduser("~/.config/watchtower/config.json")
STATE_DIR = os.path.expanduser("~/.local/state/watchtower/stalled")
LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
#: Slow by nature — stalls are measured in days — but the watchdog reads a
#: watermark older than ``stale_minutes`` (default 10) as a dead source, so the
#: cycle runs, and saves, well inside that.
POLL_SECS = 300
DEFAULT_DAYS = 3
DEFAULT_REEMIT_HOURS = 24
DEFAULT_PAIR_DAYS = 14
VERDICTS = _review_binding.VERDICTS
PLACEHOLDER = "PASTE-TOKEN-HERE"

os.environ["PATH"] = ":".join([
    os.path.expanduser("/etc/profiles/per-user/%s/bin" % os.environ.get("USER", "")),
    "/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
GH = shutil.which("gh")


def note(line):
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # As github_wake does: a line the stream cannot encode (a lone surrogate, or
        # non-ASCII on a stream that is not UTF-8) is printed as its ascii(), which every stream can take.
        print(ascii(line), flush=True)


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(profile, line):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "%s-stalled.log" % profile), "a", errors="replace") as fh:
        fh.write("%s ts=%s\n" % (line, now_ts()))


def one_line(s, cap=100):
    """A title or summary as part of one event line: whitespace collapsed, and the controls and bidi
    marks a terminal acts on but does not show dropped (:func:`_fields.unshown_dropped`),
    capped."""
    s = _fields.unshown_dropped(re.sub(r"\s+", " ", s or "")).strip()
    return (s[: cap - 1] + "…") if len(s) > cap else s


_FRACTION = re.compile(r"\.(\d+)(?=[+-]|$)")
_BARE_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_ts(raw):
    """A timestamp from any of the three APIs → aware UTC datetime, or ``None``.

    The daemons run under /usr/bin/python3 (3.9): ``fromisoformat`` there takes
    no ``Z``, only 3- or 6-digit fractions, and no ``-0700`` without a colon.
    GitHub sends ``Z``, Bitbucket six digits, Jira ``.305-0700`` — normalise all
    three before parsing rather than trusting whichever one was tested.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # Jira's bulk changelog examples use epoch seconds; accepting
        # milliseconds too keeps this boundary tolerant at the API edge.
        value = float(raw)
        if abs(value) >= 100000000000:
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text)
    text = _BARE_OFFSET.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def idle_days(raw, now):
    then = parse_ts(raw)
    if then is None:
        return None
    return max(0, int((now - then).total_seconds() // 86400))


# --------------------------------------------------------------------------
# the three fetches — each swallowed into the cycle's holes, never into silence
# --------------------------------------------------------------------------

#: codex on this branch (P2): a page that is exactly full may be truncated, and a
#: truncated inventory read as "answered" would clear a tracked stall that merely
#: fell off the page. GitHub paginates inside `gh pr list`; Bitbucket and Jira
#: are walked page by page here; hitting the cap in any of them is a hole.
GH_LIMIT = 500
PAGE_CAP = 20


class Truncated(RuntimeError):
    """The inventory could not be fetched in full — treat as NOT answered."""


#: Why this asks GraphQL directly instead of `gh pr list --json`.
#: `--json commits` implicitly expands each commit's `authors(first:100)`
#: connection, so GitHub budgets `--limit` x 100 commits x 100 authors and
#: rejects the request on the REQUESTED LIMIT, not on the data: at limit 50 the
#: budget is 510,050 against a 500,000 ceiling, and a repo with ZERO open PRs
#: fails identically. The classifier never reads commit authors — it needs the
#: head commit's `committedDate` and nothing else (`_review_binding` lines
#: 114-147) — so this asks for the head commit alone and never opens that
#: connection. Capping the limit under the cliff instead was rejected
#: deliberately: the defect is invisible to every caller below 50, which is how
#: it shipped past a review that did run the query. A budget you sit under is
#: re-broken by the next field; a connection you never request is not.
GH_PR_PAGE = 100       # a GraphQL connection returns at most 100 nodes per page

#: Only the head commit, because the classifier only ever reads the entry whose
#: `oid` matches `headRefOid` — never because depth is expensive.
#: ⚠ Raising this does NOT re-open the budget: measured against live GitHub,
#: `commits(last:100)` with no nested `authors` at page size 100 returns data
#: (≈100 x (1 + 100 + 100) = 20,100 nodes against a 500,000 ceiling). The cost
#: was the `authors` connection nested inside `commits`, never the commit depth.
#: So if GitHub ever returns a page whose head is not the last node, depth 3-5
#: is affordable and is the remedy — nothing here argues against it.
GH_COMMIT_DEPTH = 1

#: ⚠ BOUNDED, and say so: the newest 100 reviews, NOT the full history.
#: `verdict` comes from `reviewDecision`, which GitHub computes over every
#: review a PR has; `github_review_binding` then looks for a review carrying
#: that verdict inside the list handed to it.  On a PR with more than 100
#: reviews whose standing verdict falls outside the newest 100, that search
#: misses and the PR is classified `binding-unreadable` — a recorded hole and
#: `ok = False` for the github source, not a wrong answer.  Latent today (no
#: repo here is near 100 reviews on one PR) and NOT introduced by the GraphQL move: the
#: `gh pr list --json reviews` this replaced was a 100-wide connection too —
#: the 510,050-node budget above only closes with reviews_width = 100, though
#: that is inference from the arithmetic, not a test against such a PR.
#: Raising this trades directly against the node budget; paging the reviews
#: connection is the real fix and belongs in its own change (raised in review).
GH_REVIEW_DEPTH = 100

GH_PR_QUERY = """
query($owner:String!, $repo:String!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    pullRequests(states:OPEN, first:%d, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title updatedAt reviewDecision headRefOid isDraft
        author { login }
        reviews(last:%d) { nodes { state submittedAt commit { oid } author { login } } }
        commits(last:%d) { nodes { commit { oid committedDate } } }
      }
    }
  }
}
""" % (GH_PR_PAGE, GH_REVIEW_DEPTH, GH_COMMIT_DEPTH)


def _gh_pr_node(node):
    """One GraphQL PR node in the flat shape `_review_binding` already reads.

    `gh pr list --json` flattens both connections for you; asking GraphQL
    directly means doing it here. `commits` in particular arrives as
    PullRequestCommit wrappers (`{"commit": {...}}`) and the classifier expects
    the inner commit's own keys.
    """
    reviews = ((node.get("reviews") or {}).get("nodes")) or []
    commits = ((node.get("commits") or {}).get("nodes")) or []
    return {
        "number": node.get("number"),
        "title": node.get("title"),
        "author": node.get("author"),
        "updatedAt": node.get("updatedAt"),
        "reviewDecision": node.get("reviewDecision"),
        "headRefOid": node.get("headRefOid"),
        "isDraft": node.get("isDraft"),
        # ⚠ NOTHING IS FILTERED HERE, deliberately. `_review_binding` already
        # refuses a history containing a malformed entry (lines 108-111 for
        # reviews, 150-152 for commits) — dropping one silently DEFEATS that
        # guard, and the two fields fail in opposite directions when it does:
        #   commits  — drop the head entry and nothing matches headRefOid, so
        #              the classifier still reports a hole. Fail-closed.
        #   reviews  — drop the NEWEST entry carrying the standing verdict and
        #              an older one survives, so `matching` is non-empty and the
        #              classifier binds to the older review: a confident
        #              `bound-at-head` naming the WRONG reviewer and the WRONG
        #              submittedAt. Measured, not reasoned about.
        # A partial GitHub response (a null in `nodes`) is exactly how that
        # arrives. So pass the entries through and let the module whose job is
        # refusing to guess do the refusing.
        "reviews": list(reviews),
        "commits": [c.get("commit") if isinstance(c, dict) else c for c in commits],
    }


def gh_prs(repo):
    """Open PRs with GitHub's decision and full, head-bound review history.

    Pages the connection explicitly, which `gh pr list` used to do for us. The
    ``Truncated`` contract is deliberately UNCHANGED: filling ``GH_LIMIT`` is a
    hole, exactly as before. ``pageInfo.hasNextPage`` would let us answer that
    question directly rather than by inference — and so drop the false positive
    a source holding exactly ``GH_LIMIT`` PRs raises today — but loosening a
    guard is not this fix's business, so the conservative form stands and the
    refinement is left to its own change.
    """
    if not GH:
        raise RuntimeError("gh not found on PATH")
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise RuntimeError("github repo %r is not owner/name" % (repo,))
    prs = []
    cursor = None
    seen_cursors = set()
    for _page in range(PAGE_CAP):
        # ⚠ `-f`, never `-F`: -F type-converts, so an owner or repo literally
        # named `2024`, `true`, `false` or `null` — all legal on GitHub — would
        # be sent as a JSON number/boolean/null against a `String!` variable and
        # rejected ("Could not coerce value 2024 to String") on EVERY cycle,
        # taking the github leg dark. That is the symptom this whole fix exists
        # to remove, and the `gh pr list --repo` path it replaces could not fail
        # this way. Same for the cursor, which is an opaque base64 string.
        argv = [GH, "api", "graphql", "-f", "owner=" + owner, "-f", "repo=" + name]
        if cursor:
            argv += ["-f", "cursor=" + cursor]
        argv += ["-f", "query=" + GH_PR_QUERY]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            raise RuntimeError("github pr query %s failed: %s" % (repo, one_line(r.stderr, 160)))
        payload = json.loads(r.stdout)
        if payload.get("errors"):
            raise RuntimeError("github pr query %s failed: %s"
                               % (repo, one_line(json.dumps(payload["errors"]), 160)))
        repository = (payload.get("data") or {}).get("repository")
        if not isinstance(repository, dict):
            raise RuntimeError("github pr query %s: response has no repository" % repo)
        connection = repository.get("pullRequests")
        if not isinstance(connection, dict):
            raise Truncated("github pr query %s: response has no PR inventory" % repo)
        nodes = connection.get("nodes")
        if not isinstance(nodes, list) or any(not isinstance(n, dict) for n in nodes):
            raise Truncated("github pr query %s: PR inventory is missing or malformed" % repo)
        prs.extend(_gh_pr_node(node) for node in nodes)
        if len(prs) >= GH_LIMIT:
            raise Truncated("github pr query %s returned %d open PRs — inventory truncated"
                            % (repo, GH_LIMIT))
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise Truncated("github pr query %s: page has no pageInfo" % repo)
        if not page_info.get("hasNextPage"):
            return prs
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor.strip():
            raise Truncated("github pr query %s: more pages advertised with no endCursor" % repo)
        if cursor in seen_cursors:
            raise Truncated("github pr query %s: repeated endCursor — pagination made no progress"
                            % repo)
        seen_cursors.add(cursor)
    raise Truncated("github pr query %s: more than %d pages — inventory truncated"
                    % (repo, PAGE_CAP))


BB_FIELDS = ",".join([
    "values.id", "values.title", "values.updated_on", "values.author.display_name",
    "values.participants.approved", "values.participants.participated_on",
    "values.participants.state",    # "changes_requested" holds a PR too
    "values.participants.user.display_name",
    "values.participants.user.uuid", "values.participants.user.account_id",
])


def gh_merged_prs(repo, since):
    """PRs merged since ``since`` (aware UTC), with bodies — the pool a half-landed
    pair is DISCOVERED in (a pair once found outlives it; see ``compute``).

    Filtered server-side with the ``merged:>=DATE`` search qualifier. codex pass 1
    on this branch (P1): ``gh pr list`` orders by CREATION, not merge — a long-lived
    PR merged yesterday lists after last month's quick ones (this repo's #2: opened
    08-23, merged 09-08, listed last) — so the first version's "stop at the first
    merge older than the window" dropped exactly the recent merge it existed to
    find, and marked the pool answered. The qualifier is day-granular; the exact
    cut is applied below as a FILTER, never as a stop. A full page is a hole."""
    if not GH:
        raise RuntimeError("gh not found on PATH")
    r = subprocess.run([GH, "pr", "list", "--repo", repo, "--state", "merged",
                        "--search", "merged:>=%s" % since.strftime("%Y-%m-%d"),
                        "--limit", str(GH_LIMIT), "--json", "number,title,body,mergedAt"],
                       capture_output=True, text=True, timeout=45)
    if r.returncode != 0:
        raise RuntimeError("gh pr list --state merged %s failed: %s" % (repo, one_line(r.stderr, 160)))
    prs = json.loads(r.stdout)
    if len(prs) >= GH_LIMIT:
        raise Truncated("gh pr list --state merged %s: %d PRs inside the window — truncated"
                        % (repo, GH_LIMIT))
    out = []
    for pr in prs:
        when = parse_ts(pr.get("mergedAt"))
        if when is not None and when >= since:
            out.append(pr)
    return out


def gh_reachable(repo):
    """Returns 1 if the repo has ever had any PR, 0 if none. The value is
    monotonic (closed PRs do not un-exist), so a 1 -> 0 change means a broken
    fetch. It says nothing about whether the right objects came back."""
    if not GH:
        raise RuntimeError("gh not found on PATH")
    r = subprocess.run([GH, "pr", "list", "--repo", repo, "--state", "all", "--limit", "1", "--json", "number"],
                       capture_output=True, text=True, timeout=45)
    if r.returncode != 0:
        raise RuntimeError("gh pr list --state all %s failed: %s" % (repo, one_line(r.stderr, 160)))
    return 1 if json.loads(r.stdout) else 0


def gh_pr(repo, number):
    """One PR by number (``merged_at``, ``title``, ``body``): how a tracked or
    pending pair is re-read — the body is re-read on purpose, it is the
    declaration. Raises on a failed fetch."""
    return github_wake.gh_api("repos/%s/pulls/%d" % (repo, int(number)))


def pair_key(repo, number, c_repo, c_num):
    """``pair:<owner/repo>#<n>-><owner/repo>#<m>`` — the COMPANION is part of the
    key. codex pass 1 (P2): keyed by the merged half alone, two trailers on one PR
    wrote one item, and when the surviving companion merged the other slid in under
    the same key — the second window never got a line of its own."""
    return "pair:%s#%s->%s#%s" % (repo, number, c_repo, c_num)


_PAIR_KEY = re.compile(r"^pair:([^#\s]+)#(\d+)->([^#\s]+)#(\d+)$")


def parse_pair_key(key):
    """``(repo, number, companion_repo, companion_number)`` or ``None``."""
    m = _PAIR_KEY.match(key or "")
    return (m.group(1), int(m.group(2)), m.group(3), int(m.group(4))) if m else None


def pair_line(repo, number, c_repo, c_num, merged_at, title, now):
    idle = idle_days(merged_at, now)
    return 'kind=pair repo=%s #%s waits-on=%s#%d idle=%sd | %s' % (
        repo.split("/")[-1], number, c_repo, c_num, "?" if idle is None else idle,
        one_line(title or ""))

BB_PAGELEN = 50


def _bb_path(repo, page):
    return ("/repositories/%s/pullrequests?state=OPEN&pagelen=%d&page=%d&fields=%s"
            % (repo, BB_PAGELEN, page, BB_FIELDS + ",next,size"))


def _bb_confirm_end(repo, page, cwd):
    """A full page came back with no ``next`` link. Read the page after it: an
    empty page with no ``next`` of its own confirms the inventory ended there.
    Anything else — a value, a link, a failed or malformed read — is a hole."""
    try:
        resp = bitbucket_wake.bkt_api(_bb_path(repo, page), cwd)
    except Exception as e:
        raise Truncated("bkt pullrequests %s: a full page of %d with no `next` link and the page "
                        "after it could not be read (%s) — inventory truncated"
                        % (repo, BB_PAGELEN, e))
    if not isinstance(resp, dict) or resp.get("values") or resp.get("next"):
        raise Truncated("bkt pullrequests %s: a full page of %d with no `next` link, and the page "
                        "after it is not empty — inventory truncated or the API shape changed"
                        % (repo, BB_PAGELEN))


def bb_prs(repo, cwd):
    """Every open PR of ``repo`` — walked page by page; a page cap is a hole."""
    if not bitbucket_wake.BKT:
        raise RuntimeError("bkt not found on PATH")
    out = []
    size = None
    for page in range(1, PAGE_CAP + 1):
        resp = bitbucket_wake.bkt_api(_bb_path(repo, page), cwd)
        values = resp.get("values") or []
        out.extend(values)
        page_size = resp.get("size")
        if isinstance(page_size, int) and not isinstance(page_size, bool):
            size = page_size if size is None else max(size, page_size)
        # codex pass 2 on this branch (P2): a page shorter than pagelen can still
        # carry `next` — the continuation link is the completion criterion, not
        # the page size. Stopping early read a partial inventory as complete.
        if not resp.get("next"):
            if len(values) >= BB_PAGELEN:
                # `next` surviving the fields= filter is
                # the documented behaviour, not a verified one. A full page with
                # no link must be a loud hole, never a complete inventory — that
                # is the one direction this daemon must never take. But a
                # repo with exactly pagelen open PRs ends on exactly that page, so
                # the page after it is read once: empty means the inventory is
                # whole; anything else, or a failed read, is the hole.
                _bb_confirm_end(repo, page + 1, cwd)
            if size is not None and size > len(out):
                raise Truncated("bkt pullrequests %s: the API counts %d open PRs and %d were read — "
                                "inventory truncated" % (repo, size, len(out)))
            return out
    raise Truncated("bkt pullrequests %s: more than %d pages — inventory truncated"
                    % (repo, PAGE_CAP))


JIRA_FIELDS = "summary,created,status,assignee,issuetype,parent"
JIRA_PAGE = 100
JIRA_CHANGELOG_PAGE = 1000
JIRA_CHANGELOG_BATCH = 1000
JIRA_EPIC_BATCH = 50


def _jira_page_issues(resp):
    """Validate one search page before its absence can mean completion."""
    if not isinstance(resp, dict):
        raise Truncated("jira search: response is not a JSON object")
    warnings = resp.get("warnings") or resp.get("warningMessages")
    if warnings:
        raise Truncated("jira search: API warned that the result may be incomplete: %s"
                        % one_line(str(warnings), 160))
    issues = resp.get("issues")
    if not isinstance(issues, list):
        raise Truncated("jira search: response has no issue inventory")
    return issues


def jira_search(cfg, jql):
    """Every issue matching ``jql`` — the first page through the shared client (so
    the legacy fallback is decided once), continuation by ``nextPageToken`` on
    ``/search/jql`` or ``startAt`` on the legacy ``/search``; a page cap is a hole."""
    client = jira_wake.Jira(cfg)
    resp = client.search(jql, JIRA_FIELDS, JIRA_PAGE)
    issues = list(_jira_page_issues(resp))
    seen_tokens = set()
    pages = 1
    while True:
        token = resp.get("nextPageToken")
        if token:
            if token in seen_tokens:
                raise Truncated("jira search: repeated nextPageToken — pagination made no progress")
            if pages >= PAGE_CAP:
                raise Truncated("jira search: more than %d pages — inventory truncated" % PAGE_CAP)
            seen_tokens.add(token)
            resp = client.get("/rest/api/3/search/jql", {
                "jql": jql, "fields": JIRA_FIELDS, "maxResults": JIRA_PAGE, "nextPageToken": token})
        elif client.search_legacy and resp.get("total") is not None:
            page_issues = _jira_page_issues(resp)
            start = int(resp.get("startAt") or 0) + len(page_issues)
            if start >= int(resp.get("total") or 0) or not resp.get("issues"):
                if start < int(resp.get("total") or 0):
                    raise Truncated("jira search: empty legacy page before advertised total")
                return issues
            if pages >= PAGE_CAP:
                raise Truncated("jira search: more than %d pages — inventory truncated" % PAGE_CAP)
            resp = client.get("/rest/api/3/search", {
                "jql": jql, "fields": JIRA_FIELDS, "maxResults": JIRA_PAGE, "startAt": start})
        else:
            if client.search_legacy and resp.get("total") is None:
                raise Truncated("jira search: legacy response has no total")
            if not client.search_legacy and resp.get("isLast") is False:
                raise Truncated("jira search: non-final page has no nextPageToken")
            return issues
        page_issues = _jira_page_issues(resp)
        issues.extend(page_issues)
        pages += 1


def jira_changelogs(cfg, issues):
    """Complete status/assignee histories, keyed by Jira's numeric issue id.

    The bulk endpoint can omit an issue with no history and can repeat an issue
    across pages, so every requested id is initialized and returned groups are
    extended. Any ambiguous pagination is a hole, never an empty history.
    """
    unique = {}
    for issue in issues:
        issue_id = issue.get("id")
        key = issue.get("key")
        if issue_id in (None, "") or not key:
            raise Truncated("jira changelog: issue inventory is missing id or key")
        issue_id = str(issue_id)
        if issue_id in unique and unique[issue_id] != key:
            raise Truncated("jira changelog: issue id %s maps to more than one key" % issue_id)
        unique[issue_id] = key

    histories = {issue_id: [] for issue_id in unique}
    client = jira_wake.Jira(cfg)
    issue_ids = sorted(unique, key=lambda value: (len(value), value))
    for offset in range(0, len(issue_ids), JIRA_CHANGELOG_BATCH):
        batch = issue_ids[offset:offset + JIRA_CHANGELOG_BATCH]
        allowed = set(batch)
        token = None
        seen_tokens = set()
        pages = 0
        while True:
            body = {"issueIdsOrKeys": batch, "fieldIds": ["status", "assignee"],
                    "maxResults": JIRA_CHANGELOG_PAGE}
            if token:
                body["nextPageToken"] = token
            resp = client.post("/rest/api/3/changelog/bulkfetch", body)
            pages += 1
            groups = resp.get("issueChangeLogs")
            if not isinstance(groups, list):
                raise Truncated("jira changelog: bulk response has no issueChangeLogs")
            for group in groups:
                issue_id = str(group.get("issueId", ""))
                if issue_id not in allowed:
                    raise Truncated("jira changelog: response named unrequested issue %s" % issue_id)
                changes = group.get("changeHistories")
                if not isinstance(changes, list):
                    raise Truncated("jira changelog: issue %s has no history list" % unique[issue_id])
                histories[issue_id].extend(changes)
            next_token = resp.get("nextPageToken")
            if not next_token:
                break
            if next_token == token or next_token in seen_tokens:
                raise Truncated("jira changelog: repeated nextPageToken — pagination made no progress")
            if pages >= PAGE_CAP:
                raise Truncated("jira changelog: more than %d pages — history truncated" % PAGE_CAP)
            seen_tokens.add(next_token)
            token = next_token
    return histories


def jira_is_epic(issue):
    issue_type = (issue.get("fields") or {}).get("issuetype")
    if not isinstance(issue_type, dict):
        raise Truncated("jira issue %s has no issue type" % (issue.get("key") or "?"))
    level = issue_type.get("hierarchyLevel")
    if level is not None:
        try:
            return int(level) == 1
        except (TypeError, ValueError):
            raise Truncated("jira issue %s has an invalid hierarchy level" % (issue.get("key") or "?"))
    # Compatibility for older search responses that predate hierarchyLevel.
    name = issue_type.get("name")
    if not isinstance(name, str) or not name:
        raise Truncated("jira issue %s cannot be classified by issue type" % (issue.get("key") or "?"))
    return name.casefold() == "epic"


def jira_children(cfg, epics):
    """Fetch every direct child of ``epics``; Jira's ``parent`` field is the
    supported JQL relation for both company- and team-managed projects."""
    out = []
    keys = [issue.get("key") for issue in epics]
    if any(not key for key in keys):
        raise Truncated("jira epic inventory contains an issue without a key")
    for offset in range(0, len(keys), JIRA_EPIC_BATCH):
        batch = keys[offset:offset + JIRA_EPIC_BATCH]
        out.extend(jira_search(cfg, "parent in (%s) ORDER BY key" % ",".join(batch)))
    return out


def _jira_field(item):
    value = item.get("fieldId") or item.get("field") or ""
    return str(value).casefold()


def _jira_status_matches(item, current):
    current_id = current.get("id")
    target_id = item.get("to")
    if current_id not in (None, "") and target_id not in (None, ""):
        return str(current_id) == str(target_id)
    target_name = item.get("toString")
    current_name = current.get("name")
    return (isinstance(target_name, str) and isinstance(current_name, str)
            and target_name.casefold() == current_name.casefold())


def jira_semantic_time(issue, histories):
    """The latest change that can alter the issue's present work state.

    Status entry is the base. While currently In Progress, a later assignment
    change is work-state evidence too. All other Jira touches are ignored.
    """
    key = issue.get("key") or "?"
    fields = issue.get("fields") or {}
    current = fields.get("status")
    if not isinstance(current, dict) or not current.get("name"):
        raise Truncated("jira issue %s has no current status" % key)
    created = parse_ts(fields.get("created"))
    if created is None:
        raise Truncated("jira issue %s has an invalid created timestamp" % key)

    status_changes = []
    assignee_changes = []
    if not isinstance(histories, list):
        raise Truncated("jira issue %s has no complete changelog" % key)
    for history in histories:
        items = history.get("items")
        if not isinstance(items, list):
            raise Truncated("jira issue %s has a malformed changelog entry" % key)
        relevant = [item for item in items if _jira_field(item) in ("status", "assignee")]
        if not relevant:
            continue
        changed = parse_ts(history.get("created"))
        if changed is None:
            raise Truncated("jira issue %s has an invalid semantic-change timestamp" % key)
        for item in relevant:
            if _jira_field(item) == "status":
                status_changes.append((changed, item))
            else:
                assignee_changes.append(changed)

    if status_changes:
        entered, latest = max(status_changes, key=lambda pair: pair[0])
        if not _jira_status_matches(latest, current):
            raise Truncated("jira issue %s changed while its inventory was being read" % key)
    else:
        entered = created

    if str(current.get("name")).casefold() == "in progress":
        later_assignments = [changed for changed in assignee_changes if changed > entered]
        if later_assignments:
            entered = max([entered] + later_assignments)
    return entered


def jira_stalled(cfg, projects, cutoff, now, holes):
    """Return ``(items, complete)`` for Jira.

    Positively readable findings still surface when one issue is malformed,
    but ``complete`` is false so reconciliation cannot clear any tracked Jira
    finding on an incomplete cycle.
    """
    jql = 'project in (%s) AND status in (Review, "In Progress") ORDER BY key' % ",".join(projects)
    try:
        candidates = jira_search(cfg, jql)
    except Exception as e:
        note("note: [stalled] jira candidate search failed: %s" % e)
        holes.record("jira", "candidate search: %s" % e)
        return {}, False

    complete = True
    epics = []
    epic_ids = {}
    epic_keys = {}
    for issue in candidates:
        try:
            if jira_is_epic(issue):
                epics.append(issue)
                epic_ids[str(issue.get("id"))] = issue.get("key")
                epic_keys[issue.get("key")] = issue.get("key")
        except Exception as e:
            note("note: [stalled] jira issue-type read failed: %s" % e)
            holes.record("jira", str(e))
            complete = False

    children_known = True
    try:
        children = jira_children(cfg, epics) if epics else []
    except Exception as e:
        note("note: [stalled] jira epic-child search failed: %s" % e)
        holes.record("jira", "epic children: %s" % e)
        children = []
        children_known = False
        complete = False

    children_by_epic = {issue.get("key"): [] for issue in epics}
    if children_known:
        for child in children:
            parent = (child.get("fields") or {}).get("parent")
            parent_id = str(parent.get("id")) if isinstance(parent, dict) and parent.get("id") is not None else None
            parent_key = parent.get("key") if isinstance(parent, dict) else None
            epic_key = epic_ids.get(parent_id) or epic_keys.get(parent_key)
            if not epic_key:
                detail = "jira child %s has no matching Epic parent" % (child.get("key") or "?")
                note("note: [stalled] %s" % detail)
                holes.record("jira", detail)
                complete = False
                children_known = False
                break
            children_by_epic[epic_key].append(child)

    all_issues = {}
    for issue in candidates + children:
        issue_id = issue.get("id")
        if issue_id not in (None, ""):
            all_issues.setdefault(str(issue_id), issue)
        else:
            all_issues["missing:%s" % (issue.get("key") or len(all_issues))] = issue
    try:
        histories = jira_changelogs(cfg, list(all_issues.values()))
    except Exception as e:
        note("note: [stalled] jira changelog fetch failed: %s" % e)
        holes.record("jira", "semantic history: %s" % e)
        return {}, False

    semantic = {}
    for issue_id, issue in all_issues.items():
        try:
            semantic[issue_id] = jira_semantic_time(issue, histories.get(issue_id))
        except Exception as e:
            note("note: [stalled] jira semantic clock failed for %s: %s" % (issue.get("key", "?"), e))
            holes.record("jira", str(e))
            complete = False

    out = {}
    for issue in candidates:
        key = issue.get("key")
        issue_id = str(issue.get("id"))
        since = semantic.get(issue_id)
        if not key or since is None:
            complete = False
            continue
        try:
            epic = jira_is_epic(issue)
        except Exception:
            continue
        if epic:
            if not children_known:
                continue
            child_times = []
            uncertain = False
            for child in children_by_epic.get(key, []):
                child_time = semantic.get(str(child.get("id")))
                if child_time is None:
                    uncertain = True
                    break
                child_times.append(child_time)
            if uncertain:
                continue
            if child_times:
                since = max([since] + child_times)
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name")
        if status not in ("Review", "In Progress"):
            detail = "jira candidate %s returned unexpected status %r" % (key, status)
            note("note: [stalled] %s" % detail)
            holes.record("jira", detail)
            complete = False
            continue
        if since > cutoff:
            continue
        raw_assignee = fields.get("assignee")
        if raw_assignee is not None and not isinstance(raw_assignee, dict):
            detail = "jira candidate %s returned a malformed assignee" % key
            note("note: [stalled] %s" % detail)
            holes.record("jira", detail)
            complete = False
            continue
        assignee = (raw_assignee or {}).get("displayName")
        who = 'assignee="%s"' % _fields.who_field(assignee) if assignee else "unassigned"
        out["jira:%s" % key] = 'kind=jira issue=%s "%s" %s idle=%sd | %s' % (
            key, status, who, max(0, int((now - since).total_seconds() // 86400)),
            one_line(fields.get("summary", "")))
    return out, complete


def approval_time(pr):
    """The latest standing verdict's ``participated_on``; ``updated_on`` when absent.

    A verdict is an approval or a changes-requested state.
    """
    stamps = [p.get("participated_on") for p in (pr.get("participants") or [])
              if (p.get("approved") or p.get("state") == "changes_requested")
              and p.get("participated_on")]
    return max(stamps) if stamps else pr.get("updated_on")


# --------------------------------------------------------------------------
# the stalled set
# --------------------------------------------------------------------------

def compute(profile_cfg, cfg, now, holes, tracked=(), pending=(), census=None):
    """``(items, answered)``: the stalled set this cycle, and which SOURCES answered.

    ``tracked`` is the set of keys already on record (``state["stalled"]``) and
    ``pending`` the pair keys discovered earlier whose companion check has not
    yet been answered: the pair tier re-reads both by number, whether or not the
    discovery window still reaches them. Returns ``(items, answered, unresolved)``
    — ``unresolved`` is ``pending`` for the next cycle: every pair key whose
    question was asked this cycle and not answered (codex pass 2, P2: a check that
    failed on the last cycle inside the window, for a key on no record, used to
    vanish — and the next cycle announced a recovery for it).

    ``items`` maps a stable key to the line body (without ``type=``). A source
    that raised is recorded against ``holes`` and left out of ``answered``, so
    the caller neither adds nor clears anything of that source's — absence of
    data is not absence of a stall.

    ``census``, when given, is filled per GitHub repo that answered:
    ``reachable`` (``gh_reachable``, or "?" if that probe failed), ``open``
    (PRs fetched) and ``binding_readable`` (PRs whose review binding the
    classifier could read). It makes a silently empty fetch visible.
    """
    days = int(cfg.get("days") or DEFAULT_DAYS)
    cutoff = now - timedelta(days=days)
    items, answered, unresolved = {}, set(), set()

    gh_cfg = profile_cfg.get("github") or {}
    if gh_cfg.get("repos"):
        ok = True
        if not GH:
            # codex on this branch (P2): a missing gh used to park the whole daemon
            # before it read the config, Jira and Bitbucket included. It is a
            # GitHub hole and nothing else.
            note("note: [stalled] gh not found on PATH — github tier unchecked")
            holes.record("github", "gh not found on PATH")
            ok = False
        for repo in gh_cfg["repos"] if ok else []:
            rs = repo.split("/")[-1]
            try:
                prs = gh_prs(repo)
            except Exception as e:
                note("note: [stalled] github pr query failed for %s: %s" % (repo, e))
                holes.record("github", "%s: %s" % (rs, e))
                ok = False
                continue
            readable = 0
            for pr in prs:
                binding = _review_binding.github_review_binding(pr)
                # Census: the classifier ran and could read this PR,
                # whatever it concluded (a PR with no verdict still counts).
                if binding["status"] != _review_binding.BINDING_UNREADABLE:
                    readable += 1
                if binding["status"] == _review_binding.NO_STANDING_VERDICT:
                    continue
                if binding["status"] == _review_binding.BINDING_UNREADABLE:
                    detail = "%s #%s: review binding unreadable: %s" % (
                        rs, pr.get("number"), binding.get("reason") or "unknown reason")
                    note("note: [stalled] %s" % detail)
                    holes.record("github", detail)
                    ok = False
                    continue
                # Both explicit stale outcomes deliberately fall through to the
                # visible inventory below.  BINDING_STALE is determinate data,
                # not an unreadable source hole (found in review).
                verdict = binding["verdict"]
                since = binding["submitted_at"]
                then = parse_ts(since)
                if then is None:
                    detail = "%s #%s: review binding has an invalid submittedAt" % (
                        rs, pr.get("number"))
                    note("note: [stalled] %s" % detail)
                    holes.record("github", detail)
                    ok = False
                    continue
                if then > cutoff:
                    continue
                draft_token = " draft=true" if pr.get("isDraft") is True else ""
                items["pr:%s#%s" % (repo, pr.get("number"))] = (
                    'kind=pr repo=%s #%s%s %s reviewer=%s bound=%s head=%s binding=%s '
                    'idle=%dd author=%s | %s' % (
                        rs, pr.get("number"), draft_token, verdict, binding["reviewer"],
                        binding["bound_oid"], binding["head_oid"],
                        "head" if binding["status"] == _review_binding.BOUND_AT_HEAD else "stale",
                        idle_days(since, now),
                        (pr.get("author") or {}).get("login", "?"),
                        one_line(pr.get("title", ""))))
            if census is not None:
                try:
                    reachable = gh_reachable(repo)
                except Exception as e:
                    note("note: [stalled] github reachable probe failed for %s: %s" % (repo, e))
                    reachable = "?"
                census[repo] = {"reachable": reachable, "open": len(prs), "binding_readable": readable}
        if ok:
            answered.add("github")

    if gh_cfg.get("repos"):
        # Half-landed pairs. Its own source ("pairs"), so a companion
        # fetch failing does not un-answer the PR tier or vice versa.
        ok = bool(GH)
        if not ok:
            holes.record("pairs", "gh not found on PATH")
            unresolved.update(pending)          # nothing ran: the open questions stand
        pair_since = now - timedelta(days=int(cfg.get("pair_days") or DEFAULT_PAIR_DAYS))
        checked = set()
        for repo in gh_cfg["repos"] if ok else []:
            rs = repo.split("/")[-1]
            try:
                merged = gh_merged_prs(repo, pair_since)
            except Exception as e:
                note("note: [stalled] merged-PR list failed for %s: %s" % (repo, e))
                holes.record("pairs", "%s: %s" % (rs, e))
                ok = False
                continue
            for pr in merged:
                for c_repo, c_num in github_wake.pairs_of(pr.get("body"), repo=repo):
                    key = pair_key(repo, pr.get("number"), c_repo, c_num)
                    checked.add(key)
                    try:
                        closed = github_wake.companion_merged(c_repo, c_num)
                    except Exception as e:
                        # a companion that cannot be
                        # fetched is a hole for ITS key, not for the source —
                        # `ok` stayed True here would have been wrong only if it
                        # meant "everything answered"; it means "the inventory
                        # is complete". The failed key is HELD by name (see
                        # `reconcile_set`'s `held`) while every other pair
                        # reconciles. One mistyped or Bitbucket-shaped trailer
                        # used to freeze every pair on the profile forever.
                        note("note: [stalled] companion fetch failed for %s#%d: %s" % (c_repo, c_num, e))
                        holes.record("pairs", "%s#%d: %s" % (c_repo, c_num, e))
                        unresolved.add(key)     # asked, not answered: held, and retried next cycle
                        continue
                    if not closed:
                        items[key] = pair_line(repo, pr.get("number"), c_repo, c_num,
                                               pr.get("mergedAt"), pr.get("title"), now)
        # codex pass 1 (P2): `pair_days` bounds DISCOVERY only. A pair on record —
        # or one discovered but never answered (pass 2) — whose merge has aged out
        # of the pool is re-read by number until its companion merges; before this
        # it simply left the inventory of an ANSWERED source and `reconcile_set`
        # said "cleared" of a window that stood. A key whose origin repo is no
        # longer configured is not re-read and drops: the operator stopped
        # watching that repo.
        for key in (sorted(set(tracked) | set(pending)) if GH else ()):
            parsed = parse_pair_key(key)
            if key in checked or parsed is None or parsed[0] not in gh_cfg["repos"]:
                continue
            repo, number, c_repo, c_num = parsed
            try:
                origin = gh_pr(repo, number)
            except Exception as e:
                note("note: [stalled] tracked pair %s could not be re-read: %s" % (key, e))
                holes.record("pairs", "%s: %s" % (key, e))
                unresolved.add(key)             # held by name; the source still answers
                continue
            # codex pass 3 (P2): the origin's BODY is the declaration; the key is
            # only where the question was filed. A trailer corrected after a
            # failed check is what gets asked now; the old question is retired
            # (and clears, if it was on record) instead of being re-asked forever
            # with `pairs` degraded and nothing able to clear.
            declared = github_wake.pairs_of(origin.get("body"), repo=repo)
            # ⚠ THE PREDICATE, stated because three review rounds inside the
            # trailer parser were all the same bug wearing different Markdown
            #: **retiring a tracked pair requires POSITIVE EVIDENCE
            # that the trailer was withdrawn, and "the parser found nothing" is
            # not that evidence — it is evidence of nothing.**
            #
            # An empty parse has two causes that look identical here: the author
            # removed the trailer, or the parser missed it. Every fence variant
            # codex found — a tilde fence, a four-backtick fence, an inline code
            # span on its own line, a fence nested under a list item, a fence
            # opened on the list-marker line — produced an EMPTY parse on a body
            # that still declared the pair. Treating that as a withdrawal is what
            # turned each of them into a silent `stalled_cleared` on a window
            # that was still open.
            #
            # The question is asked about THIS COMPANION, not about the count.
            # An earlier version guarded only the EMPTY parse and codex found the
            # hole (round four): a parse can come back PARTIAL — one trailer read,
            # another hidden by a fence shape the scanner gets wrong — and then a
            # non-empty `declared` walked straight past the guard and retired the
            # hidden one. `companions_mentioned` reads the RAW body, fences and
            # markers included, and the three cases separate cleanly:
            #   * not named anywhere        -> genuinely withdrawn -> retire
            #   * named but unresolvable    -> ask the companion; merged closes the
            #                                  window, otherwise HOLD and retry
            #   * declared normally         -> the ordinary path below
            mentioned = github_wake.companions_mentioned(origin.get("body"), repo=repo)
            if (c_repo, c_num) not in declared:
                if (c_repo, c_num) in mentioned:
                    # Named in the body, in a shape this parser could not resolve
                    # into a declaration. Before holding, ASK: a companion that has
                    # merged closes the window whatever the body looks like, and
                    # holding on an unreadable body would otherwise keep a fully
                    # landed pair on record for ever (codex, round four).
                    # ONE reason, recorded once.
                    # The two exits used to emit a note each and record the hole
                    # twice for one key — and the second note blamed the shape of
                    # the body even when the real cause was a 404 on the companion
                    # lookup, with the origin re-read perfectly well. The state was
                    # right and the operator's log was wrong twice over.
                    try:
                        settled = github_wake.companion_merged(c_repo, c_num)
                        reason = None if settled else (
                            "names %s#%d in a shape this parser cannot read"
                            % (c_repo, c_num))
                    except Exception as e:
                        settled = False
                        reason = ("names %s#%d, which could not be reached to check: %s"
                                  % (c_repo, c_num, e))
                        # ⚠ NOT `continue` either (codex, round six). Round five
                        # took the `continue` off the branch below and left its
                        # twin here — so a companion whose lookup 404s kept
                        # hiding every OTHER declaration on the same origin, on
                        # every poll. One fix, two exits; I changed one.
                    if not settled:
                        note("note: [stalled] %s#%s %s — holding %s, not retiring"
                             % (repo, number, reason, key))
                        holes.record("pairs", "%s: %s" % (key, reason))
                        unresolved.add(key)
                        # ⚠ NOT `continue` (codex, round five): the same origin may
                        # carry OTHER companions this parser CAN read, and skipping
                        # the loop below meant a readable one was never checked or
                        # recorded — held the old key and lost the new one. Holding
                        # one companion must not blind the origin's others.
                    else:
                        note("note: [stalled] %s unreadable but its companion has merged — "
                             "window closed on positive evidence" % key)
                else:
                    note("note: [stalled] %s#%s no longer names %s#%d anywhere — question retired"
                         % (repo, number, c_repo, c_num))
            for cr, cn in declared:
                key2 = pair_key(repo, number, cr, cn)
                if key2 in checked:
                    continue
                checked.add(key2)
                try:
                    closed = github_wake.companion_merged(cr, cn)
                except Exception as e:
                    note("note: [stalled] companion fetch failed for %s#%d: %s" % (cr, cn, e))
                    holes.record("pairs", "%s: %s" % (key2, e))
                    unresolved.add(key2)        # held by name; the source still answers
                    continue
                if not closed:
                    items[key2] = pair_line(repo, number, cr, cn,
                                            origin.get("merged_at"), origin.get("title"), now)
        if ok:
            answered.add("pairs")

    bb_cfg = profile_cfg.get("bitbucket") or {}
    if bb_cfg.get("repos"):
        ok = True
        if not bitbucket_wake.BKT:
            # codex on this branch (P2): repos configured and no bkt used to skip
            # silently while the watermark advanced — apparently healthy, never checked.
            note("note: [stalled] bkt not found on PATH — bitbucket tier unchecked")
            holes.record("bitbucket", "bkt not found on PATH")
            ok = False
        cwd = os.path.expanduser(bb_cfg.get("cwd", "~"))
        for repo in bb_cfg["repos"] if ok else []:
            rs = repo.split("/")[-1]
            try:
                prs = bb_prs(repo, cwd)
            except Exception as e:
                note("note: [stalled] bkt pullrequests failed for %s: %s" % (repo, e))
                holes.record("bitbucket", "%s: %s" % (rs, e))
                ok = False
                continue
            for pr in prs:
                approvals = bitbucket_wake.approvals_of(pr)
                requests = bitbucket_wake.changes_requested_of(pr)
                since = approval_time(pr)
                then = parse_ts(since)
                if not (approvals or requests) or then is None or then > cutoff:
                    continue
                approvers = json.dumps([_fields.who_field(v) for v in approvals.values()],
                                       separators=(",", ":"))
                author = json.dumps(_fields.who_field((pr.get("author") or {}).get("display_name", "?")))
                held_by = ""
                if requests:     # a line without a request is byte-for-byte what it was
                    held_by = 'changes_requested=%d requesters=%s ' % (
                        len(requests), json.dumps([_fields.who_field(v) for v in requests.values()],
                                                  separators=(",", ":")))
                items["bb:%s#%s" % (repo, pr.get("id"))] = (
                    'kind=bb repo=%s #%s approved=%d approvers=%s %sidle=%dd author=%s | %s' % (
                        rs, pr.get("id"), len(approvals), approvers, held_by,
                        idle_days(since, now), author, one_line(pr.get("title", ""))))
        if ok:
            answered.add("bitbucket")

    jira_cfg = profile_cfg.get("jira") or {}
    if jira_cfg.get("projects") and jira_cfg.get("api_token") in (None, "", PLACEHOLDER):
        # codex pass 2 on this branch (P2): projects configured and no usable token
        # used to skip the tier silently — the watermark advanced, and a prior
        # Jira failure could even read as `degraded_recovered` with no query run.
        # but a placeholder is a tier WAITING on config,
        # the state jira_wake calls "waiting" and the watchdog "parked" — not a
        # failure to promote to `error:` fifteen minutes after a fresh install.
        # So: not answered (its stalls stay unknown), not a hole (nothing
        # escalates), and any open episode is held rather than "recovered".
        holes.skip("jira", "api_token is a placeholder")
    elif jira_cfg.get("projects"):
        jira_items, ok = jira_stalled(jira_cfg, jira_cfg["projects"], cutoff, now, holes)
        items.update(jira_items)
        if ok:
            answered.add("jira")

    return items, answered, unresolved


def source_of(key):
    return {"pr": "github", "bb": "bitbucket", "jira": "jira", "pair": "pairs"}[key.split(":", 1)[0]]


def reconcile_set(state, items, answered, now_epoch, reemit_secs, held=()):
    """Turn this cycle's set into lines. Returns the lines; mutates ``state``.

    New → ``stalled``. Still there past the re-emit window → ``stalled_still``.
    Gone, from a source that ANSWERED → ``stalled_cleared``. Gone from a source
    that did not answer → kept, silently: the next cycle that answers decides.
    ``held`` is the per-KEY version of the
    same rule: a key whose own question went unanswered this cycle — a pair
    whose companion could not be fetched — is kept without un-answering the
    whole source, so one bad trailer holds one pair, not every pair.
    """
    lines = []
    known = state.setdefault("stalled", {})
    held = set(held)
    for key, body in sorted(items.items()):
        prev = known.get(key)
        if prev is None:
            known[key] = {"first": now_ts(), "last_emit": now_epoch}
            lines.append("type=stalled %s" % body)
        elif now_epoch - float(prev.get("last_emit") or 0) >= reemit_secs:
            prev["last_emit"] = now_epoch
            lines.append("type=stalled_still %s" % body)
    for key in sorted(known):
        if key in items or key in held or source_of(key) not in answered:
            continue
        lines.append("type=stalled_cleared key=%s since=%s" % (key, known[key].get("first")))
        del known[key]
    return lines


def load_state(profile):
    try:
        with open(os.path.join(STATE_DIR, "%s.json" % profile)) as fh:
            raw = json.load(fh)
        return {"since": raw.get("since"), "stalled": raw.get("stalled", {}),
                "degraded": raw.get("degraded", {}), "waiting": raw.get("waiting", {}),
                "pair_pending": raw.get("pair_pending", [])}
    except Exception:
        return {"since": None, "stalled": {}, "degraded": {}, "waiting": {}, "pair_pending": []}


def save_state(profile, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "%s.json" % profile)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"since": state["since"], "stalled": state.get("stalled", {}),
                   "degraded": state.get("degraded", {}), "waiting": state.get("waiting", {}),
                   "pair_pending": state.get("pair_pending", []), "build": BUILD}, fh)
    os.replace(tmp, path)


def poll_profile(profile, profile_cfg, cfg, state, now=None):
    now = now or datetime.now(timezone.utc)
    holes = _degraded.Cycle()
    census = {}
    items, answered, unresolved = compute(profile_cfg, cfg, now, holes,
                                          tracked=state.get("stalled") or {},
                                          pending=state.get("pair_pending") or [],
                                          census=census)
    # The GitHub census goes to the DAEMON log (note, never emit) and
    # only when a repo's numbers change, so a quiet fleet adds no lines. A
    # repo that failed this cycle keeps its last known entry.
    previous = state.get("census") or {}
    for repo in sorted(census):
        c = census[repo]
        before = previous.get(repo) or {}
        if c == before:
            continue
        suffix = " (was reachable=1)" if before.get("reachable") == 1 and c["reachable"] == 0 else ""
        note("[%s] census github %s reachable=%s open=%s binding-readable=%s%s" % (
            profile, repo, c["reachable"], c["open"], c["binding_readable"], suffix))
    state["census"] = dict(previous, **census)
    waiting = dict(holes.skipped)
    if waiting != state.get("waiting", {}):
        for sub, why in sorted(waiting.items()):
            note("[%s] waiting: %s tier not checked — %s" % (profile, sub, why))
        for sub in sorted(set(state.get("waiting", {})) - set(waiting)):
            note("[%s] %s tier checked again" % (profile, sub))
        state["waiting"] = waiting
    reemit_secs = 3600 * float(cfg.get("reemit_hours") or DEFAULT_REEMIT_HOURS)
    for line in reconcile_set(state, items, answered, now.timestamp(), reemit_secs, held=unresolved):
        emit(profile, line)
    # the pair questions still open after this cycle, minus the ones on record
    # (those are retried as `tracked`); an empty list when everything answered
    state["pair_pending"] = sorted(unresolved - set(state.get("stalled") or {}))
    state["since"] = now.isoformat()
    # Reconciled BEFORE the save so the episode bookkeeping is persisted
    # with the state it describes.
    for emit_line, note_line in _degraded.reconcile(
            holes, state.setdefault("degraded", {}), now.timestamp()):
        if emit_line:  # a silenced repeat is daemon-log only
            emit(profile, emit_line)
        note("[%s] %s" % (profile, note_line))
    save_state(profile, state)
    return items


def report(profile):
    """``--report PROFILE``: the current stalled set on stdout, touching nothing."""
    with open(CONFIG_PATH) as fh:
        config = json.load(fh)
    profiles = config.get("profiles") or {}
    if profile not in profiles:
        # an unknown profile used to become an empty one
        # and print "(nothing stalled …)" with exit 0 — the q1 failure mode, on
        # the CLI this daemon exists to eliminate. Same convention as `just acted`.
        print("unknown profile %r — known: %s" % (profile, ", ".join(sorted(profiles)) or "none"),
              file=sys.stderr)
        return 2
    pcfg = profiles[profile]
    cfg = pcfg.get("stalled") or {}
    holes = _degraded.Cycle()
    on_disk = load_state(profile)                         # read-only: the pairs on record, and the open questions
    items, answered, _ = compute(pcfg, cfg, datetime.now(timezone.utc), holes,
                                 tracked=on_disk.get("stalled") or {},
                                 pending=on_disk.get("pair_pending") or [])
    for key in sorted(items):
        print("  " + items[key])
    if not items:
        print("  (nothing stalled in the sources that answered)")
    for sub, detail in sorted(holes.failures.items()):
        if sub in answered:      # a per-key hole — the source answered, that item is held
            print("  ⚠ %s answered with a hole — %s (that item is held, not cleared)" % (sub, detail))
        else:
            print("  ⚠ %s did NOT answer — %s (its stalls, if any, are unknown)" % (sub, detail))
    for sub, detail in sorted(holes.skipped.items()):
        print("  ⚠ %s NOT checked — %s" % (sub, detail))
    print("sources answered: %s" % (", ".join(sorted(answered)) or "none"))
    return 0


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--report":
        sys.exit(report(sys.argv[2]))
    if not GH:
        note("note: gh not found on PATH — the github tier will report as degraded until it is")
    note("stalled daemon starting build=%s" % _build.render(BUILD))
    states = {}
    failures = 0
    while True:
        try:
            with open(CONFIG_PATH) as fh:
                config = json.load(fh)
        except Exception as e:
            note("note: config unreadable (%s) — sleeping" % e)
            time.sleep(300)
            continue

        active, failed = [], None
        for profile, pcfg in (config.get("profiles") or {}).items():
            cfg = pcfg.get("stalled")
            if not cfg or not cfg.get("days"):
                continue
            active.append(profile)
            if profile not in states:
                states[profile] = load_state(profile)
                note("[%s] %s" % (profile, "first sweep" if not states[profile]["since"] else "resumed"))
            try:
                poll_profile(profile, pcfg, cfg, states[profile])
            except Exception as e:
                failed = "cycle failed for %s: %s" % (profile, e)
                note("note: " + failed)

        if failed:
            failures += 1
            if failures == 3:
                for p in active:
                    emit(p, "error: stalled polling failing repeatedly — last: %s" % failed)
        else:
            if failures >= 3:
                note("note: polling recovered after %d failures" % failures)
            failures = 0
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
