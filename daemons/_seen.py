#!/usr/bin/env python3
"""The dedup memory the polling daemons share.

Every poller keeps ``state["seen"]``: the ids it has already announced, so that a poll whose
window overlaps the last one, or a restart, does not announce them again. ``jira_wake``,
``inbox_wake`` and ``bitbucket_wake`` each saved it as ``list(state["seen"])[-CAP:]`` from a SET.
A set has no order, so past the cap an ARBITRARY subset survived, and after the next restart
whatever the source still returned and the cut had dropped was announced a second time. That was
fixed in ``github_wake`` (four overnight CI completions re-announced at the always-on host's
restart on 2026-09-29); this module is that fix for the other three, once.

The design, which ``github_wake`` proved and this ports:

* ``seen`` is an ORDERED mapping (:func:`new_seen`), and an id moves to the newest position each
  time the source returns it (:func:`touch_seen`, an LRU), so eviction sheds what has STOPPED
  appearing.
* It is pruned PER KIND, the id prefix, at the end of every cycle in the running process
  (:func:`prune_seen`), so what a restart reloads is exactly what the process already knew. Each
  daemon sizes its own caps from the windows its own queries return.
* The file carries ``seen_v`` (:data:`SEEN_V`), the marker that the order in ``seen`` means
  something. ``seen`` itself stays a FLAT list of id strings, so the previous code still reads a
  marked file: a rollback loses the order, never an id, and never fails to start.
* A file with no marker is the arbitrary-order subset. Its dropped ids are already lost, so its
  first cycle adopts, silently, an item it cannot find in ``seen`` only on positive evidence that
  the item existed when the old daemon last looked (:func:`legacy_baseline`,
  :func:`legacy_cutoff`, :func:`predates`, :func:`settle_legacy`). What happened while the daemon
  was down is still announced. And only where the file says ids were LOST and that the last cycle
  READ everything: a file under the old cap has never lost an id, and one whose last cycle left a
  sub-fetch episode open was not fully read (the old daemon advanced its watermark past the hole),
  so for those nothing is adopted and everything the daemon does not remember announces.

What is NOT here, because it differs by source: the ids and their kinds, the windows and caps,
which timestamp of an item to compare, and which sub-fetches feed the memory.

Like ``_degraded`` and ``_review_binding`` this is a sibling module: a daemon's build marker
hashes only the daemon's own file, so the watchdog cannot see that a running daemon
loaded an older copy of this one. The restart that follows the fast-forward is what loads it.

Stdlib only, and Python 3.9 (the daemons' runtime).
"""
import re
from datetime import datetime, timezone

import _degraded  # sibling module: what an open degraded episode IS is decided there, once

#: The state file's ``seen`` list. Version 1 (no marker at all) is the arbitrary-order subset the
#: old code wrote. Version 2 is oldest-first: the first id is the next to be evicted.
SEEN_V = 2

#: Every kind nobody sized shares ONE bucket, so a stream of distinct unknown prefixes cannot grow
#: the file without bound.
SEEN_CAP_OTHER = 300

#: A hole in the first cycle after loading an old-format file leaves its slice unbaselined, so the
#: baseline is held for the next cycle, at most this many, so a permanently failing fetch cannot
#: keep an adoption rule running for the life of the daemon.
LEGACY_MAX_CYCLES = 10


