"""Scaffolding shared by the seen-memory tests of the polling daemons.

A daemon's dedup memory only shows its faults across MANY cycles, a save and a
load. These tests therefore run the daemon's own ``poll_profile`` against a
miniature of its source, with the REAL ``load_state`` / ``save_state`` on a
private directory, and a simulated clock so that an item ages out of the
daemon's query window exactly as it does in life (one poll = one simulated
minute), with no real waiting.

Nothing here touches the network, the real clock or the live Watchtower
directories: ``STATE_DIR`` and ``LOG_DIR`` point at a throwaway tree, ``emit``
and ``note`` are captured, and the suite's live-write audit fails the run if a
test reaches ``~/.local/state/watchtower`` or ``~/Library/Logs/watchtower``.

Not a test module (no ``test_`` prefix), so discovery does not collect it; the
test files import it because the tests directory is the discovery root.
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

#: Where the simulated clock starts. Not the real clock, and nothing depends on it being
#: near it: a daemon computes every cutoff from the state's own watermark.
T0 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock(object):
    """Stands in for the ``datetime`` NAME inside a daemon module.

    A daemon reads the time in three ways: ``datetime.now(tz)``, for the cycle's start;
    ``datetime.fromisoformat(text)``, to read a timestamp; and (inbox) ``datetime.min``. The
    clock serves the first from the test and passes the other two to the real class.
    """

    min = datetime.min

    def __init__(self, start=T0):
        self.current = start

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)

    def now(self, tz=None):
        return self.current if tz is None else self.current.astimezone(tz)

    def fromisoformat(self, text):
        return datetime.fromisoformat(text)


class Poller(object):
    """``poll_profile`` of one daemon module against a simulated source.

    ``source`` maps the module attributes that reach the outside world to their
    simulations, for example ``{"Jira": hub}`` or ``{"bkt_api": hub}``. Every poll
    ends in the daemon's own save, so ``load()`` afterwards is exactly what a restart does.
    """

    def __init__(self, case, mod, cfg, source, clock=None, profile="acme"):
        self.mod, self.cfg, self.profile = mod, cfg, profile
        self.clock = clock or FakeClock()
        self.emitted, self.notes = [], []
        tmp = tempfile.mkdtemp()
        case.addCleanup(shutil.rmtree, tmp, True)
        self.state_dir = os.path.join(tmp, "state")
        self.log_dir = os.path.join(tmp, "logs")
        self.config_path = os.path.join(tmp, "config.json")
        patches = [mock.patch.object(mod, "STATE_DIR", self.state_dir),
                   mock.patch.object(mod, "LOG_DIR", self.log_dir),
                   mock.patch.object(mod, "emit", lambda p, line: self.emitted.append(line)),
                   mock.patch.object(mod, "note", self.notes.append),
                   mock.patch.object(mod, "datetime", self.clock)]
        patches += [mock.patch.object(mod, name, value) for name, value in source.items()]
        for patch in patches:
            patch.start()
            case.addCleanup(patch.stop)

    @property
    def state_path(self):
        return os.path.join(self.state_dir, "%s.json" % self.profile)

    def load(self):
        return self.mod.load_state(self.profile)

    def saved(self):
        with open(self.state_path) as fh:
            return json.load(fh)

    def write(self, raw):
        """Put ``raw`` on disk as the state file (a file the previous code wrote, say)."""
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.state_path, "w") as fh:
            json.dump(raw, fh)

    def poll(self, state, advance=True):
        """One cycle at the simulated ``now``; the profile-log lines it emitted. The clock then
        moves on by one poll interval, so the next thing the test does happens a poll later."""
        del self.emitted[:]
        self.mod.poll_profile(self.profile, self.cfg, state)
        if advance:
            self.clock.advance(self.mod.POLL_SECS)
        return list(self.emitted)

    def write_config(self, config):
        """A config file in the private tree (the real one is never read), and its path."""
        with open(self.config_path, "w") as fh:
            json.dump(config, fh)
        return self.config_path
