#!/usr/bin/env python3
"""Advance (or inspect) a session's ``acted_through`` cursor.

The companion to ``stop_drain.py``. The drain can only tell a session what
it was handed and never handled; something has to record the handling, and
that something is deliberately an EXPLICIT call rather than the hook
inferring action from a transcript.

Why explicit: inferring "this was dealt with" from session text is a guess,
and a wrong guess here is silent — it marks an unhandled event handled and
restores exactly the blind spot this exists to close. A session saying so
is a claim someone can be held to; a regex over a transcript is not. (The
same reasoning the spike applied to the daemon watermark: "observed" and
"acted on" are different facts, recorded by different parties.)

Unlike the hook, this fails LOUDLY on a missing profile: a human at a
prompt should be told their command did nothing, whereas a turn-boundary
hook must degrade to silence rather than wedge the seat.

Usage::

    wt_acted.py --profile acme --list          # what is still open (and record that you saw it)
    wt_acted.py --profile acme                 # all sources, to tail — if you saw it all
    wt_acted.py --profile acme --source inbox --through 13
    wt_acted.py --profile acme --all-including-unseen   # to tail, seen or not: deliberate

``--list`` reports against ``acted_through`` alone, so an item the drain
already surfaced still shows until it is genuinely handled.

A blanket act (no ``--through``) clears only what a listing SHOWED you
(a follow-up: a blanket clear at the end
of a turn swept a mail that had landed mid-turn, and said only "2").
``--list`` records in the cursor what it printed; a blanket act that would
clear an open event no listing showed this session REFUSES, prints those
events, and changes nothing (``stop_drain.unseen`` says whose listing
counts). Every act prints each event it cleared. ``--through N`` needs no
listing — a session that names the number has said what it read — but, like
every act, it refuses a source whose log was replaced after the act read it.
A listing ENDS with the by-number clears for exactly what it printed
(a review found why): a blanket act chained onto the listing
with ``&&`` passes the gate for an event nobody read yet, and a number cannot.

Ownership (``stop_drain`` module docstring): a session that is NOT the
cursor's owner is REFUSED here, loudly, naming the owner — the second
session on a seat is a charter violation made visible, not silently
tolerated. ``--take-over`` from a session claims the cursor from a dead or
abandoned session and clears NOTHING (claiming and acting are two
decisions; ``just takeover``, then ``just unactioned`` and ``just acted``). The operator — no
session identity, a human at a prompt — may act without owning and is
attributed as such, and an operator ``--take-over`` RELEASES the cursor
(owner cleared, nothing claimed) so the next session takes it: a seat's
cursor is never OWNED by a human, but the human at the terminal is the
actor every recovery message addresses and must have a way out
(found in review).
"""
import argparse
import os
import sys

import stop_drain


def _lines(events):
    """``  [source #n] text`` per event, truncated — the one format for listed, cleared and refused."""
    out = []
    for source in sorted(events):
        for event in events[source]:
            text = event.text
            if len(text) > stop_drain.LINE_CAP:
                text = text[:stop_drain.LINE_CAP - 1] + "…"
            out.append("  [%s #%d] %s" % (source, event.index, text))
    return out


def render(pending, profile):
    """One line per still-open event; a clean board says so explicitly."""
    if not pending:
        return "no unactioned events (profile=%s)" % profile
    total = sum(len(v) for v in pending.values())
    return "\n".join(["%d unactioned event(s) (profile=%s):" % (total, profile)] + _lines(pending))


def _open(view):
    """``report_unactioned``'s shape, from a view: source → events not yet acted on."""
    out = {}
    for name, v in view.items():
        fresh = [e for e in v.events if e.index > v.acted]
        if fresh:
            out[name] = fresh
    return out


def _clears(events, profile):
    """``just acted <p> <source> <n>`` per source, through the last of ``events`` — exactly those."""
    return ["just acted %s %s %d" % (profile, name, events[name][-1].index)
            for name in sorted(events)]


