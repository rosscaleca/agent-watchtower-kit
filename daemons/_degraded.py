#!/usr/bin/env python3
"""A poll cycle that completed with a hole in it.

Every daemon here wraps its *sub*-fetches — one PR's reviews, a repo's
comments, an issue's changelog — so that one failing sub-resource cannot lose
the rest of the cycle. That is right, and it created a silent failure: the
sub-fetch was logged with ``note()`` to ``daemon-<source>.log``, which nothing
tails and nothing checks, while the cycle went on to complete and
``save_state`` advanced the watermark. Every health signal then reads green —
no ``error:`` in the profile log, a fresh state-file mtime, and a watchdog that
is working exactly as designed, because the job IS loaded and the state IS
fresh. The source is partially blind and nothing says so.

Observed in production: a 404 on a PR's reviews endpoint. A
review landing on that PR during the window would not have produced a wake.

This module is the shared half of the fix. Each daemon records its swallowed
sub-fetch failures against a `Cycle`, then asks `reconcile()` what to say. The
daemons keep their own call sites — the fetches are genuinely different — but
the SEVERITY, the RATE LIMIT and the WORDING live here once, so the three
daemons cannot drift apart on what "degraded" means.

Severity, and why it is a typed event rather than ``error:``: the daemon is
healthy, the credential works, and the next cycle may well recover. That is
the same reasoning applied to ``fleet_outdated`` — a coverage gap, not
a liveness failure — and it keeps ``error:`` meaning "this source is not
polling". Both forms reach a session and the digest, since the Monitors match
``^type=|^error:``.

Promotion: the rate limit above makes a FLAPPING sub-resource bearable,
and that is exactly what made a PERMANENTLY DEAD one invisible — it produced
rate-limited ``degraded`` lines forever and never the one shape that says "this
is broken rather than noisy". A whole-cycle failure promotes to ``error:`` in
every daemon's ``main()`` after three consecutive cycles, once per episode,
with a recovery note; a sub-fetch had no equivalent. Now each sub-resource
carries its own consecutive-failure count and promotes at the SAME threshold,
the same once-per-episode way, with the same daemon-log recovery note. It is
an ADDED pair: nothing about ``degraded`` / ``degraded_recovered`` /
``suppressed=N`` changes, and a test pins that.

Stdlib only; no exceptions escape.
"""

import _fields  # a sibling module: the daemons dir is sys.path[0] when a daemon runs


#: Re-emit the same (source, sub-resource) at most this often. A transient 504
#: that recovers next cycle must not page a session every poll interval (AC-3).
DEFAULT_RENOTIFY_MIN = 60

#: A sub-resource that has failed this many CONSECUTIVE cycles is
#: promoted to an ``error:`` line, once per episode. Deliberately the same
#: number as ``if failures == 3`` in every daemon's ``main()``; a test derives
#: that literal from each daemon and pins the two together.
PROMOTE_AFTER = 3


def one_line(s, cap=160):
    # A detail is gh or bkt error text, which can quote text from outside the project. As
    # the daemons' own `one_line` does: after the whitespace collapse, the
    # controls and format characters a terminal acts on but does not show (ESC/CSI, the bidi marks,
    # zero-width spaces; `_fields.unshown_dropped`, which keeps the two joiners U+200C/U+200D) are
    # dropped, so the detail cannot redraw the `type=degraded` line.
    s = _fields.unshown_dropped(" ".join(str(s).split())).strip()
    return s[:cap]


class Cycle:
    """Sub-fetch failures collected during a single poll cycle.

    Keyed by SUB-RESOURCE, first failure wins. A repo loop can fail the same
    fetch for twenty PRs; the reader needs the resource name and one example,
    not twenty lines — and the count would be a floor anyway, since the loop
    may stop early.
    """

    def __init__(self):
        self.failures = {}
        self.skipped = {}

    def record(self, sub, detail):
        """Note that `sub` could not be fetched. Safe to call repeatedly."""
        try:
            self.failures.setdefault(str(sub), one_line(detail))
        except Exception:
            pass
        return self

    def skip(self, sub, detail):
        """Note that `sub` was NOT ATTEMPTED this cycle — a parked credential, a
        tier waiting on config. Not a failure: it neither opens an episode nor
        counts toward promotion. Not a success either: an open episode for it is
        held as-is rather than announced recovered, because nothing ran that
        could have recovered it (found in review). Safe to
        call repeatedly."""
        try:
            self.skipped.setdefault(str(sub), one_line(detail))
        except Exception:
            pass
        return self

    def __bool__(self):
        return bool(self.failures)

    __nonzero__ = __bool__


