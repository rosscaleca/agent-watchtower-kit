#!/usr/bin/env python3
"""Watchtower watchdog — notices when a sibling watcher has gone dark.

On a laptop a dead watcher gets noticed because a human is staring at the
session; on a 24/7 headless box nobody is. launchd's KeepAlive already
covers crashes — what it CANNOT see is a daemon that is alive but
useless: credentials rotted, config broken, or a poll loop wedged. The
tell for all of those is the same: the per-profile STATE WATERMARK stops
moving. This daemon watches watermark ages and plist liveness, and
emits ``error: watchdog: ...`` lines into the AFFECTED profile's own logs
— so the session that lost its signal is the session that wakes.

Classification per (profile, source):

- plist not loaded            -> error (daemon uninstalled/unloaded)
- watermark stale > threshold -> error (dead-but-loaded / wedged / auth rot)
- state file absent + config credential is a placeholder -> note only
  (a parked profile is a known state, not a failure)
- state file absent otherwise -> grace: error only after the daemon has
  had STARTUP_GRACE to seed its first watermark

Emission is rate-limited to once per RENOTIFY per (profile, source)
episode, and a recovery line is emitted when the watermark moves again.
The same cadence re-announces checkout topology and freshness episodes.
The watchdog reads config (which holds tokens) but never prints a value
from it — only key presence/placeholder-ness is classified.

Who watches the watchdog: launchd (crash restarts) and `just status`
(human glance). The watchdog itself holds no credentials and makes no
network calls, so the failure modes it exists to catch cannot happen to
it; a WEDGED watchdog degrades to exactly today's status quo. A STALE one
does not: a watchdog running old bytes would silently lack any
signal a newer revision added, and the absence of `fleet_outdated` is
exactly what "everything is current" looks like. So it checks itself from
memory every cycle — BUILD is the hash of the bytes this process imported,
compared against its own file on disk — and reports `source=watchdog`
like any other daemon; `just fleet` runs on-disk code and shows the row
from the marker recorded at start.

A further layer sits ABOVE that one: is the CHECKOUT what origin has?
The rows above compare a running process against a file, so all of them
read `current` while the checkout they share sits behind origin and every
merged fix is inert — measured on the always-on host with all three layers distinct
at once. `fleet_behind` is a separate event from `fleet_outdated` because
it is a separate remedy: behind origin needs a PULL, stale bytes need a
RESTART, a host can need both, and the wrong order silently half-works.
Fetch-free, so the watchdog still makes no network calls; see
`_build.upstream_status` for what that costs and why the fetch age is
carried beside every verdict.

A last layer makes the fetch-free limit wakeable rather than merely visible in
``just fleet``: a nominally current comparison whose repository-wide
last-fetch lower bound is stale emits ``fleet_fetch_stale``, while an observed
``behind`` / ``diverged`` state emits ``fleet_behind_still`` on the same
renotify cadence.  Neither path parses FETCH_HEAD contents or contacts origin.

Config (optional, top-level key ``watchdog``)::

    "watchdog": {"stale_minutes": 10, "renotify_minutes": 60}
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import _build  # sibling module; the daemons dir is sys.path[0] when run as a script

CONFIG_PATH = os.path.expanduser("~/.config/watchtower/config.json")
STATE_ROOT = os.path.expanduser("~/.local/state/watchtower")
STATE_DIR = os.path.join(STATE_ROOT, "watchdog")
OWN_STATE = os.path.join(STATE_DIR, "episodes.json")
#: Where the sibling daemons' source files live — the bytes a CURRENT daemon must be running.
DAEMONS_DIR = os.path.dirname(os.path.abspath(__file__))
#: The watchdog's own marker. Host-scoped (the watchdog has no per-profile state):
#: written on start so `--fleet` can read it from disk, and compared in-process each cycle
#: so a stale WATCHDOG reports itself rather than falling silent — the absence of
#: `fleet_outdated` must never be what "the watchdog is stale" looks like.
BUILD = _build.describe(__file__)
OWN_BUILD = os.path.join(STATE_DIR, "build.json")
LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
POLL_SECS = 120
DEFAULT_STALE_MIN = 10
DEFAULT_RENOTIFY_MIN = 60
STARTUP_GRACE_SECS = 300

# source -> (config key on the profile, config subkey whose emptiness means
# "not configured", credential key whose placeholder parks the profile)
SOURCES = {
    "github": ("github", "repos", None),
    "jira": ("jira", "projects", "api_token"),
    "bitbucket": ("bitbucket", "repos", None),
    "inbox": ("inbox", "inboxes", "api_key"),
    "schedule": ("schedule", "entries", None),
    "stalled": ("stalled", "days", None),
}
PLACEHOLDERS = ("PASTE-TOKEN-HERE", "PASTE-KEY-HERE", "", None)
#: The daemons the fleet report covers. Deliberately NOT `SOURCES`: that is
#: `classify()`'s config-key map (which daemons to liveness-check from config);
#: this is which files on disk a running process must match, and the watchdog is
#: in the second list but has no config block for the first.
FLEET = tuple(SOURCES) + ("watchdog",)


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
    with open(os.path.join(LOG_DIR, "%s-watchdog.log" % profile), "a", errors="replace") as fh:
        fh.write("%s ts=%s\n" % (line, now_ts()))


def loaded_labels():
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception as e:
        note("note: launchctl list failed: %s" % e)
        return None  # unknown — do not accuse daemons on missing evidence
    return {line.split()[-1] for line in out.splitlines() if "com.watchtower." in line}


def _has_credential(source, block, cred):
    """Does this source have a usable credential?

    codex P2: the inbox daemon may carry per-inbox keys instead of a
    top-level one. Checking only the top level would return "parked" for a
    migrated profile — and that return happens BEFORE the liveness and
    watermark checks, so the profile would lose watchdog alerts entirely
    while looking merely unconfigured.
    """
    if block.get(cred) not in PLACEHOLDERS:
        return True
    if source != "inbox":
        return False
    entries = (block.get("inboxes") or {}).values()
    return bool(entries) and all(
        isinstance(v, dict) and v.get("api_key") not in PLACEHOLDERS for v in entries)


def classify(profile, source, cfg_block, labels, stale_secs, now):
    """Return (status, detail) where status in {ok, parked, missing, stale, dead}."""
    key, required, cred = SOURCES[source]
    block = (cfg_block or {}).get(key)
    if not block or not block.get(required):
        return "unconfigured", ""
    if cred and not _has_credential(source, block, cred):
        return "parked", "%s is a placeholder" % cred
    if labels is not None and ("com.watchtower.%s" % source) not in labels:
        return "dead", "launchd job com.watchtower.%s not loaded" % source
    state_path = os.path.join(STATE_ROOT, source, "%s.json" % profile)
    try:
        age = now - os.path.getmtime(state_path)
    except OSError:
        return "missing", "no state file yet"
    if age > stale_secs:
        return "stale", "watermark %dm old" % int(age / 60)
    return "ok", ""


def build_status(profile, source, daemons_dir=None):
    """Is the running daemon on the bytes now on disk?

    Reads the `build` marker the daemon saved with its state and compares it
    to the hash of `<daemons_dir>/<source>_wake.py`. Returns
    `(status, loaded_src, disk_src, head)` with status in
    {current, outdated, unknown}; `unknown` is never an accusation (no state,
    no marker, unreadable file).
    """
    daemons_dir = daemons_dir or DAEMONS_DIR
    state_path = os.path.join(STATE_ROOT, source, "%s.json" % profile)
    try:
        with open(state_path) as fh:
            loaded = (json.load(fh) or {}).get("build") or {}
    except Exception:
        loaded = {}
    status, disk = _build.compare(loaded, os.path.join(daemons_dir, "%s_wake.py" % source))
    return status, loaded.get("src"), disk, loaded.get("head")


def record_own_build():
    """Write the watchdog's own marker so `--fleet` (on-disk code) can report the running one."""
    try:
        os.makedirs(os.path.dirname(OWN_BUILD), exist_ok=True)
        tmp = OWN_BUILD + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"build": BUILD, "started": now_ts()}, fh)
        os.replace(tmp, OWN_BUILD)
    except Exception as e:
        note("note: could not record own build: %s" % e)