def render_refusal(strays, profile, actor, basis, source=None, last=None):
    """Why a blanket act was refused, the events no listing showed, and the ways on.

    Every suggested command keeps the act's own ``source``: the advice for a refused ``just acted <p> inbox`` once named
    all-source commands, and following it verbatim cleared a github event nobody
    had been shown. The by-number clears are exact — through the last stray of
    each source, and everything open below it was either listed or printed here.
    ``last`` is the latest listing by ANYONE: only one is kept, so when it does
    not count for this session, say whose it was.
    """
    total = sum(len(v) for v in strays.values())
    if basis:
        why = "they arrived after the listing by %s at %s" % (_who(basis.get("by") or {}),
                                                             basis.get("at"))
    elif actor.get("kind") == "session":
        by = last.get("by") if isinstance(last, dict) else None
        if isinstance(by, dict) and by:
            why = ("the last listing was by %s at %s, not this session — only the latest "
                   "listing is kept" % (_who(by), last.get("at")))
        else:
            why = "this session has not listed the queue"
    else:
        why = "nobody has listed the queue"
    if actor.get("kind") != "session":
        why += " (you have no session identity, so the last listing by anyone counts)"
    scope = " %s" % source if source else ""
    return "\n".join(
        ["refusing a blanket clear of profile %r%s: %d open event(s) were never shown to you by "
         "`just unactioned %s` — %s. Nothing changed."
         % (profile, " (source %r)" % source if source else "", total, profile, why)]
        + _lines(strays)
        + ["  Read them. Then clear exactly these by number (%s), or list and re-run: "
           "`just unactioned %s`, read it, THEN, as a separate command, `just acted %s%s`. "
           "To clear them unread, deliberately: `just acted-including-unseen %s%s`."
           % (", ".join("`%s`" % c for c in _clears(strays, profile)),
              profile, profile, scope, profile, scope)])


def render_footer(shown, profile, may_act=True):
    """A listing's LAST lines: what it showed, then the by-number clears for exactly that.

    Last, so a cut-down view (``| tail``) still shows
    how much was listed. Also, a blanket ``just acted`` chained onto the
    listing with ``&&`` clears an event that landed mid-turn before anyone read
    it; a clear that names numbers cannot — whatever lands later stays open. The
    listing shows every open event (it floors at ``acted_through``), so through
    the last one shown clears exactly what was shown. No clears for a session the
    seat refuses anyway (``may_act``).
    """
    total = sum(len(v) for v in shown.values())
    spans = []
    for name in sorted(shown):
        first, last = shown[name][0].index, shown[name][-1].index
        spans.append("%s #%d" % (name, first) if first == last else "%s #%d–#%d" % (name, first, last))
    out = ["shown: %d event(s) — %s." % (total, "; ".join(spans))]
    if may_act:
        out.append("Handle each one; THEN, as a separate command, clear exactly these by number:")
        out += ["  " + c for c in _clears(shown, profile)]
    return "\n".join(out)


def _short(sid):
    return (sid or "?")[:8]


def _who(actor):
    if actor.get("kind") == "session":
        return "session %s" % _short(actor.get("session_id"))
    return "operator %s@%s" % (actor.get("user", "?"), actor.get("tty", "?"))