def _record(value):
    """Normalise a stored episode.

    Accepts the earliest int form and the later dict form (no
    ``announced`` / ``suppressed`` keys); both are read as an episode whose
    degraded line WAS announced, which is the only honest default for a record
    written before the distinction existed. A record from before promotion existed has no
    ``consecutive`` count and reads as 0: an old open episode counts from its
    next failure rather than from a history it cannot know.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            suppressed = int(value.get("suppressed", 0) or 0)
        except (TypeError, ValueError):
            suppressed = 0
        try:
            consecutive = int(value.get("consecutive", 0) or 0)
        except (TypeError, ValueError):
            consecutive = 0
        return {
            "last": value.get("last", 0),
            "open": bool(value.get("open", True)),
            "announced": bool(value.get("announced", True)),
            "suppressed": suppressed,
            "last_silenced": value.get("last_silenced", 0) or 0,
            "consecutive": consecutive,
        }
    try:
        return {"last": int(value), "open": True, "announced": True,
                "suppressed": 0, "last_silenced": 0, "consecutive": 0}
    except (TypeError, ValueError):
        return None


def is_open(episodes, sub):
    """Is ``sub``'s episode OPEN in a persisted ``episodes`` dict?

    An open episode means the sub-fetch was failing in the last cycle that saved this state, so the
    slice of the source it covers was not read, and the watermark advanced past it all the same. The
    jira and bitbucket migrations read that off the old state file: what such a file does not list may
    never have been announced. ``_record`` decides what an episode IS, including the two older shapes,
    and this asks it, so the two cannot drift apart. Never raises: what is not an episode is not open.
    """
    if not isinstance(episodes, dict):
        return False
    record = _record(episodes.get(sub))
    return bool(record and record["open"])


def reconcile(cycle, episodes, now, renotify_secs=DEFAULT_RENOTIFY_MIN * 60,
              promote_after=PROMOTE_AFTER):
    """What to say about this cycle's holes, given what was already said.

    `episodes` is a plain dict persisted in the daemon's own state file:
    ``"<sub>" -> epoch of the last emit``. It is MUTATED here.

    Returns a list of ``(emit_line, note_line)`` pairs — the caller owns the
    writing, so this stays testable without touching a log directory.
    ``emit_line`` is ``None`` for a pair that belongs in the daemon log ONLY;
    callers skip the profile-log write for those.

    A sub-resource that failed and is outside its renotify window produces a
    ``type=degraded`` line; one whose ANNOUNCED failure ended this cycle
    produces ``type=degraded_recovered`` and leaves the episode set. A failure
    inside the window is silenced on the profile log — that is the rate limit —
    but it is counted and noted, and its recovery is silent too.

    ⚠ The pairing invariant, stated because the first version broke
    it: **a ``degraded_recovered`` line is never emitted for an episode whose
    ``degraded`` was never emitted.** Before this, the rate limit was applied
    to one half of the pair: a failure inside the window took the silent
    branch but reopened the episode, and the next clean cycle announced a
    recovery for a failure nobody had been told about. Observed on both hosts
    within two hours of shipping — one ``degraded``, two ``recovered``, and a
    real failed sub-fetch that appeared in no log at all. The evidence had
    been rate-limited away with the notification. Now:

    * a silenced repeat still reaches ``daemon-<source>.log`` via the note
      half of its pair, with its detail, so the onset time is recoverable;
    * the number of silenced repeats rides the NEXT announced ``degraded``
      line as ``suppressed=N``, so the record survives the limit in the
      profile log too;
    * a silently-suppressed failure recovers silently — nothing was claimed,
      so nothing is retracted;
    * the anti-flapping property below is unchanged: ``last`` survives the
      recovery, so a fail/ok/fail/ok fetch still emits once per window.

    Promotion. Each sub-resource's episode also carries
    ``consecutive``, the number of cycles in a row it has failed (announced or
    silenced — the rate limit is about what is SAID, not about whether it
    failed). When that count reaches ``promote_after`` the cycle produces one
    extra pair, an ``error:`` line, exactly once per episode — the same
    threshold and once-per-episode rule as the whole-cycle counter in
    ``main()`` — and the episode's recovery adds a daemon-log note naming the
    count, as ``main()``'s does. ``promote_after=None`` disables the addition;
    the test suite uses that to prove every other line is byte-identical.
    """
    out = []
    failures = dict(getattr(cycle, "failures", {}) or {})

    for sub in sorted(failures):
        # A sub-resource never reported is always worth reporting; only a
        # REPEAT is rate-limited. `episodes.get(sub, 0)` would have compared a
        # first failure against epoch zero — true by accident for real clocks,
        # false for any small `now`, and wrong in meaning either way.
        rec = _record(episodes.get(sub))
        # Consecutive failed cycles for THIS sub-resource. It carries
        # across every branch below (announced or silenced, the fetch still
        # failed) and restarts after a closed episode, so a fetch that
        # recovered and then failed again counts from one.
        consecutive = (rec["consecutive"] if rec is not None and rec["open"] else 0) + 1
        if rec is None or now - rec["last"] >= renotify_secs:
            silenced = rec["suppressed"] if rec else 0
            suffix = " suppressed=%d" % silenced if silenced else ""
            out.append((
                "type=degraded sub=%s detail=%s%s" % (sub, failures[sub], suffix),
                "note: degraded: %s not fetched — %s" % (sub, failures[sub]),
            ))
            episodes[sub] = {"last": now, "open": True, "announced": True,
                             "suppressed": 0, "last_silenced": 0,
                             "consecutive": consecutive}
        elif rec["open"] and rec["announced"]:
            # The announced episode is still going: nothing new to say.
            rec["consecutive"] = consecutive
            episodes[sub] = rec
        else:
            # Inside the window and not part of an announced episode: silenced
            # on the profile log (the rate limit), but COUNTED and NOTED so it
            # is neither invisible nor later announced as a recovery.
            rec["open"] = True
            rec["announced"] = False
            rec["suppressed"] += 1
            rec["last_silenced"] = now
            rec["consecutive"] = consecutive
            episodes[sub] = rec
            out.append((
                None,
                "note: degraded (silenced inside the %d-minute window; suppressed=%d so far): "
                "%s not fetched — %s"
                % (renotify_secs // 60, rec["suppressed"], sub, failures[sub]),
            ))
        if promote_after and consecutive == promote_after:
            # The promotion. ONCE, at exactly the threshold (``==``,
            # as main() does), so a permanently dead sub-resource does not
            # become a new noise source; it is an added pair, so the degraded
            # rate limit above is untouched. Reaches the profile log because
            # every daemon writes a truthy emit_line, and the Monitors match
            # ``^error:``.
            out.append((
                "error: sub-fetch %s failing repeatedly (%d consecutive cycles) — last: %s"
                % (sub, consecutive, failures[sub]),
                "note: error: sub-fetch %s failing repeatedly (%d consecutive cycles) — last: %s"
                % (sub, consecutive, failures[sub]),
            ))

    # A sub-resource that was not attempted this cycle is neither failed nor
    # recovered: its episode, if any, is held exactly as it was.
    skipped = set(getattr(cycle, "skipped", {}) or {})
    for sub in sorted(set(episodes) - set(failures) - skipped):
        rec = _record(episodes.get(sub))
        if rec is None:
            del episodes[sub]
            continue
        if rec["open"]:
            if rec["announced"]:
                out.append((
                    "type=degraded_recovered sub=%s" % sub,
                    "note: recovered: %s fetching again" % sub,
                ))
            else:
                # Never announce a recovery for a failure that was
                # never announced. The daemon log still gets the pair.
                out.append((
                    None,
                    "note: recovered (silently — its failure was silenced): %s fetching again"
                    % sub,
                ))
            if promote_after and rec["consecutive"] >= promote_after:
                # Mirrors main()'s "polling recovered after N
                # failures": the daemon log only, as for the whole-cycle
                # counter. The profile log already carries the paired
                # degraded_recovered above when the episode was announced.
                out.append((
                    None,
                    "note: sub-fetch %s recovered after %d consecutive failures"
                    % (sub, rec["consecutive"]),
                ))
        # The last-emit time SURVIVES the recovery, and this is the
        # point. Deleting the episode made a flapping fetch — fail, ok, fail,
        # ok, which is what a rate-limited upstream actually looks like —
        # re-enter as a FIRST failure every other cycle and emit every time,
        # so AC-3's "a transient 504 cannot page a session every cycle" held
        # only for a PERSISTENT failure. Measured before the fix: alternating
        # fail/ok over ten one-minute cycles with a 60-minute window emitted
        # five lines. Keeping `last` and closing the episode instead means the
        # window governs a flapping fetch too; the entry is dropped once it is
        # closed and older than the window, so this cannot grow without bound.
        # (`announced` and `suppressed` ride along in the closed
        # record so the next announcement can carry the count. The entry is
        # dropped once BOTH the last emit and the last silenced failure are
        # older than the window — keyed on `last` alone, a count silenced late
        # in the window was dropped one cycle before the failure that would
        # have reported it. A count that goes a whole window with no further
        # failure has nothing to attach to and is dropped; the daemon log
        # already has each one.)
        if now - max(rec["last"], rec["last_silenced"]) >= renotify_secs:
            del episodes[sub]
        else:
            episodes[sub] = {
                "last": rec["last"],
                "open": False,
                "announced": rec["announced"],
                "suppressed": rec["suppressed"],
                "last_silenced": rec["last_silenced"],
                "consecutive": 0,
            }

    return out
