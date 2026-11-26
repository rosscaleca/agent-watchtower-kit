#!/usr/bin/env python3
"""Run the unit suite under an audit hook and FAIL if any test touched the live
watchtower state or logs (``~/.local/state/watchtower``, ``~/Library/Logs/watchtower``).

Why: on 2026-09-10 a transiently mis-indexed patch list let one test append six
fixture lines to the live ``acme-stalled.log``; the Stop-hook drain then
surfaced them to a real session. The suite was green. This is the check that would
have been red. Run it in the SAME interpreter and PATH as the suite you trust —
a leak can depend on both (``just test-audit``; also
``PATH=/usr/bin:/bin /usr/bin/python3 tests/live_write_audit.py``).

Exit 0: no writes and the suite passed. 2: live writes (listed). 1: test failures.

`just test` runs the suite THROUGH this runner (a review found: an
opt-in audit would not have caught the incident either — the command everyone
runs has to be the one that looks). `-v` for per-test output.
"""
import os
import sys
import unittest
import warnings

LIVE = (os.path.expanduser("~/.local/state/watchtower"),
        os.path.expanduser("~/Library/Logs/watchtower"))
HITS = []


#: the intent bits of an `open` event's flags — `os.open(path, O_WRONLY | O_TRUNC)`
#: raises the event with mode=None (a review found this): a string-mode
#: check read that truncation as a read.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _is_path(x):
    return isinstance(x, (str, bytes, os.PathLike))


def _live(path):
    p = os.path.abspath(os.path.expanduser(os.fsdecode(os.fspath(path))))
    return any(p == d or p.startswith(d + os.sep) for d in LIVE)


def _writes(mode, flags):
    """Does this open intend to write? By string mode OR by flags — whichever the
    event carries (``open()`` carries both, ``os.open()`` only the flags)."""
    by_mode = isinstance(mode, str) and bool(set(mode) & set("wax+"))
    by_flags = isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
    return by_mode or by_flags


def _hook(event, args):
    try:
        if event == "open":
            path = args[0] if args else None
            if not _is_path(path) or not _live(path):      # a descriptor re-open names no path
                return
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            if _writes(mode, flags):
                HITS.append((event, os.fsdecode(os.fspath(path)),
                             mode if isinstance(mode, str) else "flags=%#x" % flags))
        elif event in ("os.rename", "os.remove", "os.mkdir", "os.rmdir", "os.symlink", "os.link",
                       "os.truncate", "shutil.rmtree", "shutil.move", "shutil.copyfile"):
            for a in args:
                if _is_path(a) and _live(a):
                    HITS.append((event, os.fsdecode(os.fspath(a)), ""))
    except Exception:
        pass


def install():
    """Arm the hook for the rest of this process (audit hooks cannot be removed)."""
    sys.addaudithook(_hook)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    warnings.simplefilter("error", ResourceWarning)   # as `just test` always did
    install()
    tests = os.path.dirname(os.path.abspath(__file__))
    suite = unittest.TestLoader().discover(tests)
    result = unittest.TextTestRunner(verbosity=2 if "-v" in argv else 0).run(suite)
    print("tests run: %d  failures: %d  errors: %d"
          % (result.testsRun, len(result.failures), len(result.errors)))
    print("WRITES TOUCHING LIVE WATCHTOWER STATE OR LOGS: %d" % len(HITS))
    for hit in HITS[:40]:
        print("   ", hit)
    if HITS:
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
