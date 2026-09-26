import asyncio
import fcntl
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_free_games


class PollLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = test_free_games._load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def plugin(self, root=None):
        root = root or self.root
        with patch.object(self.mod.StarTools, "get_data_dir", return_value=root):
            plugin = self.mod.SteamUpdatePush(None, {})
        async def no_http():
            pass
        plugin._ensure_http_client = no_http
        return plugin

    def assert_available(self, path):
        with path.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def test_cancel_running_poll_releases_real_lock(self):
        old = self.plugin()
        old._claim_poll_instance()
        entered = asyncio.Event()
        async def poll():
            entered.set()
            await asyncio.Event().wait()
        old._poll_once = poll
        old._next_poll_time = lambda now: now
        task = asyncio.create_task(old._poll_loop())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assert_available(old._poll_lock_path)
            self.assertIsNone(old._poll_lock_fd)
        finally:
            old._release_poll_lock()

    async def test_initialize_retires_legacy_loop_without_finally(self):
        old, new = self.plugin(), self.plugin()
        self.assertTrue(old._try_acquire_poll_lock())
        entered = asyncio.Event()
        async def legacy(self):
            entered.set()
            await asyncio.Event().wait()
        legacy.__qualname__ = "SteamUpdatePush._poll_loop"
        task = asyncio.create_task(legacy(old))
        await entered.wait()
        try:
            await new.initialize()
            self.assertTrue(task.done())
            self.assertIsNone(old._poll_lock_fd)
            self.assertTrue(new._try_acquire_poll_lock())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            old._release_poll_lock()
            await new.terminate()

    async def test_initialize_recovers_orphan_lock_without_touching_other_file(self):
        plugin = self.plugin()
        fd = os.open(plugin._poll_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        other_path = self.root / "unrelated.lock"
        other = os.open(other_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            await plugin.initialize()
            self.assertTrue(plugin._try_acquire_poll_lock())
            with self.assertRaises(BlockingIOError):
                self.assert_available(other_path)
            # Recovery must not close descriptors it does not own.
            os.fstat(fd)
        finally:
            await plugin.terminate()
            os.close(fd)
            os.close(other)

    async def test_terminate_waits_for_task_cleanup(self):
        plugin = self.plugin()
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def worker():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        plugin._poll_task = asyncio.create_task(worker())
        await entered.wait()
        await plugin.terminate()
        self.assertTrue(cleaned.is_set())

    async def test_repeated_reload_runs_next_poll_and_preserves_other_plugin(self):
        unrelated = self.plugin(self.root / "other-plugin")
        await unrelated.initialize()
        other_task = unrelated._poll_task
        current = None
        try:
            for _ in range(3):
                new = self.plugin()
                ran = asyncio.Event()
                async def poll():
                    ran.set()
                    await asyncio.Event().wait()
                new._poll_once = poll
                new._next_poll_time = lambda now: now
                await new.initialize()
                await asyncio.wait_for(ran.wait(), 1)
                if current is not None:
                    self.assertTrue(current._poll_task.done())
                    self.assertIsNone(current._poll_lock_fd)
                current = new
                self.assertFalse(other_task.done())
                self.assertTrue(new._poll_lock_owner)
        finally:
            if current:
                await current.terminate()
            await unrelated.terminate()
        self.assert_available(self.root / ".poll.lock")

    async def test_recovery_does_not_unlock_another_process(self):
        plugin = self.plugin()
        code = "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"
        proc = subprocess.Popen([sys.executable, "-c", code, str(plugin._poll_lock_path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            await plugin.initialize()
            self.assertFalse(plugin._try_acquire_poll_lock())
        finally:
            await plugin.terminate()
            proc.stdin.close()
            proc.wait(timeout=5)
            proc.stdout.close()
