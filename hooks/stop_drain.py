#!/usr/bin/env python3
"""Turn-boundary drain for delivered-but-unactioned wakes.

The daemons in ``daemons/`` prove an event was OBSERVED: each appends a
line to ``~/Library/Logs/watchtower/<profile>-<source>.log`` and moves a
watermark under ``~/.local/state/watchtower/<source>/<profile>.json``.
Nothing anywhere proves an event was ACTED ON. That gap is not
hypothetical: on 2026-08-20 a check-in wake was written 27s after send,
delivered to a live session that was mid-send on another task, and never
returned to — surfacing ~15h later at the daily digest, which re-reads
the inbox from source rather than trusting what the session remembers.
A daemon-liveness watchdog structurally cannot see this; the watcher was
never dark.

So this is the accounting the watchdog cannot do, taken to its limit:
reconcile at EVERY turn boundary. Claude Code's ``Stop`` hook fires when
a session tries to end its turn and may refuse, re-injecting text the
session must then handle. This module is that hook. It diffs each log's
event lines against a second, session-owned cursor — ``acted_through``,
living beside the daemon watermarks at
``~/.local/state/watchtower/<profile>/acted.json`` — and blocks turn-end
with whatever the session was handed and never handled.

The log is the queue. No new daemon, no new format, no new transport.

**Two cursors, and why the second one exists.** ``acted_through`` moves
only when the session says it acted (``wt_acted.py`` / ``just acted``).
``surfaced_through`` moves when THIS hook shows an item. They are not the
same number, and collapsing them would break the mechanism in one of two
directions:

- Block on ``acted_through`` alone and an item the session cannot act on
  wedges the session forever — every turn-end re-blocks on it. (Claude
  Code passes ``stop_hook_active`` as a second, coarser guard against
  exactly this; we honour it too, but it only covers the immediate
  re-entry, not the next turn.)
- Report on ``surfaced_through`` and the mechanism launders the very gap
  it exists to expose: showing someone an item is not the same as that
  item being handled, and a tool that treats those as equal is worse
  than no tool, because it reports zero while the gap is open.

Hence: an event blocks turn-end exactly ONCE, and stays listed as
unactioned (``just unactioned <profile>``) until the session advances the
real cursor.

**The cursor has an OWNER (a design decision).** The
first version keyed the cursor by profile alone and recorded no actor, so
every process that ran this hook or ``wt_acted.py`` with ``--profile X``
WAS the session as far as the file knew: on 2026-09-08 sessions from an
unrelated project (the hook was then installed globally) surfaced this
seat's events — consuming the one-shot block — and one of them ran
``just acted`` over events nobody here had handled. Nothing said who.
The charter says one session per profile; this enforces it. The cursor
file carries ``owner`` = the session (``CLAUDE_CODE_SESSION_ID`` /
``CLAUDE_PID``, which the harness puts in every child process's
environment; the hook payload's ``session_id`` is honoured first). The
owner drains as before. A session that is NOT the owner is **silent and
non-mutating** here — it never wedges a session that isn't the seat and
never touches the seat's cursor — but registers itself in a SEPARATE
``contenders.json`` so ``just unactioned`` can say a second session is
running this profile's hook. Ownership is lost only when the owner's PID
is dead, or by an explicit ``just takeover`` — never by a timer: an idle
session is not stale, a crashed one is. The operator (no session
identity) may act and is attributed as such. A hook run by hand — from
a terminal OR from a session's Bash tool, which inherits the session's
identity — reports without consuming the surfacing: a real turn-end is
recognised by the harness's own ``hook_event_name: "Stop"`` in the
payload, and nothing else may eat the seat's one-shot block. An operator
``just takeover`` RELEASES the cursor (owner cleared, nothing claimed) so
the next session takes it. Ownership check, claim and advance are ONE
transaction under a per-profile ``flock`` (codex on this branch: two
sessions can otherwise both read "claimable", both claim, and both
consume the same events; atomic replace protects each write, not the
read-modify-write). A hook that finds the lock held stands down for
that turn — the events resurface next turn — and the CLI says so and
changes nothing. A resumed session keeps its id but gets a new pid; the
owner path re-records it, or the stale dead pid would let the next
arrival take a live seat.
An ``acted.json`` that exists but cannot be read
is NOT an unowned cursor: both entry points stand down on it (the hook
silent, the CLI naming the file), and a CLI write that did not happen is
reported as such rather than as success. "Dead" means the PROCESS the
recorded pid named is gone: the owner records the boot identity and the
process start time with the pid, so a reboot or a recycled pid reads as
dead rather than as a live foreign owner; a pid-less resume whose recorded
process is dead records unknown liveness rather than the corpse's pid.
Liveness is decided boot-first: an owner recorded under another boot is
dead whether or not it recorded a pid, and the owner's own hook run
re-records its boot — so a reboot frees a pid-less seat, while the session
that resumes across one keeps it.

**Failure posture: silence, never a wedge.** This hook runs on every
turn-end of any session configured to use it. A crash here — unreadable
cursor, vanished log dir, malformed JSON on stdin — would break the
session's ability to finish a turn at all. Every path therefore degrades
to "emit nothing, let the turn end". A drain that misses an event is a
bug; a drain that bricks the seat is an outage.
"""
import argparse
import collections
import contextlib
import errno
import fcntl
import glob
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_LOG_DIR = os.path.expanduser("~/Library/Logs/watchtower")
DEFAULT_STATE_ROOT = os.path.expanduser("~/.local/state/watchtower")
CURSOR_NAME = "acted.json"
CONTENDERS_NAME = "contenders.json"
LOCK_NAME = ".lock"
LOCK_WAIT = 2.0  # seconds to wait for the profile lock; the hook's own timeout is 10
CURSOR_VERSION = 2
# acted.json's record of what the last `just unactioned` printed, and to whom — the
# CLI's alone: the hook never reads it, and its saves carry it through untouched,
# since every writer saves the whole cursor (see `unseen`).
LISTED_KEY = "listed"
LINE_CAP = 160
_FINGERPRINT_CAP = 120
LIST_CAP = 20

# One line per event, exactly what a session's Monitor greps (`just arm`):
# `^type=` for activity, `^error:` for a watcher reporting itself sick.
EVENT_PREFIXES = ("type=",)
ERROR_PREFIXES = ("error:",)

Event = collections.namedtuple("Event", "index kind text")


# --------------------------------------------------------------------------
# reading the queue (the logs)
# --------------------------------------------------------------------------

