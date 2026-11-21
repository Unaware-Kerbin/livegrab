"""Windows stop kills the yt-dlp tree, not only the parent pid.

The stand-in is a fake process table: a parent and the child that kept
recording after os.kill(SIGTERM). Stop must leave both gone, keep a .part
file, and hide every console it would spawn. Nothing here is a livestream.
"""

import os
import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from livegrab_lib.jobs import (
    _WINDOWS_STOP_GRACE,
    JobManager,
    _blank_job,
    _popen_kwargs,
    _signal_spawned,
)
from livegrab_lib.platform import (
    _WIN_CREATE_NEW_PROCESS_GROUP,
    _WIN_CREATE_NO_WINDOW,
    _WIN_STARTF_USESHOWWINDOW,
    _WIN_SW_HIDE,
)

# CREATE_NEW_CONSOLE. Setting it opens a cmd window, so Stop must not.
_CREATE_NEW_CONSOLE = 0x00000010
PARTIAL = b"partial-bytes"


def _free_pair():
    """Two pids that are not live processes on this machine."""
    parent = 250001
    while parent < 260000:
        child = parent + 1
        if not os.path.exists(f"/proc/{parent}") and not os.path.exists(f"/proc/{child}"):
            return parent, child
        parent += 2
    raise RuntimeError("no unused pids")


def _drop_tree(pid, alive, parent_of):
    for child, parent in list(parent_of.items()):
        if parent == pid:
            _drop_tree(child, alive, parent_of)
    alive.discard(pid)


class WindowsSpawnTests(unittest.TestCase):
    def test_spawn_hides_the_console_and_skips_the_shell(self):
        with mock.patch("livegrab_lib.jobs.sys.platform", "win32"):
            kwargs = _popen_kwargs()
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("start_new_session", kwargs)
        flags = kwargs["creationflags"]
        self.assertTrue(flags & _WIN_CREATE_NO_WINDOW)
        self.assertTrue(flags & _WIN_CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & _CREATE_NEW_CONSOLE)
        info = kwargs["startupinfo"]
        self.assertTrue(info.dwFlags & _WIN_STARTF_USESHOWWINDOW)
        self.assertEqual(info.wShowWindow, _WIN_SW_HIDE)


class WindowsTreeStopTests(unittest.TestCase):
    def _stop(self, alive, parent_of, grace=_WINDOWS_STOP_GRACE):
        parent = next(pid for pid, owner in parent_of.items() if owner is None)
        child = next(pid for pid, owner in parent_of.items() if owner == parent)
        calls = []

        def fake_kill(pid, sig):
            calls.append(("kill", int(pid), sig))

        def fake_run(argv, **kwargs):
            self.assertIsInstance(argv, list)
            self.assertFalse(kwargs.get("shell", False))
            flags = kwargs.get("creationflags", 0)
            self.assertTrue(flags & _WIN_CREATE_NO_WINDOW)
            self.assertFalse(flags & _CREATE_NEW_CONSOLE)
            info = kwargs["startupinfo"]
            self.assertTrue(info.dwFlags & _WIN_STARTF_USESHOWWINDOW)
            self.assertEqual(info.wShowWindow, _WIN_SW_HIDE)
            if argv[:4] != ["taskkill", "/F", "/T", "/PID"]:
                calls.append(("ffmpeg", list(argv)))
                return mock.Mock(returncode=1, stdout="", stderr="invalid data")
            calls.append(("taskkill", int(argv[4])))
            if "/T" in argv:
                _drop_tree(int(argv[4]), alive, parent_of)
            else:
                alive.discard(int(argv[4]))
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "videos")
            os.mkdir(destination)
            part = os.path.join(destination, "clip.mp4.part")
            with open(part, "wb") as handle:
                handle.write(PARTIAL)
            manager = JobManager(os.path.join(directory, "jobs.json"))
            job = _blank_job()
            job.update(
                {
                    "url": "https://example.invalid/livegrab-windows-stop",
                    "destination": destination,
                    "template": "clip.mp4",
                    "state": "downloading",
                    "owner": "livegrab",
                    "pid": parent,
                    "audio_source": "live",
                    "live_from_start": False,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
            manager._jobs.append(job)
            manager._spawned.add(parent)
            # argv satisfies the spawn check without flipping os.name, which
            # would make pathlib try to build a WindowsPath on this host.
            argv = ["yt-dlp.exe", job["url"]]
            with (
                mock.patch("livegrab_lib.jobs.sys.platform", "win32"),
                mock.patch("livegrab_lib.jobs.read_argv", return_value=argv),
                mock.patch("livegrab_lib.jobs.pid_alive", side_effect=lambda pid: int(pid) in alive),
                mock.patch("livegrab_lib.jobs.os.kill", side_effect=fake_kill),
                mock.patch("livegrab_lib.jobs.subprocess.run", side_effect=fake_run),
                mock.patch("livegrab_lib.jobs.ffmpeg_path", return_value="ffmpeg.exe"),
                mock.patch("livegrab_lib.jobs._WINDOWS_STOP_GRACE", grace),
            ):
                status, body = manager.stop_job(job["id"])
            with open(part, "rb") as handle:
                kept = handle.read()
        return status, body, calls, kept, parent, child

    def test_stop_removes_parent_and_child(self):
        parent, child = _free_pair()
        alive = {parent, child}
        parent_of = {parent: None, child: parent}
        status, body, calls, kept, parent, child = self._stop(alive, parent_of)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "stopped")
        self.assertTrue(str(body["output_path"]).endswith(".part"))
        self.assertEqual(kept, PARTIAL)
        self.assertNotIn(parent, alive)
        self.assertNotIn(child, alive)
        self.assertIn(("taskkill", parent), calls)
        self.assertNotIn(("kill", parent, 15), calls)

    def test_break_then_taskkill_still_removes_the_child(self):
        parent, child = _free_pair()
        alive = {parent, child}
        parent_of = {parent: None, child: parent}
        with mock.patch("livegrab_lib.jobs.signal.CTRL_BREAK_EVENT", 1, create=True):
            status, body, calls, kept, parent, child = self._stop(
                alive, parent_of, grace=0
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "stopped")
        self.assertEqual(kept, PARTIAL)
        self.assertEqual(calls[0], ("kill", parent, 1))
        self.assertEqual(calls[1], ("taskkill", parent))
        self.assertNotIn(parent, alive)
        self.assertNotIn(child, alive)

    def test_historical_pid_is_not_taskkilled(self):
        calls = []
        with (
            mock.patch("livegrab_lib.jobs.sys.platform", "win32"),
            mock.patch("livegrab_lib.jobs.subprocess.run", side_effect=lambda *args, **kwargs: calls.append(args)),
            mock.patch("livegrab_lib.jobs.os.kill", side_effect=AssertionError("signaled")),
            self.assertRaises(PermissionError),
        ):
            _signal_spawned(149396)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