def returnable_polls(overlap, poll_secs):
    """How many consecutive polls an id of a ``since``-filtered query can be returned in.

    Such a query's floor is the last cycle's start less ``overlap`` and moves one poll at a time,
    so an id is returnable for the overlap PLUS one poll, in polls. ROUNDED UP: an interval that is
    not a whole number of polls still holds a partial one an id can be returned in (a 90 s poll with
    a 5 minute overlap is 4.33, and an id created just before a poll is returned in five).
    ``overlap`` is a ``timedelta``, ``poll_secs`` a number of seconds.
    """
    return int(-(-(overlap.total_seconds() + poll_secs) // poll_secs))


def caps_from(window, keep):
    """The per-kind caps: each kind's window (the most ids of that kind one poll can return) times
    the number of windows it remembers. Both are per-kind tables with the same keys."""
    return dict((kind, window[kind] * keep[kind]) for kind in window)


def scaled(caps, units=1):
    """``caps`` for a profile whose windows are asked ``units`` times a poll (per watched repo, per
    credential): the memory scales with the number of questions asked. The table given is untouched."""
    n = max(1, int(units or 1))
    return dict((kind, cap * n) for kind, cap in caps.items())


def new_seen(ids=()):
    """The dedup container: an ORDERED mapping, deliberately not a set.

    A set has no order to give, and eviction is oldest-first. ``dict.fromkeys`` keeps O(1)
    membership and records arrival; an insertion order that means "last seen" is maintained by
    :func:`touch_seen`.
    """
    return dict.fromkeys(ids)


def touch_seen(seen, mid):
    """Move an already-known id to the newest position (LRU, not FIFO).

    Re-assigning a key in a dict does NOT move it, so without this the queue would be ordered by
    FIRST sighting: an id the source keeps returning would drift to the front and be evicted while
    still in the answer, and announced again. Eviction should shed what has STOPPED appearing, and
    last-seen order does exactly that.
    """
    if mid in seen:
        del seen[mid]
        seen[mid] = None


def ordered_seen(state):
    """``state["seen"]`` as the ordered container. A set handed in by an older caller (and the
    fixtures built from one) is upgraded in place; its order is arbitrary, which is only ever true of
    a state nothing but a test builds."""
    seen = state.get("seen")
    if not isinstance(seen, dict):
        seen = state["seen"] = new_seen(seen or ())
    return seen


def id_kind(mid):
    """The kind of an id written as ``<kind>:<rest>``: the prefix. ``None`` for anything else."""
    return mid.split(":", 1)[0] if isinstance(mid, str) and ":" in mid else None


def prune_seen(order, caps, other_cap=SEEN_CAP_OTHER, kind_of=id_kind):
    """Keep the newest ids of each kind, in their original order.

    Replaces ``list(seen)[-CAP:]``, which had two defects. ``seen`` was a set, so the slice kept an
    arbitrary subset rather than the newest; and one global budget let the highest-churn kind evict
    the others. ``caps`` maps a kind to how many of its ids to keep; every kind not in it shares one
    bounded bucket of ``other_cap``. ``kind_of`` says which kind an id is (the prefix, by default;
    ``inbox_wake``'s ids are bare event ids and must stay bare, so it names its one kind itself).
    ``order`` is the ids oldest-first (an ordered mapping included, and ids are unique there); the
    result is a list, oldest first.
    """
    kept, counts = [], {}
    for mid in reversed(list(order)):             # newest first, so the newest `cap` of a kind survive
        kind = kind_of(mid)
        if kind not in caps:
            kind = None                            # the shared bucket for anything unsized
        cap = caps[kind] if kind is not None else other_cap
        if counts.get(kind, 0) >= cap:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        kept.append(mid)
    kept.reverse()
    return kept


_FRACTION = re.compile(r"\.(\d+)(?=[+-]|$)")
_OFFSET_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_ts(raw):
    """A timestamp in any spelling these sources use, as an aware UTC datetime, or ``None``.

    The daemons run under /usr/bin/python3 (3.9), whose ``fromisoformat`` takes no ``Z``, no
    ``-0700`` (Jira's own spelling) and only a 3- or 6-digit fraction, so each is normalised first.
    A timestamp with no zone at all is taken as UTC. ``None`` for anything that is not a string or
    does not parse: never raises.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    text = _OFFSET_NO_COLON.sub(r"\1:\2", text)
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def predates(raw, cutoff):
    """``True`` only on POSITIVE evidence: ``raw`` parses as a timestamp at or before ``cutoff``.
    No cutoff, no timestamp, or one that does not parse is ``False``: an item that cannot be proven
    old is announced, because a duplicate wake costs a turn and a swallowed one costs the event."""
    if cutoff is None:
        return False
    stamped = parse_ts(raw)
    return stamped is not None and stamped <= cutoff


def legacy_baseline(raw, old_cap, lag=None, subs=()):
    """The in-memory baseline for a state file just loaded, or ``None`` when it needs none.

    A file without the :data:`SEEN_V` marker is the arbitrary subset the old code wrote. Its order
    means nothing, so its first cycle runs with a baseline. The baseline rides the in-memory state
    only: once a cycle has written the marked file it is gone.

    ``cutoff`` is when the old daemon last LOOKED, which is the file's watermark for a daemon that
    saves its cycle's start. ``lag`` is how far the file's watermark trails that: ``inbox_wake`` saves
    ``cycle_start - OVERLAP``, so its last look is a ``lag`` of ``OVERLAP`` later. A watermark that
    does not parse leaves no usable cutoff, and so nothing is ever adopted.

    Adoption needs evidence that an id was LOST and that the last cycle READ everything, and the file
    can say either is not so. Then ``cutoff`` is None, ``why`` says which, and everything the daemon
    does not remember announces: a duplicate wake costs a turn, a swallowed one costs the event.

    * The file holds fewer ids than ``old_cap``, what the old code's ``list(seen)[-old_cap:]`` kept.
      A set only grows, so such a file never lost an id: an item it does not list was never
      announced, and adopting it would swallow it.
    * ``subs`` names the daemon's sub-fetches that feed the memory, and one of them has an OPEN
      degraded episode in the file. The old daemon recorded a failed sub-fetch as a hole and still
      advanced its watermark, so "stamped before its last look" does not mean "read".
    """
    if raw.get("seen_v") == SEEN_V:
        return None
    cutoff = raw.get("since")
    if lag and cutoff is not None:
        looked = parse_ts(cutoff)
        cutoff = (looked + lag).isoformat() if looked is not None else None
    why = None
    held = len(raw.get("seen") or ())
    if held < old_cap:
        why = ("the file holds %d ids, under the old cap of %d, so none was ever dropped from it and "
               "an item it does not list was never announced" % (held, old_cap))
    else:
        unread = sorted(sub for sub in subs if _degraded.is_open(raw.get("degraded"), sub))
        if unread:
            why = ("its last cycle left %s unread (an open degraded episode), so an item it does not list "
                   "may never have been announced" % ", ".join(unread))
    baseline = {"cutoff": None if why else cutoff, "cycles": LEGACY_MAX_CYCLES}
    if why:
        baseline["why"] = why
    return baseline


def legacy_cutoff(legacy, guard):
    """The instant at or before which an unseen item is adopted silently in the baseline cycle: the
    old daemon's last look less ``guard`` (a ``timedelta``, one poll interval), or ``None`` when there
    is no baseline or no usable watermark.

    The guard is the safe direction. The old daemon's last look can lag its watermark by a fetch and
    the source's clock is not this machine's, so an item this close to it is announced if unseen."""
    if not legacy:
        return None
    marked = parse_ts(legacy.get("cutoff"))
    return None if marked is None else marked - guard


def legacy_note(profile, state, guard):
    """The daemon-log line for a state that loaded in the old format, else ``None``."""
    legacy = state.get("legacy_seen")
    if not legacy:
        return None
    if legacy.get("why"):
        return ("[%s] the seen list on disk predates seen_v %d (%d ids, arbitrary order), and the first cycle "
                "adopts nothing, so it announces what it does not remember: %s"
                % (profile, SEEN_V, len(state.get("seen") or ()), legacy["why"]))
    return ("[%s] the seen list on disk predates seen_v %d (%d ids, arbitrary order): the first cycle "
            "adopts silently what is stamped %ds or more before the old daemon's last look, %s"
            % (profile, SEEN_V, len(state.get("seen") or ()), int(guard.total_seconds()),
               legacy.get("cutoff")))


def settle_legacy(profile, state, legacy, cutoff, adopted, unread, labels, say):
    """The end of a cycle that ran with the legacy baseline: say what it adopted, and decide
    whether it runs again.

    ``adopted`` counts what was adopted by kind and ``labels`` names the kinds, in the order to say
    them. ``unread`` lists the sub-fetches feeding the memory that failed this cycle. The baseline
    runs again only while one did, and only :data:`LEGACY_MAX_CYCLES` times. With no usable cutoff
    there is nothing to compare against, so there is nothing to hold. ``say`` is the daemon's
    ``note``.
    """
    if cutoff is None:
        state["legacy_seen"] = None
        return
    if any(adopted.values()):
        say("[%s] legacy seen list: adopted %s silently (stamped before %s)" % (
            profile, ", ".join("%d %s" % (adopted.get(kind, 0), label) for kind, label in labels),
            cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")))
    legacy["cycles"] -= 1
    if unread and legacy["cycles"] > 0:
        say("[%s] legacy seen list: %s not read this cycle, so the baseline holds for the next (%d left)"
            % (profile, ", ".join(unread), legacy["cycles"]))
        return
    if unread:
        say("[%s] legacy seen list: %s still not read after %d cycles, so the baseline is dropped"
            % (profile, ", ".join(unread), LEGACY_MAX_CYCLES))
    state["legacy_seen"] = None