def parse_events(text):
    """Return the event lines of a log, 1-indexed in file order.

    The index is a position in the EVENT sequence, not a raw line number,
    so daemon chatter interleaved into the log cannot shift a cursor.
    """
    events = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(EVENT_PREFIXES):
            kind = "event"
        elif line.startswith(ERROR_PREFIXES):
            kind = "error"
        else:
            continue
        events.append(Event(index=len(events) + 1, kind=kind, text=line))
    return events


def discover_sources(log_dir, profile):
    """Sources this profile has actually written, newest-agnostic.

    Globbing the logs (rather than reading config) keeps the drain honest
    about what exists on disk; a source configured but never written has
    nothing to drain, and a source written but since removed from config
    still holds events someone owes an answer to.
    """
    found = []
    pattern = os.path.join(log_dir, "%s-*.log" % profile)
    for path in sorted(glob.glob(pattern)):
        source = os.path.basename(path)[len(profile) + 1:-len(".log")]
        if source and "daemon-" not in source:
            found.append(source)
    return found


def read_source(log_dir, profile, source):
    """Return ``(events, size_bytes)`` for one source log; missing → ``([], 0)``."""
    path = os.path.join(log_dir, "%s-%s.log" % (profile, source))
    try:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
        return parse_events(text), os.path.getsize(path)
    except OSError:
        return [], 0


# --------------------------------------------------------------------------
# the cursor
# --------------------------------------------------------------------------

def cursor_path(state_root, profile):
    return os.path.join(state_root, profile, CURSOR_NAME)


