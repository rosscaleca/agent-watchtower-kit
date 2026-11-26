#!/usr/bin/env python3
"""Watchtower Inbox daemon — multi-profile agent-mail arrival poller.

Polls an Inbox (agent mail) events API (`GET /v1/events`) for
``message.received`` / ``message.received.unauthenticated`` rows on each
profile's configured inboxes and appends wake lines to
~/Library/Logs/watchtower/<profile>-inbox.log. This folds the standalone
per-inbox WebSocket supervisor's job into the profile architecture: one
daemon, N profiles, and — because inbox-wake context is inherently
per-inbox — each profile lists exactly the inbox ids its session answers
for (the address IS the routing key).

Per profile config::

    "inbox": {
      "api_url": "https://api.inbox.example",
      "api_key": "PASTE-KEY-HERE",                              # optional default
      "inboxes": {
        "<uuid-a>": "pm-acme",                            # uses the default
        "<uuid-b>": {"label": "legacy", "api_key": "td_…"}      # its own key
      }
    }

``inboxes`` maps inbox id -> either a short label (served by the profile's
``api_key``) or ``{"label": …, "api_key": …}`` carrying its own credential. A per-inbox ``api_key`` that is absent, empty or
the placeholder uses the profile ``api_key`` (lenient).
Keys are read into request headers in-process only — never printed, logged,
or placed on a command line (same policy as the Jira daemon). Auth is
Bearer; an org-scoped key may watch any of the org's inboxes, an
inbox-scoped key exactly its own (the API 403s anything else, which
surfaces here as a parked profile after 3 failures).

Why per-inbox keys exist: ``scope_inbox_id`` is SINGULAR, so
one inbox-scoped key cannot serve two inboxes, and a profile watching two
could only ever be served by an org-wide key. Retiring that key therefore
needed this. Inboxes are grouped BY CREDENTIAL and one ``/v1/events`` call
is made per group, so a profile whose inboxes all share one key still makes
exactly one call. An inbox with no key available raises ``MissingCredential``
rather than being skipped — an unpolled inbox stops waking its session and
nothing else would say so.

Unauthenticated arrivals (SPF/DKIM/DMARC not all pass) are emitted with
``auth=FAIL`` — the intake playbook treats those as hold-for-human, never
auto-act (a legitimate correspondent CAN arrive unauthenticated; the flag
is a trust boundary, not spam scoring).

Downtime coverage: the ``since`` watermark is server-side and inclusive,
with a client-side overlap + seen-set dedupe — a nap of any length is
covered on resume. First run seeds a short lookback so history is not
replayed.

What has been announced is remembered in ``seen``, an ordered memory
of bare event ids that a restart reloads intact — see ``SEEN_CAPS`` and
``_seen``. It used to be cut to an arbitrary 1500 on save.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import _build  # sibling module; the daemons dir is sys.path[0] when run as a script
import _seen  # the ordered, per-kind dedup memory the polling daemons share

#: What bytes this process is running, logged on start and saved with the state.
BUILD = _build.describe(__file__)

CONFIG_PATH = os.path.expanduser("~/.config/watchtower/config.json")
STATE_DIR = os.path.expanduser("~/.local/state/watchtower/inbox")
LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
POLL_SECS = 60
OVERLAP = timedelta(minutes=5)
FIRST_RUN_LOOKBACK = timedelta(minutes=10)
PLACEHOLDER = "PASTE-KEY-HERE"
PLACEHOLDERS = (None, "", PLACEHOLDER)
EVENT_TYPES = ("message.received", "message.received.unauthenticated")

#: What one poll asks the events API for, per credential (one call per distinct credential). This
#: BUILDS the request, and the dedup memory below is sized from it, so the two cannot drift apart.
EVENTS_PER_PAGE = 50      # `limit`: the newest 50 events since the watermark, never followed

#: The dedup memory, ``state["seen"]``: the ids of the events already announced. They are
#: BARE event ids, one kind, so the kind cannot come from a prefix (``seen_kind`` names it), and they
#: must stay bare: the previous code compares the bare id, so a prefixed id saved now would never
#: match after a rollback and every event still in the window would be announced again.
#: The design is in ``_seen``; this is its sizing.
#:
#: It was saved as ``list(seen)[-1500:]`` from a set. A set has no order, so once it passed 1500 an
#: ARBITRARY 1500 were kept. This daemon has no client-side time filter at all: the events API is asked
#: for everything since the watermark (the last cycle's start less OVERLAP) and every event it returns
#: that ``seen`` does not hold is announced, so a dropped id was announced again after the next restart
#: for as long as its event was at or after the saved watermark: the last few minutes before it, and it
#: is a wake for a message a seat may already have answered.
#:
#: The window is the most events one poll can return, per credential; the caps scale with the number of
#: credential groups a profile has (the always-on host's has one):
SEEN_WINDOW = {"event": EVENTS_PER_PAGE}                  # 50
#: An id is returnable for the overlap plus one poll: six polls, one after another (seven in the one
#: case of an event stamped exactly at a cycle start, against a ``since`` that is inclusive).
SEEN_POLLS = _seen.returnable_polls(OVERLAP, POLL_SECS)
#: How many windows the kind remembers, and why. An id that keeps being returned is moved to the newest
#: end every time (``touch_seen``) and needs only its own window; six windows is the worst case with NO
#: refresh at all, because at most six windows of newer ids can be added behind an id in the polls it is
#: still returnable in. Measured on the live profile log, 2026-09-03..30: 28 wakes on the median day, 69
#: on the busiest, so it takes a burst, not a busy day, to fill even one poll's page of 50.
SEEN_KEEP = {"event": SEEN_POLLS}
#: Per credential group: event 300.
SEEN_CAPS = _seen.caps_from(SEEN_WINDOW, SEEN_KEEP)
#: The kind of every id in ``seen``.
SEEN_KIND = "event"
#: What 38ffbbd, and every build since the daemon was written, saved: ``list(seen)[-OLD_SEEN_CAP:]`` from
#: a set. A set only grows, so a file holding FEWER ids than this has never lost one, and the migration
#: reads that off the file (``_seen.legacy_baseline``).
OLD_SEEN_CAP = 1500
#: The first cycle after loading a file the old code wrote adopts, SILENTLY, an event it cannot find in
#: ``seen`` only if its own ``at`` is at or before the old daemon's last look less this. The last look
#: is NOT the file's ``since``: that is the cycle's start less OVERLAP (see ``load_state``). The old
#: daemon's last look can lag by a fetch and the two clocks are not the same clock, so an event this
#: close to it is announced if unseen: a duplicate wake costs a turn, a swallowed one costs the event.
#: And only where the file says ids were LOST: a file under OLD_SEEN_CAP has never lost one, so for it
#: nothing is adopted and everything it does not remember announces. (There is no open-episode half
#: here: a failed call raises out of the cycle before anything is saved, so a file is only ever written
#: by a cycle that read everything.)
LEGACY_GUARD = timedelta(seconds=POLL_SECS)
#: What that first cycle counts and says.
ADOPTED = (("event", "event(s)"),)


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
    # Errors="replace", as github_wake, bitbucket_wake and stalled_wake do, so a lone surrogate
    # in a field (JSON can carry one) is written as "?" instead of raising and losing the line.
    with open(os.path.join(LOG_DIR, "%s-inbox.log" % profile), "a", errors="replace") as fh:
        fh.write("%s ts=%s\n" % (line, now_ts()))


def one_line(s, cap=140):
    s = re.sub(r"\s+", " ", s or "").strip()
    return (s[: cap - 1] + "…") if len(s) > cap else s


class MissingCredential(Exception):
    """No usable API key for a configured inbox.

    Raised rather than skipping the inbox: a silently unpolled inbox stops
    producing wakes and nothing says so, which is the silent-drop shape
    the drain exists to remove.
    """


class InboxAPI:
    def __init__(self, base, key):
        self.base = base.rstrip("/")
        self.headers = {
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        }

    def get(self, path, params=None):
        url = self.base + path
        if params:
            # urlencode with doseq so repeated keys (inbox_id, event_type)
            # become the multi-value filters the API composes OR-within-key.
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())


def seen_kind(mid):
    """Every id in ``seen`` is an event id: one kind, whatever the id looks like."""
    return SEEN_KIND


def load_state(profile):
    try:
        with open(os.path.join(STATE_DIR, "%s.json" % profile)) as fh:
            raw = json.load(fh)
        # A file without the version marker is the arbitrary-subset list the old code
        # wrote. Its order means nothing and its dropped ids are already lost, so its first cycle
        # runs with a baseline (see `poll_profile`). It rides the in-memory state only: once a cycle
        # has written the marked file it is gone. This daemon saves ``cycle_start - OVERLAP`` as
        # `since`, so the old daemon last LOOKED an OVERLAP later than the file says (``lag``); the
        # API never returns an event older than `since`, so measured from `since` nothing could ever
        # be adopted.
        return {"since": raw.get("since"), "seen": _seen.new_seen(raw.get("seen", [])),
                "legacy_seen": _seen.legacy_baseline(raw, OLD_SEEN_CAP, lag=OVERLAP)}
    except Exception:
        return {"since": None, "seen": _seen.new_seen(), "legacy_seen": None}


def save_state(profile, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "%s.json" % profile)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        # `seen` is written oldest-first and already pruned (`poll_profile` does that in the
        # running process); `seen_v` marks the order as meaningful. The old code reads this file
        # unchanged: `seen` is still a flat list and the marker is ignored.
        json.dump({"since": state["since"], "seen": list(state["seen"]), "seen_v": _seen.SEEN_V,
                   "build": BUILD}, fh)
    os.replace(tmp, path)


def inbox_groups(cfg):
    """[(api_key, [inbox_id, ...])] — one group per DISTINCT credential.

    An inbox-scoped key can serve exactly one inbox
    (``scope_inbox_id`` is singular), so a profile watching two inboxes on
    two scoped keys needs two calls. Grouping by credential rather than per
    inbox keeps the single batched call for the common case where every
    inbox shares the top-level key — which is what the always-on host runs.

    A per-inbox ``api_key`` that is absent, ``""`` or the placeholder falls
    back to the profile ``api_key`` (lenient). With no
    default to fall back to it is still ``MissingCredential``, never a
    silent skip.
    """
    default = cfg.get("api_key")
    groups = {}
    for iid, value in (cfg.get("inboxes") or {}).items():
        key = value.get("api_key") if isinstance(value, dict) else None
        # A per-inbox api_key that is absent, empty, or the literal placeholder
        # means "use the profile default" (lenient):
        # the placeholder is this daemon's word for "not filled in", and ""
        # already fell back. A review found that `key or default` kept the
        # placeholder (it is truthy), so a profile whose default was REAL parked
        # here while the watchdog, reading that real default, reported
        # dead/stale — the two gates disagreed on a reachable config.
        if key in PLACEHOLDERS:
            key = default
        if not key:
            raise MissingCredential(
                "inbox %s has no api_key and the profile has no default" % iid)
        groups.setdefault(key, []).append(iid)
    return list(groups.items())


def has_credentials(cfg):
    """True when every configured inbox has a usable key.

    codex P1 on this branch: ``main()`` gated on the TOP-LEVEL ``api_key``
    alone, so the per-inbox-only config this change exists to enable would
    park the profile, make zero requests, and stop every inbox wake — the
    feature's own target state, dead. The gate has to ask the same question
    ``inbox_groups`` answers.
    """
    if not (cfg or {}).get("inboxes"):
        return False
    try:
        groups = inbox_groups(cfg)
    except MissingCredential:
        return False
    return bool(groups) and all(k not in PLACEHOLDERS for k, _ in groups)


def inbox_label(inboxes, inbox_id):
    """Short label for the wake line, under either config shape.

    The value is a plain label in the original shape and a dict in the
    per-key shape; returning the dict would put ``inbox={'label': ...}``
    into the log a session parses.
    """
    value = (inboxes or {}).get(inbox_id)
    if isinstance(value, dict):
        return value.get("label") or (inbox_id[:8] if inbox_id else "?")
    return value or (inbox_id[:8] if inbox_id else "?")


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_FRACTION = re.compile(r"\.(\d+)(?=[+-]|$)")


def event_time(ev):
    """Sort key for emission: the event's own ``at``, parsed.

    The API emits ``at`` as ``2026-09-09T14:31:57.573374Z`` and other fields
    as ``…+00:00``; the daemons run under /usr/bin/python3 (3.9 on this host),
    whose ``fromisoformat`` rejects ``Z`` — so normalise before parsing.
    Missing or unparseable sorts FIRST and never raises: a bad timestamp must
    not stop a wake. 3.9's ``fromisoformat`` also takes a fractional part of
    exactly 3 or 6 digits (it is the inverse of ``isoformat()``), so the
    fraction is normalised to 6 first — a legitimate ``.5`` or ``.250`` must
    not silently sort to the epoch.
    """
    raw = ev.get("at")
    if not isinstance(raw, str) or not raw:
        return _EPOCH
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"),
                         raw.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _EPOCH
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def poll_profile(profile, cfg, state):
    inboxes = cfg.get("inboxes") or {}
    if not inboxes:
        return
    seen = _seen.ordered_seen(state)
    cycle_start = datetime.now(timezone.utc)
    if state["since"]:
        since = state["since"]
    else:
        since = (cycle_start - FIRST_RUN_LOOKBACK).isoformat()
    # The first cycle after a version 1 state file. What the old daemon had already
    # announced was cut to an arbitrary 1500 ids, so the ids it dropped are lost and the API still
    # returns events nobody can prove were announced. Such an event is adopted SILENTLY (remembered,
    # not announced), but only on positive evidence: its own `at` is at or before the old daemon's last
    # look less LEGACY_GUARD, i.e. it existed when the old daemon last looked. That instant, not the
    # load time, is the boundary: what arrived while the daemon was down is exactly what resume
    # coverage is for, and stays news. It is fixed for the whole baseline: `state["since"]` moves on
    # with every completed cycle and a held baseline must not move with it. Nothing is adopted at all
    # when the file could not have lost an id (it is under OLD_SEEN_CAP): see `_seen.legacy_baseline`.
    legacy = state.get("legacy_seen")
    adopt_before = _seen.legacy_cutoff(legacy, LEGACY_GUARD)
    adopted = dict.fromkeys((kind for kind, _ in ADOPTED), 0)

    items = []
    groups = inbox_groups(cfg)
    for key, ids in groups:
        api = InboxAPI(cfg["api_url"], key)
        params = [
            ("since", since),
            ("limit", EVENTS_PER_PAGE),
        ]
        # Only this credential's own inboxes: asking a scoped key for another
        # inbox's id 403s the whole group, taking a healthy inbox down with it.
        for iid in ids:
            params.append(("inbox_id", iid))
        for et in EVENT_TYPES:
            params.append(("event_type", et))
        items.extend(api.get("/v1/events", params).get("items", []))

    # Newest-first from the API — per GROUP. With two credentials `items` is
    # group 1 newest-first followed by group 2 newest-first, so `reversed()`
    # read forward within a group and backward across them. Sort on the event's own `at` so the log reads forward whatever
    # the API order or the group count.
    for ev in sorted(items, key=event_time):
        eid = str(ev.get("id"))
        if eid in seen:
            _seen.touch_seen(seen, eid)
            continue
        seen[eid] = None
        if _seen.predates(ev.get("at"), adopt_before):
            adopted["event"] += 1
            continue
        etype = ev.get("event_type", "")
        payload = ev.get("payload") or {}
        inbox_id = str(ev.get("inbox_id") or payload.get("inbox_id") or "")
        label = inbox_label(inboxes, inbox_id)
        message_id = payload.get("message_id", "?")
        authed = etype == "message.received"
        # The wake line carries IDS, not content: the received-event payload
        # has no from/subject, and the REST surface deliberately exposes no
        # read-one-message GET (verified 2026-08-18: 405 on that path — reads
        # go through threads). The woken session lists threads and reads, the
        # same flow the standalone inbox-wake proved in production. Sender
        # suppression therefore isn't possible at this layer; own-address
        # traffic is rare on PM inboxes and the session-side playbook drops it.
        emit(profile, "type=mail_received inbox=%s auth=%s message_id=%s" % (
            label, "pass" if authed else "FAIL", message_id))

    # Watermark: overlap behind the cycle start; the seen-set absorbs the
    # replay. (Not the newest event's `at` — a quiet cycle must still move
    # the window forward, or one ancient event pins `since` forever.)
    state["since"] = (cycle_start - OVERLAP).isoformat()
    # Bound the memory HERE, in the running process, so that what a restart reloads is
    # exactly what this process already knows. Every id the API returned this cycle was just moved to
    # the newest end, so it survives if the cap covers one poll's window per credential group, which it
    # is sized to. Then the baseline decides whether it runs again (a failed call raised out of this
    # function before here, so a cycle that gets this far read everything), and only after that does the
    # file get written.
    state["seen"] = _seen.new_seen(_seen.prune_seen(
        seen, _seen.scaled(SEEN_CAPS, len(groups)), kind_of=seen_kind))
    if legacy:
        _seen.settle_legacy(profile, state, legacy, adopt_before, adopted, [], ADOPTED, note)
    save_state(profile, state)


def main():
    note("inbox daemon starting build=%s" % _build.render(BUILD))
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
            cfg = pcfg.get("inbox")
            if not cfg or not cfg.get("inboxes"):
                continue
            if not has_credentials(cfg):
                if profile not in waiting:
                    note("[%s] waiting: no usable inbox api_key "
                         "(top-level or per-inbox)" % profile)
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
                if e.code in (401, 403):
                    note("[%s] error: inbox auth rejected (%d)" % (profile, e.code))
                    emit(profile, "error: inbox auth rejected (%d) — check api_key / inbox scope" % e.code)
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
                    emit(p, "error: inbox polling failing repeatedly — last: %s" % failed)
        else:
            if failures >= 3:
                note("note: polling recovered after %d failures" % failures)
            failures = 0
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
