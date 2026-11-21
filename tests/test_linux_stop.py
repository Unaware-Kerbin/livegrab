"""POSIX stop: signal the yt-dlp session, then make sure it is gone.

The fake yt-dlp prints a live progress line with no percentage, writes a
.part file, and ignores SIGINT and SIGTERM. Stop waits out the remux grace,
then SIGKILL the group. The partial is not media, so it stays a .part.
Nothing here downloads a stream.
"""

import os
import shlex
import signal
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from livegrab_lib.jobs import (
    _POSIX_STOP_GRACE,
    _POSIX_STOP_KILL_WAIT,
    JobManager,
    _popen_kwargs,
    pid_alive,
)
from livegrab_lib.progress import parse_progress_line

LIVE_LINE = "[download]    1.23MiB at  Unknown B/s (00:00:04) (frag 4)"
UNKNOWN_ONLY = "[download]   N/A% at  Unknown B/s ETA Unknown"
PARTIAL = b"partial-bytes"
OLDER = b"keep-me"


def _stat_fields(pid):
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            stat_text = handle.read()
    except OSError:
        return None
    end = stat_text.rfind(")")
    if end < 0:
        return None
    tail = stat_text[end + 1 :].split()
    if len(tail) < 5:
        return None
    try:
        return {
            "state": tail[0],
            "ppid": int(tail[1]),
            "pgrp": int(tail[2]),
            "sid": int(tail[3]),
        }
    except ValueError:
        return None


def _kill_test_group(pid):
    """SIGKILL a session this test created. Never the caller's session."""
    if not pid:
        return
    try:
        leader = int(pid)
    except (TypeError, ValueError):
        return
    if leader <= 1:
        return
    try:
        if os.getsid(leader) == os.getsid(0):
            return
    except ProcessLookupError:
        return
    except OSError:
        return
    try:
        os.killpg(leader, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return


class LiveProgressTests(unittest.TestCase):
    def test_live_line_without_percent_is_stored(self):
        parsed = parse_progress_line(LIVE_LINE)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["phase"], "downloading")
        self.assertEqual(parsed["size"], "1.23MiB")
        self.assertEqual(parsed["fragment"], 4)
        self.assertIsNone(parsed["fragment_total"])
        self.assertIsNone(parsed["percent"])
        self.assertIn("Unknown", parsed["speed"])

    def test_unknown_speed_without_size_is_stored(self):
        parsed = parse_progress_line(UNKNOWN_ONLY)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["phase"], "downloading")
        self.assertIsNone(parsed["percent"])
        self.assertEqual(parsed["speed"], "Unknown B/s")

    def test_fragment_with_total_still_parses(self):
        parsed = parse_progress_line(
            "[download] 1.00MiB at 100.00KiB/s (00:00:01) (frag 10/20)"
        )
        self.assertEqual(parsed["fragment"], 10)
        self.assertEqual(parsed["fragment_total"], 20)
        self.assertEqual(parsed["percent"], 50.0)