def load_cursor(state_root, profile):
    """Read the cursor; anything unreadable reads as empty.

    A corrupt cursor must not raise: the cost of a reset is re-surfacing
    events (noisy, safe), and the cost of raising is a session that
    cannot end a turn (an outage).
    """
    try:
        with open(cursor_path(state_root, profile)) as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("sources"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": CURSOR_VERSION, "sources": {}}


def save_cursor(state_root, profile, cursor):
    """Atomically replace the cursor file (crash-safe: temp + rename).

    Returns True when written, False when it could not be. The hook ignores
    the result (never wedge a turn over bookkeeping); the CLI checks it —
    codex pass 3 on this branch: a take-over must not report success for a
    write that never happened.
    """
    path = cursor_path(state_root, profile)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cursor, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        return False  # see module docstring: never wedge the turn over bookkeeping


def read_cursor(state_root, profile):
    """``(cursor, state)`` with ``state`` in ``missing`` | ``ok`` | ``unreadable``.

    codex pass 3 on this branch: :func:`load_cursor` launders a read failure
    into an EMPTY cursor — right for the drain's positions (they resurface:
    noisy, safe) and wrong for ownership, where an unreadable ``acted.json``
    made a live owner's seat look claimable and a foreign hook could replace
    it. Ownership decisions read through this and STAND DOWN on
    ``unreadable``; the recovery is the operator removing or repairing the
    file, then ``just takeover`` — which is what the old "reset" amounted to,
    made explicit.
    """
    path = cursor_path(state_root, profile)
    empty = {"version": CURSOR_VERSION, "sources": {}}
    if not os.path.exists(path):
        return empty, "missing"
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty, "unreadable"
    if isinstance(data, dict) and isinstance(data.get("sources"), dict):
        return data, "ok"
    return empty, "unreadable"


def _entry(cursor, source):
    got = cursor.get("sources", {}).get(source)
    if not isinstance(got, dict):
        return {"acted_through": 0, "surfaced_through": 0, "bytes": 0, "first": None, "by": None}
    return {
        "acted_through": int(got.get("acted_through") or 0),
        "surfaced_through": int(got.get("surfaced_through") or 0),
        "bytes": int(got.get("bytes") or 0),
        "first": got.get("first"),
        "by": got.get("by") if isinstance(got.get("by"), dict) else None,
    }


def _fingerprint(events):
    """Identity of the file's FIRST event — changes iff the file was replaced.

    Byte size alone cannot detect rotation: a log rotated and then grown
    past its old size before the next drain looks like ordinary growth,
    and the cursor stays parked over the new file's early events. Those
    events are then never surfaced — the exact silent drop this module
    exists to prevent (found in review, 2026-08-22).
    """
    return events[0].text[:_FINGERPRINT_CAP] if events else None


def _effective(entry, size, first=None):
    """Cursor adjusted for rotation.

    A log that SHRANK was rotated or truncated, so its old positions name
    different events now. Keeping the stale numbers would park the cursor
    past the end and silently hide everything the new file holds — the
    exact silent-drop this layer exists to prevent — so a shrink resets
    both cursors to zero and the events resurface.

    Shrink is necessary but NOT sufficient: rotate-then-outgrow never
    shrinks as observed, so the first event's identity is compared too.
    A cursor written before fingerprints existed has no ``first`` and
    falls back to size-only rather than resurfacing everything once on
    upgrade.
    """
    if size < entry["bytes"]:
        return 0, 0
    stored = entry.get("first")
    if stored and first is not None and stored != first:
        return 0, 0
    return entry["acted_through"], entry["surfaced_through"]


def _pending(log_dir, state_root, profile, floor, cursor=None):
    """Sources → events past ``floor(acted, surfaced)``. Empty sources omitted.

    ``floor`` is the whole difference between blocking and reporting: the
    block path floors at max(acted, surfaced) so an item blocks once; the
    report path floors at acted alone so surfacing never counts as doing.
    """
    cursor = load_cursor(state_root, profile) if cursor is None else cursor
    out = {}
    for source in discover_sources(log_dir, profile):
        events, size = read_source(log_dir, profile, source)
        acted, surfaced = _effective(_entry(cursor, source), size, _fingerprint(events))
        mark = floor(acted, surfaced)
        fresh = [e for e in events if e.index > mark]
        if fresh:
            out[source] = fresh
    return out


def collect_unactioned(log_dir, state_root, profile, cursor=None):
    """Events that should BLOCK turn-end (not yet acted on, not yet surfaced)."""
    return _pending(log_dir, state_root, profile, max, cursor)


def report_unactioned(log_dir, state_root, profile):
    """Events not yet ACTED on, whether or not they have been surfaced."""
    return _pending(log_dir, state_root, profile, lambda acted, surfaced: acted)


# --------------------------------------------------------------------------
# what a listing showed (the blind blanket clear)
# --------------------------------------------------------------------------

View = collections.namedtuple("View", "events acted size first")


def read_view(log_dir, profile, cursor, source=None):
    """``{source: View(events, acted, size, first)}`` from ONE read of each log.

    The CLI's view of the queue for one decision — what ``just unactioned``
    prints and records, what a blanket ``just acted`` checks, clears and then
    names — so none of those can disagree about an event that landed in
    between. ``acted`` is rotation-adjusted exactly as the drain adjusts it;
    ``size`` and ``first`` are the file's identity at this read.
    """
    out = {}
    for name in ([source] if source else discover_sources(log_dir, profile)):
        events, size = read_source(log_dir, profile, name)
        first = _fingerprint(events)
        acted, _surfaced = _effective(_entry(cursor, name), size, first)
        out[name] = View(events, acted, size, first)
    return out


def same_generation(snap, size, first):
    """Is a read of ``size`` bytes, whose first event is ``first``, of the same log FILE
    that ``snap`` read — grown or not? ``_effective``'s rotation test, asked of a snapshot
    (codex on f58b31c: positions read from one file must never land on its replacement).
    """
    if size < snap.size:
        return False
    return not (snap.first and first is not None and snap.first != first)


def record_listing(cursor, actor, view, now=None):
    """Record what a ``just unactioned`` listing printed, and to whom. Caller saves, under the lock.

    Per source, ``through`` is the tail at that read: a listing prints every
    open event, uncapped, so everything up to the tail was either handled or
    shown. The file's identity rides along (``bytes``, ``first``) so a rotated
    log voids the record rather than lending its positions to a new file's
    events. Only the LAST listing is kept, and no position moves.
    """
    cursor[LISTED_KEY] = {
        "by": dict(actor),
        "at": now or _stamp(),
        "sources": {name: {"through": len(v.events), "bytes": v.size, "first": v.first}
                    for name, v in view.items()},
    }
    return cursor


def listing_for(cursor, actor):
    """The listing record that counts for ``actor``, or ``{}`` — whose listing counts is ``unseen``'s rule."""
    rec = cursor.get(LISTED_KEY)
    if not isinstance(rec, dict):
        return {}
    by = rec.get("by") if isinstance(rec.get("by"), dict) else {}
    if actor.get("kind") == "session" and by.get("session_id") != actor.get("session_id"):
        return {}
    return rec


def unseen(cursor, actor, view):
    """Open events in ``view`` that no listing showed ``actor`` — what a blanket act must not clear.

    Follow-up: a blanket ``just acted``
    cleared through the CURRENT tail and printed a count, so an event landing
    mid-turn — after the session last looked — was swept unread.

    Whose listing counts: a SESSION's own, matched on ``CLAUDE_CODE_SESSION_ID``,
    which the harness puts in the environment of the session's Bash tool (see
    ``actor_identity``); another session's listing is not this session's
    reading. The OPERATOR has no such identity — ``user@tty`` is not one two
    invocations reliably share (``notty`` covers every non-interactive caller)
    — so for the operator the fallback is the last listing by ANYONE.

    What counts as shown: a ``just unactioned`` listing, never the Stop hook's
    block. The block prints at most ``LIST_CAP`` events and folds the rest into
    "… and N more", yet marks every one of them surfaced — so
    ``surfaced_through`` is not "seen".

    Limits. A recorded listing proves the bytes left
    the process, not that anyone read them: piped through ``head``, ``tail`` or
    ``grep``, or cut short by a tool's output cap, it records events the reader
    never saw — hence the docs' "never pipe or chain the listing", and the
    listing's closing count. And a subagent shares its parent's
    ``CLAUDE_CODE_SESSION_ID``, so a listing a subagent printed counts as the
    parent's reading, though the parent may see only a summary.
    """
    listed = listing_for(cursor, actor).get("sources")
    listed = listed if isinstance(listed, dict) else {}
    out = {}
    for name, v in view.items():
        mark = max(v.acted, _listed_through(listed.get(name), v))
        fresh = [e for e in v.events if e.index > mark]
        if fresh:
            out[name] = fresh
    return out


def _listed_through(src, view):
    """How far a listing's record of one source reaches into the file ``view`` read; 0 if it does not apply."""
    if not isinstance(src, dict):
        return 0
    try:
        entry = {"acted_through": int(src.get("through") or 0), "surfaced_through": 0,
                 "bytes": int(src.get("bytes") or 0), "first": src.get("first")}
    except (TypeError, ValueError):
        return 0  # a malformed record reads as "nothing shown": refuse, never sweep
    return _effective(entry, view.size, view.first)[0]


# --------------------------------------------------------------------------
# advancing the cursors
# --------------------------------------------------------------------------

def _advance(log_dir, state_root, profile, field, source=None, through=None, by=None,
             strict=False, cursor=None):
    # codex pass 4 on this branch: an entry point that already holds the cursor
    # — with a claim or a pid refresh applied IN MEMORY — passes it in, so
    # ownership and positions are written in ONE save. Reloading here would
    # drop that ownership if its own write had been lost, and persist consumed
    # positions under a missing or stale owner.
    cursor = load_cursor(state_root, profile) if cursor is None else cursor
    cursor["version"] = CURSOR_VERSION
    sources = cursor.setdefault("sources", {})
    targets = [source] if source else discover_sources(log_dir, profile)
    for name in targets:
        events, size = read_source(log_dir, profile, name)
        first = _fingerprint(events)
        entry = _entry(cursor, name)
        acted, surfaced = _effective(entry, size, first)
        entry["acted_through"], entry["surfaced_through"] = acted, surfaced
        # Clamp to the real tail: an overshooting `through` would park the
        # cursor in the FUTURE, silently pre-marking events that have not
        # arrived yet as handled — a typo becoming a silent drop. A mapping
        # pins each source to a SNAPSHOT the caller read (``read_view``): it
        # moves to that snapshot's tail, and only if THIS read is the same
        # generation of the log — a log replaced in between would be handed
        # positions that name other events (codex on f58b31c). A source the
        # mapping omits, or whose log was replaced, does not move.
        if isinstance(through, dict):
            snap = through.get(name)
            want = len(snap.events) if snap is not None and same_generation(snap, size, first) else 0
        else:
            want = through
        target = len(events) if want is None else min(int(want), len(events))
        before = entry[field]
        entry[field] = max(entry[field], target)
        # Attribution: WHO moved this cursor, and which of the two it was.
        # Stamped only on actual movement so a no-op call never overwrites
        # the record of the actor that really advanced it.
        if by is not None and entry[field] > before:
            entry["by"] = dict(by, field=field, at=_stamp())
        # Acting on an event implies it was surfaced; keeps the pair sane
        # if a session records an action the hook never showed it.
        if field == "acted_through":
            entry["surfaced_through"] = max(entry["surfaced_through"], entry[field])
        entry["bytes"] = size
        entry["first"] = first
        sources[name] = entry
    if not save_cursor(state_root, profile, cursor) and strict:
        raise CursorWriteFailed(cursor_path(state_root, profile))
    return cursor


def mark_acted(log_dir, state_root, profile, source=None, through=None, by=None, strict=False,
               cursor=None):
    """Record that the session HANDLED events through ``through`` (default: tail).

    ``through`` may be a ``{source: View}`` mapping of snapshots (see ``_advance``).
    ``strict=True`` raises :class:`CursorWriteFailed` when the cursor could not
    be written, for callers that report to a human. ``cursor`` is the caller's
    in-memory cursor, written together with the positions (see ``_advance``).
    """
    return _advance(log_dir, state_root, profile, "acted_through", source, through, by, strict,
                    cursor)


def mark_surfaced(log_dir, state_root, profile, source=None, through=None, by=None, cursor=None):
    """Record that the hook SHOWED events through ``through`` (default: tail)."""
    return _advance(log_dir, state_root, profile, "surfaced_through", source, through, by, False,
                    cursor)


# --------------------------------------------------------------------------
# who is acting, and who owns the cursor
# --------------------------------------------------------------------------

def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def actor_identity(payload=None, environ=None):
    """The session (id + pid) this process belongs to, or the operator.

    The harness puts ``CLAUDE_CODE_SESSION_ID`` and ``CLAUDE_PID`` into the
    environment of every child it spawns — hook and Bash alike (measured
    2026-09-09: the Bash value equals the session's own id). The hook's
    stdin payload also carries ``session_id`` and is honoured first. No
    session identity means a human at a prompt: the operator.
    """
    env = os.environ if environ is None else environ
    sid = (payload or {}).get("session_id") or env.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        pid = None
        try:
            pid = int(env.get("CLAUDE_PID") or 0) or None
        except (TypeError, ValueError):
            pid = None
        return {"kind": "session", "session_id": str(sid), "pid": pid, "cwd": os.getcwd()}
    user = env.get("USER") or env.get("LOGNAME") or "?"
    try:
        tty = os.ttyname(sys.stdin.fileno()) if sys.stdin.isatty() else "notty"
    except (OSError, ValueError, AttributeError):
        tty = "notty"
    return {"kind": "operator", "user": user, "tty": tty, "cwd": os.getcwd()}


def pid_alive(pid):
    """True / False when knowable; None when there is no pid to ask about.

    ``None`` is treated as ALIVE by the caller: a session that failed to
    record its pid must not have its cursor taken by the next arrival. Only
    a positive "that process is gone" frees a lease — and a human can
    always ``just takeover``.
    """
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return None
    return True


_BOOT_CACHE = {}


def boot_identity():
    """An identity for THIS boot of the host, cached per process.

    codex pass 5 on this branch: after a reboot a dead owner's stored pid can
    name an unrelated LIVE process, which read as "owner alive" — replacement
    sessions' hooks then stood down silently and ``just acted`` refused until
    someone noticed. A pid recorded under a different boot cannot be alive.
    Linux: ``/proc/sys/kernel/random/boot_id``; macOS: ``sysctl kern.bootsessionuuid``,
    fixed for the life of a boot. ``kern.boottime`` is the LAST resort only —
    codex pass 8 on this branch: it is calendar-derived, and XNU moves it when
    the clock is stepped (osfmk/kern/clock.c), so with it as the identity a
    stepped clock read as a reboot and a live owner as dead before its pid or
    start time were even consulted.
    """
    if "v" in _BOOT_CACHE:
        return _BOOT_CACHE["v"]
    ident = _linux_boot_id()
    if ident is None:
        ident = _sysctl("kern.bootsessionuuid")
        if ident is not None and not _UUID.match(ident):
            ident = None  # a positive marker unique to the real path, or nothing
    if ident is None:
        m = re.search(r"sec\s*=\s*(\d+)", _sysctl("kern.boottime") or "")
        ident = ("boottime:" + m.group(1)) if m else None
    _BOOT_CACHE["v"] = ident
    return ident


_UUID = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")


def _boot_kind(ident):
    return "boottime" if str(ident).startswith("boottime:") else "id"


def boot_differs(stored, current):
    """Two boot identities are DIFFERENT only when both are known, of the same
    kind, and unequal.

    codex pass 9 on this branch: the fallback chain means one boot can answer
    as a session uuid in one process and as ``boottime:…`` in another (the
    uuid probe failing transiently); comparing those strings read the same
    boot as a reboot and a live owner as dead. A change of KIND is unknown —
    unknown holds the lease — and the owner's own next hook run re-records
    whichever kind the instrument gives. This also makes the rollout of the
    uuid identity read as unknown rather than as a reboot.
    """
    if not stored or not current:
        return False
    if _boot_kind(stored) != _boot_kind(current):
        return False
    return stored != current


def _linux_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _sysctl(name):
    """``sysctl -n NAME`` stripped, or ``None`` — never raises, never a wrong answer."""
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                             timeout=2).stdout.strip()
        return out or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_start(pid):
    """When process ``pid`` started, as ``ps`` prints it; ``None`` when unknowable.

    A pid recycled to another process within one boot has a different start
    time (codex pass 5). Second resolution: a reuse within the same second is
    the residual, and ``just takeover`` still exists for it.
    """
    if not pid:
        return None
    try:
        # codex pass 6: `lstart` is printed in LOCAL time and locale. Two sessions
        # with different TZ — or one host whose zone changed — would disagree
        # about when the same process started, and disagreement reads as
        # "dead". The instrument pins its own environment so the caller's
        # cannot change the answer.
        env = dict(os.environ, TZ="UTC", LC_ALL="C", LANG="C")
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="], capture_output=True,
                             text=True, timeout=2, env=env).stdout.strip()
        return out or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_identity(pid):
    return {"boot": boot_identity(), "start": process_start(pid) if pid else None}


