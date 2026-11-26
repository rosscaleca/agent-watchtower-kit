#!/usr/bin/env python3
"""Watchtower schedule daemon — cron-style wake lines, per profile.

The one source that isn't reactive: a PM/coordination session needs
TIME-driven wakes (morning digest, stale-ticket sweep, SLA nudges), not
just event-driven ones. Each profile lists named entries; when an entry's
moment passes, one ``type=cron name=<entry>`` line lands in
~/Library/Logs/watchtower/<profile>-schedule.log and the session's
Monitor wakes it. What the session DOES with a named wake is the
session's playbook (charter/skill), not this daemon's business.

Per profile config::

    "schedule": {
      "entries": [
        {"name": "morning-digest", "at": "07:30", "days": ["mon","tue","wed","thu","fri"]},
        {"name": "stale-sweep",    "at": "13:00"}
      ]
    }

Times are the MACHINE's local time (a 24/7 box has a stable clock and
timezone; keep the box on the timezone the humans schedule in). ``days``
omitted means every day. Catch-up semantics: an entry fires when its
moment for today has passed and it hasn't fired today — so a daemon (or
machine) that was down at 07:30 still fires the digest on resume the same
day, and never replays yesterday. That is deliberately at-most-once per
day per entry: schedule twice daily as two named entries.

No credentials, no network — this daemon reads config and the clock.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

import _build  # sibling module; the daemons dir is sys.path[0] when run as a script

#: What bytes this process is running, logged on start and saved with the state.
BUILD = _build.describe(__file__)

CONFIG_PATH = os.path.expanduser("~/.config/watchtower/config.json")
STATE_DIR = os.path.expanduser("~/.local/state/watchtower/schedule")
LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
POLL_SECS = 30
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
    with open(os.path.join(LOG_DIR, "%s-schedule.log" % profile), "a", errors="replace") as fh:
        fh.write("%s ts=%s\n" % (line, now_ts()))


def load_state(profile):
    try:
        with open(os.path.join(STATE_DIR, "%s.json" % profile)) as fh:
            return json.load(fh)
    except Exception:
        return {"last_fired": {}}


def save_state(profile, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "%s.json" % profile)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(dict(state, build=BUILD), fh)
    os.replace(tmp, path)


def valid_entry(entry):
    """(name, HH, MM, days) or None. Bad entries are skipped loudly by the
    caller rather than half-firing."""
    name = entry.get("name") or ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", name):
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", entry.get("at") or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    days = entry.get("days")
    if days is not None:
        days = [str(d).lower()[:3] for d in days]
        if not days or any(d not in DAY_KEYS for d in days):
            return None
    return name, hh, mm, days


def due_entries(entries, last_fired, now):
    """Names due to fire at local time ``now`` (pure — unit-testable).

    Due = today matches ``days``, today's HH:MM has passed, and
    ``last_fired[name]`` is not already today (catch-up included: any time
    later the same day still fires; yesterday never replays).
    """
    today = now.strftime("%Y-%m-%d")
    weekday = DAY_KEYS[now.weekday()]
    due = []
    for entry in entries:
        parsed = valid_entry(entry)
        if parsed is None:
            continue
        name, hh, mm, days = parsed
        if days is not None and weekday not in days:
            continue
        if (now.hour, now.minute) < (hh, mm):
            continue
        if last_fired.get(name) == today:
            continue
        due.append(name)
    return due


def main():
    note("schedule daemon starting build=%s" % _build.render(BUILD))
    states = {}
    warned = set()
    while True:
        try:
            with open(CONFIG_PATH) as fh:
                config = json.load(fh)
        except Exception as e:
            note("note: config unreadable (%s) — sleeping" % e)
            time.sleep(300)
            continue

        now = datetime.now()  # machine-local, on purpose (see module docstring)
        for profile, pcfg in (config.get("profiles") or {}).items():
            cfg = pcfg.get("schedule")
            entries = (cfg or {}).get("entries") or []
            if not entries:
                continue
            for entry in entries:
                if valid_entry(entry) is None:
                    key = "%s:%s" % (profile, json.dumps(entry, sort_keys=True))
                    if key not in warned:
                        note("note: [%s] invalid schedule entry skipped: %s" % (profile, entry))
                        warned.add(key)
            if profile not in states:
                states[profile] = load_state(profile)
            state = states[profile]
            fired = False
            for name in due_entries(entries, state["last_fired"], now):
                emit(profile, "type=cron name=%s" % name)
                note("[%s] fired %s" % (profile, name))
                state["last_fired"][name] = now.strftime("%Y-%m-%d")
                fired = True
            if fired:
                # Prune names no longer configured so state can't grow forever.
                live = {valid_entry(e)[0] for e in entries if valid_entry(e)}
                state["last_fired"] = {k: v for k, v in state["last_fired"].items() if k in live}
            # Heartbeat every cycle, fired or not: the watchdog judges liveness
            # by this file's mtime, and a state written only on (daily) fires
            # looks permanently stale to it. `since` mirrors the other daemons'
            # watermark so `just status` shows freshness too.
            state["since"] = datetime.now(timezone.utc).isoformat()
            save_state(profile, state)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
