#!/usr/bin/env python3
"""Every daemon's ``emit`` writes its one line and raises nothing when the line holds a lone
surrogate (U+D800..U+DFFF), which UTF-8 cannot encode.

A Jira field, a mail subject or a GitHub title reaches a line as text decoded from JSON, and JSON can
carry an escaped lone surrogate. ``github_wake``, ``bitbucket_wake`` and ``stalled_wake`` opened their log with ``errors="replace"``; ``jira_wake``, ``inbox_wake``, ``schedule_wake`` and
``watchdog_wake`` did not, so the write raised ``UnicodeEncodeError`` and the line was lost. This test
holds every daemon to the rule, the ones that already kept it included, so a new daemon cannot drop it.
"""
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

DAEMONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons")
sys.path.insert(0, DAEMONS)

#: (module, the log file its emit writes, as "<profile>-<name>.log").
EMITTERS = (
    ("github_wake", "github"),
    ("bitbucket_wake", "bitbucket"),
    ("jira_wake", "jira"),
    ("inbox_wake", "inbox"),
    ("stalled_wake", "stalled"),
    ("watchdog_wake", "watchdog"),
    ("schedule_wake", "schedule"),
)


def _defines_emit(name):
    with open(os.path.join(DAEMONS, name), encoding="utf-8") as fh:
        return "def emit(" in fh.read()


class EveryEmitSurvivesALoneSurrogate(unittest.TestCase):

    def test_the_list_names_every_daemon_that_writes_a_log(self):
        """A writer is found by a ``*_wake.py`` file that defines ``def emit(``. A daemon that writes its log
        under another name, or from a file not named ``*_wake.py``, is not found by this guard."""
        writers = sorted(f[:-3] for f in os.listdir(DAEMONS) if f.endswith("_wake.py") and _defines_emit(f))
        self.assertEqual(writers, sorted(m for m, _ in EMITTERS))

    def test_each_emit_writes_one_line_with_a_replacement_and_raises_nothing(self):
        for module, name in EMITTERS:
            with self.subTest(module=module):
                mod = importlib.import_module(module)
                with tempfile.TemporaryDirectory() as tmp:
                    logs = os.path.join(tmp, "logs")
                    with mock.patch.object(mod, "LOG_DIR", logs):
                        mod.emit("p", "type=x by=\"A\" | bad \ud800 text")
                        mod.emit("p", "type=y | plain")
                    with open(os.path.join(logs, "p-%s.log" % name), "rb") as fh:
                        lines = fh.read().decode("utf-8").splitlines()
                self.assertEqual(len(lines), 2, lines)
                self.assertTrue(lines[0].startswith('type=x by="A" | bad ? text ts='), lines[0])
                self.assertTrue(lines[1].startswith("type=y | plain ts="), lines[1])


class EveryNoteSurvivesALoneSurrogate(unittest.TestCase):
    """Every daemon's ``note`` prints its line, or the ``ascii()`` of it, and raises nothing when
    the stream cannot encode it (a lone surrogate on a strict UTF-8 stdout, as ``env -i /usr/bin/python3``
    gives), as ``github_wake.note`` does."""

    def test_each_note_prints_and_raises_nothing(self):
        import io
        for module, _ in EMITTERS:
            with self.subTest(module=module):
                mod = importlib.import_module(module)
                raw = io.BytesIO()
                out = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="\n")
                with mock.patch.object(sys, "stdout", out):
                    mod.note("fine")
                    mod.note("bad \ud800 line")
                    out.flush()
                lines = raw.getvalue().decode("utf-8").splitlines()
                self.assertEqual(lines, ["fine", "'bad \\ud800 line'"])


if __name__ == "__main__":
    unittest.main()