def owner_alive(owner):
    """Is the OWNER's process alive — not merely "is some process wearing that pid".

    ``True`` / ``False`` when knowable, ``None`` when unknown (unknown holds the
    lease; only a positive "gone" frees it). Dead means: it was recorded under
    another boot — pid or no pid, no process survives a reboot (codex pass 7) —
    OR the pid is gone, OR the process now wearing the pid started at a
    different time than the one recorded. Looked up through the module so
    tests and a future backend can replace the three instruments.
    """
    if not isinstance(owner, dict):
        return None
    if boot_differs(owner.get("boot"), boot_identity()):
        return False
    pid = owner.get("pid")
    if not pid:
        return None
    state = pid_alive(pid)
    if state is False:
        return False
    stored_start, current_start = owner.get("start"), process_start(pid)
    if stored_start and current_start and stored_start != current_start:
        return False
    return state


def ownership(cursor, actor, alive=None):
    """How ``actor`` stands to the cursor's owner.

    ``owner``     — this session owns it.
    ``claimable`` — nobody does, or the owner's process is dead.
    ``foreign``   — another session owns it and is (as far as we can tell) alive.
    ``operator``  — no session identity: a human, who may act but never owns.
    """
    alive = alive or owner_alive  # looked up at CALL time, so a test (or a future
    # liveness backend) can replace it; a definition-time default would not.
    if actor.get("kind") != "session":
        return "operator"
    owner = cursor.get("owner")
    if not isinstance(owner, dict) or not owner.get("session_id"):
        return "claimable"
    if owner.get("session_id") == actor.get("session_id"):
        return "owner"
    if alive(owner) is False:
        return "claimable"
    return "foreign"


