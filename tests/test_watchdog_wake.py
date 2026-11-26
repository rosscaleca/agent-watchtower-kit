#!/usr/bin/env python3
"""Completed watchdog cycles leave a host-scoped watermark.

Run the real loop with temporary state/config/log paths and no launchd calls.
The watermark records loop progress even when the checkout verdict is unknown.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daemons"))

import watchdog_wake as wd  # noqa: E402


class WatchdogCycleWatermark(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = os.path.join(tmp.name, "state", "watchdog")
        self.watermark = os.path.join(self.state, "cycle.json")
        self.episodes = os.path.join(self.state, "episodes.json")
        self.build = os.path.join(self.state, "build.json")
        config = os.path.join(tmp.name, "config.json")
        with open(config, "w") as fh:
            json.dump({"profiles": {"p": {}, "q": {}}}, fh)
        self.now = datetime(2026, 9, 14, 23, 40, tzinfo=timezone.utc)
        self.clock = mock.Mock(wraps=datetime)
        self.clock.now.return_value = self.now
        self.notes = mock.Mock()
        self.checkout = mock.Mock(return_value={"state": "unknown"})
        patches = [
            mock.patch.multiple(wd, STATE_ROOT=os.path.dirname(self.state),
                                OWN_STATE=self.episodes, OWN_BUILD=self.build,
                                CONFIG_PATH=config, LOG_DIR=os.path.join(tmp.name, "logs"),
                                datetime=self.clock, note=self.notes,
                                checkout_status=self.checkout),
            mock.patch.object(wd, "STATE_DIR", self.state),
            mock.patch.object(wd, "loaded_labels", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _read_watermark(self):
        if not os.path.exists(self.watermark):
            return None
        with open(self.watermark) as fh:
            return json.load(fh)

    def _cycles(self, count):
        snapshots = []

        def sleep(seconds):
            self.assertEqual(seconds, wd.POLL_SECS)
            snapshots.append(self._read_watermark())
            if len(snapshots) == count:
                raise KeyboardInterrupt
            self.clock.now.return_value += timedelta(seconds=wd.POLL_SECS)

        with mock.patch.object(wd.time, "sleep", side_effect=sleep):
            with self.assertRaises(KeyboardInterrupt):
                wd.main()
        return snapshots

    def test_completed_cycle_writes_one_host_watermark_after_all_profiles(self):
        os.makedirs(self.state)
        episodes = '{"upstream:p/checkout": "abc1234"}\n'
        with open(self.episodes, "w") as fh:
            fh.write(episodes)
        completed = self.now + timedelta(seconds=5)

        def checkout():
            self.assertFalse(os.path.exists(self.watermark), "cycle is still in progress")
            self.clock.now.return_value = completed
            return {"state": "unknown"}

        self.checkout.side_effect = checkout
        with mock.patch.object(wd.os, "replace", wraps=os.replace) as replace:
            snapshots = self._cycles(1)
        self.assertEqual(snapshots, [{"since": completed.isoformat()}])
        self.assertEqual(self.checkout.call_count, 2)
        self.assertEqual([call.args[1] for call in replace.call_args_list],
                         [self.build, self.watermark])
        self.assertFalse(os.path.exists(self.watermark + ".tmp"))
        with open(self.episodes) as fh:
            self.assertEqual(fh.read(), episodes, "unknown must leave the open episode intact")
        with open(self.build) as fh:
            self.assertEqual(json.load(fh), {"build": wd.BUILD, "started": self.now.strftime("%Y-%m-%dT%H:%M:%SZ")})

    def test_watermark_advances_on_the_next_cycle(self):
        snapshots = self._cycles(2)
        expected = [self.now, self.now + timedelta(seconds=wd.POLL_SECS)]
        self.assertEqual(snapshots, [{"since": when.isoformat()} for when in expected])
        first, second = [datetime.fromisoformat(state["since"]) for state in snapshots]
        self.assertEqual(first.utcoffset(), timedelta(0))
        self.assertGreater(second, first)
        self.assertEqual(self.checkout.call_count, 4)

    def test_failed_write_preserves_watermark_and_loop_retries(self):
        os.makedirs(self.state)
        previous = {"since": (self.now - timedelta(seconds=wd.POLL_SECS)).isoformat()}
        with open(self.watermark, "w") as fh:
            json.dump(previous, fh)
        replace = os.replace
        attempts = []

        def fail_first_watermark(src, dst):
            if dst == self.watermark:
                attempts.append(dst)
                if len(attempts) == 1:
                    raise OSError("watermark replace denied")
            return replace(src, dst)

        with mock.patch.object(wd.os, "replace", side_effect=fail_first_watermark):
            snapshots = self._cycles(2)
        self.assertEqual(len(attempts), 2, "the loop must retry the failed watermark on its next cycle")
        self.assertEqual(snapshots, [previous, {
            "since": (self.now + timedelta(seconds=wd.POLL_SECS)).isoformat()}])
        self.assertEqual(self.checkout.call_count, 4, "both cycles must poll both profiles")
        self.assertTrue(any("watermark replace denied" in call.args[0]
                            for call in self.notes.call_args_list), "the write failure must be logged")


if __name__ == "__main__":
    unittest.main()
