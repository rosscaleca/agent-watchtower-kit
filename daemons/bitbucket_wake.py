#!/usr/bin/env python3
"""Watchtower Bitbucket daemon — multi-profile PR / comment poller.

Shells `bkt api` so the credential stays inside bkt; this process never touches
a token. Promotes the old session-bound pr_watch pattern to a durable daemon.
Events append to ~/Library/Logs/watchtower/<profile>-bitbucket.log.

Per profile config: {"repos": ["workspace/slug", ...],
"cwd": "~/path/to/any/clone" (bkt resolves auth context from a repo dir),
"suppress_user_ids": ["{uuid}", "account_id", ...] (preferred; display-name
"suppress_users" kept as fallback — names change and collide)}

Note: Bitbucket Pipelines state is NOT watched — the bkt token lacks
pipeline-read scope. PR lifecycle (opened / pushed / merged / declined),
approvals (the one signal the standalone pr-watch existed for;
without it a cross-side approval is invisible until someone looks), changes-requested
verdicts (``pr_changes_requested`` and ``pr_changes_cleared``) and
comments are covered; lifecycle events are never suppressed.

The comments already announced are remembered in ``seen``, an ordered
memory (``c:<id>``) that a restart reloads intact — see ``SEEN_CAPS`` and
``_seen``. It used to be cut to an arbitrary 2000 on save.
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import _build  # sibling module; the daemons dir is sys.path[0] when run as a script
import _degraded  # a cycle that completes with a hole in it must say so
import _fields  # a person's name is flattened before it reaches an event line
import _seen  # the ordered, per-kind dedup memory the polling daemons share

#: What bytes this process is running, logged on start and saved with the state.
BUILD = _build.describe(__file__)

CONFIG_PATH = os.path.expanduser("~/.config/watchtower/config.json")
STATE_DIR = os.path.expanduser("~/.local/state/watchtower/bitbucket")
LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
POLL_SECS = 90
OVERLAP = timedelta(minutes=5)

#: What one poll of one repo asks Bitbucket for. These BUILD the request URLs, and the dedup memory
#: below is sized from them, so the two cannot drift apart.
PRS_PER_PAGE = 25         # the OPEN listing: the first 25 open PRs
COMMENTS_PER_PAGE = 50    # a PR's comments: the first page, with NO `since` filter

#: The dedup memory, ``state["seen"]``: the ids of the comments already announced,
#: ``c:<id>``. The design is in ``_seen``; this is its sizing.
#:
#: It was saved as ``list(seen)[-2000:]`` from a set. A set has no order, so once it passed 2000 an
#: ARBITRARY 2000 were kept, and whatever Bitbucket still returned that had been dropped was announced
#: again after the next restart. A comment older than the watermark less OVERLAP is remembered without
#: a wake (the time check in ``poll_profile``), so what could come back was the last few minutes before
#: the restart: a peer's "please fix the migration", announced a second time, is a request that has
#: already been answered.
#:
#: ``seen`` holds comments and nothing else. A PR is tracked in ``state["prs"]``, keyed by number and
#: deleted when it merges or is declined, and Pipelines are not watched at all (the bkt token lacks the
#: scope), so neither has an id in it.
#:
#: The window is the most comment ids one poll of one repo can return, and with no `since` every poll
#: returns the first page of every listed PR again, so an id keeps being returned for as long as its PR
#: stays in the listing; the caps scale with the number of repos a profile watches:
SEEN_WINDOW = {"c": PRS_PER_PAGE * COMMENTS_PER_PAGE}     # 1250 per repo
#: How many windows the kind remembers, and why. Every id the API returns is moved to the newest end
#: each poll (``touch_seen``), so an id that KEEPS being returned needs only its one window. The rest is
#: for ids the API stops returning for a while and then returns: the listing is the first 25 open PRs,
#: so a PR at its edge can flicker in and out. Its comments are untouched on the polls it sits out,
#: while the PRs still listed fill a whole window, so it needs a second window of room or its comments
#: are evicted and announced again the moment it is back. The same room is how long a PR that leaves
#: the listing keeps its comments remembered. Measured on the live profile log, 2026-09-03..27: 9 comment
#: wakes in 26 days (3 on the busiest day) and 34 ids in the state file, so this is the size of one
#: poll's answer, not of any volume.
SEEN_KEEP = {"c": 2}
#: Per watched repo: c 2500.
SEEN_CAPS = _seen.caps_from(SEEN_WINDOW, SEEN_KEEP)
#: Cycle sub-resources whose failure leaves a slice of the memory unread.
SEEN_SUBS = ("comments",)
#: What 38ffbbd, and every build since the daemon was written, saved: ``list(seen)[-OLD_SEEN_CAP:]`` from
#: a set. A set only grows, so a file holding FEWER ids than this has never lost one, and the migration
#: reads that off the file (``_seen.legacy_baseline``). The live file held 34 ids on 2026-09-29.
OLD_SEEN_CAP = 2000
#: The first cycle after loading a file the old code wrote adopts, SILENTLY, a comment it cannot find in
#: ``seen`` only if its own ``created_on`` is at or before the file's watermark less this: one poll
#: interval, which is 90 s here, where the other daemons poll every 60. The old daemon's last look can
#: lag its watermark by a fetch and the two clocks are not the same clock, so a comment this close to
#: the watermark is announced if unseen: a duplicate wake costs a turn, a swallowed one costs the event.
#: And only where the file says ids were LOST and the last cycle READ everything: a file under
#: OLD_SEEN_CAP has never lost one, and one with an open ``comments`` episode was not fully read (the old
#: daemon advanced its watermark past the hole), so for those nothing is adopted and everything it does
#: not remember announces.
LEGACY_GUARD = timedelta(seconds=POLL_SECS)
#: What that first cycle counts and says.
ADOPTED = (("c", "comment(s)"),)

os.environ["PATH"] = ":".join([
    os.path.expanduser("/etc/profiles/per-user/%s/bin" % os.environ.get("USER", "")),
    "/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
BKT = shutil.which("bkt")


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
    with open(os.path.join(LOG_DIR, "%s-bitbucket.log" % profile), "a", errors="replace") as fh:
        fh.write("%s ts=%s\n" % (line, now_ts()))


def one_line(s, cap=180):
    """Bitbucket text (a PR title, a comment) as part of one event line: whitespace collapsed, and the
    controls and bidi marks a terminal acts on but does not show dropped
    (:func:`_fields.unshown_dropped`), capped."""
    s = _fields.unshown_dropped(re.sub(r"\s+", " ", s or "")).strip()
    return (s[: cap - 1] + "…") if len(s) > cap else s


who_field = _fields.who_field  # moved to the module every daemon shares


# Explicit partial-response fields: the OPEN listing carries participants only
# when asked (verified against the live API 2026-09-02 — `approved`, `role`,
# `user.uuid`, `user.display_name` all come through on the list endpoint, so
# approvals cost no extra call per PR).
LIST_FIELDS = ",".join([
    "values.id", "values.title", "values.source.commit.hash",
    "values.author.display_name",
    "values.participants.approved", "values.participants.role",
    "values.participants.user.display_name", "values.participants.user.uuid",
    "values.participants.user.account_id",
    # The participant's verdict: null, "approved" or "changes_requested".
    "values.participants.state",
])


def approvals_of(pr):
    """{identity: display_name} for every participant with approved=true.

    Identity is uuid, else account_id, else the display name — the same
    precedence the suppression config uses, so a suppressed reviewer's own
    approval is recognised whichever form the config carries.
    """
    out = {}
    for p in pr.get("participants") or []:
        if not p.get("approved"):
            continue
        u = p.get("user") or {}
        who = u.get("display_name", "?")
        out[u.get("uuid") or u.get("account_id") or who] = who
    return out


def changes_requested_of(pr):
    """{identity: display_name} for every participant whose state is "changes_requested".

    A changes-requested verdict is a participant's ``state``; it does not
    set ``approved``, so ``approvals_of`` never sees it. Identity is the same
    precedence as ``approvals_of`` (uuid, else account_id, else the display name).
    """
    out = {}
    for p in pr.get("participants") or []:
        if p.get("state") != "changes_requested":
            continue
        u = p.get("user") or {}
        who = u.get("display_name", "?")
        out[u.get("uuid") or u.get("account_id") or who] = who
    return out


def bkt_api(path, cwd):
    r = subprocess.run([BKT, "api", path], capture_output=True, text=True,
                       timeout=45, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError("bkt api %s failed: %s" % (path.split("?")[0], one_line(r.stderr, 160)))
    return json.loads(r.stdout)


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def load_state(profile):
    try:
        with open(os.path.join(STATE_DIR, "%s.json" % profile)) as fh:
            raw = json.load(fh)
        # A file without the version marker is the arbitrary-subset list the old code
        # wrote. Its order means nothing and its dropped ids are already lost, so its first cycle
        # runs with a baseline (see `poll_profile`). It rides the in-memory state only: once a cycle
        # has written the marked file it is gone.
        return {"since": raw.get("since"), "seen": _seen.new_seen(raw.get("seen", [])),
                "prs": raw.get("prs", {}), "degraded": raw.get("degraded", {}),
                "first_run": False,
                "legacy_seen": _seen.legacy_baseline(raw, OLD_SEEN_CAP, subs=SEEN_SUBS)}
    except Exception:
        return {"since": None, "seen": _seen.new_seen(), "prs": {}, "degraded": {},
                "first_run": True, "legacy_seen": None}


def save_state(profile, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "%s.json" % profile)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        # `seen` is written oldest-first and already pruned (`poll_profile` does that in the
        # running process); `seen_v` marks the order as meaningful. The old code reads this file
        # unchanged: `seen` is still a flat list and the marker is ignored.
        json.dump({"since": state["since"], "seen": list(state["seen"]), "seen_v": _seen.SEEN_V,
                   "prs": state["prs"], "degraded": state.get("degraded", {}),
                   "build": BUILD}, fh)
    os.replace(tmp, path)


def poll_profile(profile, cfg, state):
    repos = cfg.get("repos") or []
    if not repos:
        return
    cwd = os.path.expanduser(cfg.get("cwd", "~"))
    holes = _degraded.Cycle()  # holes in THIS cycle
    suppress = set(cfg.get("suppress_users", []))
    suppress_ids = set(cfg.get("suppress_user_ids", []))
    seen = _seen.ordered_seen(state)
    first_run = state["first_run"]
    cutoff = parse_ts(state["since"]) if state["since"] else None
    if cutoff is None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    cutoff = cutoff.astimezone(timezone.utc) - OVERLAP
    cycle_start = datetime.now(timezone.utc)
    baseline = [0, 0]
    # The first cycle after a version 1 state file. What the old daemon had already
    # announced was cut to an arbitrary 2000 ids, so the ids it dropped are lost and Bitbucket still
    # returns comments nobody can prove were announced. Such a comment is adopted SILENTLY (remembered,
    # not announced), but only on positive evidence: its own `created_on` is at or before the old file's
    # watermark less LEGACY_GUARD, i.e. it existed when the old daemon last looked. The watermark, not
    # the load time, is the boundary: what was said while the daemon was down is exactly what resume
    # coverage is for, and stays news. It is the FILE's watermark, fixed for the whole baseline:
    # `state["since"]` moves on with every completed cycle and a held baseline must not move with it.
    # (A comment older than `cutoff` above was always adopted silently, by the time check below.)
    # Nothing is adopted at all when the file could not have lost an id (it is under OLD_SEEN_CAP) or was
    # not fully read (a `comments` episode is open in it): see `_seen.legacy_baseline`.
    legacy = state.get("legacy_seen")
    adopt_before = _seen.legacy_cutoff(legacy, LEGACY_GUARD)
    adopted = dict.fromkeys((kind for kind, _ in ADOPTED), 0)

    for repo in repos:
        rs = repo.split("/")[-1]
        prs_state = state["prs"].setdefault(repo, {})

        listing = bkt_api("/repositories/%s/pullrequests?state=OPEN&pagelen=%d&fields=%s"
                          % (repo, PRS_PER_PAGE, LIST_FIELDS), cwd)
        open_now = {}
        for pr in listing.get("values", []):
            n = str(pr.get("id"))
            open_now[n] = pr
            head = ((pr.get("source") or {}).get("commit") or {}).get("hash", "")[:9]
            approvers = approvals_of(pr)
            requesters = changes_requested_of(pr)
            prev = prs_state.get(n)
            if prev is None:
                prs_state[n] = {"head": head, "approvals": sorted(approvers),
                                "approver_names": dict(approvers),
                                "changes": sorted(requesters),
                                "changes_names": dict(requesters)}
                if first_run:
                    baseline[0] += 1
                else:
                    emit(profile, 'type=pr_opened repo=%s #%s by="%s" | %s' % (
                        rs, n, who_field((pr.get("author") or {}).get("display_name", "?")),
                        one_line(pr.get("title", ""), 100)))
                continue
            if prev.get("head") != head:
                emit(profile, "type=pr_push repo=%s #%s head=%s (was %s)" % (
                    rs, n, head, prev.get("head")))
                prev["head"] = head
            # Approval set diff. Identity is the reviewer's uuid where
            # Bitbucket gives one, so a display-name change cannot double-emit;
            # the line carries the display name because that is what a human
            # reads. Own approvals are own writes and are suppressed like
            # comments; a peer's approval is the wake that matters most on a
            # cross-side-review team.
            before = set(prev.get("approvals") or [])
            for ident in sorted(set(approvers) - before):
                who = approvers[ident]
                if ident in suppress_ids or who in suppress:
                    continue
                emit(profile, 'type=pr_approved repo=%s #%s by="%s" | %s' % (
                    rs, n, who_field(who), one_line(pr.get("title", ""), 100)))
            for ident in sorted(before - set(approvers)):
                who = prev.get("approver_names", {}).get(ident, ident)
                if ident in suppress_ids or who in suppress:
                    continue
                emit(profile, 'type=pr_unapproved repo=%s #%s by="%s"' % (rs, n, who_field(who)))
            prev["approvals"] = sorted(approvers)
            prev["approver_names"] = dict(approvers)
            # Changes-requested set diff. The same rules as approvals: identity by
            # uuid, own verdicts suppressed. A saved record with no `changes` key (every file
            # written before this) adopts the requests standing now SILENTLY — they were never
            # announced because they could not be seen, and "new" would be a lie; the clearing of
            # an adopted request is news from the next cycle on.
            if "changes" in prev:
                held = set(prev.get("changes") or [])
                for ident in sorted(set(requesters) - held):
                    who = requesters[ident]
                    if ident in suppress_ids or who in suppress:
                        continue
                    emit(profile, 'type=pr_changes_requested repo=%s #%s by="%s" | %s' % (
                        rs, n, who_field(who), one_line(pr.get("title", ""), 100)))
                for ident in sorted(held - set(requesters)):
                    who = (prev.get("changes_names") or {}).get(ident, ident)
                    if ident in suppress_ids or who in suppress:
                        continue
                    emit(profile, 'type=pr_changes_cleared repo=%s #%s by="%s"' % (rs, n, who_field(who)))
            prev["changes"] = sorted(requesters)
            prev["changes_names"] = dict(requesters)

        for n in list(prs_state.keys()):
            if n in open_now:
                continue
            try:
                pr = bkt_api("/repositories/%s/pullrequests/%s" % (repo, n), cwd)
            except Exception as e:
                note("note: [%s] state fetch failed for #%s: %s" % (profile, n, e))
                holes.record("pr_state", "#%s: %s" % (n, e))
                continue
            st = pr.get("state", "?")
            if st in ("MERGED", "DECLINED"):
                if not first_run:
                    emit(profile, "type=pr_%s repo=%s #%s | %s" % (
                        st.lower(), rs, n, one_line(pr.get("title", ""), 100)))
                del prs_state[n]

        for n in open_now:
            try:
                comments = bkt_api(
                    "/repositories/%s/pullrequests/%s/comments?pagelen=%d"
                    % (repo, n, COMMENTS_PER_PAGE), cwd)
            except Exception as e:
                note("note: [%s] comments fetch failed for #%s: %s" % (profile, n, e))
                holes.record("comments", "#%s: %s" % (n, e))
                continue
            for c in comments.get("values", []):
                if c.get("deleted"):
                    continue
                mid = "c:%s" % c.get("id")
                if mid in seen:
                    _seen.touch_seen(seen, mid)
                    continue
                seen[mid] = None
                ts = parse_ts(c.get("created_on", ""))
                if ts is None or ts.astimezone(timezone.utc) <= cutoff:
                    continue
                if first_run:
                    baseline[1] += 1
                    continue
                if _seen.predates(c.get("created_on"), adopt_before):
                    adopted["c"] += 1
                    continue
                cu = c.get("user") or {}
                who = cu.get("display_name", "?")
                if (who in suppress or cu.get("uuid") in suppress_ids
                        or cu.get("account_id") in suppress_ids):
                    continue
                emit(profile, 'type=comment repo=%s #%s by="%s" | %s' % (
                    rs, n, who_field(who), one_line((c.get("content") or {}).get("raw", ""))))

    if first_run:
        note("[%s] baseline: seeded %d open prs, %d comments" % (
            profile, baseline[0], baseline[1]))
        state["first_run"] = False
    state["since"] = cycle_start.isoformat()
    # A completed cycle with a hole in it used to reach only
    # `daemon-bitbucket.log`, which nothing tails, while `save_state` below advanced
    # the watermark and every health signal read green.
    for emit_line, note_line in _degraded.reconcile(
            holes, state.setdefault("degraded", {}), time.time()):
        if emit_line:  # a silenced repeat is daemon-log only
            emit(profile, emit_line)
        note("[%s] %s" % (profile, note_line))

    # Bound the memory HERE, in the running process, so that what a restart reloads is
    # exactly what this process already knows. Every id the API returned this cycle was just moved to
    # the newest end, so it survives if the cap covers one poll's window, which it is sized to. Then the
    # baseline decides whether it runs again, and only after that does the file get written.
    state["seen"] = _seen.new_seen(_seen.prune_seen(seen, _seen.scaled(SEEN_CAPS, len(repos))))
    if legacy:
        _seen.settle_legacy(profile, state, legacy, adopt_before, adopted,
                            sorted(set(holes.failures) & set(SEEN_SUBS)), ADOPTED, note)

    save_state(profile, state)


def main():
    if not BKT:
        note("error: bkt not found on PATH — install/auth bkt, then restart")
        while True:
            time.sleep(600)
    note("bitbucket daemon starting build=%s" % _build.render(BUILD))
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
            cfg = pcfg.get("bitbucket")
            if not cfg or not cfg.get("repos"):
                continue
            active.append(profile)
            if profile not in states:
                states[profile] = load_state(profile)
                note("[%s] %s" % (profile, "first run — seeding baseline"
                                  if states[profile]["first_run"] else "resumed"))
                migrating = _seen.legacy_note(profile, states[profile], LEGACY_GUARD)
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
                    emit(p, "error: bitbucket polling failing repeatedly — last: %s" % failed)
        else:
            if failures >= 3:
                note("note: polling recovered after %d failures" % failures)
            failures = 0
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