def claim(cursor, actor, now=None):
    """Make ``actor`` the owner. Caller saves."""
    now = now or _stamp()
    ident = process_identity(actor.get("pid"))
    cursor["owner"] = {
        "session_id": actor.get("session_id"),
        "pid": actor.get("pid"),
        "boot": ident["boot"],
        "start": ident["start"],
        "cwd": actor.get("cwd"),
        "since": now,
        "last_active": now,
    }
    # the previous owner may have been SHOWN events it
    # never acted on, and the block floor is max(acted, surfaced) — so those
    # would sit past the new owner's floor and never block again, while
    # `just unactioned` still listed them. A new owner starts from what was
    # HANDLED, not from what was shown: this moves nothing that records
    # handling and can only re-surface, which the module already calls
    # noisy-safe.
    for entry in (cursor.get("sources") or {}).values():
        if isinstance(entry, dict):
            acted = int(entry.get("acted_through") or 0)
            if int(entry.get("surfaced_through") or 0) > acted:
                entry["surfaced_through"] = acted
    return cursor


def refresh_owner(cursor, actor):
    """The owner's own hook run is proof of life: bring its recorded identity up to date.

    A resumed session keeps its id but gets a new pid (and maybe cwd) — codex P2
    on this branch: without this the stored pid goes stale, reads as dead, and
    the next arrival takes a LIVE seat. Returns True when something changed (the
    caller saves). A missing actor pid never erases a known, live one.
    """
    owner = cursor.get("owner")
    if not isinstance(owner, dict) or owner.get("session_id") != actor.get("session_id"):
        return False
    changed = False
    ident = process_identity(actor.get("pid"))
    if actor.get("pid"):
        boot_changed = boot_differs(owner.get("boot"), ident["boot"])
        # codex pass 8: a changed START is a different process too. A same-pid
        # resume across a reboot while the boot instrument is silent otherwise
        # kept the old boot on record beside the new start, and the live owner
        # read as dead the moment the instrument recovered.
        start_changed = bool(ident["start"] and owner.get("start")
                             and ident["start"] != owner.get("start"))
        if owner.get("pid") != actor.get("pid") or boot_changed or start_changed:
            # A different process — a new pid, or the same numeric pid on the far
            # side of a reboot (codex pass 7): the old identity is meaningless,
            # take the new one wholesale. Unknown parts stay unknown rather than
            # inheriting the previous process's values — a stale start would read
            # as dead the moment the instrument recovered.
            owner["pid"], owner["boot"], owner["start"] = actor["pid"], ident["boot"], ident["start"]
            changed = True
        else:
            # codex pass 6: the same process (same pid, same boot) — refresh what
            # the instruments say now, but a transiently silent instrument (None)
            # must not erase a known value.
            for key in ("boot", "start"):
                if ident[key] and ident[key] != owner.get(key):
                    owner[key] = ident[key]
                    changed = True
    else:
        # A pid-less resume: this session is alive — it is running this hook —
        # but its process is unknown.
        if owner.get("pid") and owner_alive(owner) is False:
            # codex pass 5: never keep a DEAD pid on record — the next arrival
            # would read the seat as claimable while this session is live.
            # Unknown liveness holds the lease; only take-over frees it.
            owner["pid"], owner["boot"], owner["start"] = None, None, None
            changed = True
        # codex pass 7: the recorded boot must be THIS boot, or the next arrival
        # reads a live seat as one that died in a reboot. With no pid on record
        # the boot is the only identity there is, so an unknown boot is recorded
        # as unknown rather than left stale: unknown holds the lease, stale frees it.
        if ident["boot"]:
            boot = ident["boot"]
        elif owner.get("pid"):
            boot = owner.get("boot")          # a live pid attests; keep the known boot
        else:
            boot = None
        if owner.get("boot") != boot:
            owner["boot"] = boot
            changed = True
    if actor.get("cwd") and owner.get("cwd") != actor.get("cwd"):
        owner["cwd"] = actor["cwd"]
        changed = True
    if changed:
        owner["last_active"] = _stamp()
    return changed


class ProfileBusy(Exception):
    """Another actor holds the profile lock: stand down (hook) or say so (CLI)."""


class CursorIndeterminate(Exception):
    """``acted.json`` exists but cannot be read: ownership cannot be decided, so nothing may change."""


class CursorWriteFailed(Exception):
    """A cursor write the caller needed to see through did not happen."""


def lock_path(state_root, profile):
    return os.path.join(state_root, profile, LOCK_NAME)