@unittest.skipUnless(os.name == "posix", "posix process groups")
class LinuxStopTests(unittest.TestCase):
    def test_spawn_hides_no_terminal_and_uses_a_new_session(self):
        kwargs = _popen_kwargs()
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["start_new_session"])
        blob = " ".join(str(value) for value in kwargs.values())
        self.assertNotIn("x-terminal-emulator", blob)

    def test_stop_kills_the_group_and_keeps_the_partial(self):
        leader = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "videos"
            destination.mkdir()
            older = destination / "older.mp4"
            older.write_bytes(OLDER)
            past = time.time() - 3600
            os.utime(older, (past, past))
            marker = root / "signals"
            ready = root / "child.pid"
            hold = root / "hold.py"
            hold.write_text(
                "import os\nimport signal\nimport sys\nimport time\nmarker, ready = sys.argv[1], sys.argv[2]\ndef note(signum, frame):\n    handle = open(marker, 'a', encoding='utf-8')\n    handle.write('child %s\\n' % signum)\n    handle.flush()\n    os.fsync(handle.fileno())\n    handle.close()\nsignal.signal(signal.SIGINT, note)\nsignal.signal(signal.SIGTERM, note)\nhandle = open(ready, 'w', encoding='utf-8')\nhandle.write(str(os.getpid()))\nhandle.flush()\nos.fsync(handle.fileno())\nhandle.close()\nwhile True:\n    time.sleep(0.2)\n",
                encoding="utf-8",
            )
            fake = root / "yt-dlp"
            script = "\n".join(
                [
                    "#!/bin/sh",
                    "output=",
                    "prev=",
                    'for arg in "$@"; do',
                    '  if [ "$prev" = "-o" ]; then',
                    "    output=$arg",
                    "  fi",
                    "  prev=$arg",
                    "done",
                    f"marker={shlex.quote(str(marker))}",
                    f"ready={shlex.quote(str(ready))}",
                    f"hold={shlex.quote(str(hold))}",
                    f"python={shlex.quote(os.path.realpath(sys_executable()))}",
                    "trap 'echo shell INT >> \"$marker.shell\"' INT",
                    "trap 'echo shell TERM >> \"$marker.shell\"' TERM",
                    '"$python" "$hold" "$marker" "$ready" &',
                    'while [ ! -s "$ready" ]; do',
                    "  sleep 0.05",
                    "done",
                    'part="${output}.part"',
                    "printf '{}' {} > \"$part\"".format(PARTIAL.decode("ascii"), shlex.quote(PARTIAL.decode("ascii"))),
                    f"echo {shlex.quote(LIVE_LINE)}",
                    "while true; do",
                    "  sleep 0.2",
                    "done",
                    "",
                ]
            )
            fake.write_text(script, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            manager = JobManager(str(root / "jobs.json"))
            url = f"https://example.invalid/livegrab-linux-stop-{uuid.uuid4().hex}"
            try:
                started = manager.start_job(
                    url,
                    destination=str(destination),
                    template="clip.mp4",
                    audio_source="live",
                    ytdlp_path=str(fake),
                )
                leader = started["pid"]
                self.assertIn(started["state"], ("starting", "downloading", "merging"))
                self.assertEqual(started["url"], url)
                self.assertEqual(started["destination"], str(destination))
                deadline = time.monotonic() + 5
                current = started
                while time.monotonic() < deadline:
                    rows = manager.list_jobs()
                    self.assertEqual(len(rows), 1)
                    current = rows[0]
                    if current.get("fragment") == 4 and ready.is_file():
                        break
                    time.sleep(0.05)
                self.assertEqual(current["id"], started["id"])
                self.assertEqual(current["state"], "downloading")
                self.assertEqual(current["size"], "1.23MiB")
                self.assertEqual(current["fragment"], 4)
                self.assertIsNone(current["percent"])
                self.assertTrue(pid_alive(leader))
                child = int(ready.read_text(encoding="utf-8").strip())
                leader_fields = _stat_fields(leader)
                child_fields = _stat_fields(child)
                self.assertIsNotNone(leader_fields)
                self.assertIsNotNone(child_fields)
                self.assertEqual(leader_fields["sid"], leader)
                self.assertEqual(leader_fields["pgrp"], leader)
                self.assertNotEqual(leader_fields["sid"], os.getsid(0))
                self.assertEqual(leader_fields["ppid"], os.getpid())
                self.assertEqual(child_fields["pgrp"], leader)
                self.assertTrue(pid_alive(child))
                with open(f"/proc/{leader}/cmdline", "rb") as handle:
                    cmdline = handle.read()
                self.assertNotIn(b"x-terminal-emulator", cmdline)
                self.assertNotIn(b"--live-from-start", cmdline)
                self.assertIn(url.encode("utf-8"), cmdline)
                began = time.monotonic()
                status, body = manager.stop_job(started["id"])
                elapsed = time.monotonic() - began
                self.assertEqual(status, 200)
                # The fake never exits on SIGINT, so Stop uses the full grace
                # before SIGKILL. Two seconds would cut off a real remux.
                self.assertGreater(elapsed, 20.0)
                self.assertLess(elapsed, _POSIX_STOP_GRACE + _POSIX_STOP_KILL_WAIT + 10.0)
                self.assertEqual(body["state"], "stopped")
                self.assertEqual(body["id"], started["id"])
                self.assertTrue(body.get("output_path"))
                self.assertTrue(str(body["output_path"]).endswith(".part"))
                part_path = Path(body["output_path"])
                self.assertEqual(part_path.read_bytes(), PARTIAL)
                self.assertEqual(older.read_bytes(), OLDER)
                self.assertFalse(pid_alive(leader))
                self.assertFalse(pid_alive(child))
                noted = marker.read_text(encoding="utf-8") if marker.is_file() else ""
                shell_note = ""
                shell_path = Path(str(marker) + ".shell")
                if shell_path.is_file():
                    shell_note = shell_path.read_text(encoding="utf-8")
                self.assertTrue(
                    "child 2" in noted or "shell INT" in shell_note,
                    msg=f"signals={noted!r} shell={shell_note!r}",
                )
                time.sleep(0.3)
                listed = manager.list_jobs()
                self.assertEqual([item["id"] for item in listed], [started["id"]])
                self.assertEqual(listed[0]["state"], "stopped")
                self.assertEqual(listed[0]["output_path"], body["output_path"])
                self.assertEqual(Path(listed[0]["output_path"]).read_bytes(), PARTIAL)
            finally:
                _kill_test_group(leader)


def sys_executable():
    import sys

    return sys.executable


if __name__ == "__main__":
    unittest.main()