def render_owner(cursor, contenders, actor, status, cstate="ok", path=None,
                 contenders_state="ok", contenders_file=None):
    """The ownership line(s): who owns the cursor, who is asking, who else runs the hook."""
    owner = cursor.get("owner") if isinstance(cursor.get("owner"), dict) else None
    out = []
    if cstate == "unreadable":
        out.append("owner: UNREADABLE — %s exists but cannot be read, so ownership is indeterminate; "
                   "remove or repair it; the next session to drain or act claims it" % path)
        return "\n".join(out)
    if owner:
        alive = stop_drain.owner_alive(owner)
        liveness = {True: "alive", False: "DEAD", None: "pid unknown"}[alive]
        out.append("owner: session %s (pid %s, %s) in %s — since %s" % (
            _short(owner.get("session_id")), owner.get("pid"), liveness,
            owner.get("cwd"), owner.get("since")))
    else:
        out.append("owner: none (unowned — the next session to drain or act claims it)")
    standing = {
        "owner": "this session (owner)",
        "foreign": "%s — NOT the owner; `just acted` will refuse" % _who(actor),
        "claimable": "%s — will claim on the next act" % _who(actor),
        "operator": "%s — may act, never owns" % _who(actor),
    }[status]
    out.append("you: " + standing)
    if contenders_state == "unreadable":
        # unreadable must not read as "nobody else is here".
        out.append("contenders: UNREADABLE — %s exists but cannot be read, so other sessions' "
                   "hook runs are unknown; remove or repair it" % contenders_file)
    for sid, c in sorted(contenders.items()):
        if owner and sid == owner.get("session_id"):
            continue
        # codex pass 2: a contender is a historical observation, not a live
        # conflict, unless its process is still there — reporting every entry
        # as "running" turned every resolved conflict into a permanent warning.
        alive = stop_drain.owner_alive(c)   # pid + boot + start, as for the owner (codex pass 6)
        if alive is True:
            state = "RUNNING this profile's hook now (not the owner)"
        elif alive is False:
            state = "ran this profile's hook earlier; its process is gone"
        else:
            state = "ran this profile's hook (pid unknown, so liveness unknown)"
        out.append("also: session %s in %s — %s; last %s, %s time(s)"
                   % (_short(sid), c.get("cwd"), state, c.get("last"), c.get("count")))
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Watchtower acted-through cursor")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--source", default=None, help="one source (default: all)")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--through", type=int, default=None,
                       help="event index to mark acted through (default: current tail — refused "
                            "while an open event was never shown to you by --list)")
    scope.add_argument("--all-including-unseen", action="store_true",
                       help="clear to the tail INCLUDING events no --list showed you: deliberate use")
    parser.add_argument("--list", action="store_true",
                        help="show unactioned and record that you saw them; moves no position")
    parser.add_argument("--take-over", action="store_true",
                        help="from a session: claim this profile's cursor from its current owner; "
                             "from a terminal: release it, owning nothing. Clears nothing either way")
    parser.add_argument("--log-dir", default=stop_drain.DEFAULT_LOG_DIR)
    parser.add_argument("--state-root", default=stop_drain.DEFAULT_STATE_ROOT)
    args = parser.parse_args(argv)

    profile = stop_drain.resolve_profile(args.profile, args.log_dir)
    if not profile:
        print("no profile: pass --profile or set WATCHTOWER_PROFILE", file=sys.stderr)
        return 2

    actor = stop_drain.actor_identity()

    if args.list:
        # The listing is RECORDED now (a follow-up), a read-modify-write of
        # acted.json, so it takes the lock like every other writer. A busy
        # profile still gets its listing — reading needs no lock, writes are
        # atomic replaces — it just goes unrecorded, and says so.
        try:
            with stop_drain.profile_lock(args.state_root, profile):
                return _list(args, profile, actor)
        except stop_drain.ProfileBusy as exc:
            return _list(args, profile, actor, busy=exc)

    try:
        with stop_drain.profile_lock(args.state_root, profile):
            return _act(args, profile, actor)
    except stop_drain.ProfileBusy as exc:
        print("profile %r: %s. Nothing changed — retry, or check the lock file."
              % (profile, exc), file=sys.stderr)
        return 2
    except stop_drain.CursorWriteFailed as exc:
        # codex pass 3: a write that did not happen must not read as success.
        print("profile %r: the cursor write FAILED (%s). Nothing changed — check the "
              "state directory." % (profile, exc), file=sys.stderr)
        return 2


