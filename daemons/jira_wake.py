#!/usr/bin/env python3
"""Watchtower Jira daemon — multi-profile issue / comment / change poller.

Reads ~/.config/watchtower/config.json every cycle (token paste needs no
restart); each profile with a "jira" block is polled and its events appended to
~/Library/Logs/watchtower/<profile>-jira.log. stdout carries operational lines
only. The API token is read into request headers in-process only — never
printed, logged, or placed on a command line.

Per profile config: {"site": "https://x.atlassian.net", "email": "...",
"api_token": "...", "projects": ["TD"], "suppress_account_ids": [...]}

The server-side JQL window stretches to cover however long the daemon was down
(max(30m, downtime+10m), capped at 7 days) — a fixed window would silently skip
anything updated during a longer nap and never touched again.

What has been announced is remembered in ``seen``, an ordered per-kind memory
(``created:`` ``comment:`` ``change:``) that a restart reloads intact — see ``SEEN_CAPS``
and ``_seen``. It used to be cut to an arbitrary 1500 on save.
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import _build  # sibling module; the daemons dir is sys.path[0] when run as a script
import _degraded  # A cycle that completes with a hole in it must say so
import _fields  # A person's name is flattened before it reaches an event line
import _seen  # The ordered, per-kind dedup memory the polling daemons share

#: What bytes this process is running, logged on start and saved with the state.
BUILD = _build.describe(__file__)

CONFIG_PATH = os.path.expanduser("~/.config/watchtower/config.json")
STATE_DIR = os.path.expanduser("~/.local/state/watchtower/jira")
LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
POLL_SECS = 60
OVERLAP = timedelta(minutes=5)
FIRST_RUN_LOOKBACK = timedelta(minutes=10)
PLACEHOLDER = "PASTE-TOKEN-HERE"

#: What one poll of one profile asks Jira for. These BUILD the requests, and the dedup memory
#: below is sized from them, so the two cannot drift apart.
SEARCH_MAX_ISSUES = 50    # the search: ONE page of the most recently updated issues, never followed
COMMENTS_PER_ISSUE = 10   # an issue's comments, newest first
CHANGES_PER_ISSUE = 15    # an issue's changelog: its last 15 entries

#: The dedup memory, ``state["seen"]``: the ids already announced, ``created:<KEY>``,
#: ``comment:<id>`` and ``change:<id>``. The design is in ``_seen``; this is its sizing.
#:
#: It was saved as ``list(seen)[-1500:]`` from a set. A set has no order, so once it passed 1500 an
#: ARBITRARY 1500 were kept, and whatever Jira still returned that had been dropped was announced again
#: after the next restart. The always-on host's file was already at exactly 1500 (751 comments, 654 changes, 95
#: created). What comes back is not history: an item is only CONSIDERED when it is newer than the
#: watermark less OVERLAP, so it is the last few minutes before the restart, and that can be a
#: "status -> Done" or a "CLAIMED" comment that a seat then acts on as if it were new.
#:
#: The window is the most ids of a kind that one poll can hand this code. The stretched JQL window (up
#: to 7 days after a long nap) widens the time range and NOT the page: the search is one page of at most
#: SEARCH_MAX_ISSUES issues, and each yields its newest comments and its last changelog entries. One
#: search covers every project of a profile, so nothing here scales with the project count.
SEEN_WINDOW = {
    "created": SEARCH_MAX_ISSUES,                        #  50: at most one per issue on the page
    "comment": SEARCH_MAX_ISSUES * COMMENTS_PER_ISSUE,   # 500
    "change": SEARCH_MAX_ISSUES * CHANGES_PER_ISSUE,     # 750
}
#: An id is returnable for the overlap plus one poll: six polls, one after another.
SEEN_POLLS = _seen.returnable_polls(OVERLAP, POLL_SECS)
#: How many windows each kind remembers, and why. Every kind is filtered by TIME, not by a listing: an
#: item is considered only while it is newer than the watermark less OVERLAP, so it is returnable for
#: SEEN_POLLS polls running, and each of those polls may bring a whole window of new ids. An id that
#: keeps being returned is moved to the newest end every time (``touch_seen``) and needs only its own
#: window; six is the worst case with NO refresh at all. Even then an id in the last of its six polls
#: has at most five windows of newer ids of its own kind behind it, so nothing it can still be asked for
#: is evicted. Per kind, so a bulk transition (750 change ids a poll) cannot push a comment out.
#: Measured on the live profile log, 2026-09-03..29: 32 comments, 31 changes and 7 created issues on the
#: median day, 145, 92 and 27 on the busiest. The caps hold 20, 48 and 11 of those busiest days.
SEEN_KEEP = dict((kind, SEEN_POLLS) for kind in SEEN_WINDOW)
#: created 300, comment 3000, change 4500.
SEEN_CAPS = _seen.caps_from(SEEN_WINDOW, SEEN_KEEP)
#: Cycle sub-resources whose failure leaves a slice of the memory unread.
SEEN_SUBS = ("comments", "changelog")
#: What 38ffbbd, and every build since the daemon was written, saved: ``list(seen)[-OLD_SEEN_CAP:]`` from
#: a set. A set only grows, so a file holding FEWER ids than this has never lost one, and the migration
#: reads that off the file (``_seen.legacy_baseline``).
OLD_SEEN_CAP = 1500
#: The first cycle after loading a file the old code wrote adopts, SILENTLY, an item it cannot find in
#: ``seen`` only if the item's own timestamp is at or before the file's watermark less this. The old
#: daemon's last look can lag its watermark by a fetch and the two clocks are not the same clock, so an
#: item this close to the watermark is announced if unseen: a duplicate wake costs a turn, a swallowed
#: one costs the event. And only where the file says ids were LOST and the last cycle READ everything:
#: a file under OLD_SEEN_CAP has never lost one, and one with an open ``comments`` or ``changelog``
#: episode was not fully read (the old daemon advanced its watermark past the hole), so for those
#: nothing is adopted and everything it does not remember announces.
LEGACY_GUARD = timedelta(seconds=POLL_SECS)
#: What that first cycle counts and says, in order.
ADOPTED = (("created", "created issue(s)"), ("comment", "comment(s)"), ("change", "change(s)"))


def note(line):
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # As github_wake: a line the stream cannot encode (a lone surrogate, or
        # non-ASCII on a stream that is not UTF-8) is printed as its ascii(), which every stream can take.
        print(ascii(line), flush=True)


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(profile, line):
    os.makedirs(LOG_DIR, exist_ok=True)
    # errors="replace", as github_wake, bitbucket_wake and stalled_wake do, so a lone surrogate
    # in a field (JSON can carry one) is written as "?" instead of raising and losing the line.
    with open(os.path.join(LOG_DIR, "%s-jira.log" % profile), "a", errors="replace") as fh:
        fh.write("%s ts=%s\n" % (line, now_ts()))


def parse_ts(s):
    """Jira gives 2026-08-06T13:31:18.975-0700 — py3.9 fromisoformat needs -07:00."""
    if not s:
        return None
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", s)
    if m:
        s = m.group(1) + ":" + m.group(2)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def adf_text(node, out):
    if isinstance(node, dict):
        t = node.get("text")
        if isinstance(t, str):
            out.append(t)
        for v in node.values():
            if isinstance(v, (dict, list)):
                adf_text(v, out)
    elif isinstance(node, list):
        for v in node:
            adf_text(v, out)


def one_line(s, cap=180):
    """Jira text (a summary, a comment, a changelog field and value) as part of one event line:
    whitespace collapsed, and the controls and bidi marks a terminal acts on but does not show
    dropped (:func:`_fields.unshown_dropped`), capped."""
    s = _fields.unshown_dropped(re.sub(r"\s+", " ", s or "")).strip()
    return (s[: cap - 1] + "…") if len(s) > cap else s


class Jira:
    def __init__(self, cfg):
        self.site = cfg["site"].rstrip("/")
        tok = "%s:%s" % (cfg["email"], cfg["api_token"])
        self.auth = "Basic " + base64.b64encode(tok.encode()).decode()
        self.search_legacy = False

    def get(self, path, params=None):
        url = self.site + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": self.auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def post(self, path, body):
        """POST a JSON document without putting credentials or payload on a CLI."""
        req = urllib.request.Request(
            self.site + path,
            data=json.dumps(body).encode(),
            headers={"Authorization": self.auth, "Accept": "application/json",
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def search(self, jql, fields, max_results):
        if not self.search_legacy:
            try:
                return self.get("/rest/api/3/search/jql", {
                    "jql": jql, "fields": fields, "maxResults": max_results})
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    self.search_legacy = True
                    note("note: /search/jql unavailable (%d) — legacy fallback" % e.code)
                else:
                    raise
        return self.get("/rest/api/3/search", {
            "jql": jql, "fields": fields, "maxResults": max_results})


def recent_changelog(j, key, want=None):
    want = CHANGES_PER_ISSUE if want is None else want
    head = j.get("/rest/api/3/issue/%s/changelog" % key, {"maxResults": 1})
    total = head.get("total", 0)
    if not total:
        return []
    page = j.get("/rest/api/3/issue/%s/changelog" % key,
                 {"startAt": max(0, total - want), "maxResults": want})
    return page.get("values", [])


def load_state(profile):
    try:
        with open(os.path.join(STATE_DIR, "%s.json" % profile)) as fh:
            raw = json.load(fh)
        # A file without the version marker is the arbitrary-subset list the old code
        # wrote. Its order means nothing and its dropped ids are already lost, so its first cycle
        # runs with a baseline (see `poll_profile`). It rides the in-memory state only: once a
        # cycle has written the marked file it is gone.
        return {"since": raw.get("since"), "seen": _seen.new_seen(raw.get("seen", [])),
                "degraded": raw.get("degraded", {}),
                "legacy_seen": _seen.legacy_baseline(raw, OLD_SEEN_CAP, subs=SEEN_SUBS)}
    except Exception:
        return {"since": None, "seen": _seen.new_seen(), "degraded": {}, "legacy_seen": None}


def save_state(profile, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "%s.json" % profile)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        # `seen` is written oldest-first and already pruned (`poll_profile` does that in the
        # running process); `seen_v` marks the order as meaningful. The old code reads this file
        # unchanged: `seen` is still a flat list and the marker is ignored.
        json.dump({"since": state["since"], "seen": list(state["seen"]), "seen_v": _seen.SEEN_V,
                   "degraded": state.get("degraded", {}), "build": BUILD}, fh)
    os.replace(tmp, path)


def poll_profile(profile, cfg, state):
    projects = cfg.get("projects") or []
    if not projects:
        return
    j = Jira(cfg)
    holes = _degraded.Cycle()  # Holes in THIS cycle
    suppress = set(cfg.get("suppress_account_ids", []))
    seen = _seen.ordered_seen(state)
    cutoff = parse_ts(state["since"]) if state["since"] else None
    if cutoff is None:
        cutoff = datetime.now(timezone.utc) - FIRST_RUN_LOOKBACK
    cutoff = cutoff.astimezone(timezone.utc) - OVERLAP
    cycle_start = datetime.now(timezone.utc)
    # The first cycle after a version 1 state file. What the old daemon had already
    # announced was cut to an arbitrary 1500 ids, so the ids it dropped are lost and Jira still
    # returns items nobody can prove were announced. Such an item is adopted SILENTLY (remembered, not
    # announced), but only on positive evidence: its own timestamp is at or before the old file's
    # watermark less LEGACY_GUARD, i.e. it existed when the old daemon last looked. The watermark, not
    # the load time, is the boundary: what happened while the daemon was down is exactly what the
    # stretched window below is for, and stays news. The boundary is the FILE's watermark, fixed for the
    # whole baseline: `state["since"]` moves on with every completed cycle and a held baseline must not
    # move with it. (An item older than `cutoff` above is not considered at all, as it never was.)
    # Nothing is adopted at all when the file could not have lost an id (it is under OLD_SEEN_CAP) or was
    # not fully read (a `comments` or `changelog` episode is open in it): see `_seen.legacy_baseline`.
    legacy = state.get("legacy_seen")
    adopt_before = _seen.legacy_cutoff(legacy, LEGACY_GUARD)
    adopted = dict.fromkeys((kind for kind, _ in ADOPTED), 0)

    gap_min = 30
    prev = parse_ts(state["since"]) if state["since"] else None
    if prev is not None:
        down = (datetime.now(timezone.utc) - prev.astimezone(timezone.utc)).total_seconds()
        gap_min = max(30, int(down // 60) + 10)
    gap_min = min(gap_min, 10080)

    jql = "project in (%s) AND updated >= -%dm ORDER BY updated DESC" % (
        ",".join(projects), gap_min)
    res = j.search(jql, "summary,updated,created,creator", SEARCH_MAX_ISSUES)

    for issue in res.get("issues", []):
        key = issue.get("key", "?")
        f = issue.get("fields", {})
        upd = parse_ts(f.get("updated"))
        if upd is None or upd.astimezone(timezone.utc) <= cutoff:
            continue

        created = parse_ts(f.get("created"))
        cid = "created:" + key
        if created and created.astimezone(timezone.utc) > cutoff:
            if cid in seen:
                _seen.touch_seen(seen, cid)
            else:
                seen[cid] = None
                if _seen.predates(f.get("created"), adopt_before):
                    adopted["created"] += 1
                else:
                    who = f.get("creator") or {}
                    if who.get("accountId") not in suppress:
                        emit(profile, 'type=created issue=%s by="%s" | %s' % (
                            key, _fields.who_field(who.get("displayName", "?")),
                            one_line(f.get("summary", ""), 120)))

        try:
            comments = j.get("/rest/api/3/issue/%s/comment" % key,
                             {"orderBy": "-created", "maxResults": COMMENTS_PER_ISSUE}).get("comments", [])
        except Exception as e:
            note("note: [%s] comment fetch failed for %s: %s" % (profile, key, e))
            holes.record("comments", "%s: %s" % (key, e))
            comments = []
        for c in comments:
            ts = parse_ts(c.get("created"))
            mid = "comment:" + str(c.get("id"))
            if ts is None or ts.astimezone(timezone.utc) <= cutoff:
                continue
            if mid in seen:
                _seen.touch_seen(seen, mid)
                continue
            seen[mid] = None
            if _seen.predates(c.get("created"), adopt_before):
                adopted["comment"] += 1
                continue
            author = c.get("author") or {}
            if author.get("accountId") in suppress:
                continue
            buf = []
            adf_text(c.get("body"), buf)
            emit(profile, 'type=comment issue=%s by="%s" | %s' % (
                key, _fields.who_field(author.get("displayName", "?")), one_line(" ".join(buf))))

        try:
            entries = recent_changelog(j, key)
        except Exception as e:
            note("note: [%s] changelog fetch failed for %s: %s" % (profile, key, e))
            holes.record("changelog", "%s: %s" % (key, e))
            entries = []
        for entry in entries:
            ts = parse_ts(entry.get("created"))
            mid = "change:" + str(entry.get("id"))
            if ts is None or ts.astimezone(timezone.utc) <= cutoff:
                continue
            if mid in seen:
                _seen.touch_seen(seen, mid)
                continue
            seen[mid] = None
            if _seen.predates(entry.get("created"), adopt_before):
                adopted["change"] += 1
                continue
            author = entry.get("author") or {}
            if author.get("accountId") in suppress:
                continue
            bits = ['%s->"%s"' % (one_line(i.get("field") or "?", 60), one_line(i.get("toString") or "", 60))
                    for i in entry.get("items", [])]
            if bits:
                emit(profile, 'type=change issue=%s by="%s" | %s' % (
                    key, _fields.who_field(author.get("displayName", "?")), "; ".join(bits)))

    state["since"] = cycle_start.isoformat()
    # A completed cycle with a hole in it used to reach only
    # `daemon-jira.log`, which nothing tails, while `save_state` below advanced
    # the watermark and every health signal read green.
    for emit_line, note_line in _degraded.reconcile(
            holes, state.setdefault("degraded", {}), time.time()):
        if emit_line:  # A silenced repeat is daemon-log only
            emit(profile, emit_line)
        note("[%s] %s" % (profile, note_line))

    # Bound the memory HERE, in the running process, so that what a restart reloads is
    # exactly what this process already knows. Every id Jira returned this cycle was just moved to the
    # newest end, so it survives if its kind's cap covers one poll's window, which the caps are sized
    # to. Then the baseline decides whether it runs again, and only after that does the file get
    # written.
    state["seen"] = _seen.new_seen(_seen.prune_seen(seen, SEEN_CAPS))
    if legacy:
        _seen.settle_legacy(profile, state, legacy, adopt_before, adopted,
                            sorted(set(holes.failures) & set(SEEN_SUBS)), ADOPTED, note)

    save_state(profile, state)


def main():
    note("jira daemon starting build=%s" % _build.render(BUILD))
    states = {}
    waiting = set()
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
            cfg = pcfg.get("jira")
            if not cfg or not cfg.get("projects"):
                continue
            if cfg.get("api_token") in (None, "", PLACEHOLDER):
                if profile not in waiting:
                    note("[%s] waiting: jira api_token is a placeholder" % profile)
                    waiting.add(profile)
                continue
            if profile in waiting:
                note("[%s] credentials detected — polling" % profile)
                waiting.discard(profile)
            active.append(profile)
            if profile not in states:
                states[profile] = load_state(profile)
                note("[%s] %s" % (profile, "watermark initialized" if not states[profile]["since"]
                                  else "watermark resumed"))
                migrating = _seen.legacy_note(profile, states[profile], LEGACY_GUARD)
                if migrating:
                    note(migrating)
            try:
                poll_profile(profile, cfg, states[profile])
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    note("[%s] error: jira auth rejected (401)" % profile)
                    emit(profile, "error: jira auth rejected (401) — check email/api_token")
                    waiting.add(profile)
                    continue
                failed = "cycle failed for %s: HTTP %d" % (profile, e.code)
                note("note: " + failed)
            except Exception as e:
                failed = "cycle failed for %s: %s" % (profile, e)
                note("note: " + failed)

        if failed:
            failures += 1
            if failures == 3:
                for p in active:
                    emit(p, "error: jira polling failing repeatedly — last: %s" % failed)
        else:
            if failures >= 3:
                note("note: polling recovered after %d failures" % failures)
            failures = 0
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