@contextlib.contextmanager
def profile_lock(state_root, profile, wait=None):
    """Serialise ownership + cursor updates per profile, across processes.

    ``flock`` on ``<state>/<profile>/.lock``, held across load → ownership →
    claim → advance → save at the ENTRY POINTS only (``drain``, the CLI's act
    and take-over). The inner helpers stay lock-free so they cannot
    self-deadlock — ``flock`` is per open file description, so a second fd in
    the same process contends. Contention means another actor is mid-
    operation: raise :class:`ProfileBusy` after ``wait`` seconds and let the
    caller stand down. A lock file that cannot even be OPENED also stands
    down (codex pass 2 on this branch): the first version yielded unlocked
    on that path, reasoning the save would fail too — but ``.lock`` can be
    unopenable (wrong owner, a directory in its place) while ``acted.json``
    is still replaceable, which is exactly the unprotected transaction the
    lock exists to prevent. Neither path wedges: the hook stays silent, the
    CLI says which it was.
    """
    wait = LOCK_WAIT if wait is None else wait
    path = lock_path(state_root, profile)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ProfileBusy("cannot open the lock file %s (%s)" % (path, exc.__class__.__name__))
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ProfileBusy("busy — another session's hook or act holds the lock %s" % path)
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def contenders_path(state_root, profile):
    return os.path.join(state_root, profile, CONTENDERS_NAME)


def load_contenders(state_root, profile):
    """``(contenders, state)`` — state is ``missing`` / ``ok`` / ``unreadable``.

    an unreadable file used to read as "nobody else
    is here", and the next writer rebuilt it from nothing — the laundering
    pass 3 rejected for ``acted.json``. Advisory-only, but the CLI half of
    "never lie to a human" applies: unreadable is reported, not emptied.
    """
    path = contenders_path(state_root, profile)
    if not os.path.exists(path):
        return {}, "missing"
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}, "unreadable"
    if not isinstance(data, dict):
        return {}, "unreadable"
    return data, "ok"