def _list(args, profile, actor, busy=None):
    """``just unactioned``: print what is open, then RECORD what was printed (a follow-up).

    The record is what a later blanket ``just acted`` checks: it may clear only
    events a listing showed (``stop_drain.unseen``). Printing and recording come
    from ONE read of each log, so the record cannot claim an event the listing
    did not print. No position moves. Nothing is recorded by a FOREIGN session
    (another live session owns the seat: this one may not act, and never writes
    the seat's cursor), over an unreadable cursor (nothing may replace it), or
    without the lock — and an unrecorded listing makes a blanket act refuse,
    which is the safe side.
    """
    cursor, cstate = stop_drain.read_cursor(args.state_root, profile)
    status = stop_drain.ownership(cursor, actor)
    view = stop_drain.read_view(args.log_dir, profile, cursor)
    shown = _open(view)
    try:
        # Out of the process BEFORE it is recorded as shown (codex on f58b31c): into
        # a pipe, print only fills a buffer, and a reader that had gone would leave
        # these events recorded as seen by nobody. With stdout closed at start-up
        # (`>&-`) Python sets it to None and print() drops the listing silently.
        # A flush proves the bytes left, not that anyone read them: the docs say never to pipe or chain the listing.
        if sys.stdout is None:
            raise OSError("stdout is closed")
        print(render(shown, profile))
        sys.stdout.flush()
    except (OSError, ValueError) as exc:  # BrokenPipeError is an OSError
        print("listing NOT recorded: it could not be written out (%s), so nothing was shown "
              "and a blanket `just acted %s` will refuse these events." % (exc, profile),
              file=sys.stderr)
        return 2
    note = None
    if busy is not None:
        note = "profile %r: %s" % (profile, busy)
    elif cstate != "unreadable" and status != "foreign":
        stop_drain.record_listing(cursor, actor, view)
        if not stop_drain.save_cursor(args.state_root, profile, cursor):
            note = "profile %r: the cursor write failed" % profile
    contenders, contenders_state = stop_drain.load_contenders(args.state_root, profile)
    print(render_owner(cursor, contenders, actor, status, cstate,
                       stop_drain.cursor_path(args.state_root, profile),
                       contenders_state, stop_drain.contenders_path(args.state_root, profile)))
    if shown:
        print(render_footer(shown, profile, may_act=status != "foreign" and cstate != "unreadable"))
    if note:
        print("listing NOT recorded (%s): a blanket `just acted %s` will refuse what it shows "
              "until a listing is." % (note, profile), file=sys.stderr)
    return 0


