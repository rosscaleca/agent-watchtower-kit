"""The audit hook behind `just test-audit` — exercised DIRECTLY, with no real
write anywhere: a hook that misses a write class is a green light over a leak."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import live_write_audit as lwa  # noqa: E402  (the tests dir is the discovery root)


class AuditHook(unittest.TestCase):
    LIVE_LOG = os.path.join(lwa.LIVE[1], "acme-stalled.log")
    LIVE_STATE = os.path.join(lwa.LIVE[0], "stalled", "acme.json")

    def setUp(self):
        lwa.HITS.clear()

    def tearDown(self):
        lwa.HITS.clear()

    def test_a_low_level_truncating_open_of_a_live_path_is_a_hit(self):
        """A review found: `os.open(path, O_WRONLY | O_TRUNC)` raises the
        `open` event with mode=None — the string-mode check called it a read."""
        lwa._hook("open", (self.LIVE_LOG, None, os.O_WRONLY | os.O_TRUNC))
        self.assertEqual([h[0:2] for h in lwa.HITS], [("open", self.LIVE_LOG)], lwa.HITS)

    def test_a_string_mode_write_is_a_hit_and_a_read_is_not(self):
        lwa._hook("open", (self.LIVE_STATE, "a", os.O_WRONLY | os.O_CREAT | os.O_APPEND))
        self.assertEqual(len(lwa.HITS), 1)
        lwa._hook("open", (self.LIVE_STATE, "r", os.O_RDONLY))
        lwa._hook("open", (self.LIVE_STATE, None, os.O_RDONLY))
        self.assertEqual(len(lwa.HITS), 1, "reads are not writes")

    def test_a_rename_or_removal_inside_the_live_directories_is_a_hit(self):
        lwa._hook("os.rename", ("/tmp/acme.json.tmp", self.LIVE_STATE, None, None))
        lwa._hook("os.remove", (self.LIVE_LOG, None))
        self.assertEqual([h[0] for h in lwa.HITS], ["os.rename", "os.remove"], lwa.HITS)

    def test_writes_outside_the_live_directories_are_not_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            lwa._hook("open", (os.path.join(tmp, "x.log"), "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC))
            lwa._hook("open", (os.path.join(tmp, "x.log"), None, os.O_WRONLY | os.O_TRUNC))
            lwa._hook("os.rename", (os.path.join(tmp, "a"), os.path.join(tmp, "b"), None, None))
        self.assertEqual(lwa.HITS, [])

    def test_a_descriptor_open_event_is_ignored_not_crashed(self):
        lwa._hook("open", (4, "a", os.O_WRONLY))
        lwa._hook("open", ())
        self.assertEqual(lwa.HITS, [])

    def test_a_real_write_through_the_daemons_own_paths_trips_the_installed_hook(self):
        """The positive control (a review found): every test above feeds
        `_hook` a synthetic tuple, so a hook that never fires would pass them all.
        This one installs the REAL hook in a child process (audit hooks cannot be
        removed, hence the child), points LIVE at a temp tree, and writes through
        the daemons' own `emit` (open "a") and `save_state` (open "w" + os.replace).

        the child's tree is the parent's TemporaryDirectory, removed when the test
        ends. The child made its own with `mkdtemp()` and nothing removed it, so every suite
        run left one directory under TMPDIR. Its TMPDIR is an empty directory of the parent's,
        and must still be empty when the child has exited."""
        here = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as tmp:
            live, spare = os.path.join(tmp, "live"), os.path.join(tmp, "tmpdir")
            os.mkdir(live)
            os.mkdir(spare)
            code = r"""
import json, os, sys
sys.path.insert(0, %(daemons)r); sys.path.insert(0, %(tests)r)
import live_write_audit as lwa, github_wake as gw, stalled_wake as sw
tmp = %(live)r
lwa.LIVE = (tmp,)
gw.LOG_DIR = os.path.join(tmp, "logs"); sw.STATE_DIR = os.path.join(tmp, "state")
lwa.install()
gw.emit("probe", "type=probe")
sw.save_state("probe", {"since": "x", "stalled": {}, "degraded": {}, "waiting": {}, "pair_pending": []})
print(json.dumps(sorted({(h[0], os.path.basename(h[1])) for h in lwa.HITS})))
""" % {"daemons": os.path.join(os.path.dirname(here), "daemons"), "tests": here, "live": live}
            run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                 env=dict(os.environ, TMPDIR=spare))
            left = sorted(os.listdir(spare))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(left, [], "the child left this behind in its TMPDIR")
        hits = [tuple(h) for h in json.loads(run.stdout)]
        self.assertIn(("open", "probe-github.log"), hits, hits)
        self.assertIn(("open", "probe.json.tmp"), hits, hits)
        self.assertIn(("os.rename", "probe.json"), hits, hits)


if __name__ == "__main__":
    unittest.main()
