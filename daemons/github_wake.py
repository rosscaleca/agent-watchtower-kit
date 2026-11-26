#!/usr/bin/env python3
"""Watchtower GitHub daemon — multi-profile PR / review / comment / Actions poller.

Shells `gh api` so the credential stays inside gh; this process never touches a
token. Reads ~/.config/watchtower/config.json every cycle (edits need no
restart); each profile with a "github" block is polled and its events appended
to ~/Library/Logs/watchtower/<profile>-github.log. stdout carries operational
lines only (launchd redirects it to the daemon log).

Per profile config: {"repos": ["owner/name", ...], "suppress_authors": [...]}
Suppression applies to comments and reviews (the agent's own writes); PR
lifecycle and run events are never suppressed — a merge or push is state worth
confirming whoever clicked it. First run per profile seeds a baseline silently.

Review, comment and run lines carry the id GitHub gave them (``id=`` on
reviews and comments, ``run=<id>.<attempt>`` on runs), so a repeated line can be
told from a second event. What has been announced is remembered in ``seen``, an
ordered per-kind memory that a restart reloads intact — see ``SEEN_CAPS``.

A PR body may declare a cross-repo companion with a ``Lands-with:
owner/repo#N`` trailer. When such a PR MERGES while its companion is still
unmerged, ``type=pair_window_open`` is emitted at that moment — a feature
exists and the thing that makes it safe does not; ``stalled_wake`` carries the
half-landed pair until the companion merges.
"""
import collections
import copy
import functools
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

import _build  # sibling module; the daemons dir is sys.path[0] when run as a script
import _degraded  # A cycle that completes with a hole in it must say so
import _fields  # Text from GitHub loses its terminal controls before it reaches an event line

#: What bytes this process is running, logged on start and saved with the state.
BUILD = _build.describe(__file__)

CONFIG_PATH = os.path.expanduser("~/.config/watchtower/config.json")
STATE_DIR = os.path.expanduser("~/.local/state/watchtower/github")
LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
POLL_SECS = 60
OVERLAP = timedelta(minutes=5)

#: What one poll of one repo asks GitHub for. These BUILD the request URLs, and the
#: dedup memory below is sized from them, so the two cannot drift apart.
PRS_PER_PAGE = 15         # the PR listing: most recently updated first, every state
REVIEWS_PER_PAGE = 20     # a PR's reviews: the first page, with NO `since` filter
COMMENTS_PER_PAGE = 30    # per endpoint, `since`-filtered
COMMENT_ENDPOINTS = 2     # issue comments and PR review comments
#: Pages of comments read per endpoint per repo. A page is requested only after
#: a FULL page, so the steady state is still one request per endpoint. Reaching the bound
#: is told (`type=unread_comments reason=page_cap`), never a silent truncation. ⚠ Raising
#: this grows `SEEN_WINDOW["c"]` and so `SEEN_CAPS["c"]` with it, which is what the dedup
#: memory is sized from.
COMMENTS_MAX_PAGES = 5
RUNS_PER_PAGE = 20        # the newest runs