def _act(args, profile, actor):
    """The mutating path, run under the profile lock.

    codex P2 on this branch: the ownership check, the claim and the advance
    must be one serialised transaction, or two sessions can both read
    "claimable", both claim, and both consume the same events.
    """
    cursor, cstate = stop_drain.read_cursor(args.state_root, profile)
    if cstate == "unreadable":
        print("profile %r: %s exists but cannot be read, so ownership is indeterminate. "
              "Nothing changed — remove or repair it; the next session to drain or act claims it."
              % (profile, stop_drain.cursor_path(args.state_root, profile)),
              file=sys.stderr)
        return 2
    status = stop_drain.ownership(cursor, actor)

    if args.take_over:
        previous = cursor.get("owner") if isinstance(cursor.get("owner"), dict) else None
        if status == "operator":
            # the human at the terminal is the actor every
            # recovery message addresses, and was the one actor refused here. An
            # operator take-over RELEASES: the owner is cleared and nothing is
            # claimed, so the next session to drain or act takes the seat —
            # "the operator never owns" still holds, and the unknown-liveness
            # holds have a way out that is not editing the file by hand.
            if not previous:
                print("profile %r is not owned; nothing to release. Nothing changed." % profile)
                return 0
            cursor["owner"] = None
            if not stop_drain.save_cursor(args.state_root, profile, cursor):
                print("profile %r: ownership NOT saved — the cursor write failed. Nothing changed."
                      % profile, file=sys.stderr)
                return 2
            print("ownership of profile %r RELEASED from session %s (pid %s, %s) by %s — nothing "
                  "claimed; the next session to drain or act takes it. Nothing cleared."
                  % (profile, _short(previous.get("session_id")), previous.get("pid"),
                     {True: "alive", False: "dead", None: "pid unknown"}[
                         stop_drain.owner_alive(previous)],
                     _who(actor)))
            return 0
        if not stop_drain.save_cursor(args.state_root, profile, stop_drain.claim(cursor, actor)):
            print("profile %r: ownership NOT saved — the cursor write failed. Nothing changed."
                  % profile, file=sys.stderr)
            return 2
        if previous:
            print("ownership of profile %r taken from session %s (pid %s, %s) by %s. "
                  "Nothing cleared — run `just unactioned %s`, then `just acted %s` to act."
                  % (profile, _short(previous.get("session_id")), previous.get("pid"),
                     {True: "alive", False: "dead", None: "pid unknown"}[
                         stop_drain.owner_alive(previous)],
                     _who(actor), profile, profile))
        else:
            print("ownership of profile %r claimed by %s. Nothing cleared." % (profile, _who(actor)))
        return 0

    if status == "foreign":
        owner = cursor["owner"]
        # `foreign` covers a live owner AND one whose
        # liveness is unknown — say which, as `render_owner` already does.
        liveness = {True: "alive", False: "dead", None: "pid unknown, liveness unknown"}[
            stop_drain.owner_alive(owner)]
        print("refusing: profile %r is owned by session %s (pid %s, %s) in %s since %s; "
              "you are %s. Nothing changed.\n  A second session on one seat is what the charter "
              "forbids. If that session is gone: `just takeover %s` (from a session: claims it; "
              "from a terminal: releases it)."
              % (profile, _short(owner.get("session_id")), owner.get("pid"), liveness,
                 owner.get("cwd"), owner.get("since"), _who(actor), profile), file=sys.stderr)
        return 2

    # ONE read of each log in scope (a follow-up): the overshoot and unseen
    # checks, the positions the act clears through, and the cleared list all come
    # from it — so a number is bound to the very read that validated it.
    view = stop_drain.read_view(args.log_dir, profile, cursor, args.source)
    if args.through is not None:
        # Refuse an overshoot rather than clamping silently: on the human
        # surface a fat-fingered `--through 133` that "worked" is how a
        # cursor ends up parked in the future, pre-marking events nobody
        # has seen. The library clamps for safety; here we say so.
        for name, v in view.items():
            if args.through > len(v.events):
                print("refusing --through %d for source %r: it has %d event(s). "
                      "Nothing changed." % (args.through, name, len(v.events)),
                      file=sys.stderr)
                return 2
    if args.through is None and not args.all_including_unseen:
        strays = stop_drain.unseen(cursor, actor, view)
        if strays:
            print(render_refusal(strays, profile, actor, stop_drain.listing_for(cursor, actor),
                                 source=args.source, last=cursor.get(stop_drain.LISTED_KEY)),
                  file=sys.stderr)
            return 2

    # The claim or pid refresh is applied IN MEMORY and persisted by the strict
    # act save below, together with the positions (codex pass 4): a failed write
    # then fails the whole act, rather than leaving positions consumed under a
    # missing or stale owner.
    if status == "claimable":
        stop_drain.claim(cursor, actor)
    elif status == "owner":
        stop_drain.refresh_owner(cursor, actor)

    # Every act is pinned to the view: each source clears through the tail the view
    # read — or through N of it — and only if the advance's own read is the same
    # generation of that log (``_advance``). An event landing in between was neither
    # checked nor printed, so it stays open; a log REPLACED in between clears nothing
    # (found in review, for both the blanket form and --through): positions read in the old
    # log would otherwise land on the new one's unseen events.
    if args.through is None:
        through = view
    else:
        through = {name: v._replace(events=v.events[:max(args.through, 0)])
                   for name, v in view.items()}
    stop_drain.mark_acted(args.log_dir, args.state_root, profile,
                          source=args.source, through=through,
                          by=actor, strict=True, cursor=cursor)
    cleared, replaced = {}, []
    for name, v in view.items():
        entry = (cursor.get("sources") or {}).get(name) or {}
        # The advance stored the identity of the log it read: another generation than
        # the view's means the view's events are not the ones the positions now name.
        if not stop_drain.same_generation(v, int(entry.get("bytes") or 0), entry.get("first")):
            replaced.append(name)
            continue
        now = int(entry.get("acted_through") or 0)
        gone = [e for e in v.events if v.acted < e.index <= now]
        if gone:
            cleared[name] = gone
    print("acted: %d event(s) cleared (profile=%s%s)"
          % (sum(len(v) for v in cleared.values()), profile,
             ", source=%s" % args.source if args.source else ""))
    for line in _lines(cleared):
        print(line)
    for name in replaced:
        print("refusing to clear source %r: its log was replaced after this act read it, so "
              "positions read in the old log would name other events in the new one. Nothing "
              "cleared for %r — `just unactioned %s` shows what the new log holds."
              % (name, name, profile), file=sys.stderr)
    after = stop_drain.report_unactioned(args.log_dir, args.state_root, profile)
    if after:
        print(render(after, profile))
    return 2 if replaced else 0


if __name__ == "__main__":
    status = main()
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
    except (OSError, ValueError):
        # The reader went away (Python's own SIGPIPE recipe): point stdout at /dev/null,
        # or the interpreter's flush at exit fails again and turns the status into 120.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        status = status or 2
    sys.exit(status)