def note_contender(state_root, profile, actor):
    """A foreign session's hook records itself here — the ONLY thing it writes.

    A separate file, so a non-owner can never race the owner's cursor
    writes: a lost update on ``acted.json`` could only ever lower a cursor
    (noisy, safe), but "never touches the seat's cursor" should be true
    by construction, not by luck.
    """
    try:
        data, state = load_contenders(state_root, profile)
        if state == "unreadable":
            return  # never rebuild from nothing over a file that cannot be read
        sid = actor.get("session_id") or "?"
        prev = data.get(sid) if isinstance(data.get(sid), dict) else {}
        ident = process_identity(actor.get("pid"))
        data[sid] = {
            "pid": actor.get("pid"),
            "boot": ident["boot"],     # codex pass 6: contenders carry the same
            "start": ident["start"],   # identity as owners, so a recycled pid is "gone"
            "cwd": actor.get("cwd"),
            "first": prev.get("first") or _stamp(),
            "last": _stamp(),
            "count": int(prev.get("count") or 0) + 1,
        }
        path = contenders_path(state_root, profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# the hook itself
# --------------------------------------------------------------------------

def format_reason(pending, profile):
    total = sum(len(v) for v in pending.values())
    lines = [
        "Watchtower drain: %d delivered event(s) reached this session and were "
        "never acted on (profile=%s)." % (total, profile),
        "",
    ]
    shown = 0
    for source in sorted(pending):
        for event in pending[source]:
            if shown >= LIST_CAP:
                break
            text = event.text
            if len(text) > LINE_CAP:
                text = text[:LINE_CAP - 1] + "…"
            lines.append("  [%s #%d] %s" % (source, event.index, text))
            shown += 1
    if total > shown:
        lines.append("  … and %d more" % (total - shown))
    lines += [
        "",
        "Handle each one now. If an item genuinely needs no action, say so and",
        "record it — do not just re-end the turn:",
        # This block is not a listing: the CLI refuses a blanket
        # `just acted` for events only shown here, so point at the listing first,
        # and as a command of its own: chained onto
        # the listing with `&&`, a blanket clear passes the gate for an event that
        # landed mid-turn before anyone read it. No by-number literal either (note
        # 6): this block shows only what is newly surfaced, so "through #n" could
        # sweep an older open event it never showed. The listing shows every open
        # event and ends with the by-number clears for exactly those. No
        # backticks on this line: it is shaped
        # to be copied whole, and an interactive zsh, where `#` starts no
        # comment by default, would run a backticked `just acted` as a command
        # substitution, a blanket clear with its output swallowed.
        "  just unactioned %s   # read and handle each one; THEN, as a separate command, "
        "just acted %s" % (profile, profile),
        "  (the listing ends with the by-number clears for exactly what it showed)",
        "",
        "These are surfaced ONCE and will not block turn-end again, but they stay",
        "listed by `just unactioned %s` until the cursor advances." % profile,
    ]
    return "\n".join(lines)


def probe(log_dir, state_root, profile):
    """What a real turn-end WOULD show, touching nothing.

    a hook run by hand — ``echo '{}' | python3
    stop_drain.py …`` — from a session's Bash tool inherits that session's
    identity, so it read as the OWNER and consumed the seat's one-shot
    surfacing exactly as the 2026-09-08 probe did; the operator path only
    ever covered a terminal with the environment stripped. A real Stop
    invocation is recognised by its payload (``hook_event_name: "Stop"``,
    which the harness sets on every hook it runs); anything else reports
    without consuming, whoever runs it.
    """
    try:
        pending = collect_unactioned(log_dir, state_root, profile)
    except Exception:
        return {}
    if not pending:
        return {}
    return {"decision": "block", "reason": format_reason(pending, profile)}


def drain(log_dir, state_root, profile, stop_hook_active=False, actor=None,
          real_invocation=False):
    """Return the Stop-hook decision, advancing ``surfaced_through`` when blocking.

    ``stop_hook_active`` means this turn-end was ALREADY blocked once by a
    hook and re-entered; blocking again from here is how a session gets
    wedged, so we stand down and let the turn end.

    Ownership (module docstring): the owner drains; a claimable cursor is
    claimed first; a FOREIGN session gets ``{}`` and touches nothing but
    ``contenders.json``; the OPERATOR (no session identity — a hook run by
    hand) gets the block text WITHOUT ``surfaced_through`` moving, so a
    probe cannot consume the seat's one-shot block.
    """
    if stop_hook_active:
        return {}
    if not real_invocation:
        return probe(log_dir, state_root, profile)
    try:
        actor = actor or actor_identity()
        with profile_lock(state_root, profile):
            cursor, cstate = read_cursor(state_root, profile)
            if cstate == "unreadable":
                return {}  # ownership indeterminate: touch nothing (codex pass 3)
            status = ownership(cursor, actor)
            if status == "foreign":
                note_contender(state_root, profile, actor)
                return {}
            # Ownership changes are applied IN MEMORY and persisted with the
            # positions (codex pass 4): one save carries both, so a lost write
            # loses both — the next turn re-claims and re-surfaces, consistent —
            # and positions can never be consumed under a missing or stale owner.
            if status == "claimable":
                claim(cursor, actor)
                ownership_changed = True
            elif status == "owner":
                ownership_changed = refresh_owner(cursor, actor)
            else:
                ownership_changed = False
            pending = collect_unactioned(log_dir, state_root, profile, cursor=cursor)
            if not pending:
                if ownership_changed:
                    save_cursor(state_root, profile, cursor)  # unchecked: silence over a wedge
                return {}
            # A real Stop payload with NO session identity — unreachable today, the
            # harness always sends session_id — takes the operator branch: it blocks
            # without consuming, as that path always has.
            if status != "operator":
                for source, events in pending.items():
                    mark_surfaced(log_dir, state_root, profile, source=source,
                                  through=max(e.index for e in events), by=actor,
                                  cursor=cursor)
            reason = format_reason(pending, profile)
    except ProfileBusy:
        return {}  # another actor is mid-operation; the events resurface next turn
    except Exception:
        return {}  # never wedge a turn over bookkeeping
    return {"decision": "block", "reason": reason}


def resolve_profile(explicit=None, log_dir=None):
    """Which profile does this session serve?

    Explicit flag wins, then ``WATCHTOWER_PROFILE``, then — only if the
    log tree holds exactly one profile's logs — that one. Ambiguity
    resolves to None (and the hook stays silent) rather than draining a
    profile this session never armed.

    Auto-detection scans ``log_dir`` when given, so a caller pointed at a
    non-default tree resolves against what it was pointed at rather than
    against whatever this machine happens to have.
    """
    if explicit:
        return explicit
    env = os.environ.get("WATCHTOWER_PROFILE")
    if env:
        return env
    try:
        names = set()
        for path in glob.glob(os.path.join(log_dir or DEFAULT_LOG_DIR, "*-*.log")):
            base = os.path.basename(path)[:-len(".log")]
            if "daemon-" in base:
                continue
            names.add(base.rsplit("-", 1)[0])
        if len(names) == 1:
            return names.pop()
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------
# session roots: which projects' sessions this profile's hook serves
# --------------------------------------------------------------------------
#
# Follow-up. On a host where this hook is also registered globally, every
# Claude Code session runs it, and while the seat's own session is dead the first
# unrelated session to end a turn claims the cursor, then surfaces and clears the
# seat's events: a side conversation in another project did exactly that on
# one occasion. Ownership cannot help — a dead owner is claimable by design — so the
# hook is told, per host and per profile, which project roots its sessions live in
# (``just session-roots``, ``hooks/wt_roots.py``). Opt-in: with no setting nothing
# changes. The CLI never consults it — acting, listing and take-over are deliberate.

ROOTS_NAME = "session-roots.json"


def roots_path(state_root, profile):
    return os.path.join(state_root, profile, ROOTS_NAME)


def load_session_roots(state_root, profile):
    """``(roots, state, why)``, ``state`` in ``missing`` | ``ok`` | ``unreadable``.

    An EMPTY list is unreadable, not "no session is allowed": that reading would
    silence the seat itself, which a setting must never do by accident —
    ``wt_roots.py --clear`` is how a restriction is lifted.
    """
    path = roots_path(state_root, profile)
    # lexists: a dangling symlink is a setting that cannot be read, not "no setting"
    #. Either way the hook serves; this makes it say so.
    if not os.path.lexists(path):
        return [], "missing", None
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return [], "unreadable", "%s: %s" % (exc.__class__.__name__, exc)
    roots = data.get("roots") if isinstance(data, dict) else None
    if not (isinstance(roots, list) and roots and all(_is_path_string(r) for r in roots)):
        return [], "unreadable", ('it needs "roots": a non-empty list of absolute paths, each one '
                                  'a string a filesystem can take (no NUL, encodable)')
    return roots, "ok", None


def _is_path_string(root):
    """An absolute path any filesystem call accepts — codex on caca76c: a NUL (or an
    unencodable character) makes ``os.stat`` raise ValueError, not OSError, and such a
    root is malformed CONTENT, like a relative one: the whole setting is unreadable."""
    if not (isinstance(root, str) and os.path.isabs(root)):
        return False
    try:
        return b"\0" not in os.fsencode(root)
    except ValueError:  # UnicodeEncodeError included
        return False


def _root_stat(root):
    """``(stat, None, False)`` for a usable root — an existing directory — else
    ``(None, why not, doubtful)``.

    No path-validation error may escape (codex on caca76c: a ValueError reached main's
    blanket handler and silenced every session). A root that is DEFINITELY absent —
    missing, not a directory, not a path at all — holds no session, and is ignored. One
    that cannot be EXAMINED (refused, looping, or any other error) is ``doubtful``: it may
    well exist and hold the very session asking (codex on 3fc801b, and the sweep after it).
    """
    try:
        st = os.stat(root)
    except ValueError:
        return None, "not a valid path", False
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None, "missing", False
        if exc.errno == errno.ENAMETOOLONG:
            return None, "not a valid path", False
        return None, "cannot be examined: %s" % (exc.strerror or exc.__class__.__name__), True
    if not stat.S_ISDIR(st.st_mode):
        return None, "not a directory", False
    return st, None, False


def root_status(root):
    """``(why it cannot be a session root, or None; doubtful)`` — the one test the hook and
    ``just session-roots`` share, so they cannot disagree about a root."""
    _st, problem, doubtful = _root_stat(root)
    return problem, doubtful


def _root_scan(roots):
    """``(ids of the usable roots, [why] for each root that cannot be examined)``.

    codex on d12809a: a root naming a regular file stats fine, and counting it enforced a
    restriction no session could ever satisfy: every session silenced, the seat's included.
    Only existing directories count; none usable is the fail-open case (``session_verdict``).
    """
    ids, doubtful = set(), []
    for root in roots:
        st, problem, unsure = _root_stat(root)
        if st is not None:
            ids.add((st.st_dev, st.st_ino))
        elif unsure:
            doubtful.append("session root %r %s" % (root, problem))
    return ids, doubtful


def _root_ids(roots):
    return _root_scan(roots)[0]


def _inside(path, ids):
    """True / False — or None when it cannot be decided. "Outside" silences the session,
    so False is only ever a DEFINITE answer: every component of the resolved path
    examined, and no root among them.

    Inside means one of the directories ``ids`` names, or below one, by file identity: a
    symlink in either the root or the session's path cannot hide the match, nor can letter
    case on a case-insensitive filesystem (macOS by default), where realpath keeps
    whatever spelling was typed. One pass from the top examines each component realpath
    produced (lstat, so nothing is followed twice):

    - a component that does not exist (ENOENT / ENOTDIR) ends the pass — a directory
      deleted since: the part that exists still decides;
    - any other refusal (EACCES, EPERM, ELOOP, ENAMETOOLONG, ...), a symlink left
      unresolved, or a string no filesystem accepts is UNKNOWN. realpath is not strict
      (3.9 has no strict mode): it keeps a component it could not examine, and walking
      that alias's lexical parents placed the session wherever the alias sat — "outside",
      even when it pointed into a root (codex on 3fc801b). A NUL or unencodable string
      raises from realpath on 3.14 and from lstat on 3.9 (codex on caca76c).
    """
    try:
        path = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    components = [os.sep]                                  # "/", "/a", "/a/b", ...
    for part in [p for p in path.split(os.sep) if p]:
        components.append(os.path.join(components[-1], part))
    seen = []
    for prefix in components:
        try:
            st = os.lstat(prefix)
        except ValueError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                break
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
        seen.append((st.st_dev, st.st_ino))
    return any(ident in ids for ident in seen)


def session_verdict(payload, roots, environ=None):
    """``(True | False | None, why)``. False — "outside", which silences — only when it is
    definite; None is unknown, and ``why`` says what could not be decided (None there
    means no root is usable at all: the setting-level fail-open).

    Where a session is: its CURRENT directory — the payload's ``cwd``, or the process
    cwd when the payload has none — OR the project root it started in,
    ``CLAUDE_PROJECT_DIR``, which Claude Code exports to every hook. ``cwd`` follows
    ``cd``, so a cwd-only test would silence the seat whenever it ended a turn
    inside another repo; a session is left out only when BOTH are outside.
    """
    env = os.environ if environ is None else environ
    ids, doubtful = _root_scan(roots)
    if not ids:
        return None, None
    cwd = (payload or {}).get("cwd")
    if isinstance(cwd, str) and cwd:
        places = [cwd]
    else:
        try:
            places = [os.getcwd()]
        except OSError:
            places = []
    if env.get("CLAUDE_PROJECT_DIR"):
        places.append(env["CLAUDE_PROJECT_DIR"])
    if not places:
        return None, "its location is unknown"
    verdicts = [_inside(p, ids) for p in places]
    if True in verdicts:
        return True, None
    if None in verdicts:
        return None, "its location cannot be resolved"  # unknown, never "outside": fail open
    if doubtful:
        return None, "; ".join(doubtful)  # it may be inside a root we could not examine
    return False, None


def session_inside(payload, roots, environ=None):
    """``session_verdict`` without the reason: True, False (definitely outside), or None."""
    return session_verdict(payload, roots, environ)[0]


def _warn(text):
    """Claude Code sends a Stop hook's stderr to its debug log; never let it raise."""
    try:
        print(text, file=sys.stderr)
    except (OSError, ValueError):
        pass


def _roots_gate(payload, state_root, profile):
    """``(serve, note)``. ``serve`` False means another project's session: the hook then
    does nothing at all. Only a DEFINITE "outside" may say so. Everything else serves,
    with a note where the restriction could not be applied: an unusable setting must never
    silence the seat's own drain, and neither may an error deciding — any exception here
    used to reach main's blanket handler, which is silence (the sweep after codex on
    3fc801b). The notes go where they can be seen: the hook log and the blocked session.
    """
    try:
        roots, rstate, why = load_session_roots(state_root, profile)
        if rstate == "missing":
            return True, None
        if rstate == "ok":
            inside, unsure = session_verdict(payload, roots)
            if inside is True:
                return True, None
            if inside is False:
                return False, None
            if unsure:  # the setting is usable; whether THIS session is inside it is not known
                return True, ("note: whether this session is inside the session roots cannot be "
                              "decided (%s), so the restriction is OFF for this session: its hook "
                              "serves profile %s as if no roots were set." % (unsure, profile))
            why = "none of its roots is a usable directory"
    except Exception as exc:  # never let an error decide "outside"
        why = "deciding raised %s: %s" % (exc.__class__.__name__, exc)
    # Addressed to the operator, not to the session reading it:
    # while the setting is unusable EVERY session is served, so this can reach another
    # project's agent — and one that "fixed" it by storing its own directory would silence
    # the seat.
    return True, ("note: %s cannot be used (%s), so the session-roots restriction is OFF and every "
                  "session's hook serves this profile. Tell the operator; do not change it from "
                  "this session. The fix is `just session-roots %s <absolute root> ...`, or "
                  "`just session-roots %s --clear`." % (roots_path(state_root, profile), why,
                                                       profile, profile))


def run_hook(payload, log_dir, state_root, profile):
    """Return the JSON the hook should print (``""`` = allow the turn to end)."""
    if not profile:
        return ""
    payload = payload or {}
    serve, note = _roots_gate(payload, state_root, profile)
    real = payload.get("hook_event_name") == "Stop"
    if not serve:
        if real:
            return ""  # another project's session: no block, no claim, no record, no write
        # A hook run by hand writes nothing (``probe``), so gating it buys no safety, and
        # its silence read exactly like "nothing pending". It
        # reports, and says that a real turn end here does nothing.
        note = ("note: this session is outside the session roots of profile %s, so at a real "
                "turn end its hook does nothing. This hand-run probe reports anyway; it "
                "changes nothing." % profile)
    decision = drain(log_dir, state_root, profile,
                     stop_hook_active=bool(payload.get("stop_hook_active")),
                     actor=actor_identity(payload),
                     real_invocation=real)
    if note:
        _warn(note)
        if decision.get("reason"):
            decision["reason"] += "\n\n" + note
    return json.dumps(decision) if decision else ""


def main(argv=None):
    parser = argparse.ArgumentParser(description="Watchtower Stop-hook drain")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--state-root", default=DEFAULT_STATE_ROOT)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        payload = {}
    try:
        out = run_hook(payload, args.log_dir, args.state_root,
                       resolve_profile(args.profile, args.log_dir))
    except Exception:
        out = ""  # a hook that raises is a seat that cannot finish a turn
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
