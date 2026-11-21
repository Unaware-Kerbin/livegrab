"""Stop leaves a real mp4, not a leftover partial.

A direct remux test feeds ffmpeg a short file ffmpeg itself generated.
A fake yt-dlp writes that file as name.mp4.part and ignores the first
signal long enough that a 2 second kill would skip the remux. Neither
test contacts Twitch.
"""

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from livegrab_lib.jobs import (
    _POSIX_STOP_GRACE,
    _WINDOWS_STOP_GRACE,
    JobManager,
    _blank_job,
    _finalize_partial,
    pid_alive,
)
from livegrab_lib.platform import (
    _WIN_CREATE_NEW_PROCESS_GROUP,
    _WIN_CREATE_NO_WINDOW,
    _WIN_STARTF_USESHOWWINDOW,
    _WIN_SW_HIDE,
    ffmpeg_path,
)

LIVE_LINE = "[download]    1.23MiB at  Unknown B/s (00:00:04) (frag 4)"
_CREATE_NEW_CONSOLE = 0x00000010


def _seed_media(directory: Path):
    """A tiny valid mp4, or None when ffmpeg cannot make one."""
    binary = ffmpeg_path()
    if not binary:
        return None
    seed = directory / "seed.mp4"
    result = subprocess.run(
        [
            binary,
            "-y",
            "-f",
            "s16le",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "/dev/zero",
            "-t",
            "0.25",
            "-c:a",
            "aac",
            str(seed),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not seed.is_file() or seed.stat().st_size <= 0:
        return None
    return seed


def _job(destination: Path) -> dict:
    job = _blank_job()
    job.update(
        {
            "destination": str(destination),
            "template": "clip.mp4",
            "state": "stopped",
            "owner": "livegrab",
            "started_at": datetime.now(UTC).isoformat(),
            "output_path": str(destination / "clip.mp4.part"),
        }
    )
    return job


def _kill_test_group(pid):
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


class GraceTests(unittest.TestCase):
    def test_stop_grace_covers_a_remux(self):
        self.assertGreaterEqual(_POSIX_STOP_GRACE, 20)
        self.assertGreaterEqual(_WINDOWS_STOP_GRACE, 20)


class FinalizeTests(unittest.TestCase):
    def test_failed_remux_leaves_the_part_and_records_the_error(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            part = destination / "clip.mp4.part"
            part.write_bytes(b"partial-bytes")
            final = destination / "clip.mp4"
            job = _job(destination)
            with (
                mock.patch("livegrab_lib.jobs.ffmpeg_path", return_value="ffmpeg"),
                mock.patch(
                    "livegrab_lib.jobs.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["ffmpeg"], 1, "", "boom"
                    ),
                ),
            ):
                _finalize_partial(job)
            self.assertEqual(part.read_bytes(), b"partial-bytes")
            self.assertFalse(final.exists())
            self.assertTrue(str(job["output_path"]).endswith(".part"))
            self.assertIn("boom", job["error"])

    def test_missing_ffmpeg_leaves_the_part(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            part = destination / "clip.mp4.part"
            part.write_bytes(b"partial-bytes")
            job = _job(destination)
            with mock.patch("livegrab_lib.jobs.ffmpeg_path", return_value=None):
                _finalize_partial(job)
            self.assertEqual(part.read_bytes(), b"partial-bytes")
            self.assertIn("ffmpeg", job["error"].lower())
            self.assertTrue(str(job["output_path"]).endswith(".part"))

    def test_existing_mp4_is_kept_and_the_part_can_go(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            part = destination / "clip.mp4.part"
            final = destination / "clip.mp4"
            part.write_bytes(b"partial-bytes")
            final.write_bytes(b"already-an-mp4")
            job = _job(destination)
            with mock.patch(
                "livegrab_lib.jobs.subprocess.run",
                side_effect=AssertionError("ffmpeg was started"),
            ):
                _finalize_partial(job)
            self.assertEqual(job["output_path"], str(final))
            self.assertGreater(final.stat().st_size, 0)
            self.assertFalse(part.exists())
            self.assertEqual(final.read_bytes(), b"already-an-mp4")

    def test_non_ts_failure_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            part = destination / "clip.mp4.part"
            part.write_bytes(b"partial-bytes")
            job = _job(destination)
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                self.assertFalse(kwargs.get("shell", False))
                return subprocess.CompletedProcess(argv, 1, "", "nope")

            with (
                mock.patch("livegrab_lib.jobs.ffmpeg_path", return_value="ffmpeg"),
                mock.patch("livegrab_lib.jobs.subprocess.run", side_effect=fake_run),
            ):
                _finalize_partial(job)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("-bsf:a", calls[0])
            self.assertEqual(part.read_bytes(), b"partial-bytes")
            self.assertFalse((destination / "clip.mp4").exists())

    def test_mpegts_retry_uses_aac_bitstream_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            part = destination / "clip.mp4.part"
            packet = bytes([0x47]) + b"\x00" * 187
            part.write_bytes(packet * 4)
            final = destination / "clip.mp4"
            job = _job(destination)
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                self.assertIsInstance(argv, list)
                self.assertFalse(kwargs.get("shell", False))
                self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
                if "-bsf:a" in argv:
                    Path(argv[-1]).write_bytes(b"ok-mp4")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(argv, 1, "", "copy failed")

            with (
                mock.patch("livegrab_lib.jobs.ffmpeg_path", return_value="ffmpeg"),
                mock.patch("livegrab_lib.jobs.subprocess.run", side_effect=fake_run),
            ):
                _finalize_partial(job)
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                calls[0],
                ["ffmpeg", "-y", "-i", str(part), "-c", "copy", "-movflags", "+faststart", str(final)],
            )
            self.assertEqual(
                calls[1],
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(part),
                    "-c",
                    "copy",
                    "-bsf:a",
                    "aac_adtstoasc",
                    "-movflags",
                    "+faststart",
                    str(final),
                ],
            )
            self.assertEqual(job["output_path"], str(final))
            self.assertGreater(final.stat().st_size, 0)
            self.assertFalse(part.exists())
            self.assertFalse(job.get("error"))

    def test_truncated_mpegts_keeps_an_ftyp_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            part = destination / "clip.mp4.part"
            packet = bytes([0x47]) + b"\x00" * 187
            part.write_bytes(packet * 4)
            final = destination / "clip.mp4"
            job = _job(destination)
            ftyp = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if "-bsf:a" in argv:
                    final.write_bytes(ftyp)
                    return subprocess.CompletedProcess(argv, 1, "", "truncated")
                return subprocess.CompletedProcess(argv, 1, "", "copy failed")

            with (
                mock.patch("livegrab_lib.jobs.ffmpeg_path", return_value="ffmpeg"),
                mock.patch("livegrab_lib.jobs.subprocess.run", side_effect=fake_run),
            ):
                _finalize_partial(job)
            self.assertEqual(len(calls), 2)
            self.assertEqual(job["output_path"], str(final))
            self.assertGreater(final.stat().st_size, 0)
            self.assertIn(b"ftyp", final.read_bytes())
            self.assertFalse(part.exists())
            self.assertFalse(job.get("error"))

    def test_remux_hides_the_windows_console(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            part = destination / "clip.mp4.part"
            part.write_bytes(b"partial-bytes")
            job = _job(destination)
            seen = {}

            def fake_run(argv, **kwargs):
                seen["argv"] = list(argv)
                seen["kwargs"] = kwargs
                return subprocess.CompletedProcess(argv, 1, "", "nope")

            with (
                mock.patch("livegrab_lib.platform.sys.platform", "win32"),
                mock.patch("livegrab_lib.jobs.ffmpeg_path", return_value="ffmpeg.exe"),
                mock.patch("livegrab_lib.jobs.subprocess.run", side_effect=fake_run),
            ):
                _finalize_partial(job)
            kwargs = seen["kwargs"]
            self.assertFalse(kwargs["shell"])
            flags = kwargs["creationflags"]
            self.assertTrue(flags & _WIN_CREATE_NO_WINDOW)
            self.assertTrue(flags & _WIN_CREATE_NEW_PROCESS_GROUP)
            self.assertFalse(flags & _CREATE_NEW_CONSOLE)
            info = kwargs["startupinfo"]
            self.assertTrue(info.dwFlags & _WIN_STARTF_USESHOWWINDOW)
            self.assertEqual(info.wShowWindow, _WIN_SW_HIDE)
            self.assertEqual(seen["argv"][0], "ffmpeg.exe")
            self.assertIn("-c", seen["argv"])
            self.assertEqual(seen["argv"][seen["argv"].index("-c") + 1], "copy")

    @unittest.skipUnless(ffmpeg_path(), "ffmpeg is not installed")
    def test_finalize_remuxes_a_generated_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = _seed_media(root)
            if seed is None:
                self.skipTest("ffmpeg could not make a sample")
            destination = root / "videos"
            destination.mkdir()
            part = destination / "clip.mp4.part"
            part.write_bytes(seed.read_bytes())
            job = _job(destination)
            _finalize_partial(job)
            final = Path(job["output_path"])
            self.assertTrue(str(final).endswith(".mp4"))
            self.assertFalse(str(final).endswith(".part"))
            self.assertTrue(final.is_file())
            self.assertGreater(final.stat().st_size, 0)
            self.assertFalse(part.exists())
            self.assertFalse(job.get("error"))


@unittest.skipUnless(os.name == "posix", "posix process groups")
class SlowSignalRemuxTests(unittest.TestCase):
    def test_stop_waits_past_two_seconds_and_remuxes(self):
        if ffmpeg_path() is None:
            self.skipTest("ffmpeg is not installed")
        leader = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = _seed_media(root)
            if seed is None:
                self.skipTest("ffmpeg could not make a sample")
            destination = root / "videos"
            destination.mkdir()
            marker = root / "signals"
            fake = root / "yt-dlp"
            script = "\n".join(
                [
                    "#!" + sys.executable,
                    "import os",
                    "import shutil",
                    "import signal",
                    "import sys",
                    "import time",
                    f"seed = {str(seed)!r}",
                    f"marker = {str(marker)!r}",
                    "output = None",
                    "prev = None",
                    "for arg in sys.argv[1:]:",
                    "    if prev == '-o':",
                    "        output = arg",
                    "    prev = arg",
                    "if not output:",
                    "    raise SystemExit(2)",
                    "part = output + '.part'",
                    "shutil.copyfile(seed, part)",
                    "def on_int(signum, frame):",
                    "    handle = open(marker, 'w', encoding='utf-8')",
                    "    handle.write('int\\n')",
                    "    handle.flush()",
                    "    os.fsync(handle.fileno())",
                    "    handle.close()",
                    "    time.sleep(4)",
                    "    handle = open(marker, 'a', encoding='utf-8')",
                    "    handle.write('exit\\n')",
                    "    handle.flush()",
                    "    os.fsync(handle.fileno())",
                    "    handle.close()",
                    "    os._exit(0)",
                    "signal.signal(signal.SIGINT, on_int)",
                    "signal.signal(signal.SIGTERM, on_int)",
                    f"sys.stdout.write({LIVE_LINE!r} + '\\n')",
                    "sys.stdout.flush()",
                    "while True:",
                    "    time.sleep(0.2)",
                    "",
                ]
            )
            fake.write_text(script, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            manager = JobManager(str(root / "jobs.json"))
            url = f"https://example.invalid/livegrab-remux-{uuid.uuid4().hex}"
            try:
                started = manager.start_job(
                    url,
                    destination=str(destination),
                    template="clip.mp4",
                    audio_source="live",
                    ytdlp_path=str(fake),
                )
                leader = started["pid"]
                part = destination / "clip.mp4.part"
                deadline = time.monotonic() + 5
                current = started
                while time.monotonic() < deadline:
                    rows = manager.list_jobs()
                    self.assertEqual(len(rows), 1)
                    current = rows[0]
                    if current.get("state") == "downloading" and part.is_file() and part.stat().st_size > 0:
                        break
                    time.sleep(0.05)
                self.assertEqual(current["state"], "downloading")
                self.assertGreater(part.stat().st_size, 0)
                began = time.monotonic()
                status, body = manager.stop_job(started["id"])
                elapsed = time.monotonic() - began
                self.assertEqual(status, 200)
                self.assertEqual(body["state"], "stopped")
                self.assertGreater(elapsed, 3.0)
                self.assertLess(elapsed, 20.0)
                self.assertTrue(str(body["output_path"]).endswith(".mp4"))
                self.assertFalse(str(body["output_path"]).endswith(".part"))
                produced = Path(body["output_path"])
                self.assertTrue(produced.is_file())
                self.assertGreater(produced.stat().st_size, 0)
                self.assertIn("exit", marker.read_text(encoding="utf-8"))
                self.assertFalse(pid_alive(leader))
                self.assertFalse(part.exists())
            finally:
                _kill_test_group(leader)


if __name__ == "__main__":
    unittest.main()
