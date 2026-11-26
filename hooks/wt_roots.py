#!/usr/bin/env python3
"""Show, set or clear a profile's SESSION ROOTS — the only project roots whose Claude Code
sessions its Stop hook serves on this host (``stop_drain.session_inside``).

Why: where the hook is registered globally, every session on the host runs it, and while the
seat's own session is dead the first unrelated session to end a turn claims the cursor, then
surfaces and clears the seat's events — a side conversation in another project did exactly
that on 2026-09-19. With roots set, any other session's hook does nothing at all.

Usage::

    wt_roots.py --profile acme                                  # show
    wt_roots.py --profile acme ~/Development/acme [...]   # set (replaces)
    wt_roots.py --profile acme --clear                          # remove

Opt-in and per host: the setting lives beside the cursor, in
``<state-root>/<profile>/session-roots.json``, and with none the hook behaves exactly as
before. Each root must be an ABSOLUTE path to an existing directory; it is stored
realpath-normalized. A relative root is refused: it resolves
against wherever the command runs, and ``just`` runs recipes from the Watchtower checkout,
so ``.`` stored the checkout and silenced the seat. An unreadable setting is IGNORED by the
hook (fails open) — shown here as such, never silent.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import stop_drain


def _who(actor):
    if not isinstance(actor, dict):
        return "?"
    if actor.get("kind") == "session":
        return "session %s" % (actor.get("session_id") or "?")[:8]
    return "operator %s@%s" % (actor.get("user", "?"), actor.get("tty", "?"))


def show(state_root, profile):
    roots, state, why = stop_drain.load_session_roots(state_root, profile)
    path = stop_drain.roots_path(state_root, profile)
    if state == "missing":
        print("session roots (profile=%s): not set — the Stop hook serves every session that "
              "runs it." % profile)
        return roots
    if state == "unreadable":
        print("session roots (profile=%s): UNREADABLE — %s (%s). The hook ignores it and serves "
              "every session (fails open). Fix: `just session-roots %s <absolute root> ...`, or "
              "`--clear`."
              % (profile, path, why, profile))
        return []
    try:
        with open(path) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = {}
    print("session roots (profile=%s), set %s by %s:" % (profile, meta.get("at", "?"), _who(meta.get("by"))))
    statuses = [stop_drain.root_status(r) for r in roots]   # the hook's own test, shared
    for root, (problem, doubtful) in zip(roots, statuses):
        if problem is None:
            print("  %s" % root)
        elif doubtful:  # it may hold sessions: the hook cannot call anyone "outside" for sure
            print("  %s   (%s: while it is, any session not inside the other roots is served)"
                  % (root, problem))
        else:
            print("  %s   (%s: ignored)" % (root, problem))
    if all(problem for problem, _doubtful in statuses):
        print("None of these is a usable directory, so the hook ignores the setting (fails open).")
    else:
        print("The Stop hook serves only sessions started in, or working in, one of these; any "
              "other session's hook does nothing.")
    return roots


def main(argv=None):
    parser = argparse.ArgumentParser(description="Watchtower session roots")
    parser.add_argument("--profile", required=True)
    parser.add_argument("roots", nargs="*", help="set these (replacing any); none: show")
    parser.add_argument("--clear", action="store_true", help="remove the setting")
    parser.add_argument("--state-root", default=stop_drain.DEFAULT_STATE_ROOT)
    args = parser.parse_args(argv)
    if args.clear and args.roots:
        parser.error("--clear takes no roots")
    path = stop_drain.roots_path(args.state_root, args.profile)

    if args.clear:
        try:
            if not os.path.lexists(path):   # a dangling symlink is still there to remove
                raise FileNotFoundError(path)
            with stop_drain.profile_lock(args.state_root, args.profile):
                os.remove(path)
        except FileNotFoundError:
            print("session roots (profile=%s): not set — nothing to clear." % args.profile)
            return 0
        except stop_drain.ProfileBusy as exc:
            print("profile %r: %s. Nothing changed — retry." % (args.profile, exc), file=sys.stderr)
            return 2
        except OSError as exc:
            print("profile %r: %s could not be removed (%s). Nothing changed."
                  % (args.profile, path, exc), file=sys.stderr)
            return 2
        print("session roots (profile=%s) cleared — the Stop hook serves every session that runs "
              "it again." % args.profile)
        return 0

    if not args.roots:
        show(args.state_root, args.profile)
        return 0

    resolved = []
    for root in args.roots:
        try:
            expanded = os.path.expanduser(root)
        except (OSError, ValueError):  # "~user" with a NUL raises from the user lookup
            expanded = None
        if expanded is not None and not os.path.isabs(expanded):
            print("refusing: %r is a relative path. A relative root resolves against wherever this "
                  "command runs, which is not necessarily your project: pass an absolute path. "
                  "Nothing changed." % root, file=sys.stderr)
            return 2
        try:
            real = os.path.realpath(expanded) if expanded is not None else None
        except (OSError, ValueError):  # Python 3.14's realpath raises for a NUL / unencodable
            real = None
        # Never store what the hook's loader would reject (stop_drain._is_path_string).
        if real is None or not stop_drain._is_path_string(real) or not os.path.isdir(real):
            print("refusing: %r is not an existing directory. Nothing changed." % root,
                  file=sys.stderr)
            return 2
        if real not in resolved:
            resolved.append(real)
    data = {"version": 1, "roots": resolved, "by": stop_drain.actor_identity(),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        # One writer at a time, under the profile lock, as acted.json and contenders.json are
        # written (codex on d12809a): two overlapping runs shared this temp file, and one
        # could write into the file the other had just published.
        with stop_drain.profile_lock(args.state_root, args.profile):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
            os.replace(tmp, path)
    except stop_drain.ProfileBusy as exc:
        print("profile %r: %s. Nothing changed — retry." % (args.profile, exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("profile %r: session roots NOT saved (%s). Nothing changed." % (args.profile, exc),
              file=sys.stderr)
        return 2
    show(args.state_root, args.profile)

    # The cursor's owner is served only from inside. Its recorded cwd is where its LAST action
    # ran, a hook run or a `just` command, and its project root,
    # which also counts, is not recorded, so this can say "outside" but never "still served"
    # (note 2). After a `just acted` from the Watchtower checkout the recorded cwd IS the
    # checkout, so this fires even for the right roots (second review, note 2). Its advice
    # must therefore be safe when the roots are right: the dangerous "fix" is storing the
    # directory it names, which replaces the setting and silences the seat.
    cursor, cstate = stop_drain.read_cursor(args.state_root, args.profile)
    owner = cursor.get("owner") if cstate == "ok" and isinstance(cursor.get("owner"), dict) else None
    if owner and isinstance(owner.get("cwd"), str) and owner["cwd"] and stop_drain.session_inside(
            {"cwd": owner["cwd"]}, resolved, environ={}) is False:   # unknown is not "outside"
        print("note: the cursor's owner (%s) was last recorded in %s, by its last hook run or "
              "`just` command, and that is outside these roots. This check cannot see the owner's "
              "project root, so it fires even when the roots are right, after a `just` command run "
              "from the Watchtower checkout. Do not store that directory unless it is the seat's project root, the directory its session started in. Confirm that the seat's "
              "next wake still blocks; change the roots only if it does not. If the owner is another "
              "project's session, reclaim the cursor from the seat with `just takeover %s`."
              % (_who(dict(owner, kind="session")), owner["cwd"], args.profile))
    # And the session running this command: a seat that stores roots leaving itself outside
    # has just silenced its own drain. Its cwd is the caller's (the recipe is [no-cd]). Its
    # project root is known only from CLAUDE_PROJECT_DIR, which a Bash tool does NOT have
    #: without it only the cwd is checked, and the
    # usual cwd, the Watchtower checkout, is outside the right roots. So the definite warning
    # needs the project root; without it, a hedged note that warns off the dangerous "fix".
    if data["by"].get("kind") == "session" and stop_drain.session_inside({}, resolved) is False:
        try:
            here = os.getcwd()
        except OSError:
            here = "unknown"
        project = os.environ.get("CLAUDE_PROJECT_DIR")
        if project:
            print("warning: this session is outside these roots (cwd %s; project root %s), so its "
                  "own Stop hook now does nothing for profile %s. If this session is the seat, fix "
                  "the roots now." % (here, project, args.profile))
        else:
            print("note: this command ran in %s, which is outside these roots. It cannot see the "
                  "session's project root: if the seat started inside one of these roots, its hook "
                  "is still served. Do not store this directory unless it is the seat's project root, the directory its session started in. Confirm that the seat's next wake "
                  "still blocks." % here)
    return 0


if __name__ == "__main__":
    sys.exit(main())