#: The dedup memory, ``state["seen"]``: the ids already announced.
#:
#: It was a set saved as ``list(seen)[-2000:]``. A set has no order, so once it
#: passed 2000 an ARBITRARY 2000 were kept and the rest dropped at random, and
#: whatever the API still returned that had been dropped was announced again
#: after the next restart: four overnight CI completions at the always-on host's restart on
#: 2026-09-29, with the state files at exactly 2000 (1376 of them runs, in the one
#: inspected). The standalone watcher fixed the same line on 2026-08-24; this is
#: that fix, adapted to a profile that watches several repos.
#:
#: * ``seen`` is an ORDERED mapping, and an id moves to the newest position every
#:   time the API returns it (LRU), so eviction sheds what has STOPPED appearing.
#: * It is pruned PER KIND, the id prefix (``review:`` ``run:`` ``c:``): the kinds
#:   need very different memories, and one shared budget let the highest-churn
#:   kind starve the neediest.
#: * It is pruned at the end of every cycle, in the running process, so what a
#:   restart reloads is exactly what this process already knows.
#: * Each kind's cap is sized from its query window, PER REPO, and scaled by the
#:   number of repos the profile watches.
#:
#: The window is the most ids of a kind that one poll of one repo can return:
SEEN_WINDOW = {
    "review": PRS_PER_PAGE * REVIEWS_PER_PAGE,       # 300: every listed open PR, re-fetched in full each poll
    "run": RUNS_PER_PAGE,                            #  20: the newest page
    "c": COMMENT_ENDPOINTS * COMMENTS_PER_PAGE * COMMENTS_MAX_PAGES,   # 300: `since`-filtered, paged by COMMENTS_MAX_PAGES
}
#: An id is returnable for the overlap plus one poll: six polls.
COMMENT_POLLS = int((OVERLAP.total_seconds() + POLL_SECS) // POLL_SECS)
#: How many windows each kind remembers, and why. Every id the API returns is moved to
#: the newest end each poll, so an id that KEEPS being returned needs only its one
#: window. The rest is for ids the API stops returning for a while and then returns:
#:
#: * review 2: the listing is the 15 most recently updated PRs, so a PR at its edge
#:   flickers in and out as others are updated. Its reviews are untouched on the polls
#:   it sits out, while the PRs still listed fill a whole window, so it needs a second
#:   window of room or its reviews are evicted and announced again the moment it is
#:   back. The same room is how long a PR that leaves the listing keeps its reviews
#:   remembered. Measured on the live profile log, 2026-09-03..29: 7.9 emitted reviews
#:   a day, 43 on the busiest (the account's own, suppressed, are extra), and six PRs
#:   open across all three repos on 2026-09-29, a live window of 120 at most. Hundreds
#:   of free slots per repo is weeks of memory.
#: * run 10: one window of NEW ids a poll is the newest page, and the off-page
#:   fetch announces a completion once and never asks for it again. Runs are the
#:   highest-churn kind and have a budget of their own, so the slack costs no other kind
#:   a slot. The page-back reads deeper pages back to the previous poll, less a
#:   margin, so a burst IS read again on the next poll or two; those ids are known and
#:   are refreshed. What the page-back adds is CHURN, up to RUNS_MAX_PAGES - 1 pages of
#:   new ids a cycle, and a test pins this cap against that. While a failed read HOLDS a
#:   repo's cutoff, runs already announced behind the failing page are NOT refreshed,
#:   and enough other ids (by-id completions) could evict them before the page answers
#:   and the held cutoff reads them back as news. So the run ids a held read can
#:   still return are EXEMPT from the cap (`prune_seen`'s ``exempt``): every run key
#:   touched or announced in a held repo since that repo's hold began, less
#:   RUNS_CUTOFF_MARGIN (`state["runs_recent"]`, kept for every repo, held or not, so the
#:   margin before a hold is covered). Every other run id is pruned to the cap, so the
#:   run kind holds at most the cap plus the exempt set, however long holds keep reopening.
#: * c 6: an id stays in the answer for COMMENT_POLLS polls, one after another, so
#:   re-observation keeps it. Six windows is the worst case with no refresh at all:
#:   each of the polls it can still be returned in may add a whole window of new ids.
SEEN_KEEP = {"review": 2, "run": 10, "c": COMMENT_POLLS}
#: Per watched repo: review 600, run 200, c 1800.
SEEN_CAPS = dict((kind, SEEN_WINDOW[kind] * SEEN_KEEP[kind]) for kind in SEEN_WINDOW)
#: Every kind nobody sized shares ONE bucket, so a stream of distinct unknown
#: prefixes cannot grow the file without bound.
SEEN_CAP_OTHER = 300
#: Cycle sub-resources whose failure leaves a slice of the memory unread.
SEEN_SUBS = ("reviews", "comments", "runs")

#: The state file's ``seen`` list. Version 1 (no marker at all) is the arbitrary-order
#: subset the old code wrote. Version 2 is oldest-first. It stays a FLAT list of id
#: strings so the previous code can still read a version 2 file: a rollback loses
#: the order, never an id, and never fails to start.
SEEN_V = 2
#: The first cycle after loading a version 1 file adopts, SILENTLY, an item it cannot
#: find in ``seen`` only if the item's own timestamp is at or before the old file's
#: watermark minus this. The old daemon's last look can lag its watermark by a fetch
#: and the two clocks are not the same clock, so an item this close to the watermark
#: is announced if unseen: a duplicate wake costs a turn, a swallowed one costs the event.
LEGACY_GUARD = timedelta(seconds=POLL_SECS)
#: A hole in that first cycle leaves its slice unbaselined, so the baseline is held for
#: the next cycle, at most this many, so a permanently failing fetch cannot keep an
#: adoption rule running for the life of the daemon.
LEGACY_MAX_CYCLES = 10

#: A run seen in flight is remembered, and once it has fallen off the
#: runs page it is fetched by id every cycle until it completes. These bound
#: that: per-id fetches per repo per cycle, and how long a run may stay in
#: flight before it is given up on. The cap keeps a pile of runs parked on
#: "waiting" from spending the REST budget every minute. Which runs
#: are fetched ROTATES, least recently checked first (never-checked first, then
#: oldest id), because the page-back tracks every run still going in a burst:
#: always taking the oldest ten let ten parked runs starve every completion
#: behind them (12 parked in a 40-run burst announced 0 of 28 CI runs).
INFLIGHT_FETCH_CAP = 10
INFLIGHT_MAX_SECS = 72 * 3600

#: In-flight tracking starts once a poll has SEEN it in flight. A run no poll
#: saw stayed invisible: created after one poll and pushed off the newest page by
#: RUNS_PER_PAGE newer runs before the next, it never entered `runs_inflight`, and
#: its completion was never emitted. Seven merges in a row once created 49
#: runs inside about a minute and lost 3 of the 7 CI@main completions in the PM's
#: profile. So each poll PAGES BACK, at the same page size, until a page's oldest
#: run was created before the previous poll began, less this margin (clock skew
#: between GitHub's `created_at` and this host, and a run listed a little after it
#: was created). Pages are contiguous newest-first, so a COMPLETE read skips
#: nothing created since; a run created between two page requests only repeats on
#: the next page. A read that is not complete holds the cutoff (RUNS_HOLD_MAX).
RUNS_CUTOFF_MARGIN = timedelta(minutes=2)
#: The page-back's bound: 100 runs a poll per repo, twice the 2026-09-30 burst.
#: EVERY hit is told by its own `type=unread_runs reason=page_cap` line, outside
#: the degraded rate limit, never a silent truncation. The steady state is still
#: one request: a page that reaches past the previous poll, or a short page, ends
#: it. ⚠ Raising this grows the per-cycle churn on `seen`'s run budget; a test
#: pins the cap against it.
RUNS_MAX_PAGES = 5
#: A runs read with a hole in it (a page that failed, or a full page carrying no
#: usable `created_at`) does not move that repo's cutoff on: the next poll pages
#: back to where the incomplete read began, so the runs behind the hole are read
#: once the fetch works. The hold is bounded so a permanently failing fetch
#: cannot pin it forever: past this age it is released, and the release is told
#: by `type=unread_runs reason=hold_expired`. A page cap is NOT a hole for this
#: purpose: holding would only re-read the same newest pages and hit the cap again.
RUNS_HOLD_MAX = timedelta(minutes=30)

os.environ["PATH"] = ":".join([
    os.path.expanduser("/etc/profiles/per-user/%s/bin" % os.environ.get("USER", "")),
    "/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
GH = shutil.which("gh")


def note(line):
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # A lone surrogate in a field the line was not built from through `one_line`
        # cannot be encoded by the stream; the ascii() of the line always can (repr() keeps a
        # printable non-ASCII character, which a stream that is not UTF-8 refuses just the same).
        print(ascii(line), flush=True)


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(profile, line):
    os.makedirs(LOG_DIR, exist_ok=True)
    # errors="replace": a lone surrogate in a field that does not pass through
    # `one_line` (a run's `conclusion`, say) is written as "?", not raised at the write.
    with open(os.path.join(LOG_DIR, "%s-github.log" % profile), "a", errors="replace") as fh:
        fh.write("%s ts=%s\n" % (line, now_ts()))


def one_line(s, cap=180):
    # The line is made buildable, so a bad field costs no cycle: a number is spelled,
    # anything else that is not a string (None, a list, a dict) is empty, a lone surrogate is "?".
    if not isinstance(s, str):
        s = str(s) if isinstance(s, (int, float)) else ""
    s = s.encode("utf-8", "replace").decode("utf-8")
    # As bitbucket_wake and stalled_wake: after the whitespace collapse, the
    # controls and format characters a terminal acts on but does not show (ESC/CSI, the bidi marks,
    # zero-width spaces; `_fields.unshown_dropped`, which keeps the two joiners U+200C/U+200D for
    # emoji and script spelling) are dropped, so a title cannot redraw the line.
    s = _fields.unshown_dropped(re.sub(r"\s+", " ", s)).strip()
    return (s[: cap - 1] + "…") if len(s) > cap else s


def _login_raw(obj):
    """The login of a GitHub `user` object exactly as GitHub sent it, or "?" when `obj` is not
    a dict with a string `login`. This is the copy `suppress_authors` is compared with: flattening it
    first would let `"alice\\n"` match `alice`, and cut a long login short."""
    login = obj.get("login") if isinstance(obj, dict) else None
    return login if isinstance(login, str) else "?"


def _login(obj):
    """The login of a GitHub `user` object as it is shown: :func:`_login_raw` on one line
    (see :func:`one_line`)."""
    return one_line(_login_raw(obj))


def _sha9(head):
    """The first nine characters of a PR's head commit, or "" when `head` is not a dict
    with a string `sha`."""
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha[:9] if isinstance(sha, str) else ""


#: One `gh api` call may take this long in all, and is waited for in slices of
#: GH_WAIT_SLICE_SECS so that a SIGTERM is acted on within one slice, not when the call returns.
GH_TIMEOUT_SECS = 45
GH_WAIT_SLICE_SECS = 1

#: A clock the wait reads by this name, so that neither `subprocess` nor a test that replaces
#: this module's `time` (for `time.time` and `time.sleep`) changes how long a call may take.
_monotonic = time.monotonic


def gh_api(path):
    """One `gh api` call. The wait is in slices of :data:`GH_WAIT_SLICE_SECS`, and a stop
    requested by SIGTERM (:func:`stop_point`) kills and reaps the child and raises
    :class:`Terminated` at the next slice, before the call starts and after it returns. With
    `subprocess.run` a signal during the wait ran the handler and the wait resumed, so a stop
    requested at the start of a slow call was acted on when the call returned or after
    :data:`GH_TIMEOUT_SECS`, past launchd's 20 s. The total is unchanged: after
    :data:`GH_TIMEOUT_SECS` the child is killed and `subprocess.TimeoutExpired` raised, as
    `subprocess.run(timeout=)` did."""
    stop_point()
    cmd = [GH, "api", path]
    deadline = _monotonic() + GH_TIMEOUT_SECS
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True) as proc:
        try:
            while True:
                left = deadline - _monotonic()
                try:
                    out, err = proc.communicate(timeout=max(0.0, min(GH_WAIT_SLICE_SECS, left)))
                    break
                except subprocess.TimeoutExpired:
                    stop_point()
                    if _monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(cmd, GH_TIMEOUT_SECS)
        except BaseException:
            proc.kill()      # the `with` closes both pipes and reaps the child
            raise
    stop_point()
    if proc.returncode != 0:
        raise RuntimeError("gh api %s failed: %s" % (path.split("?")[0], one_line(err, 160)))
    return json.loads(out)


def issue_number(url):
    m = re.search(r"/(?:issues|pulls)/(\d+)", url or "")
    return m.group(1) if m else "?"


def new_seen(ids=()):
    """The dedup container: an ORDERED mapping, deliberately not a set.

    A set has no order to give, and eviction is oldest-first. ``dict.fromkeys``
    keeps O(1) membership and records arrival; an insertion order that means
    "last seen" is maintained by :func:`touch_seen`. An ``OrderedDict``, so that
    the move to the newest position is one operation, `move_to_end`, not a delete and an insert.
    """
    return collections.OrderedDict.fromkeys(ids)


def touch_seen(seen, mid):
    """Move an already-known id to the newest position (LRU, not FIFO).

    Re-assigning a key in a dict does NOT move it, so without this the queue would
    be ordered by FIRST sighting: a review re-fetched every cycle (the reviews query
    has no ``since`` filter) would drift to the front and eventually be evicted, and
    a PR open for weeks would announce its own old reviews. Eviction should shed what
    has STOPPED appearing, and last-seen order does exactly that.
    """
    if mid in seen:
        seen.move_to_end(mid)


def seen_caps(n_repos=1):
    """The per-kind caps for a profile watching ``n_repos`` repos: every window in
    :data:`SEEN_WINDOW` is per repo, so the memory scales with the repo count."""
    n = max(1, int(n_repos))
    return dict((kind, cap * n) for kind, cap in SEEN_CAPS.items())


def prune_seen(order, n_repos=1, exempt=frozenset()):
    """Keep the newest ids of each kind, in their original order.

    An id in ``exempt`` (the run ids a held runs read can still return) is kept
    whatever its age and counts against no cap; every other id is pruned as below.

    Replaces ``list(seen)[-SEEN_CAP:]``, which had two defects. ``seen`` was a set,
    so the slice kept an arbitrary subset rather than the newest; and one global
    budget let the highest-churn kind evict the others. The kind is the id prefix
    written at each call site (``review:`` ``run:`` ``c:``); every other prefix
    shares one bounded bucket. ``order`` is the ids oldest-first (an ordered mapping
    included, and ids are unique there); the result is a list, oldest first.
    """
    caps = seen_caps(n_repos)
    kept, counts = [], {}
    for mid in reversed(list(order)):             # newest first, so the newest `cap` of a kind survive
        if mid in exempt:                          # A held read can still return it
            kept.append(mid)
            continue
        kind = mid.split(":", 1)[0] if isinstance(mid, str) else None
        if kind not in caps:
            kind = None                            # the shared bucket for anything unsized
        cap = caps[kind] if kind is not None else SEEN_CAP_OTHER
        if counts.get(kind, 0) >= cap:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        kept.append(mid)
    kept.reverse()
    return kept


def _ordered_seen(state):
    """``state["seen"]`` as the ordered container. A set handed in by an older caller
    (and the fixtures built from one) is upgraded in place; its order is arbitrary,
    which is only ever true of a state nothing but a test builds."""
    seen = state.get("seen")
    if not isinstance(seen, collections.OrderedDict):   # A plain dict has no move_to_end
        seen = state["seen"] = new_seen(seen or ())
    return seen


_FRACTION = re.compile(r"\.(\d+)(?=[+-]|$)")

#: The stamps `_parse_ts` accepts: a day inside `datetime`'s range at either end. Every
#: offset a cycle subtracts from a parsed stamp (OVERLAP, RUNS_CUTOFF_MARGIN, LEGACY_GUARD) is under
#: a day, so no arithmetic on an accepted stamp leaves the range. Unbounded, a saved hold, floor or
#: watermark at the edge (``0001-01-01T00:00:00+00:00``, or one whose conversion to UTC leaves the
#: range) raised OverflowError in every cycle, and the profile was never polled again.
_TS_RANGE = (datetime.min.replace(tzinfo=timezone.utc) + timedelta(days=1),
             datetime.max.replace(tzinfo=timezone.utc) - timedelta(days=1))


def _parse_ts(raw):
    """A GitHub timestamp (``2026-09-29T16:10:16Z``) or this daemon's own watermark
    (``...+00:00``) as an aware UTC datetime, or ``None``. The daemons run under
    /usr/bin/python3 (3.9), whose ``fromisoformat`` takes no ``Z`` and only 3- or
    6-digit fractions, so both are normalised first. A stamp outside :data:`_TS_RANGE`
    is ``None`` too."""
    if not isinstance(raw, str) or not raw:
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"),
                         raw.strip().replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    return parsed if _TS_RANGE[0] <= parsed <= _TS_RANGE[1] else None


def _predates(raw, cutoff):
    """``True`` only on POSITIVE evidence: ``raw`` parses as a timestamp at or before
    ``cutoff``. No cutoff, no timestamp, or one that does not parse is ``False``: an
    item that cannot be proven old is announced."""
    if cutoff is None:
        return False
    stamped = _parse_ts(raw)
    return stamped is not None and stamped <= cutoff


def _created_after(raw, cutoff):
    """``True`` only on POSITIVE evidence: ``raw`` parses as a timestamp after ``cutoff``."""
    stamped = _parse_ts(raw)
    return cutoff is not None and stamped is not None and stamped > cutoff


def runs_url(repo, since_date, page=1):
    """The runs listing. Page one is the request this daemon has always made; a deeper
    page adds only ``&page=N``, so the page size, and with it every page's offset, is fixed."""
    url = "repos/%s/actions/runs?per_page=%d&created=%%3E%%3D%s" % (repo, RUNS_PER_PAGE, since_date)
    return url if page == 1 else "%s&page=%d" % (url, page)


#: The statuses whose runs are listed every cycle with NO date filter. GitHub's
#: ``status`` filter is on a run's CURRENT attempt, so a re-run of an old run is listed
#: whatever its age; the plain listing above is newest-first by the run's ORIGINAL
#: ``created_at``, so a re-run sits among the old runs, behind pages the page-back never reads.
RUNS_STATUS_LISTINGS = ("in_progress", "queued")


#: The status listings' own page size, GitHub's largest. It is not RUNS_PER_PAGE: a re-run is
#: last in a newest-first list, so a page of 20 hid it behind 20 newer runs that were going.
RUNS_STATUS_PER_PAGE = 100


def runs_status_url(repo, status):
    """The newest page of ``repo``'s runs that are ``status`` now. No ``created`` filter."""
    return "repos/%s/actions/runs?status=%s&per_page=%d" % (repo, status, RUNS_STATUS_PER_PAGE)


def note_status_listing(profile, repo, status, runs, full_told):
    """A status answer that came back FULL may have more runs behind it, which are not read:
    that is told, never a silent truncation. The daemon log notes every full answer; the
    ``type=unread_runs reason=status_cap`` line is written once per (repo, status) episode, and
    again after an answer that was not full. ``full_told`` is that memory, a set of
    ``(repo, status)`` kept in memory for the running process only (it is not saved: after a
    restart a still-full answer is told once more)."""
    key = (repo, status)
    if len(runs) < RUNS_STATUS_PER_PAGE:
        full_told.discard(key)
        return
    rs = one_line(repo.split("/")[-1])      # Shown, never used as a key
    note("note: [%s] %s status=%s listing is full (%d runs): a run going now and deeper than them "
         "is not read" % (profile, repo, status, len(runs)))
    if key in full_told:
        return
    emit(profile, "type=unread_runs repo=%s reason=status_cap status=%s | the newest %d runs that are "
         "%s were read; a run that is %s now and deeper than them may be untracked — check by run id"
         % (rs, status, RUNS_STATUS_PER_PAGE, status, status))
    full_told.add(key)                          # remembered once written, never before


def _listing(answer):
    """The runs of one listing answer. ``workflow_runs`` missing or ``null`` is a malformed
    answer: it raises, so the caller's ``except`` records it as the hole it is (and the
    cutoff holds) instead of failing the whole cycle or reading it as the end of the list."""
    runs = answer.get("workflow_runs") if isinstance(answer, dict) else None
    if not isinstance(runs, list):
        raise RuntimeError("runs listing answered no workflow_runs list")
    if not all(isinstance(run, dict) for run in runs):
        raise RuntimeError("runs listing answered a workflow_runs entry that is not an object")
    return runs


def page_back_runs(profile, repo, since_date, first, cutoff, holes, told=None):
    """The runs on pages 2.. of the listing, read until no run created after
    ``cutoff`` can be deeper. Returns ``(runs, complete)``.

    ``first`` is page one. The page-back stops at a page whose oldest run was created at
    or before ``cutoff`` (it straddles the boundary, and every later page is older), or at
    a short page (the end of the listing): both are a complete read. A deeper page that
    fails, or a full page that carries no usable ``created_at``, is a HOLE: recorded,
    and ``complete`` is ``False``, so the caller holds the cutoff for the next poll.
    :data:`RUNS_MAX_PAGES` is the bound, and reaching it is told on EVERY hit by its own
    ``type=unread_runs reason=page_cap`` line, outside the degraded rate limit. It does
    not hold the cutoff (re-reading the same newest pages would only hit it again).
    ``told`` (per profile, saved with the state) remembers the last cap window
    written for each repo: a hit whose ``since`` AND ``until`` both equal it says nothing new
    and is noted only. That is what a frozen watermark (a cycle that keeps raising) re-reads
    every poll; any other hit, a moved ``until`` included, is written.
    ``cutoff`` ``None`` means there is no previous poll to have missed anything (the
    first run): page one only. ``runs`` is every run on the deeper pages read,
    unfiltered; the caller decides which of them are news.
    """
    deeper, page, runs = [], 1, first
    if cutoff is None:
        return deeper, True
    rs = one_line(repo.split("/")[-1])      # Shown, never used as a key
    stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    while len(runs) >= RUNS_PER_PAGE:
        created = [c for c in (_parse_ts(r.get("created_at")) for r in runs) if c is not None]
        if not created:
            # GitHub always sends `created_at`; a page without one cannot say where the
            # boundary is. Stop rather than page to the cap on a malformed answer, and
            # treat it as the hole it is.
            note("note: [%s] %s runs page %d carries no usable created_at: the page-back stops "
                 "here and the cutoff %s is held" % (profile, repo, page, stamp))
            holes.record("runs", "%s page %d: no usable created_at; runs created since %s held"
                         % (rs, page, stamp))
            return deeper, False
        oldest = min(created)
        if oldest <= cutoff:
            return deeper, True
        if page >= RUNS_MAX_PAGES:
            until = oldest.strftime("%Y-%m-%dT%H:%M:%SZ")
            note("note: [%s] %s page-back cap reached: %d pages read and the oldest run on the last "
                 "(%s) is still newer than %s" % (profile, repo, page, until, stamp))
            if told is not None and told.get(repo) == (stamp, until):
                return deeper, True
            emit(profile, "type=unread_runs repo=%s reason=page_cap since=%s until=%s | the newest "
                 "%d runs were read; a run created in that window and deeper than them may be unread"
                 " — check the window by run id" % (rs, stamp, until, RUNS_PER_PAGE * page))
            if told is not None:
                told[repo] = (stamp, until)    # Remembered once written, never before
            return deeper, True
        page += 1
        try:
            runs = _listing(gh_api(runs_url(repo, since_date, page)))
        except Exception as e:
            note("note: [%s] runs page %d fetch failed: %s" % (profile, page, e))
            holes.record("runs", "%s page %d: %s" % (rs, page, e))
            return deeper, False
        deeper.extend(runs)
    return deeper, True


def runs_cutoff(state, repo, last_start, cycle_start):
    """How far back this poll pages for ``repo``: ``(cutoff, expired)``.

    * The base is the previous cycle's start (``last_start``, the saved watermark, so a
      restart pages back to where the daemon left off), less :data:`RUNS_CUTOFF_MARGIN`.
      ``cutoff`` is ``None`` (page one only) when there is no previous cycle.
    * A repo whose last read had a hole carries its own earlier start in
      ``state["runs_from"]``: the base stays there until a read completes. A hold older
      than :data:`RUNS_HOLD_MAX` is still READ from this once (the first poll back from a
      long downtime can heal it); ``expired`` is the held start, and the caller releases
      it, told, only if this read is incomplete too.
    * ``state["runs_baseline_at"]`` is the first run's start: never reach behind it. The
      first run seeds page one silently, and anything older is history; reading it on a
      later poll would announce it (a fresh install, or a lost state file, after a burst).
    """
    if last_start is None:
        return None, None
    held_map = state.setdefault("runs_from", {})
    base, expired = last_start, None
    held = _parse_ts(held_map.get(repo))
    if held is not None and held < last_start:
        base = held
        if cycle_start - held > RUNS_HOLD_MAX:
            expired = held
    elif repo in held_map:
        del held_map[repo]
    cutoff = base - RUNS_CUTOFF_MARGIN
    floor = _parse_ts(state.get("runs_baseline_at"))
    if floor is not None and cutoff < floor:
        cutoff = floor
    return cutoff, expired


def legacy_note(profile, state):
    """The daemon-log line for a state that loaded in the version 1 format, else ``None``."""
    legacy = state.get("legacy_seen")
    if not legacy:
        return None
    return ("[%s] the seen list on disk predates the ordered seen list (%d ids, arbitrary order): the first cycle "
            "adopts silently what is stamped %ds or more before its watermark %s"
            % (profile, len(state.get("seen") or ()), int(LEGACY_GUARD.total_seconds()),
               legacy.get("cutoff")))


def _settle_legacy(profile, state, legacy, holes, adopted, cutoff):
    """The end of a cycle that ran with the legacy baseline: say what it adopted, and
    decide whether it runs again. It runs again only while a fetch that feeds the
    memory failed (:data:`SEEN_SUBS`), and only :data:`LEGACY_MAX_CYCLES` times. With no
    usable watermark there is nothing to compare against, so there is nothing to hold."""
    if cutoff is None:
        state["legacy_seen"] = None
        return
    if any(adopted.values()):
        note("[%s] legacy seen list: adopted %d review(s), %d comment(s), %d run(s) silently "
             "(stamped before %s)" % (profile, adopted["review"], adopted["c"], adopted["run"],
                                      cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")))
    unread = sorted(set(holes.failures) & set(SEEN_SUBS))
    legacy["cycles"] -= 1
    if unread and legacy["cycles"] > 0:
        note("[%s] legacy seen list: %s not read this cycle, so the baseline holds for the next (%d left)"
             % (profile, ", ".join(unread), legacy["cycles"]))
        return
    if unread:
        note("[%s] legacy seen list: %s still not read after %d cycles, so the baseline is dropped"
             % (profile, ", ".join(unread), LEGACY_MAX_CYCLES))
    state["legacy_seen"] = None


#: A cross-repo pair is declared in BOTH PR bodies with a trailer line
#: ``Lands-with: <companion>`` (case-insensitive key, one per line, several
#: allowed). "These must land together" written in prose is a hope; this is the
#: form a daemon can read. The companion is a GITHUB pull request, in one of
#: three spellings: ``owner/repo#N``, a full ``https://github.com/owner/repo/pull/N``
#: URL, or ``#N`` for the same repository as the PR carrying the trailer. A
#: Bitbucket half is declared in prose and by the GitHub half's trailer — the
#: daemon has no way to ask Bitbucket "is #N merged" by this route (a
#: Bitbucket-shaped ``workspace/repo#N`` parses like a GitHub one and then
#: 404s forever).
#:
#: The number is at most nine digits. Unbounded, a body
#: carrying ``Lands-with: owner/repo#`` and more than 4,300 digits made ``int()`` raise
#: ``ValueError`` on every interpreter with CPython's int-string cap (3.14, and 3.9.14 and
#: later, which includes CI's), so the same PR raised every cycle and stopped the profile.
#: An oversized number now fails to match, so it reads as an unparsed trailer and is told.
#: No GitHub pull request number comes near nine digits.
_COMPANION = re.compile(
    r"^(?:https?://github\.com/)?([\w.-]+/[\w.-]+)(?:/pull/|#)(\d{1,9})/?$", re.I)
_SAME_REPO = re.compile(r"^#(\d{1,9})$")
#: A fenced code block is documentation ABOUT the trailer, not a declaration
#: its lines are dropped before any line is read.
#:
#: The first version was
#: ``re.compile(r"```.*?```", re.S)``, which understands exactly one fence
#: shape. A TILDE fence and a four-backtick fence holding a three-backtick
#: example are both valid GitHub Markdown and both left their contents
#: visible, so a body DOCUMENTING the trailer produced a real pair window.
#: A fence opens on 3+ backticks or 3+ tildes and closes only on the SAME
#: character, at least as long, carrying no info string.
#:
#: ⚠ Two refinements from codex's review of the first version of this scanner,
#: both of which it reproduced and both of which I confirmed:
#:
#: * **Indentation is not limited to CommonMark's three spaces.** A fenced
#:   example nested under a list item (``1. Example:``) validly carries four or
#:   more, and a three-space rule missed BOTH its fences while the declaration regex
#:   happily read the indented ``Lands-with:`` between them — so documentation
#:   declared a real pair. The regex the scanner replaced had no indentation rule
#:   at all and stripped that block, so a three-space limit was a REGRESSION.
#:   Any leading whitespace opens a fence here: over-stripping is the safe
#:   direction, since a declaration missed inside a code block still surfaces
#:   through the companion PR's own half.
#: * **A backtick fence's info string may not contain a backtick** (CommonMark).
#:   Without that rule a line that is simply an inline code span — ```` ```x``` ````
#:   on its own line — opened a fence that never closed, discarded the rest of
#:   the body, and put a tracked pair straight back on the silent-clear path
#:   this ticket exists to close.
#: ⚠ FIVE review rounds inside this scanner said the APPROACH was wrong, not
#: incomplete. Each round produced another shape of valid GitHub Markdown — a
#: tilde fence, a four-backtick fence, an inline span alone on a line, a fence
#: under a list item, a fence on the list-marker line, a fence behind THREE
#: markers, a multiline inline span — and each time the answer was another rule.
#: That is an unbounded chase against a spec this daemon has no business
#: implementing.
#:
#: The DECLARATION FORM is narrowed instead, which is a thing this project
#: controls: **a declaration is an UNINDENTED line.** `docs/agent-conventions.md`
#: already says the trailer goes "on its own line"; requiring column zero makes
#: that checkable without parsing Markdown, and retires the whole list/indent
#: family in one rule — an example nested in a list, at ANY depth, is indented
#: by construction.
#:
#: Fences still matter for the one case indentation cannot see: an unindented
#: example inside a top-level fence. That scan is deliberately timid — only
#: BALANCED fences strip, and an unclosed one strips NOTHING. An unclosed fence
#: used to swallow the body to EOF, which is how a real declaration went missing
#: and a new pair was never discovered. Discovery has no predicate protecting it
#: (nothing is tracked yet), so its failure must be a FALSE WINDOW — loud and
#: correctable — never silence.
#:
#: Left unhandled, stated rather than discovered later: an unindented
#: `Lands-with:` inside a multiline inline code span that opens and closes
#: mid-line reads as a declaration. Loud, and rare enough to accept.
#: 0–3 leading spaces, which is CommonMark's actual rule for a TOP-LEVEL fence
#: and now the whole job this regex has: anything indented four or more is inside
#: a list or an indented block, and its contents are caught by the column-zero
#: declaration rule instead. Anchoring at column zero exactly (codex, round six)
#: missed an opener indented one space, promoted its unindented CLOSER to an
#: opener, and let a later bare fence close that phantom block — stripping a real
#: declaration in between, and stripping it SILENTLY, which is the property this
#: design exists to rule out.
#: OPENING and CLOSING are different rules, and the asymmetry is the point.
#: Rounds four, six, seven and eight were all ONE generator: an opener the
#: scanner cannot SEE leaves a closer it CAN see, that closer becomes a phantom
#: OPENER, and the next real fence closes the phantom — stripping every line
#: between them, a real declaration included, SILENTLY. Each round moved the
#: hole rather than closing it: 0-3 indent broke on list markers; marker-aware
#: opening with a 0-3 closer broke on ``10. `` (whose content, and closer, sit
#: at four spaces); column-zero-only broke the 0-3 noise suppression that keeps
#: a PR documenting the syntax from firing an event.
#:
#: A phantom is always an OPENER. So the closer is the side that can be relaxed:
#: `_FENCE_CLOSE` is only ever consulted while a fence is already open, and can
#: therefore never invent a span. It refuses a list marker, because inside a
#: fenced ``diff`` block a line like ``- ``` `` is content (round four), and its
#: indent is BOUNDED BY ITS OPENER rather than free (round nine): at most three
#: past the opener's prefix. Unbounded, a ``    ~~~`` sitting as CONTENT inside a
#: top-level tilde fence closed it early, and the real closer then opened a
#: phantom. The bound is what a fence under ``10. `` needs (prefix four, closer
#: four) while still refusing that content line under a prefix of zero.
#:
#: ⚠ KNOWN LIMIT, stated rather than papered over. A fence opening at FOUR
#: spaces under a list paragraph (``1. Example:``) is past the 0-3 opening rule
#: and invisible; if its closer sits at THREE it becomes visible, opens a
#: phantom, and a declaration after it can be stripped. It takes INCONSISTENT
#: indentation inside one block — the same fence written with both ends at four
#: parses correctly. It is not wholly silent either: the example inside it is
#: reported as an unparsed trailer, so the PR carries a visible anomaly. Closing
#: it needs real block-structure tracking, which is where the cost stops being
#: worth it: nine rounds have each traded one constructed shape for another, and
#: the retirement predicate reads the RAW body, so tracked pairs are unaffected.
#:
#: OPENING keeps the 0-3 CommonMark indent, which is load-bearing for more than
#: correctness: a fence at 0-3 is a TOP-LEVEL block, so its example is stripped
#: and stays silent, while at 4+ it is inside a list or an indented block and
#: the example is reported instead. Marker prefixes are accepted because a
#: marker is not indentation — ``- ```sh`` opens a real fence at column zero.
_FENCE_OPEN = re.compile(r"^ {0,3}(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*(`{3,}|~{3,})[ \t]*(.*)$")
_FENCE_CLOSE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")
#: A DECLARATION sits at column zero. Anything indented, quoted or behind a list
#: marker is an example — still surfaced by `unparsed_trailers`, so an author who
#: indents one is told rather than ignored.
_DECLARATION = re.compile(r"^lands[-_ ]?with[ \t]*:[ \t]*(.*?)[ \t]*$", re.I)
_ANY_TRAILER = re.compile(
    r"^[ \t>]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*lands[-_ ]?with[ \t]*:[ \t]*(.*?)[ \t]*$", re.I)


def _normalise(body):
    # A body that is not a string (a number, a list, a dict) is an empty body: it would
    # otherwise raise here, after `pr_opened` / `pr_merged` was written and before the PR was recorded.
    if not isinstance(body, str):
        body = ""
    return body.replace("\r\n", "\n").replace("\r", "\n")


def _unfenced_lines(lines):
    """``lines`` with BALANCED top-level fenced blocks removed; an unclosed fence
    strips nothing, because over-stripping is the failure that is silent."""
    fence, spans, start = None, [], 0
    for i, line in enumerate(lines):
        if fence is None:
            hit = _FENCE_OPEN.match(line)
            if not hit:
                continue
            delim, info = hit.group(1), hit.group(2)
            if delim[0] == "`" and "`" in info:
                continue                      # an inline code span, not a fence
            # third element: the opener's prefix width, which bounds how far its
            # CLOSER may be indented (round nine).
            fence, start = (delim[0], len(delim), hit.start(1)), i
        else:
            hit = _FENCE_CLOSE.match(line)
            if not hit:
                continue
            delim = hit.group(1)
            if (delim[0] == fence[0] and len(delim) >= fence[1]
                    and hit.start(1) <= fence[2] + 3):
                spans.append((start, i))
                fence = None
    dropped = {i for a, b in spans for i in range(a, b + 1)}
    return [line for i, line in enumerate(lines) if i not in dropped]


MAX_PAIRS_PER_BODY = 8


def _trailer_lines(body):
    """The value of every DECLARATION — column zero, outside a balanced fence.

    Line endings are normalised first: `_DECLARATION` is anchored and
    a stray ``\r`` rides into the captured value, so a CRLF body — which is what
    GitHub's web form produces — parsed to nothing, and on a tracked pair that was
    a silent `stalled_cleared` on a still-open window.
    """
    out = []
    for line in _unfenced_lines(_normalise(body).split("\n")):
        hit = _DECLARATION.match(line)
        if hit:
            out.append(hit.group(1))
    return out


def _companion_of(value, repo):
    m = _COMPANION.match(value)
    if m:
        return (m.group(1), int(m.group(2)))
    m = _SAME_REPO.match(value)
    if m and repo:
        return (repo, int(m.group(1)))
    return None


def pairs_of(body, repo=None):
    """``[(owner/repo, number), ...]`` declared by ``Lands-with:`` trailers in a PR
    body — deduplicated, in order, at most :data:`MAX_PAIRS_PER_BODY`. ``repo``
    is the repository of the PR that carries the body; without it the ``#N``
    same-repo spelling cannot be resolved and reads as unparsed."""
    out = []
    for value in _trailer_lines(body):
        pair = _companion_of(value, repo)
        if pair is not None and pair not in out:
            out.append(pair)
    return out[:MAX_PAIRS_PER_BODY]


#: Anything trailer-SHAPED, wherever it sits — fenced, indented, malformed,
#: behind a list marker. Deliberately looser than the declaration regex and applied to
#: the raw body without stripping fences, because it answers a different
#: question: not "what parsed" but "was there anything to parse".
def _any_trailer_lines(body):
    """Every trailer-SHAPED line anywhere — indented, fenced, quoted, marker-led.
    Not "what declares" but "what was written"."""
    out = []
    for line in _normalise(body).split("\n"):
        m = _ANY_TRAILER.match(line)
        if m:
            out.append(m.group(1))
    return out


def companions_mentioned(body, repo=None):
    """Every companion named by a trailer-shaped line ANYWHERE in the body.

    The companion-specific half of the retirement predicate: `pairs_of` returning
    ``[]`` — or a PARTIAL set — cannot tell a withdrawn declaration from one this
    parser failed to read, and an earlier bug shows what that conflation costs.
    """
    out = []
    for value in _any_trailer_lines(body):
        pair = _companion_of(value, repo)
        if pair is not None and pair not in out:
            out.append(pair)
    return out


def unparsed_trailers(body, repo=None):
    """The trailer-shaped lines an author should be TOLD about.

    Two kinds, and both are mistakes rather than documentation: a line that does
    not resolve to a companion at all (a typo, a missing owner, a Bitbucket
    reference), and a line that resolves but is not at column zero, so it was
    never a declaration. The daemon exists because a
    hope in prose is not a sequence, and a near-miss that parsed to nothing
    silently degraded straight back to prose.

    ⚠ Scoped to lines OUTSIDE balanced fences, unlike
    :func:`companions_mentioned`, and the difference is deliberate. A fenced
    `Lands-with:` is a PR documenting the syntax — reporting it would fire an
    event on every such PR, which is noise, not a signal. The generous,
    fences-included scan exists for the retirement predicate, where a fenced
    mention might be a real declaration this parser wrongly fenced and the cost
    of missing it is silence.
    """
    declared = set(_trailer_lines(body))
    out = []
    for line in _unfenced_lines(_normalise(body).split("\n")):
        m = _ANY_TRAILER.match(line)
        if not m:
            continue
        value = m.group(1)
        if (_companion_of(value, repo) is None or value not in declared) and value not in out:
            out.append(value)
    return out


def companion_merged(repo, number):
    """``True`` / ``False`` for the declared companion; raises on a failed fetch, so
    the caller records a hole rather than guessing — an unreachable companion is
    not a closed window."""
    pr = gh_api("repos/%s/pulls/%d" % (repo, number))
    return bool(pr.get("merged_at"))


def settle_pair(profile, holes, entry, body=None):
    """The declared companions of one merged half. ``entry`` is
    ``{repo, number, title, done: [companions already answered], said: [unparsed
    trailers already told]}``. ``body`` is
    the origin's body when the caller has it (the merge transition); otherwise
    the origin is RE-READ, so a trailer corrected after a failed check is what
    gets asked, not the cached one (codex pass 3 on this branch, P2: a mistyped
    number that 404'd was retried forever, ``pairs`` degraded, after the author
    had fixed the body). Emits ``pair_window_open`` for each companion still
    unmerged, once each (``done``). ``True`` when every declared companion
    answered; ``False`` when one did not — the hole is recorded AND the caller
    keeps the entry for the next cycle (codex pass 1, P2: the merge transition
    runs once, so a check lost there was lost for good). A companion that never
    answers keeps the ``pairs`` hole open on purpose: a trailer the daemon cannot
    resolve is a fact worth a degraded line."""
    repo, number = entry["repo"], entry["number"]
    #: The merge transition hands us the body it already scanned, and has already
    #: emitted `pair_trailer_unparsed` for it. Only a RE-READ needs to announce
    #: (codex, round nine) — otherwise a merged body carrying one readable and
    #: one unreadable trailer fires the identical event twice in one cycle. And
    #: the entry it makes carries what it told in `said`: with `said`
    #: empty, the first retry of a failed companion check, which IS a re-read,
    #: told the same unchanged trailer a second time.
    reread = body is None
    if body is None:
        try:
            body = gh_api("repos/%s/pulls/%s" % (repo, number)).get("body")
        except Exception as e:
            note("note: [%s] origin re-read failed for %s#%s: %s" % (profile, repo, number, e))
            holes.record("pairs", "%s#%s: %s" % (repo, number, e))
            return False
    done = entry.setdefault("done", [])
    said = entry.setdefault("said", [])
    settled = True
    declared = pairs_of(body, repo=repo)
    unreadable = unparsed_trailers(body, repo=repo)
    # TELL THE AUTHOR, on this path too and even when something else DID parse
    # (codex, round eight). A pending entry is deleted the moment its readable
    # companions answer, and an already-closed PR never passes the open or merge
    # notification paths again — so this is the LAST place an unreadable trailer
    # beside a readable one can be reported at all.
    # ONCE each, not once per poll: this runs every cycle while the body is
    # unchanged, and unlike the degraded line these events have no suppression —
    # one indented trailer would be ~1,440 wakes a day. `said` rides on the
    # entry, so a CORRECTED body announces again.
    # `one_line` because a PR body is attacker-controllable text: `_normalise`
    # handles CRLF but not U+2028, and `stop_drain.parse_events` splits on
    # `splitlines()`, so an unsanitised value containing a Unicode line
    # separator injects a fabricated Watchtower event. The open and merge paths
    # already do this; this one did not.
    for value in (unreadable[:MAX_PAIRS_PER_BODY] if reread else []):
        if value in said:
            continue
        emit(profile, 'type=pair_trailer_unparsed repo=%s #%s | Lands-with: %s' % (
            one_line(repo.split("/")[-1]), number, one_line(value, 120)))
        said.append(value)
    # POSITIVE EVIDENCE ON THIS PATH TOO (codex, round seven). `stalled_wake`
    # learned it in round four and this function did not: an EMPTY parse is not
    # evidence that there is nothing left to check. A pending entry created
    # before the fix from an INDENTED trailer parses to no declarations now, so
    # the loop below ran zero times, `settled` stayed True, the caller deleted
    # the entry and announced `degraded_recovered` — companion never checked, on
    # the retry path a profile WITHOUT the optional stalled daemon relies on.
    # Not held when SOMETHING parsed: the readable companions are being checked,
    # the events above tell the author about the rest, and holding on a stray
    # unreadable line would wedge the entry on an author who never returns.
    if not declared and unreadable:
        note("note: [%s] %s#%s has trailer-shaped lines that declare nothing — "
             "holding the pair check, not settling it: %s"
             % (profile, repo, number, one_line(", ".join(unreadable[:3]), 160)))
        holes.record("pairs", "%s#%s: trailer-shaped but unreadable" % (repo, number))
        return False
    for c_repo, c_num in declared:
        tag = "%s#%d" % (c_repo, c_num)
        if tag in done:
            continue
        try:
            closed = companion_merged(c_repo, c_num)
        except Exception as e:
            note("note: [%s] companion fetch failed for %s: %s" % (profile, tag, e))
            holes.record("pairs", "%s: %s" % (tag, e))
            settled = False
            continue
        if not closed:
            emit(profile, "type=pair_window_open repo=%s #%s waits-on=%s | %s" % (
                one_line(repo.split("/")[-1]), number, one_line(tag),
                one_line(entry.get("title", ""), 100)))
        done.append(tag)    # After the line, so a write that fails is retried, not lost
    return settled

def load_state(profile):
    try:
        with open(os.path.join(STATE_DIR, "%s.json" % profile)) as fh:
            raw = json.load(fh)
        # A file without the version marker is the arbitrary-subset list the
        # old code wrote. Its order means nothing and its dropped ids are already lost,
        # so its first cycle runs with a baseline (see `poll_profile`). It rides the
        # in-memory state only: once a cycle has written the marked file it is gone.
        legacy = None
        if raw.get("seen_v") != SEEN_V:
            legacy = {"cutoff": raw.get("since"), "cycles": LEGACY_MAX_CYCLES}
        # Every value below is read into the shape the cycle uses, as an earlier change began
        # with `runs_from`: loaded as the file held it, a null or a list where an object belongs
        # (or an entry of the wrong type) raised in every cycle, and the profile was never
        # polled again. No version writes one. `since` is read by `_parse_ts` in the cycle.
        prs = raw.get("prs")
        return {"since": raw.get("since"), "seen": new_seen(raw.get("seen", [])),
                "prs": _prs(prs),
                "wf_last": _wf_last(raw.get("wf_last"), list(prs) if isinstance(prs, dict) else []),
                "degraded": _episodes(raw.get("degraded")),
                "pair_pending": _pair_pending(raw.get("pair_pending")),
                "runs_inflight": _runs_inflight(raw.get("runs_inflight")), "first_run": False,
                "runs_attempt": _runs_attempt(raw.get("runs_attempt")),
                "runs_from": _runs_from(raw.get("runs_from")),
                "runs_baseline_at": raw.get("runs_baseline_at"),
                "runs_checked": _runs_checked(raw.get("runs_checked")),
                "runs_recent": _runs_recent(raw.get("runs_recent")),
                "runs_cap_told": _cap_told(raw.get("runs_cap_told")),
                "reviews_unseeded": _reviews_unseeded(raw.get("reviews_unseeded")),
                "legacy_seen": legacy}
    except Exception:
        return {"since": None, "seen": new_seen(), "prs": {}, "wf_last": {},
                "degraded": {}, "pair_pending": [], "runs_inflight": {}, "runs_attempt": {}, "first_run": True,
                "runs_from": {}, "runs_baseline_at": None, "runs_checked": {}, "runs_recent": {}, "runs_cap_told": {},
                "reviews_unseeded": {}, "legacy_seen": None}


def _prs(raw):
    """The saved PR memory, ``{repo: {number: {"head": …, "state": …}}}``. A repo whose
    value is not an object, or a PR that is not an object holding both keys, is dropped: loaded as
    it was, every cycle raised at ``.setdefault`` or ``.get``, or at ``prev["head"]`` once the PR was
    listed. At worst that PR is announced as opened again, or its close goes untold."""
    if not isinstance(raw, dict):
        return {}
    return dict((repo, dict((n, pr) for n, pr in prs.items()
                            if isinstance(pr, dict) and "head" in pr and "state" in pr))
                for repo, prs in raw.items() if isinstance(prs, dict))


def _wf_last(raw, repos):
    """The scheduled baseline, ``{repo: {workflow name: its last scheduled conclusion}}``.

    It was keyed by NAME alone, across all of a profile's repos, so a scheduled success in one repo
    moved the baseline of another repo's workflow of the same name: that repo's real recovery was
    suppressed as cron noise. A name-keyed entry (a text value at the top level: a file written
    before the scheduled baseline was keyed by repo, or one the previous code wrote into after a rollback) cannot say which repo
    wrote it, and every repo's next scheduled run was compared against it, so it is copied to every
    repo in ``repos``, the file's `prs` keys (every repo the daemon has polled: each cycle records
    one there before it reads its runs). Each repo's first comparison is then the one the code that
    wrote it would have made, and nothing the old key knew is lost. It is copied OVER a repo's own
    entry for that name: beside per-repo entries it was written after them. A conclusion that is not
    text is dropped, which reads as a missing baseline, as a stored null always did, and so
    is anything else that is not this shape."""
    if not isinstance(raw, dict):
        return {}
    out = dict((repo, dict((name, concl) for name, concl in names.items() if isinstance(concl, str)))
               for repo, names in raw.items() if isinstance(names, dict))
    for name, concl in raw.items():
        if isinstance(concl, str):
            for repo in repos:
                out.setdefault(repo, {})[name] = concl
    return out


def _is_time(at):
    """Whether a saved epoch is one the cycle can do arithmetic on: a
    number, not a bool, and finite as a float. JSON has no bound on an integer, so `10**400` loads
    as an int that `float()` cannot hold, and every cycle raised OverflowError on it, as on an
    infinite count."""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return False
    try:
        return math.isfinite(float(at))
    except OverflowError:
        return False


def _episodes(raw):
    """The saved `degraded` episodes, ``{sub: episode}``. An episode `_degraded` cannot read
    (its ``_record`` raises, as an infinite count does) or whose ``last`` or ``last_silenced``
    is not a number (or is one no float can hold, `_is_time`) is dropped, and anything but an
    object is dropped whole: loaded as it was, the degraded tail raised in every cycle. At worst
    a hole is told again or a recovery goes untold."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for sub, value in raw.items():
        try:
            record = _degraded._record(value)
        except Exception:
            continue
        if record is not None and all(_is_time(record[k]) for k in ("last", "last_silenced")):
            out[sub] = value
    return out


def _pair_pending(raw):
    """The saved companion checks, ``[{repo, number, title, done, said}]``. An entry that is
    not an object, or has no number, is dropped (at worst that check is lost), and a ``done`` or
    ``said`` that is not a list reads as empty (at worst a window or an unparsed trailer is told
    again): loaded as they were, the retry raised in every cycle at ``.get``, ``entry["number"]``
    or ``in``."""
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or "number" not in entry:
            continue
        entry = dict(entry)
        for key in ("done", "said"):
            if key in entry and not isinstance(entry[key], list):
                entry[key] = []
        out.append(entry)
    return out


def _is_run_id(rid):
    try:
        int(rid)
    except (TypeError, ValueError):
        return False
    return True


def _runs_inflight(raw):
    """The saved runs in flight, ``{repo: {run id: when it was first tracked}}``. A repo
    whose value is not an object, a run id that is not a number, or a time that is not one
    (`_is_time`), is dropped: loaded as they were, every cycle raised at ``.setdefault``, at
    ordering the runs off the page or at a run's age. At worst a run off the page is no longer
    tracked, so its completion goes untold."""
    if not isinstance(raw, dict):
        return {}
    return dict((repo, dict((rid, at) for rid, at in runs.items()
                            if _is_run_id(rid) and _is_time(at)))
                for repo, runs in raw.items() if isinstance(runs, dict))


def _is_attempt(attempt):
    """Whether a run's ``run_attempt`` is one the retirement key can carry: a whole number from 1
    up, not a bool."""
    return isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1


def _runs_attempt(raw):
    """The saved attempt of each run in flight, ``{repo: {run id: run_attempt}}``, kept
    beside `runs_inflight` rather than inside its entries (those are times, `_runs_inflight`, and a
    file the previous code wrote must still load). A repo whose value is not an object, a run id that
    is not a number, or an attempt that is not a whole number from 1 up, is dropped: at worst a run
    is keyed ``?`` until the next listing or by-id answer says its attempt again."""
    if not isinstance(raw, dict):
        return {}
    return dict((repo, dict((rid, n) for rid, n in runs.items() if _is_run_id(rid) and _is_attempt(n)))
                for repo, runs in raw.items() if isinstance(runs, dict))


def _reviews_unseeded(raw):
    """The open PRs whose reviews the first run could not read, ``{repo: {number: the first
    run's start}}``; see `poll_profile`. Anything not that shape, or a start that is not a timestamp,
    is dropped: at worst that PR's old reviews are announced, as before that memory existed."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for repo, prs in raw.items():
        kept = dict((n, at) for n, at in prs.items() if _parse_ts(at) is not None) if isinstance(prs, dict) else {}
        if kept:
            out[repo] = kept
    return out


def _runs_from(raw):
    """The saved holds, ``{repo: start}``. Anything but an object (a null or a
    list, which no version writes) is dropped whole: loaded as it was, every cycle raised at
    ``.get`` and the profile was never polled again. A start that is not text, text that is not a
    timestamp, or a timestamp within a day of either end of the date range (it raised in
    every cycle, see `_parse_ts`), is dropped by `runs_cutoff` itself. Otherwise, at worst a hold is
    lost, which is what a file the previous code wrote loads as."""
    return dict(raw) if isinstance(raw, dict) else {}


def _runs_checked(raw):
    """The saved rotation memory, ``{repo: {run id: epoch}}``. A repo whose value is
    not an object, or a check time that is not a number (`_is_time`), is dropped: at worst that
    run is ordered as never checked. Loaded as it was, a null or a list made every cycle raise."""
    if not isinstance(raw, dict):
        return {}
    return dict((repo, dict((rid, at) for rid, at in checks.items() if _is_time(at)))
                for repo, checks in raw.items() if isinstance(checks, dict))


def _runs_recent(raw):
    """The saved record of run keys touched lately, ``{repo: {"run:<id>:<attempt>": epoch}}``.
    A repo whose value is not an object, a key that is not text, or a time that is not a number
    (`_is_time`), is dropped, as in `_runs_checked`: loaded as it was, a null or a list made every
    cycle raise. At worst a run a held read can return is not exempt from the cap, and is announced
    again if the cap evicted it meanwhile."""
    if not isinstance(raw, dict):
        return {}
    return dict((repo, dict((key, at) for key, at in recent.items() if isinstance(key, str) and _is_time(at)))
                for repo, recent in raw.items() if isinstance(recent, dict))


def _cap_told(raw):
    """The saved cap memory, ``{repo: [since, until]}`` in JSON, back as the tuples
    `page_back_runs` compares. Anything malformed is dropped: at worst a cap line is told again."""
    if not isinstance(raw, dict):
        return {}
    return dict((repo, tuple(w)) for repo, w in raw.items() if isinstance(w, list) and len(w) == 2)


def save_state(profile, state, checkpointing=False):
    """Write the state file. ``checkpointing`` is the write after a FAILED cycle (see
    :func:`checkpoint`): it keeps the file's mtime, which the watchdog reads as the age of
    the watermark, so a daemon whose cycles keep failing still looks stale; and a checkpoint
    that would change nothing writes nothing. Nor does one with no file to keep, and it notes
    nothing either: a fresh install whose first run ended and then raised in its degraded tail,
    which a restart re-baselines as before checkpointing, or a state file
    removed while the daemon runs (review 5398737519, S7)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "%s.json" % profile)
    tmp = path + ".tmp"
    # `seen` is written oldest-first and already pruned (`poll_profile` does that in
    # the running process); `seen_v` marks the order as meaningful. The old code reads
    # this file unchanged: `seen` is still a flat list and the marker is ignored.
    # `wf_last` is keyed by repo, then name. The previous code reads a repo's slug as a
    # workflow name and finds none, so after a rollback each workflow's first scheduled success is
    # suppressed as if it had been passing; what that code then writes is name-keyed, and the next
    # load copies it to every repo (`_wf_last`).
    text = json.dumps({"since": state["since"], "seen": list(state["seen"]), "seen_v": SEEN_V,
                       "prs": state["prs"], "wf_last": state.get("wf_last", {}),
                       "degraded": state.get("degraded", {}),
                       "pair_pending": state.get("pair_pending", []),
                       "runs_inflight": state.get("runs_inflight", {}),
                       # Read with .get too; the previous code ignores it, and a file it
                       # wrote loads here with no attempts (a retirement key of `?` until one is seen).
                       "runs_attempt": state.get("runs_attempt", {}),
                       # All three new and read with .get (malformed values dropped since
                       # an earlier change), so the previous code ignores them, and a file it wrote loads here
                       # with no hold, no baseline floor and no rotation memory.
                       "runs_from": state.get("runs_from", {}),
                       "runs_baseline_at": state.get("runs_baseline_at"),
                       "runs_checked": state.get("runs_checked", {}),
                       # Read with .get too; the previous code ignores it.
                       "runs_recent": state.get("runs_recent", {}),
                       # Read with .get too; the previous code ignores it.
                       "runs_cap_told": state.get("runs_cap_told", {}),
                       # The same; a rollback loses it, and the PRs it names announce
                       # their old reviews when they answer, as before that memory existed.
                       "reviews_unseeded": state.get("reviews_unseeded", {}), "build": BUILD})
    kept = None
    if checkpointing:
        try:
            kept = os.stat(path)
        except FileNotFoundError:
            return
        with open(path) as fh:
            if fh.read() == text:
                return
    with open(tmp, "w") as fh:
        fh.write(text)
    if kept is not None:
        os.utime(tmp, ns=(kept.st_atime_ns, kept.st_mtime_ns))
    os.replace(tmp, path)


def _count_unwritten_failure(episodes, sub, now):
    """Count this cycle's failure of ``sub`` when the
    degraded tail raised before it folded any of ``sub``'s lines, and record nothing as told.

    The tail folds each sub after its own lines, so a raise at an earlier sub's write or note,
    or at this sub's own first write, left this sub's episode as the cycle before had left it.
    `reconcile` then counted from that old ``consecutive`` and promotes only at ``==``: a hole
    that failed three cycles in a row and then healed never got its ``error: sub-fetch`` line,
    and one that persisted got it a cycle late. Main reconciled every sub before any write, so
    it counted the failure. Here only the count moves:

    * an open episode keeps everything else (its ``last``, whether it was told, its silenced
      count), and its count advances, held one short of the promotion if it reached it, since
      none of this sub's lines was written this cycle;
    * any other failure opens an episode that is NOT told, so its recovery is never announced,
      with the ``last`` it had, or one renotify window ago for a first failure, so a
      ``degraded`` line that was due is still due at the next failure.
    """
    old = _degraded._record(episodes.get(sub))
    consecutive = (old["consecutive"] if old is not None and old["open"] else 0) + 1
    if consecutive == _degraded.PROMOTE_AFTER:
        consecutive -= 1
    if old is None:
        old = {"last": now - _degraded.DEFAULT_RENOTIFY_MIN * 60, "open": False, "announced": False,
               "suppressed": 0, "last_silenced": 0}
    episodes[sub] = dict(old, open=True, announced=old["open"] and old["announced"],
                         consecutive=consecutive)


def _account_unfolded(episodes, holes, folded, now, healed=None):
    """What a cycle that RAISED owes the degraded episodes of the subs it had
    not folded: each failed sub not in ``folded`` has this cycle's failure counted and nothing
    marked told (:func:`_count_unwritten_failure`), and each open episode of a sub that did not
    fail and was not skipped has its count restarted at 0, its episode kept open, and told if it
    was. ``healed`` limits the second to the subs known to have been fetched this cycle: a raise
    before the degraded tail has not looked at the others, which are held as they were. The tail's
    handler and :func:`_count_early_raise` both call this, so they cannot drift apart."""
    failed = sorted(holes.failures)
    for sub in failed:
        if sub not in folded:
            _count_unwritten_failure(episodes, sub, now)
    recovered = set(episodes) - set(failed) - set(holes.skipped) - set(folded)
    if healed is not None:
        recovered &= set(healed)
    for sub in sorted(recovered):
        old = _degraded._record(episodes.get(sub))
        if old is not None and old["open"]:
            episodes[sub] = dict(old, consecutive=0)


#: The key under which a running cycle leaves its `Cycle`, its set of ``(repo, sub)``
#: fetched OK and its repos on ``state``, for :func:`checkpointed` to account if the cycle raises
#: before its degraded tail. Never saved (`save_state` writes a fixed list of keys); removed when
#: the tail begins to account for itself, and by the handler.
_OPEN_CYCLE = "_open_cycle"

#: Which repos an open degraded episode failed in, ``sub -> {repo}``, kept on ``state``
#: in memory only. A sub's episode is one record for the whole profile (it does not carry the repo),
#: so this is how a raising cycle knows whose success may heal it. Absent for an episode a restart
#: reloaded until the next cycle begins, when :func:`_seed_episode_repos` gives it every repo of the
#: profile: every one of them then has to have answered.
_EPISODE_REPOS = "_episode_repos"


def _track_repos(holes):
    """Make ``holes``, a `Cycle`, remember which repo each failure was recorded in:
    ``holes.failed_in`` maps a sub to the repos it failed in, ``None`` standing for a failure
    recorded outside the repo loop (the watermark, a pending companion check), which belongs to
    every repo. `poll_profile` sets ``holes.repo`` at the top of each repo's turn. Done on the
    instance, so ``holes`` stays whatever `_degraded.Cycle` makes. Returns ``holes``."""
    holes.repo = None
    holes.failed_in = {}
    record = holes.record

    def record_in_repo(sub, detail):
        try:
            holes.failed_in.setdefault(str(sub), set()).add(holes.repo)
        except Exception:
            pass
        return record(sub, detail)

    holes.record = record_in_repo
    return holes


def _remember_episode_repos(state, holes, repos):
    """Add the repos this cycle's failures were recorded in to ``state``'s map of where
    each open episode failed, and forget every sub whose episode is no longer open. Called once
    the cycle's episodes are as it leaves them. Never raises."""
    try:
        owners = state.setdefault(_EPISODE_REPOS, {})
        for sub, where in (getattr(holes, "failed_in", None) or {}).items():
            owners.setdefault(sub, set()).update(set(repos) if None in where else where)
        episodes = state.get("degraded") or {}
        for sub in list(owners):
            record = _degraded._record(episodes.get(sub))
            if record is None or not record["open"]:
                del owners[sub]
    except Exception:
        pass


def _seed_episode_repos(state, repos):
    """At the start of a cycle, give every open episode that has no entry in the map of
    where it failed (one a restart reloaded: the map is in memory only) every repo of the profile.
    Without it the first raising cycle after a restart would create an entry of only the repos that
    cycle failed in, and an answer from those alone would restart an episode that failed elsewhere.
    An episode that opens in this cycle has no record yet and is not seeded. Never raises."""
    try:
        owners = state.setdefault(_EPISODE_REPOS, {})
        for sub, value in (state.get("degraded") or {}).items():
            record = _degraded._record(value)
            if record is not None and record["open"] and sub not in owners:
                owners[sub] = set(repos)
    except Exception:
        pass


def _answered_pairs(attempted_ok, holes):
    """The ``(repo, sub)`` pairs of ``attempted_ok`` that count as answered: a failure of a
    sub in a repo outweighs an answer of that sub in that repo in the same cycle, whichever request
    answered (``runs`` is fed by the plain listing, the status listings and the by-id fetches). A
    failure recorded outside the repo loop (``None`` in ``holes.failed_in``) belongs to every repo."""
    failed = getattr(holes, "failed_in", None) or {}
    return set((repo, sub) for repo, sub in attempted_ok
               if repo not in failed.get(sub, ()) and None not in failed.get(sub, ()))


def _healed_subs(state, attempted_ok, repos):
    """The subs whose open episode a raising cycle may restart: one answered in EVERY
    repo it failed in (:data:`_EPISODE_REPOS`), or in every repo of the profile where that is not
    known. A success in another repo says nothing about the one that failed. ``attempted_ok`` is
    what :func:`_answered_pairs` returns."""
    owners = state.get(_EPISODE_REPOS) or {}
    every = set(repos)
    healed = set()
    for sub in set(s for _, s in attempted_ok):
        need = (set(owners.get(sub) or ()) & every) or every
        if all((repo, sub) in attempted_ok for repo in need):
            healed.add(sub)
    return healed


def _count_early_raise(state, now):
    """A cycle raised BEFORE its degraded tail (a failed PR listing, which raises by
    design, or a failed run, PR, review or comment write): it reconciled no sub-fetch, so the
    failures it had recorded were counted by nobody and a sub it had fetched fine was never seen
    to heal, and the promotion count drifted from the one an unraised cycle would keep. Account
    them as the tail's handler does (:func:`_account_unfolded`), the healed subs being those fetched
    OK in the repos their episode failed in (:func:`_healed_subs`). A cycle that raised inside the
    tail has taken its marker already and is not counted twice. Never raises: the cycle's own
    exception is what the caller re-raises."""
    open_cycle = state.pop(_OPEN_CYCLE, None)
    if open_cycle is None:
        return
    try:
        holes, attempted_ok, repos = open_cycle
        episodes = state.setdefault("degraded", {})
        answered = _answered_pairs(attempted_ok, holes)
        _account_unfolded(episodes, holes, set(), now, healed=_healed_subs(state, answered, repos))
        _remember_episode_repos(state, holes, repos)
    except Exception:
        pass


class Terminated(BaseException):
    """The daemon was sent SIGTERM (launchd stopping it). A BaseException on purpose:
    `poll_profile` has many ``except Exception`` around its fetches, which would swallow an
    ``Exception`` subclass and keep the cycle running until launchd's SIGKILL."""


#: When SIGTERM was received (on `_monotonic`), or ``None``. Set by :func:`_on_sigterm`,
#: read and cleared by :func:`stop_point`.
_STOP_AT = None

#: True only inside :func:`_sleep`, where nothing is half recorded.
_IDLE = False


def stop_point():
    """Raise :class:`Terminated` here if SIGTERM was received, and clear the request.

    The handler raised at any bytecode, which broke every two-statement invariant of the cycle
    (an id moved to the newest position in two steps, a line written and then recorded): a stop
    between the two halves was checkpointed as it stood. Now the handler only records the
    request, and the cycle acts on it where its state is settled: before and after each
    `gh api` call and inside its wait (:func:`gh_api`), at the top of each repo, at the top of
    each sub-resource of the degraded tail, and in :func:`_sleep`. A request is not lost by the
    test-and-clear: one that arrives after the test is seen at the next point."""
    global _STOP_AT
    if _STOP_AT is None:
        return
    _STOP_AT = None
    raise Terminated()


def _on_sigterm(signum, frame):
    """`main()` registers this for SIGTERM. It records the request and returns, so the
    cycle stops at its next :func:`stop_point` and :func:`checkpointed` saves what it had
    recorded. Inside :func:`_sleep` there is nothing to settle, and it raises there at once."""
    global _STOP_AT
    if _STOP_AT is None:
        _STOP_AT = _monotonic()
    if _IDLE:
        stop_point()


def _sleep(secs):
    """`time.sleep(secs)` that ends the moment SIGTERM is received: `time.sleep`
    resumes after a handler that returns (PEP 475), so a flag alone would not wake it. Between
    cycles the state is settled, so the handler may raise inside this bracket. A request made
    before the bracket opened is seen by the first :func:`stop_point` in it, and one made after
    it closed by the next point of the cycle."""
    global _IDLE
    _IDLE = True
    try:
        stop_point()
        time.sleep(secs)
    finally:
        _IDLE = False
    stop_point()


def checkpoint(profile, state):
    """Save what a cycle that RAISED had already recorded.

    The state used to be saved only at the end of a completed cycle, so every line a raising
    cycle wrote (a failed PR listing raises it by design) was remembered by the running
    process alone. A restart reloaded the file from before it and wrote again what the streak
    had written inside the window the restart re-reads: up to 100 runs per repo with the
    page-back, plus the cap and hold_expired lines and every PR, pair, review and comment line.

    Saving the state as it stands is safe because every line is recorded only AFTER it is
    written and nothing moves a cutoff past what was not: ``since``
    moves only once every repo has been read, and a repo's runs hold only once its runs are
    announced. So what a restart reloads is what the running process would carry into its
    next cycle. Not for a first run that raises before every repo is read, nor a
    legacy-baseline cycle: their baseline must run to completion, so a restart repeats their
    cycle as before. (A first run that raises in its degraded tail has already ended, and is
    checkpointed when a state file exists and reads as UTF-8; over one that does not, the
    checkpoint is noted and a restart re-baselines, as before checkpointing.) A checkpoint that cannot
    be saved is noted, except that with no state file (a fresh install, or one removed while the
    daemon runs) nothing is written or noted. Either way the cycle's own failure stands.

    And told: a ``type=degraded sub=checkpoint`` line in the profile log, at most once
    per ``_degraded.DEFAULT_RENOTIFY_MIN`` (when it was last told is kept on ``state`` in memory,
    never saved), and a ``type=degraded_recovered sub=checkpoint`` line the next time a save
    succeeds after one was told: a checkpoint's, or the save at the end of a completed cycle
    (`poll_profile`), which is how a streak of failed cycles that ends in completed ones says so.
    Each is written in its own try: the log's disk may be the one that
    is failing, and then only the note remains. The checkpoint is not a fetch, so it is never
    promoted to an ``error: sub-fetch`` line."""
    if state.get("first_run") or state.get("legacy_seen"):
        return
    try:
        save_state(profile, state, checkpointing=True)
    except Exception as e:
        note("note: [%s] the failed cycle's checkpoint was not saved: %s" % (profile, e))
        _tell_checkpoint(profile, state, e)
    else:
        _tell_checkpoint_recovered(profile, state)


#: When a checkpoint failure was last written to the profile log, kept on ``state``
#: in memory only (`save_state` writes a fixed list of keys). Present only while one is told.
_CHECKPOINT_TOLD = "_checkpoint_told"


def _tell_checkpoint(profile, state, error):
    """The ``degraded sub=checkpoint`` line for a checkpoint that could not be saved,
    unless one was written within the renotify window. Marked told only once it is written, so a
    log that cannot be written leaves only the note, and the next failure tries again."""
    try:
        now = time.time()
        told = state.get(_CHECKPOINT_TOLD)
        if told is not None and now - told < _degraded.DEFAULT_RENOTIFY_MIN * 60:
            return
        emit(profile, "type=degraded sub=checkpoint detail=%s" % _degraded.one_line(
            "the failed cycle's checkpoint was not saved: %s" % (error,)))
        state[_CHECKPOINT_TOLD] = now
    except Exception:
        pass


def _tell_checkpoint_recovered(profile, state):
    """``degraded_recovered sub=checkpoint``, once, for a checkpoint that saves after a
    failure was told. Forgotten only once written: a log that cannot be written is tried again by
    the next checkpoint that saves."""
    if state.get(_CHECKPOINT_TOLD) is None:
        return
    try:
        emit(profile, "type=degraded_recovered sub=checkpoint")
        del state[_CHECKPOINT_TOLD]
    except Exception:
        pass


def checkpointed(cycle):
    """``cycle``, checkpointed when it raises (:func:`checkpoint`), and re-raised so
    `main()` still counts the cycle as failed. A decorator rather than a wrapper function so
    that `poll_profile` keeps its own body under its own name, which the structural guards in
    tests/test_degraded.py read."""
    @functools.wraps(cycle)
    def run(profile, cfg, state):
        try:
            return cycle(profile, cfg, state)
        except (Exception, Terminated):  # A SIGTERM mid-cycle is checkpointed too
            # Counted BEFORE the save, so the checkpoint holds the counts: a raise
            # before the degraded tail reconciled nothing (a raise inside it has been counted).
            _count_early_raise(state, time.time())
            checkpoint(profile, state)
            raise
    return run


def read_comments(profile, repo, endpoint, path, holes, told, quiet=False):
    """One comments endpoint of one repo, read page after page: the comments on it.

    ``path`` is the first page's request. A further page (``&page=N``) is asked for only after
    a FULL page (:data:`COMMENTS_PER_PAGE`), up to :data:`COMMENTS_MAX_PAGES`, so the steady
    state is one request. A page that fails is noted and recorded as a ``comments`` hole,
    and what the pages before it answered is kept.

    Reaching the bound with a full last page is told by a ``type=unread_comments
    reason=page_cap`` line, outside the degraded rate limit, ONCE per episode: ``told`` is the
    per-profile set of ``(repo, endpoint)`` that already said so, and the next read of that
    endpoint that ends short of the bound closes the episode. ``quiet`` (the first run, whose
    comments are a baseline and not news) notes the hit and says nothing. ``told`` is
    in-memory: a restart tells an episode that is still going once more."""
    rs = one_line(repo.split("/")[-1])      # Shown, never used as a key
    key = (repo, endpoint)
    got = []
    for page in range(1, COMMENTS_MAX_PAGES + 1):
        try:
            batch = gh_api(path if page == 1 else "%s&page=%d" % (path, page))
        except Exception as e:
            if page == 1:
                note("note: [%s] comment fetch failed: %s" % (profile, e))
                holes.record("comments", "%s: %s" % (rs, e))
            else:
                note("note: [%s] %s %s comments page %d fetch failed, the %d read before it are kept: %s"
                     % (profile, rs, endpoint, page, len(got), e))
                holes.record("comments", "%s %s page %d: %s" % (rs, endpoint, page, e))
            return got
        got.extend(batch)
        if len(batch) < COMMENTS_PER_PAGE:
            told.discard(key)
            return got
    since = re.search(r"since=([^&]+)", path)
    stamp = since.group(1) if since else "?"
    note("note: [%s] %s %s comments page cap reached: %d pages read and the last was full"
         % (profile, rs, endpoint, COMMENTS_MAX_PAGES))
    if quiet or key in told:
        return got
    emit(profile, "type=unread_comments repo=%s endpoint=%s reason=page_cap since=%s | the oldest %d "
         "comments were read; a comment made since then and newer than them may be unread — check "
         "the repo's %s comments" % (rs, endpoint, stamp, len(got), endpoint))
    told.add(key)                          # remembered once written, never before
    return got


@checkpointed
def poll_profile(profile, cfg, state):
    repos = cfg.get("repos") or []
    if not repos:
        return
    suppress = set(cfg.get("suppress_authors", []))
    seen = _ordered_seen(state)
    first_run = state["first_run"]
    # `_parse_ts`, as for `last_start` below: `fromisoformat` raised TypeError on a saved
    # watermark that is not text, and the subtraction below OverflowError on one at the edge of
    # the date range, in every cycle. Either is now a watermark that does not parse, told below.
    since_dt = _parse_ts(state.get("since"))
    # The runs page-back reads back to the previous cycle's start (the saved
    # watermark) less a margin, per repo; see `runs_cutoff`. The first run has no
    # previous poll, and stamps the baseline floor no later poll reaches behind.
    last_start = None
    if not first_run:
        last_start = _parse_ts(state.get("since"))
        if last_start is None:
            note("note: [%s] watermark %r does not parse: no runs page-back this cycle"
                 % (profile, state.get("since")))
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
    # The comments window is the watermark less OVERLAP, but never earlier than the first
    # run's start (`runs_baseline_at`, the floor the runs page-back already had): what came before
    # the first run is history. A first run whose comments fetch failed seeded none of them, and the
    # next cycle announced the comments made in the 5 minutes before it. Once the watermark is
    # OVERLAP past the first run the floor no longer binds; where the first run did read them they
    # are in `seen`, so nothing it announces changes.
    comments_from = since_dt - OVERLAP
    floor = None if first_run else _parse_ts(state.get("runs_baseline_at"))
    if floor is not None and comments_from < floor:
        comments_from = floor
    comments_since = comments_from.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_date = (since_dt - OVERLAP).strftime("%Y-%m-%d")
    cycle_start = datetime.now(timezone.utc)
    if first_run:
        state["runs_baseline_at"] = cycle_start.isoformat()
    baseline = [0, 0, 0]
    # The first cycle after a version 1 state file. What the old daemon had
    # already announced was cut to an arbitrary 2000 ids, so the ids it dropped are
    # lost and the API still returns items that nobody can prove were announced.
    # Such an item is adopted SILENTLY — remembered, not announced — but only on
    # positive evidence: its own timestamp is at or before the old file's watermark
    # minus LEGACY_GUARD, i.e. it existed when the old daemon last looked. The
    # watermark, not the load time, is the boundary: an event that happened while
    # the daemon was down is exactly what resume coverage is for, and stays news.
    # The cutoff is the FILE's watermark, fixed for the whole baseline: `state["since"]`
    # moves on with every completed cycle and a held baseline must not move with it.
    legacy = state.get("legacy_seen")
    cutoff = None
    if legacy:
        marked = _parse_ts(legacy.get("cutoff"))
        if marked is not None:
            cutoff = marked - LEGACY_GUARD
    adopted = {"review": 0, "c": 0, "run": 0}
    # Holes in THIS cycle. Recorded beside every `note()` below, then
    # reconciled once at the end so the profile log learns what was not fetched.
    holes = _track_repos(_degraded.Cycle())
    # The (repo, sub) pairs fetched OK so far, and the cycle's holes, where `checkpointed`
    # can find them if the cycle raises before the degraded tail below (`_count_early_raise`).
    attempted_ok = set()
    state[_OPEN_CYCLE] = (holes, attempted_ok, list(repos))
    _seed_episode_repos(state, repos)      # An episode a restart reloaded failed in who knows which repos
    if not first_run and last_start is None:
        holes.record("runs", "watermark %r does not parse; no runs page-back" % (state.get("since"),))
    # Companion checks an earlier cycle could not complete, retried first;
    # what fails again (and what fails below) is what the next cycle retries. An
    # entry whose origin repo has left `repos` is dropped, not retried (codex
    # pass 2, P2): the operator stopped watching that repo, and its window is
    # not this profile's to announce.
    pending, dropped = [], 0
    for entry in (state.get("pair_pending") or []):
        if entry.get("repo") not in repos:
            dropped += 1
        elif not settle_pair(profile, holes, entry):
            pending.append(entry)
    if dropped:
        note("[%s] dropped %d pending companion check(s) for repos no longer configured" % (profile, dropped))
    state["pair_pending"] = pending      # appended to below, by name, for every merge that could not settle

    for repo in repos:
        stop_point()    # Between repos, where the previous repo's lines are all recorded
        rs = one_line(repo.split("/")[-1])      # Shown, never used as a key
        holes.repo = repo                  # The repo a failure recorded from here on belongs to
        prs_state = state["prs"].setdefault(repo, {})

        # ⚠ Deliberately NOT caught: a failed PR listing raises
        # the cycle, so the watermark does not move and a total outage is a failed cycle
        # (`main()`'s `error: github polling failing repeatedly`, a stale state file). Caught,
        # the cycle completed with holes and moved the watermark past comments it never read,
        # and a first run or a version 1 file cleared its baseline on a listing that never
        # answered. A frozen watermark re-reads the same runs window every poll; the cap
        # line's (since, until) guard in `page_back_runs` keeps an unchanged window from being
        # written again, and each poll in which the 100th newest run's creation second has
        # moved still writes one more line.
        prs = gh_api("repos/%s/pulls?state=all&sort=updated&direction=desc&per_page=%d"
                     % (repo, PRS_PER_PAGE))
        open_numbers = []
        for pr in prs:
            n = str(pr["number"])
            head = _sha9(pr.get("head"))
            st = pr.get("state")
            merged = bool(pr.get("merged_at"))
            if st == "open":
                open_numbers.append(n)
            prev = prs_state.get(n)
            if prev is None:
                if first_run:
                    baseline[0] += 1
                elif st == "open":
                    emit(profile, 'type=pr_opened repo=%s #%s by=%s | %s' % (
                        rs, n, _login(pr.get("user")),
                        one_line(pr.get("title", ""), 100)))
                    # A trailer that almost
                    # parses is told to its author NOW, while the PR is open
                    # and the body is still theirs to fix.
                    for value in unparsed_trailers(pr.get("body"), repo=repo):
                        emit(profile, 'type=pair_trailer_unparsed repo=%s #%s | Lands-with: %s' % (
                            rs, n, one_line(value, 120)))
                # A PR is recorded only after its lines are written, as a transition
                # below always was: a write that fails leaves it to be announced next cycle.
                prs_state[n] = {"head": head, "state": st}
                continue
            if prev["head"] != head and st == "open":
                emit(profile, "type=pr_push repo=%s #%s head=%s (was %s)" % (
                    rs, n, one_line(head, 40), one_line(prev["head"], 40)))
            if prev["state"] == "open" and st == "closed":
                emit(profile, "type=%s repo=%s #%s | %s" % (
                    "pr_merged" if merged else "pr_closed", rs, n,
                    one_line(pr.get("title", ""), 100)))
                if merged:
                    # A merged half whose declared companion is still
                    # unmerged is a LIVE WINDOW: a feature exists and the thing
                    # that makes it safe does not. Say so as an event, at the
                    # moment it opens. `stalled_wake` carries it until it closes.
                    # This transition runs ONCE (the PR is recorded closed before
                    # the check, whatever it does), so a check that fails here
                    # goes to `pair_pending` and is retried every cycle until it
                    # answers (codex pass 1, P2).
                    said = []
                    for value in unparsed_trailers(pr.get("body"), repo=repo):
                        emit(profile, 'type=pair_trailer_unparsed repo=%s #%s | Lands-with: %s' % (
                            rs, n, one_line(value, 120)))
                        said.append(value)
                    if pairs_of(pr.get("body"), repo=repo):
                        # `said` is what was just told: the retry of a check that fails
                        # here re-reads the body and tells only what is not in it.
                        entry = {"repo": repo, "number": n, "title": one_line(pr.get("title", ""), 100),
                                 "done": [], "said": said}
                        # RECORDED BEFORE ASKED. `settle_pair` calls `gh_api`, whose safe
                        # points can stop the cycle; the lines above are already written. The PR
                        # (closed) and its entry are recorded first, and the entry is the one
                        # `settle_pair` updates as it tells each companion, so a stop inside it is
                        # checkpointed with the merge recorded, the entry kept and `done` as far
                        # as it got: the restart repeats neither the merge, nor `said`, nor a
                        # companion already told. Removed again below if it settles. (The two
                        # statements have no safe point between them.)
                        prs_state[n] = {"head": head, "state": st}
                        state["pair_pending"].append(entry)
                        if settle_pair(profile, holes, entry, body=pr.get("body")):
                            state["pair_pending"][:] = [e for e in state["pair_pending"] if e is not entry]
            prs_state[n] = {"head": head, "state": st}

        # An open PR whose reviews the first run could not read. Reviews are asked for
        # with no `since`, so the first time they answered every one was news: up to 20 old reviews.
        # The PR is remembered with the first run's start (saved with the state), and when its
        # reviews first answer, those submitted at or before that start are seeded silently, as
        # the first run would have; a later one, or one with no readable time, is announced.
        unseeded = state.setdefault("reviews_unseeded", {})
        for n in open_numbers:
            try:
                reviews = gh_api("repos/%s/pulls/%s/reviews?per_page=%d" % (repo, n, REVIEWS_PER_PAGE))
                attempted_ok.add((repo, "reviews"))
            except Exception as e:
                note("note: [%s] reviews fetch failed for #%s: %s" % (profile, n, e))
                holes.record("reviews", "%s #%s: %s" % (rs, n, e))
                if first_run:
                    unseeded.setdefault(repo, {})[n] = cycle_start.isoformat()
                continue
            seed_until = None if first_run else _parse_ts(unseeded.get(repo, {}).get(n))
            seeded = 0
            for rv in reviews:
                mid = "review:%s" % rv.get("id")
                if mid in seen:
                    touch_seen(seen, mid)
                    continue
                who = _login_raw(rv.get("user"))          # Compared raw, shown flattened
                if first_run:
                    baseline[2] += 1
                elif _predates(rv.get("submitted_at"), cutoff):
                    adopted["review"] += 1
                elif _predates(rv.get("submitted_at"), seed_until):
                    seeded += 1
                elif who not in suppress:
                    # `id=` is not decoration. An empty-bodied COMMENTED review renders
                    # byte-identical to the previous one from the same author on the same PR, so
                    # without it a reader cannot tell a repeat from a second review.
                    emit(profile, 'type=review repo=%s #%s id=%s by=%s %s | %s' % (
                        rs, n, one_line(rv.get("id")), one_line(who), one_line(rv.get("state", "?"), 40), one_line(rv.get("body", ""))))
                # Remembered only once its line is written (or none is owed): a
                # write that fails leaves the review to be announced next cycle, not lost.
                seen[mid] = None
            # Forgotten only once every review has been judged: a write that fails
            # above leaves the rest to be seeded by the next cycle, not announced.
            if n in unseeded.get(repo, {}):
                if seeded:
                    note("[%s] %s #%s: its reviews answered for the first time since the first run could not "
                         "read them: seeded %d silently (submitted at or before %s)"
                         % (profile, repo, n, seeded, unseeded[repo][n]))
                del unseeded[repo][n]
                if not unseeded[repo]:
                    del unseeded[repo]

        for endpoint in ("issues", "pulls"):
            comments = read_comments(
                profile, repo, endpoint,
                "repos/%s/%s/comments?since=%s&per_page=%d" % (repo, endpoint, comments_since, COMMENTS_PER_PAGE),
                holes, state.setdefault("comments_cap_told", set()), quiet=first_run)
            attempted_ok.add((repo, "comments"))
            for c in comments:
                mid = "c:%s" % c.get("id")
                if mid in seen:
                    touch_seen(seen, mid)
                    continue
                who = _login_raw(c.get("user"))           # Compared raw, shown flattened
                if first_run:
                    pass                                  # the baseline: remembered, not announced
                elif _predates(c.get("created_at"), cutoff):
                    adopted["c"] += 1
                elif who not in suppress:
                    emit(profile, 'type=comment repo=%s #%s id=%s by=%s | %s' % (
                        rs, issue_number(c.get("issue_url") or c.get("pull_request_url")),
                        one_line(c.get("id")), one_line(who), one_line(c.get("body", ""))))
                seen[mid] = None                           # As for reviews above

        run_cutoff, expired = runs_cutoff(state, repo, last_start, cycle_start)
        # The listing's `created>=` date must reach the CUTOFF,
        # not only the watermark. A hold kept open across 00:00 UTC otherwise asked for a
        # listing that no longer held the runs it was kept for, read a short page one as
        # complete, and released the hold unread.
        repo_date = since_date
        if run_cutoff is not None:
            repo_date = min(since_date, run_cutoff.strftime("%Y-%m-%d"))
        complete = True
        try:
            page = _listing(gh_api(runs_url(repo, repo_date)))
            attempted_ok.add((repo, "runs"))
        except Exception as e:
            note("note: [%s] runs fetch failed: %s" % (profile, e))
            holes.record("runs", "%s: %s" % (rs, e))
            page, complete = [], False
        inflight = state.setdefault("runs_inflight", {}).setdefault(repo, {})
        # The deeper pages, when a burst may have pushed a run created since
        # the previous poll off page one. What joins the page from them: every run still
        # going (in-flight tracking follows it from here, whatever its `created_at`: a re-run keeps
        # its original one), every run already tracked, and every completed run created
        # after the cutoff — news nobody can have read yet. A completed run older than
        # the cutoff on the straddling page is NOT read: it is from before the previous
        # poll, so if `seen` has lost it (an eviction, a lost or legacy file) announcing
        # it now would be a replay; one with no usable `created_at` cannot be proven new
        # and is not read either. A run repeated across pages (created between two page
        # requests) joins once.
        if complete:
            deeper, complete = page_back_runs(profile, repo, repo_date, page, run_cutoff, holes,
                                              told=state.setdefault("runs_cap_told", {}))
            known = set(str(run.get("id")) for run in page)
            for run in deeper:
                rid = str(run.get("id"))
                if not run.get("id") or rid in known:
                    continue
                if (run.get("status") != "completed" or rid in inflight
                        or _created_after(run.get("created_at"), run_cutoff)):
                    known.add(rid)
                    page.append(run)
        # A RE-RUN keeps its run's original `created_at`, so it sits among the old runs
        # of the listing above and below, behind pages nothing reads; and once an attempt is
        # announced the run is no longer tracked, so a later attempt (attempt 3 of a PR's CI)
        # was never seen. So every cycle also asks which runs are going NOW,
        # by status and with no date filter (the status is the current attempt's, whatever the
        # run's age). They join `runs_inflight` below exactly as page one's runs do, and the
        # by-id fetch settles them. A status read that fails is a hole in the runs sub, noted and
        # recorded, but it holds no cutoff: the cutoff belongs to the plain listing. Two more
        # requests a repo a cycle.
        going = []
        for status in RUNS_STATUS_LISTINGS:
            try:
                listed = _listing(gh_api(runs_status_url(repo, status)))
                attempted_ok.add((repo, "runs"))
            except Exception as e:
                note("note: [%s] runs fetch (status=%s) failed: %s" % (profile, status, e))
                holes.record("runs", "%s status=%s: %s" % (rs, status, e))
            else:
                going.extend(listed)
                note_status_listing(profile, repo, status, listed, state.setdefault("runs_status_full", set()))
        # ⚠ THE PAGE IS ONLY THE 20 NEWEST RUNS, and a run is judged on
        # it only once it is completed. A merge in one busy repo starts six or
        # seven runs, so a ~36-minute CI@main run still going when two or three
        # more merges land has fallen off the page by the time it finishes, and
        # its completion was never emitted: not late, never. Six of the nine
        # CI@main outcomes on 2026-09-29 never arrived; on an earlier night one
        # of the missing six was a failure. So a run seen in flight is
        # remembered, and once it is off the page it is fetched by id until it
        # completes. That also covers a run created before midnight, which the
        # `created>=<date>` filter drops from the page once the date turns.
        # ("The page" below is page one plus what the page-back added.)
        now = time.time()
        # The attempt of each tracked run is saved where the run joins, and again where a
        # by-id answer returns, so the retirement key below does not depend on the run still being
        # listed then: a run parked at a deployment gate (`waiting`) is in neither status listing and
        # is off the page.
        attempt_of = state.setdefault("runs_attempt", {}).setdefault(repo, {})
        for run in page + going:
            if run.get("status") != "completed" and run.get("id"):
                inflight.setdefault(str(run["id"]), now)
                if _is_attempt(run.get("run_attempt")):
                    attempt_of[str(run["id"])] = run["run_attempt"]
        on_page = set(str(run.get("id")) for run in page)
        # Least recently ATTENDED first: a run's last by-id check, or, never checked,
        # when it was first tracked; oldest id among equals. Never-checked-first let an inflow
        # of ten newly tracked runs a poll postpone every re-check. `runs_checked` is saved
        # with the state, so a restart does not restart the rotation.
        checked = state.setdefault("runs_checked", {}).setdefault(repo, {})
        for rid in [k for k in checked if k not in inflight]:
            del checked[rid]
        off_page = sorted((k for k in inflight if k not in on_page),
                          key=lambda k: (checked.get(k) or inflight[k], int(k)))
        if len(off_page) > INFLIGHT_FETCH_CAP:
            # The cap bounds the REST cost, and a budget
            # squeeze from runs parked on queued/waiting needs a visible cause.
            # Daemon log only: it is operational, not something a session acts on.
            note("note: [%s] %s has %d runs in flight off the page: checking %d this cycle, %d wait"
                 % (profile, repo, len(off_page), INFLIGHT_FETCH_CAP, len(off_page) - INFLIGHT_FETCH_CAP))
        fetched = []
        for rid in off_page[:INFLIGHT_FETCH_CAP]:
            checked[rid] = now
            if now - inflight[rid] > INFLIGHT_MAX_SECS:
                note("note: [%s] %s run %s still not completed after %dh — no longer tracking it"
                     % (profile, repo, rid, INFLIGHT_MAX_SECS // 3600))
                # The profile log says it too: with the by-id fetch failing, or another
                # repo's listing dead, this note was the only trace and the outcome was never
                # announced. Written BEFORE the run is dropped, and remembered only once written.
                # The status listing tracks a run that is still going again next cycle,
                # which would retire it again every INFLIGHT_MAX_SECS: so the line is told once per
                # run and attempt, remembered in `seen` (saved with the state, so a restart repeats
                # nothing; its prefix is unsized, so it shares the bounded `SEEN_CAP_OTHER` bucket).
                told = "retired:%s:%s" % (rid, attempt_of.get(rid, "?"))
                if told not in seen:
                    emit(profile, "type=unread_runs repo=%s reason=retired run=%s | tracked %d h without "
                         "completing; its outcome was not announced, check it by id"
                         % (rs, one_line(rid), INFLIGHT_MAX_SECS // 3600))
                    seen[told] = None
                del inflight[rid]
                continue
            try:
                answer = gh_api("repos/%s/actions/runs/%s" % (repo, rid))
                fetched.append(answer)
                attempted_ok.add((repo, "runs"))
                if isinstance(answer, dict) and _is_attempt(answer.get("run_attempt")):
                    attempt_of[rid] = answer["run_attempt"]
            except Exception as e:
                if "HTTP 404" in str(e):
                    # Gone (deleted): not a coverage hole, and asking again every
                    # minute would never get a different answer.
                    note("note: [%s] %s run %s is gone (404) — no longer tracking it" % (profile, repo, rid))
                    del inflight[rid]
                else:
                    note("note: [%s] run fetch failed for %s: %s" % (profile, rid, e))
                    holes.record("runs", "%s run %s: %s" % (rs, rid, e))
        # Oldest-first so per-workflow conclusion transitions are tracked in order.
        # A run is RECORDED (remembered in `seen`, no longer tracked in flight, its
        # `wf_last`) only once its line is written, or once it is judged to owe none. Recorded
        # first, a run whose write failed was skipped by every later cycle and never announced,
        # and the checkpoint of a raising cycle would save that loss for the restart too.
        # Every run key this repo's read touches or announces is noted with this
        # cycle's start, held or not: a hold that opens later reads back to its start less the
        # margin, and what it can return was touched or announced in that last margin.
        recent = state.setdefault("runs_recent", {}).setdefault(repo, {})
        for run in sorted(page + fetched, key=lambda r: r.get("id", 0)):
            if run.get("status") != "completed":
                continue
            rid = str(run.get("id"))
            mid = "run:%s:%s" % (run.get("id"), run.get("run_attempt", 1))
            recent[mid] = cycle_start.timestamp()
            if mid in seen:
                touch_seen(seen, mid)
                inflight.pop(rid, None)
                continue
            # A run the version 1 file lost that completed before the old daemon
            # last looked. It is adopted BEFORE the scheduled baseline below on purpose:
            # `wf_last` in that file already reflects every run that completed before its
            # watermark, so re-applying an older one out of order can only regress it (a
            # dropped old failure would flip a passing nightly back and the next real
            # success would be announced as a recovery that never happened).
            if _predates(run.get("updated_at"), cutoff):
                adopted["run"] += 1
                seen[mid] = None
                inflight.pop(rid, None)
                continue
            name = run.get("name", "?")
            concl = run.get("conclusion", "?")
            # Per repo, then name: keyed by name alone, a scheduled success in one repo
            # moved the baseline of another repo's workflow of the same name, and that repo's
            # real recovery was then suppressed below as cron noise (see `_wf_last`).
            # Keyed on a string: a `name` that is a list or a dict is unhashable and
            # raised here every cycle, before the run was recorded. A non-string name is the
            # empty key (the same "empty field" `one_line` makes of it for the line); a string
            # keeps its own spelling, so the keys a saved state already holds still match.
            wf_key = name if isinstance(name, str) else ""
            prev = state.setdefault("wf_last", {}).get(repo, {}).get(wf_key)
            # ⚠ ONLY A SCHEDULED RUN MAY MOVE THE SCHEDULED BASELINE.
            # `wf_last[repo][name]` is read in exactly one place: the suppression rule
            # below, which fires only for `event == "schedule"`. So a write from
            # any other event can never be read for its own sake — it can only
            # corrupt the next scheduled comparison.
            #
            # That became reachable when perf-nightly.yml gained a `ref` input.
            # A candidate dispatched from main reports the same
            # workflow NAME and `head_branch=main`, so before this: nightly
            # fails -> someone dispatches a branch run that passes -> the pass
            # is announced as `Perf nightly@main: success`, which is FALSE (it
            # measured a branch), and it sets the baseline to success, so the
            # next genuine main recovery is SUPPRESSED. Wrong in both
            # directions from one write.
            #
            # Non-scheduled runs still EMIT — the rule below lets push/PR/manual
            # through unconditionally. They simply no longer speak for the cron.
            schedule = run.get("event") == "schedule"
            if first_run:
                baseline[1] += 1
            # Steady-state cron successes are noise: a SCHEDULED run that
            # succeeded while the workflow was already passing is suppressed.
            # Failures always emit; the first success after a non-success
            # emits (the recovery signal); push/PR/manual runs always emit.
            elif not (schedule and concl == "success" and prev in (None, "success")):
                emit(profile, 'type=run repo=%s run=%s.%s %s@%s: %s | %s' % (
                    rs, one_line(run.get("id")), one_line(run.get("run_attempt", 1)), one_line(name, 100),
                    one_line(run.get("head_branch", "?"), 100), one_line(concl, 40),
                    one_line(run.get("display_title", ""), 70)))
            # The baseline moves after the line too: moved first, a recovery whose
            # write failed became its own baseline, and was then suppressed as cron noise.
            if schedule:
                state["wf_last"].setdefault(repo, {})[wf_key] = concl
            seen[mid] = None
            inflight.pop(rid, None)
        # An attempt is kept only for a run still in flight (retired, gone or completed
        # runs leave with their entry; a later attempt of the run is saved again when it joins).
        for rid in [k for k in attempt_of if k not in inflight]:
            del attempt_of[rid]

        # An incomplete read holds this repo's cutoff where the read began, so
        # the next poll pages back over what this one missed; a complete read releases it.
        # Only now, once this repo's runs are announced: the hold is the repo's runs
        # watermark, and released before them, a cycle that raised part-way through the runs
        # above left a cutoff past the rest, which the next poll (or, via the checkpoint, the
        # restart) never paged back to.
        held_map = state.setdefault("runs_from", {})
        if complete:
            held_map.pop(repo, None)
        elif expired is not None:
            since = (expired - RUNS_CUTOFF_MARGIN).strftime("%Y-%m-%dT%H:%M:%SZ")
            until = (cycle_start - RUNS_CUTOFF_MARGIN).strftime("%Y-%m-%dT%H:%M:%SZ")
            note("note: [%s] %s runs read held since %s for more than %dm and still incomplete: released"
                 % (profile, repo, held_map.get(repo), RUNS_HOLD_MAX.total_seconds() // 60))
            emit(profile, "type=unread_runs repo=%s reason=hold_expired since=%s until=%s | the runs "
                 "read was held for more than %dm and is still incomplete; a run created in that window"
                 " and deeper than page one may be unread — check the window by run id"
                 % (rs, since, until, RUNS_HOLD_MAX.total_seconds() // 60))
            del held_map[repo]
        elif repo not in held_map and last_start is not None:
            held_map[repo] = last_start.isoformat()
        # Keep only what a read held from here can still return: back to the hold's
        # start less the margin, or, with no hold, to this cycle's start less the margin (a hold
        # opened next cycle starts at this one). Entries before that are dropped, exempt no more.
        recent_from = _parse_ts(held_map.get(repo)) if repo in held_map else None
        recent_from = ((recent_from or cycle_start) - RUNS_CUTOFF_MARGIN).timestamp()
        for key in [k for k, at in recent.items() if at < recent_from]:
            del recent[key]

    if first_run:
        note("[%s] baseline: seeded %d prs, %d runs, %d reviews" % (
            profile, baseline[0], baseline[1], baseline[2]))
        # A first run ends HERE, before the degraded lines,
        # as it did before checkpointing: the baseline is complete once every repo is read. Ended
        # after them, a failed `degraded` write left `first_run` set and `since` unset, so the
        # next cycle silently re-seeded and swallowed whatever was made in between. Both move
        # together: `first_run` cleared over an unreadable file with `since` still unset would
        # be checkpointed as a normal state with no watermark, and the restart would announce
        # what the first run's holes left unseeded as news (the review's mutant V2).
        state.update(first_run=False, since=cycle_start.isoformat())    # One assignment
    # A completed cycle with a hole in it used to reach only
    # `daemon-github.log`, which nothing tails, while `save_state` below
    # advanced the watermark and every health signal read green. Reconciled
    # BEFORE the save so the episode bookkeeping is persisted with the state
    # it describes.
    #
    # Each sub-resource's episode is recorded only once ITS line is written.
    # `reconcile` marks an episode told before the caller writes the line, so each sub is
    # reconciled on a copy of its own episode and folded into the state after each of its
    # profile-log writes (a silenced pair has none) and BEFORE that pair's note. A write that
    # fails leaves that sub as its last written line left it, every later recovered sub as it
    # was but for its count, which restarts (below), and every failed sub not yet folded with
    # only this cycle's failure counted (below):
    # the cycle raises, the checkpoint saves it so, and the next cycle tells what is still a
    # hole, or the recovery of a told one that healed. Folded all at once instead, a failed
    # second line dropped the first sub's episode too (review 5397883871, N3); folded after the
    # note, a note that failed after a written `degraded` line dropped that line's episode, and
    # its recovery was never told (review 5398328909, F1). While the sub's `error: sub-fetch`
    # line is still to be written, its count is folded one short: folded in full, a failed
    # `error:` write left the count past the `==` that promotes, and a hole that persisted was
    # never promoted. The fold after the pairs is for a sub that produced none (an announced
    # episode still going); for every other sub it repeats the last fold. It also marks that sub
    # folded, so a raise later in the cycle does not count its failure a second time: after a
    # reset count (below) that second count made a hole's next failure its third, and promoted
    # it a cycle early (review 5399050359, P4). `reconcile` treats
    # every sub independently, so the lines and episodes of a cycle whose writes all succeed
    # are exactly those of one call, in the same order: the failed subs, then the recovered
    # ones, each sorted.
    #
    # On a raise, each failed sub not yet folded (the one whose first write failed, and every
    # failed sub after it) has this cycle's failure counted and nothing marked told
    # (`_count_unwritten_failure`); then the raise goes on to the checkpoint and `main()`. Left
    # as it was, its count missed this cycle, so a hole that failed three cycles in a row and then
    # healed never got the `error: sub-fetch` line main writes (review 5398737519, G1).
    #
    # And each recovered sub not yet folded (every sub that healed, sorted after the write or
    # note that failed) has its count reset to 0 and keeps everything else: its episode stays
    # open, and told if it was, so its recovery is still written at its next healthy cycle. It
    # did heal; only its line was not written. Left with its old count, its next failure counted
    # on from the failures before the heal: a hole was promoted after one or two real failures,
    # and a promoted hole's next episode counted past the `==` that promotes and was never
    # promoted; with a raise before its fold in the next cycle, G1's count was added to the
    # stale one (review 5399050359, I-2: B1 and B2). Main closed the episode, so it counted
    # from one, and so does this.
    state.pop(_OPEN_CYCLE, None)       # From here the handler below accounts, not `checkpointed`
    episodes = state.setdefault("degraded", {})
    told_at = time.time()
    failed = sorted(holes.failures)
    folded = set()
    try:
        for sub in failed + sorted(set(episodes) - set(failed) - set(holes.skipped)):
            stop_point()    # The previous sub is folded; this one is not begun
            one = _degraded.Cycle()
            if sub in holes.failures:
                one.failures[sub] = holes.failures[sub]
            mine = {sub: copy.deepcopy(episodes[sub])} if sub in episodes else {}
            pairs = _degraded.reconcile(one, mine, told_at)
            for i, (emit_line, note_line) in enumerate(pairs):
                if emit_line:  # A silenced repeat is daemon-log only
                    emit(profile, emit_line)
                later = [e for e, _ in pairs[i + 1:] if e]
                if sub not in mine:
                    episodes.pop(sub, None)
                elif later and later[0].startswith("error: sub-fetch"):
                    episodes[sub] = dict(mine[sub], consecutive=mine[sub]["consecutive"] - 1)
                else:
                    episodes[sub] = mine[sub]
                folded.add(sub)
                note("[%s] %s" % (profile, note_line))
            if sub in mine:
                episodes[sub] = mine[sub]
            else:
                episodes.pop(sub, None)
            folded.add(sub)
    except BaseException:
        _account_unfolded(episodes, holes, folded, told_at)   # The same as a raise before this tail
        _remember_episode_repos(state, holes, repos)
        raise
    _remember_episode_repos(state, holes, repos)   # Where the episodes that remain failed
    # The watermark is recorded only once those lines are written: a write that
    # fails leaves it where it was, and the next cycle re-reads the window. (A first run has
    # already ended above.)
    state["since"] = cycle_start.isoformat()

    # Bound the memory HERE, in the running process, so that what a restart
    # reloads is exactly what this process already knows. Every id the API returned this
    # cycle was just moved to the newest end, so it survives if its kind's cap covers one
    # poll's window, which the caps are sized to. Then the baseline decides whether it runs
    # again, and only after that does the file get written.
    for key in ("runs_from", "runs_checked", "runs_attempt", "runs_recent", "runs_cap_told", "reviews_unseeded"):
        for gone in [r for r in (state.get(key) or {}) if r not in repos]:
            del state[key][gone]
    # Only the run ids a HELD read can return are exempt from the cap.
    exempt = frozenset(key for repo in state.get("runs_from") or {}
                       for key in (state.get("runs_recent") or {}).get(repo, {}))
    state["seen"] = new_seen(prune_seen(seen, len(repos), exempt=exempt))
    if legacy:
        _settle_legacy(profile, state, legacy, holes, adopted, cutoff)

    save_state(profile, state)
    # A completed cycle's save proves the state's disk writes again, and a streak of failed
    # cycles that ends in completed ones never checkpoints again to say so.
    _tell_checkpoint_recovered(profile, state)


def main():
    """SIGTERM (launchd stopping the daemon) is recorded by the handler and raises
    :class:`Terminated` at the cycle's next :func:`stop_point`; a cycle in progress is
    checkpointed by :func:`checkpointed` as it unwinds, and the stop is noted and exits 0."""
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        _serve()
    except Terminated:
        note("note: stopped by SIGTERM")


def _serve():
    if not GH:
        note("error: gh not found on PATH — install/auth gh, then restart")
        while True:
            _sleep(600)
    note("github daemon starting build=%s" % _build.render(BUILD))
    states = {}
    failures = 0
    while True:
        try:
            with open(CONFIG_PATH) as fh:
                config = json.load(fh)
        except Exception as e:
            note("note: config unreadable (%s) — sleeping" % e)
            _sleep(300)
            continue

        active, failed = [], None
        for profile, pcfg in (config.get("profiles") or {}).items():
            cfg = pcfg.get("github")
            if not cfg or not cfg.get("repos"):
                continue
            active.append(profile)
            if profile not in states:
                states[profile] = load_state(profile)
                note("[%s] %s" % (profile, "first run — seeding baseline"
                                  if states[profile]["first_run"] else "resumed"))
                migrating = legacy_note(profile, states[profile])
                if migrating:
                    note(migrating)
            try:
                poll_profile(profile, cfg, states[profile])
            except Exception as e:
                failed = "cycle failed for %s: %s" % (profile, e)
                note("note: " + failed)

        if failed:
            failures += 1
            if failures == 3:
                for p in active:
                    emit(p, "error: github polling failing repeatedly — last: %s" % one_line(failed, 400))
        else:
            if failures >= 3:
                note("note: polling recovered after %d failures" % failures)
            failures = 0
        _sleep(POLL_SECS)


if __name__ == "__main__":
    main()
