"""A box runs one bot. The 2026-11-22 double-reply outage is the test.

Five `start.sh` trees and two `bot.py` processes shared one token, so every
message got two replies. `single_instance.claim` is what stops that; these
tests hold the two properties that matter: a second claim fails while the
first is alive, and the lock is released the moment the holder goes away (no
pidfile staleness, no manual cleanup after a crash).
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import single_instance


class ClaimTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="lock-")
        self.path = os.path.join(d, "bot.lock")
        self.addCleanup(lambda: [os.unlink(p) for p in
                                 (self.path,)
                                 if os.path.exists(p)])

    def test_first_claim_wins_and_records_its_pid(self):
        fh = single_instance.claim(self.path)
        self.addCleanup(fh.close)
        self.assertEqual(single_instance.holder(self.path), str(os.getpid()))

    def test_a_second_claim_in_the_same_process_is_refused(self):
        fh = single_instance.claim(self.path)
        self.addCleanup(fh.close)
        with self.assertRaises(single_instance.AlreadyRunning) as cm:
            single_instance.claim(self.path)
        self.assertEqual(cm.exception.pid, str(os.getpid()))

    def test_the_lock_is_released_when_the_holder_goes_away(self):
        fh = single_instance.claim(self.path)
        fh.close()                                  # a crash looks like this
        again = single_instance.claim(self.path)    # must not need cleanup
        again.close()

    def test_another_live_process_blocks_the_claim(self):
        """The real case: a second supervisor spawned its own bot."""
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})
            sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
            import single_instance
            hold = single_instance.claim({self.path!r})   # keep the handle: the
            print("held", flush=True)                     # lock dies with it
            time.sleep(30)
        """)
        child = subprocess.Popen([sys.executable, "-c", code],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            with self.assertRaises(single_instance.AlreadyRunning):
                single_instance.claim(self.path)
        finally:
            child.kill()
            child.wait()
            child.stdout.close()
        # and when that process dies, the lock dies with it
        again = single_instance.claim(self.path)
        again.close()


class BotRefusesTests(unittest.TestCase):
    """bot.claim_singleton must exit 3 (the supervisor's 'someone else is
    driving' code) rather than connect a second session."""

    def test_second_instance_exits_three(self):
        import bot
        d = tempfile.mkdtemp(prefix="lock-")
        path = os.path.join(d, "bot.lock")
        held = single_instance.claim(path)
        self.addCleanup(held.close)
        old = os.environ.get("BOT_LOCK_PATH")
        os.environ["BOT_LOCK_PATH"] = path
        try:
            with self.assertRaises(SystemExit) as cm:
                bot.claim_singleton()
            self.assertEqual(cm.exception.code, 3)
        finally:
            if old is None:
                os.environ.pop("BOT_LOCK_PATH", None)
            else:
                os.environ["BOT_LOCK_PATH"] = old

    def test_first_instance_is_allowed(self):
        import bot
        d = tempfile.mkdtemp(prefix="lock-")
        old = os.environ.get("BOT_LOCK_PATH")
        os.environ["BOT_LOCK_PATH"] = os.path.join(d, "bot.lock")
        try:
            bot.claim_singleton()          # no SystemExit
            self.assertIsNotNone(bot._LOCK_HANDLE)
        finally:
            if bot._LOCK_HANDLE is not None:
                bot._LOCK_HANDLE.close()
                bot._LOCK_HANDLE = None
            if old is None:
                os.environ.pop("BOT_LOCK_PATH", None)
            else:
                os.environ["BOT_LOCK_PATH"] = old


if __name__ == "__main__":
    unittest.main()
