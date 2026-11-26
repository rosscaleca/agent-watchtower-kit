#!/usr/bin/env python3
"""What code is this daemon actually running?

"Is the fleet current?" used to need a human: measure pids, plist paths and
symlink targets, then reason about whether an import moved files verbatim.
An incident showed the trap — three daemons had not restarted since 2026-08-27 and
were nonetheless current BY CONTENT, so any marker keyed on the checkout's
HEAD or on the restart time would have failed three correct daemons.

The fact that answers the question is the LOADED BYTES. Every daemon computes
its own source hash at import, logs it on start and writes it into its state
file; the watchdog compares that against the hash of the file now on disk and
reports `outdated` only when they differ. HEAD is carried as context (which
commit the checkout was on when the daemon started), never as the predicate.

Stdlib only; no exceptions escape — a daemon must never fail to start because
its own provenance could not be read.
"""
import hashlib
import os
import subprocess
import time

_HASH_CHARS = 12


def source_hash(path):
    """sha256 of the file's bytes, truncated; `None` if unreadable."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:_HASH_CHARS]
    except OSError:
        return None


def _git(path, *args):
    """A git command in the checkout containing `path`; `None` on any failure.

    Every caller in this module passes a LOCAL subcommand — `rev-parse`,
    `rev-list`. Nothing here contacts a remote, which is what lets the watchdog
    keep its "holds no credentials and makes no network calls" property while
    still answering a question about origin.
    """
    try:
        out = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.abspath(path))] + list(args),
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def git_head(path):
    """Short HEAD of the checkout containing `path`, or `None` (no git, not a repo)."""
    return _git(path, "rev-parse", "--short", "HEAD")


def describe(module_file):
    """The marker a daemon carries: `{"src": <hash>, "head": <sha|None>}`."""
    path = os.path.abspath(module_file)
    return {"src": source_hash(path), "head": git_head(path)}


def render(build):
    """One token for a log line: `src=<hash> head=<sha>`."""
    if not build:
        return "src=? head=?"
    return "src=%s head=%s" % (build.get("src") or "?", build.get("head") or "?")


def compare(loaded, disk_file):
    """`current` / `outdated` / `unknown` for a daemon's recorded marker vs the file now on disk.

    `unknown` when the state carries no marker (a daemon from before the marker existed,
    or one that has not saved since it started) — never an accusation.
    """
    now = source_hash(disk_file)
    src = (loaded or {}).get("src")
    if not src or now is None:
        return "unknown", now
    return ("current" if src == now else "outdated"), now


#: How long a fetch may be stale before `render_upstream` says so out loud. A
#: verdict of `current` is only as fresh as the last fetch SOMEONE ran, so the
#: age travels with the verdict and is never dropped: "current, fetched 4m ago"
#: and "current, fetched 6d ago" are different facts wearing the same word.
STALE_FETCH_SECS = 24 * 3600


def _fetch_age(path):
    """Seconds since this checkout last fetched ANYTHING, or `None` if it never has.

    ⚠ READ THIS AS A ONE-DIRECTIONAL BOUND, which is the only sound reading git
    supports. Git records when a fetch HAPPENED; it does not record when a
    particular remote-tracking ref was last CONFIRMED. So:

    * a LARGE age proves the compared ref has been unconfirmed at least that
      long — which is the case worth catching, and the case an earlier incident found;
    * a SMALL age proves nothing about the compared ref. The fetch may have been
      ``git fetch origin some-feature``, or from a different remote entirely.

    An earlier version tried to close that gap by reading FETCH_HEAD's CONTENT
    for the branch name. Review took it apart three ways and each was right:
    the entry does not name the REMOTE, so a fetch from a fork matched; a
    ``--append`` fetch RETAINS old entries under a new mtime, so an untouched
    ref looked freshly fetched; and a slash-separated branch was truncated, so a
    genuinely fresh fetch read ``never``. All three were the same mistake —
    dressing an approximation up as a measurement. The honest bound is smaller
    and true, and it is what `render_upstream` labels ``last-fetch``.

    FETCH_HEAD IS READ ACROSS THE WHOLE REPOSITORY, not just this checkout, and
    the newest wins. Worktrees SHARE their remote-tracking refs but each keeps
    its own FETCH_HEAD, so a fetch run in the main checkout updates the refs a
    linked worktree compares against while leaving that worktree's FETCH_HEAD
    untouched — reporting ``never`` for a ref just confirmed. Taking the newest
    across the common dir and every worktree is sound precisely BECAUSE the
    value is a one-directional bound: if the most recent fetch anywhere in the
    repository was nine days ago, the ref is unconfirmed at least nine days.
    """

    def _newest(paths):
        best = None
        for candidate in paths:
            try:
                mtime = os.stat(candidate).st_mtime
            except OSError:
                continue
            if best is None or mtime > best:
                best = mtime
        return best

    common = _git(path, "rev-parse", "--git-common-dir")
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(os.path.dirname(os.path.abspath(path)), common)
    candidates = [os.path.join(common, "FETCH_HEAD")]
    worktrees = os.path.join(common, "worktrees")
    try:
        for name in os.listdir(worktrees):
            candidates.append(os.path.join(worktrees, name, "FETCH_HEAD"))
    except OSError:
        pass
    newest = _newest(candidates)
    return None if newest is None else max(0.0, time.time() - newest)


def upstream_status(path):
    """Is the CHECKOUT containing `path` behind the upstream ref its last fetch recorded?

    The THIRD staleness layer. `compare()` answers the second — loaded
    bytes vs the file on disk — and the watchdog answers it for itself. Neither
    can see this one: every daemon can be current by content while the checkout
    they all read sits two commits behind origin, so a merged fix is inert on
    the host with nothing anywhere saying so. That was measured exactly on
    one host: origin/main 5bf8592 -> checkout 44ea263 -> processes 4be56a7.

    FETCH-FREE BY CONSTRUCTION. It reads the remote-tracking ref the last fetch
    already wrote; it never runs `git fetch`. So the honest reading is "behind
    the origin I last heard about", and on a checkout nobody has fetched it
    reports `current` while origin has moved. That is why `fetched` rides along
    in every rendering — a verdict without it invites the reader to hear a
    freshness the check cannot provide.

    Returns a dict, always carrying `state`:

    ``current``   HEAD is the upstream ref, or ahead of it — nothing to pull
    ``behind``    the upstream ref contains HEAD and has commits HEAD does not
    ``diverged``  each has commits the other does not
    ``unknown``   no git, not a repo, or no upstream ref — never an accusation
    """
    out = {"state": "unknown", "head": None, "ref": None, "upstream": None,
           "ahead": None, "behind": None, "fetched": _fetch_age(path), "why": None}
    head = _git(path, "rev-parse", "HEAD")
    if not head:
        out["why"] = "not a git checkout"
        return out
    out["head"] = head[:7]

    ref = _git(path, "rev-parse", "--symbolic-full-name", "@{u}")
    if not ref and _git(path, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"):
        # A detached HEAD, or a branch with no upstream configured. origin/main
        # is the fleet's convention and is the ref a `git pull` here would move
        # toward; naming it explicitly beats reporting `unknown` on a checkout
        # that plainly has an origin.
        ref = "refs/remotes/origin/main"
    if not ref:
        out["why"] = "no upstream ref"
        return out
    out["ref"] = ref

    upstream = _git(path, "rev-parse", ref)
    if not upstream:
        out["why"] = "upstream ref unreadable"
        return out
    out["upstream"] = upstream[:7]

    counts = _git(path, "rev-list", "--left-right", "--count", "%s...%s" % (head, upstream))
    if not counts:
        out["why"] = "rev-list failed"
        return out
    try:
        ahead, behind = (int(n) for n in counts.split())
    except ValueError:
        out["why"] = "rev-list output unparsed: %r" % counts
        return out
    out["ahead"], out["behind"] = ahead, behind
    out["state"] = "diverged" if (ahead and behind) else ("behind" if behind else "current")
    return out


def _age(secs):
    if secs is None:
        return "never"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s" % (int(secs // size), unit)
    return "%ds" % int(secs)


def stale_fetch_bound_exceeded(st):
    """Whether a nominally clean comparison has a provably stale fetch bound.

    This deliberately consumes only the one-directional bound returned by
    ``_fetch_age``.  A large age (or no FETCH_HEAD anywhere in the repository)
    proves that the compared ref has gone unconfirmed for at least that long;
    a small age still proves nothing about that particular ref.  In particular,
    do not replace this with FETCH_HEAD content parsing -- see ``_fetch_age``
    for the three rejected ways that over-claimed freshness.

    ``behind`` and ``diverged`` remain evidence even when the fetch is old, so
    only a raw ``current`` verdict is degraded to unverified.
    """
    if not st or st.get("state") != "current":
        return False
    fetched = st.get("fetched")
    return fetched is None or fetched >= STALE_FETCH_SECS


def render_upstream(st):
    """One log line's worth: state, both shas, the gap, and the last fetch.

    The age is not optional formatting — see `_fetch_age`. It is labelled
    ``last-fetch`` rather than ``fetched`` on purpose: it says when this checkout
    last fetched anything, which puts a FLOOR under staleness and no ceiling on
    it — large proves the comparison is at least that old, small proves nothing. A ``stale-fetch`` suffix marks a verdict old enough that the
    reader should not lean on it at all.
    """
    if not st:
        return "state=? head=? upstream=? last-fetch=?"
    #: A CLEAN ROW OVER A STALE COMPARISON RENDERS AS `current-unverified`, not
    #: as `current` (a consumer-side review pointed out that the row needed this
    #: and not just the number). The asymmetry
    #: stated for the age applies to the VERDICT too: `behind` is proof, while
    #: `current` only says the checkout matches the ref AS OF THE LAST FETCH. On
    #: a host nobody has fetched in a week that is a clean row over a week-old
    #: comparison, and it would otherwise be indistinguishable from a genuinely
    #: current checkout. Encoding it here means every reader inherits it instead
    #: of deriving it — which is this ticket's own defect one layer out, since a
    #: green light that cannot mean what it looks like is exactly what an earlier
    #: defect report was about. The DICT keeps `state="current"`, so the daemon's
    #: recovery logic is unchanged; only what a human reads is qualified.
    state = st.get("state") or "?"
    fetched = st.get("fetched")
    if stale_fetch_bound_exceeded(st):
        state = "current-unverified"
    line = "state=%s head=%s ref=%s upstream=%s ahead=%s behind=%s last-fetch=%s" % (
        state, st.get("head") or "?",
        (st.get("ref") or "?").replace("refs/remotes/", ""),
        st.get("upstream") or "?",
        "?" if st.get("ahead") is None else st["ahead"],
        "?" if st.get("behind") is None else st["behind"],
        _age(fetched))
    if fetched is None or fetched >= STALE_FETCH_SECS:
        line += " stale-fetch"
    if st.get("why"):
        line += " why=%s" % st["why"].replace(" ", "-")
    return line