def watchdog_build_status(daemons_dir=None):
    """The watchdog's own row: the marker it recorded on start vs its file on disk now."""
    try:
        with open(OWN_BUILD) as fh:
            loaded = (json.load(fh) or {}).get("build") or {}
    except Exception:
        loaded = {}
    disk_file = os.path.join(daemons_dir or DAEMONS_DIR, "watchdog_wake.py")
    status, disk = _build.compare(loaded, disk_file)
    return status, loaded.get("src"), disk, loaded.get("head")


def checkout_status(daemons_dir=None):
    """The THIRD staleness layer: is the CHECKOUT behind origin?

    Host-scoped, like the watchdog's own row — there is one checkout, not one
    per profile. Fetch-free: it reads the remote-tracking ref the last fetch
    already wrote and never touches the network, so the watchdog keeps its "no
    credentials, no network calls" property. See `_build.upstream_status`.
    """
    return _build.upstream_status(os.path.join(daemons_dir or DAEMONS_DIR, "watchdog_wake.py"))


def fleet_report(config, daemons_dir=None):
    """One line per (profile, source), plus one host-scoped line for the watchdog itself.

    The answer to "is the fleet current?" without a shell on the host beyond
    `just fleet` — its reason to exist. Runs on-disk code, so it is
    correct for the five pollers whatever the watchdog process is running;
    the watchdog's own row comes from the marker it recorded on start.
    """
    lines = []
    for profile in sorted((config.get("profiles") or {})):
        for source in SOURCES:
            status, loaded, disk, head = build_status(profile, source, daemons_dir)
            lines.append("%s/%s: %s loaded=%s disk=%s head=%s" % (
                profile, source, status, loaded or "?", disk or "?", head or "?"))
    status, loaded, disk, head = watchdog_build_status(daemons_dir)
    lines.append("host/watchdog: %s loaded=%s disk=%s head=%s" % (
        status, loaded or "?", disk or "?", head or "?"))
    # The rows above answer "is the running process the file on disk?".
    # This one answers "is the file on disk what origin has?", which none of
    # them can see: every row can read `current` while the checkout they all
    # share sits behind origin and every merged fix is inert.
    lines.append("host/checkout: %s" % _build.render_upstream(checkout_status(daemons_dir)))
    return lines


