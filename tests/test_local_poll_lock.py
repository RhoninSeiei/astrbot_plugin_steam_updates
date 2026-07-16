import asyncio
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_free_games import _load_module


class LocalPollLockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _plugin(self):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._data_dir = Path(
            "/tmp/astrbot_plugin_steam_updates_tests"
        )
        plugin._poll_lock_path = (
            plugin._data_dir / ".poll.lock"
        )
        plugin._poll_lock_fd = None
        plugin._poll_lock_owner = False
        plugin._poll_instance_token = ""
        plugin._log_warn = lambda *args, **kwargs: None
        plugin._log_debug = lambda *args, **kwargs: None
        return plugin

    def test_release_orphan_poll_lock_fds_closes_matching_fd(self):
        plugin = self._plugin()
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin._poll_lock_path = (
                Path(temp_dir) / ".poll.lock"
            )
            fd = os.open(
                plugin._poll_lock_path,
                os.O_RDWR | os.O_CREAT,
                0o644,
            )
            try:
                closed = plugin._release_orphan_poll_lock_fds()
                self.assertEqual(closed, 1)
                with self.assertRaises(OSError):
                    os.fstat(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_cancel_legacy_tasks_skips_self_and_releases_owner(self):
        plugin = self._plugin()
        plugin._stop_event = asyncio.Event()
        released = {"lock": 0, "claim": 0}

        legacy = types.SimpleNamespace(
            _data_dir=plugin._data_dir,
            _stop_event=asyncio.Event(),
            _release_poll_lock=lambda: released.__setitem__(
                "lock",
                released["lock"] + 1,
            ),
            _release_poll_instance_claim=lambda: released.__setitem__(
                "claim",
                released["claim"] + 1,
            ),
        )

        class Coro:
            def __init__(self, owner):
                self.__qualname__ = "SteamUpdatePush._poll_loop"
                self.cr_frame = types.SimpleNamespace(
                    f_locals={"self": owner}
                )

        class Task:
            def __init__(self, owner):
                self.coro = Coro(owner)
                self.cancelled = False

            def get_coro(self):
                return self.coro

            def cancel(self):
                self.cancelled = True

        current_task = Task(plugin)
        legacy_task = Task(legacy)
        with patch.object(
            self.mod.asyncio,
            "all_tasks",
            return_value={current_task, legacy_task},
        ):
            cancelled = plugin._cancel_legacy_poll_tasks()

        self.assertEqual(cancelled, 1)
        self.assertFalse(current_task.cancelled)
        self.assertTrue(legacy_task.cancelled)
        self.assertTrue(legacy._stop_event.is_set())
        self.assertEqual(released, {"lock": 1, "claim": 1})

    def test_poll_loop_releases_lock_after_poll_exception(self):
        plugin = self._plugin()
        plugin._stop_event = asyncio.Event()
        plugin._is_current_poll_instance = lambda: True
        plugin._next_poll_time = lambda now: now
        plugin._try_acquire_poll_lock = lambda: True
        calls = {"release": 0}
        plugin._release_poll_lock = (
            lambda:
            calls.__setitem__(
                "release",
                calls["release"] + 1,
            )
        )

        async def fail_once():
            plugin._stop_event.set()
            raise RuntimeError("poll failed")

        plugin._poll_once = fail_once

        asyncio.run(plugin._poll_loop())

        self.assertEqual(calls["release"], 1)


if __name__ == "__main__":
    unittest.main()