def load_episodes():
    try:
        with open(OWN_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_episodes(episodes):
    os.makedirs(os.path.dirname(OWN_STATE), exist_ok=True)
    tmp = OWN_STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(episodes, fh)
    os.replace(tmp, OWN_STATE)


def record_cycle_watermark():
    """Record a completed loop for `just status`; checkout verdicts belong to the checkout layer."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "cycle.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"since": datetime.now(timezone.utc).isoformat()}, fh)
        os.replace(tmp, path)
    except Exception as e:
        note("note: could not record cycle watermark: %s" % e)


def _episode_record(value, legacy_state=None):
    """Normalise one checkout episode without touching unrelated state values.

    Previously the topology episode was just the upstream SHA.  Keeping the
    compatibility here lets an installed watchdog consume that state lazily:
    its first observed standing cycle re-announces immediately (there is no
    honest last-emit time to preserve), while a real ``current`` verdict still
    closes it through the existing ``fleet_caught_up`` path.
    """
    if isinstance(value, dict):
        try:
            last_emit = value.get("last_emit")
            last_emit = None if last_emit is None else float(last_emit)
        except (TypeError, ValueError):
            last_emit = None
        try:
            first = value.get("first")
            first = None if first is None else float(first)
        except (TypeError, ValueError):
            first = None
        return {"state": value.get("state") or legacy_state,
                "upstream": value.get("upstream"),
                "first": first, "last_emit": last_emit}
    if isinstance(value, str):
        return {"state": legacy_state, "upstream": value,
                "first": None, "last_emit": None}
    if isinstance(value, (int, float)):
        # There was no earlier freshness key, but accepting a numeric value
        # makes a partially-written/manual state no reason for the daemon to die.
        return {"state": legacy_state, "upstream": None,
                "first": None, "last_emit": float(value)}
    return None


def _episode_since(first):
    """A stable event token for an episode onset, including legacy unknowns."""
    if first is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(float(first), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def reconcile_checkout(profile, up, episodes, now, renotify_secs):
    """Reconcile the host checkout's topology and stale-fetch-bound episodes.

    Returns ``(event/note pairs, changed)`` and mutates only this profile's two
    episode keys.  Topology and the fetch-age bound are deliberately separate facts:
    ``behind``/``diverged`` is proof against the recorded ref, while a stale
    nominal ``current`` is an honest statement that the comparison cannot
    support an all-clear.  Keeping them separate also preserves the existing
    recovery contract: raw ``current`` closes a topology episode, and when that
    comparison is stale the same cycle opens a stale-bound episode.
    """
    lines = []
    changed = False
    state = (up or {}).get("state")
    topology_key = "upstream:%s/checkout" % profile
    fetch_bound_key = "fetch-bound:%s/checkout" % profile

    # Unknown evidence neither opens nor closes anything.  In particular, it
    # must not erase a previously observed behind/diverged episode.
    if state not in ("current", "behind", "diverged"):
        return lines, changed

    if state in ("behind", "diverged"):
        # An observed gap is stronger than the stale-current warning.  Replace
        # that warning silently with the actionable fact; calling this a
        # "verified" recovery would claim more than FETCH_HEAD can establish.
        if fetch_bound_key in episodes:
            del episodes[fetch_bound_key]
            changed = True

        rec = _episode_record(episodes.get(topology_key))
        same_revision = rec is not None and rec.get("upstream") == up.get("upstream")
        same_state = rec is not None and rec.get("state") in (None, state)
        fact_changed = not (same_revision and same_state)
        due = (rec is None or fact_changed or rec["last_emit"] is None
               or now - rec["last_emit"] >= renotify_secs)
        if due:
            first = now if rec is None else rec.get("first")
            event_type = "fleet_behind" if rec is None or fact_changed else "fleet_behind_still"
            lines.append((
                "type=%s source=checkout since=%s %s" % (
                    event_type, _episode_since(first), _build.render_upstream(up)),
                ("checkout diverged from recorded upstream — %s ahead, %s behind; "
                 "reconcile, then restart what it changed" % (up.get("ahead"), up.get("behind")))
                if state == "diverged" else
                ("checkout behind recorded upstream by %s commit(s) — pull, then restart what it changed"
                 % up.get("behind")),
            ))
            episodes[topology_key] = {
                "state": state, "upstream": up.get("upstream"),
                "first": first, "last_emit": now,
            }
            changed = True
        return lines, changed

    # A raw current verdict keeps the checkout layer's recovery semantics.  The rendering
    # qualifies it as current-unverified when appropriate, and the fetch-bound
    # episode below makes that measurable reason independently wakeable.
    topology = _episode_record(episodes.get(topology_key))
    if topology is not None:
        lines.append((
            "type=fleet_caught_up source=checkout since=%s %s" % (
                _episode_since(topology.get("first")), _build.render_upstream(up)),
            "checkout is no longer behind/diverged relative to recorded upstream (head=%s)"
            % (up.get("head") or "?"),
        ))
        del episodes[topology_key]
        changed = True

    fetch_bound_stale = _build.stale_fetch_bound_exceeded(up)
    fetch_bound = _episode_record(episodes.get(fetch_bound_key), legacy_state="stale")
    if fetch_bound_stale:
        due = (fetch_bound is None or fetch_bound["last_emit"] is None
               or now - fetch_bound["last_emit"] >= renotify_secs)
        if due:
            first = now if fetch_bound is None else fetch_bound.get("first")
            event_type = "fleet_fetch_stale" if fetch_bound is None else "fleet_fetch_stale_still"
            no_fetch_record = up.get("fetched") is None
            reason = "no-fetch-record" if no_fetch_record else "stale-fetch-bound"
            bound_note = (
                "checkout comparison unverified — no usable repository fetch timestamp"
                if no_fetch_record else
                "checkout comparison unverified — no repository fetch is recorded inside the freshness window"
            )
            lines.append((
                "type=%s source=checkout since=%s reason=%s %s" % (
                    event_type, _episode_since(first), reason, _build.render_upstream(up)),
                bound_note,
            ))
            episodes[fetch_bound_key] = {
                "state": "stale", "upstream": up.get("upstream"),
                "first": first, "last_emit": now,
            }
            changed = True
    elif fetch_bound is not None:
        # A recent fetch of *anything* removes the proof of oldness; it does not
        # prove this ref was refreshed.  Name exactly that weaker recovery.
        lines.append((
            "type=fleet_fetch_stale_cleared source=checkout "
            "reason=stale-bound-no-longer-proven %s" % _build.render_upstream(up),
            "repository-wide last-fetch bound is below the freshness window; tracked-ref freshness remains unestablished",
        ))
        del episodes[fetch_bound_key]
        changed = True
    return lines, changed


def main():
    note("watchdog starting (grace %ds) build=%s" % (STARTUP_GRACE_SECS, _build.render(BUILD)))
    record_own_build()
    started = time.time()
    episodes = load_episodes()  # "<profile>/<source>" -> last error emit epoch
    while True:
        try:
            with open(CONFIG_PATH) as fh:
                config = json.load(fh)
        except Exception as e:
            note("note: config unreadable (%s) — sleeping" % e)
            time.sleep(300)
            continue

        wd = config.get("watchdog") or {}
        stale_secs = 60 * int(wd.get("stale_minutes", DEFAULT_STALE_MIN))
        renotify_secs = 60 * int(wd.get("renotify_minutes", DEFAULT_RENOTIFY_MIN))
        labels = loaded_labels()
        now = time.time()
        in_grace = (now - started) < STARTUP_GRACE_SECS

        for profile, pcfg in (config.get("profiles") or {}).items():
            for source in SOURCES:
                status, detail = classify(profile, source, pcfg, labels, stale_secs, now)
                key = "%s/%s" % (profile, source)
                bad = status in ("dead", "stale") or (status == "missing" and not in_grace)
                if bad:
                    last = episodes.get(key, 0)
                    if now - last >= renotify_secs:
                        emit(profile, "error: watchdog: %s %s — %s" % (source, status, detail))
                        note("[%s] %s %s — %s" % (profile, source, status, detail))
                        episodes[key] = now
                        save_episodes(episodes)
                elif key in episodes and status == "ok":
                    emit(profile, "type=watchdog_recovered source=%s" % source)
                    note("[%s] %s recovered" % (profile, source))
                    del episodes[key]
                    save_episodes(episodes)
                # A live daemon on stale bytes is not a liveness
                # failure, so it is a typed event rather than an `error:`,
                # and it fires once per disk revision rather than per cycle.
                if status == "ok":
                    bkey = "build:%s" % key
                    bstatus, loaded, disk, head = build_status(profile, source)
                    if bstatus == "outdated" and episodes.get(bkey) != disk:
                        emit(profile, "type=fleet_outdated source=%s loaded=%s disk=%s head=%s"
                             % (source, loaded, disk, head or "?"))
                        note("[%s] %s outdated: running %s, disk is %s — restart it" % (profile, source, loaded, disk))
                        episodes[bkey] = disk
                        save_episodes(episodes)
                    elif bstatus == "current" and bkey in episodes:
                        emit(profile, "type=fleet_current source=%s src=%s" % (source, loaded))
                        note("[%s] %s current again (%s)" % (profile, source, loaded))
                        del episodes[bkey]
                        save_episodes(episodes)
            # The watchdog checks ITSELF from memory, not from a
            # state file: BUILD is the hash of the bytes this process
            # imported, so a merge that changed this file shows up here on
            # the next cycle even though the process never restarted.
            self_status, self_disk = _build.compare(BUILD, os.path.join(DAEMONS_DIR, "watchdog_wake.py"))
            skey = "build:%s/watchdog" % profile
            if self_status == "outdated" and episodes.get(skey) != self_disk:
                emit(profile, "type=fleet_outdated source=watchdog loaded=%s disk=%s head=%s"
                     % (BUILD.get("src"), self_disk, BUILD.get("head") or "?"))
                note("[%s] watchdog outdated: running %s, disk is %s — kickstart com.watchtower.watchdog"
                     % (profile, BUILD.get("src"), self_disk))
                episodes[skey] = self_disk
                save_episodes(episodes)
            # The arm that closes the episode above, as the
            # per-source arm closes its own.  Hosts that still carry a stale
            # self key get one catch-up fleet_current per profile on the
            # first cycle after this lands.
            elif self_status == "current" and skey in episodes:
                emit(profile, "type=fleet_current source=watchdog src=%s" % BUILD.get("src"))
                note("[%s] watchdog current again (%s)" % (profile, BUILD.get("src")))
                del episodes[skey]
                save_episodes(episodes)
            # The third layer.  It remains fetch-free, but an
            # old clean comparison now says so as an event, and an observed
            # standing gap re-announces on the watchdog's existing cadence.
            up = checkout_status()
            checkout_lines, checkout_changed = reconcile_checkout(
                profile, up, episodes, now, renotify_secs)
            for event_line, note_line in checkout_lines:
                emit(profile, event_line)
                note("[%s] %s" % (profile, note_line))
            if checkout_changed:
                save_episodes(episodes)
        record_cycle_watermark()
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    import sys

    if "--fleet" in sys.argv[1:]:
        with open(CONFIG_PATH) as fh:
            for line in fleet_report(json.load(fh)):
                print(line)
    else:
        main()
