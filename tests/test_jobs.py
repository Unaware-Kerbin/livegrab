"""Job start errors. These tests do not spawn yt-dlp."""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from livegrab_lib.jobs import (
    FOLDER_UNCREATABLE,
    JobError,
    JobManager,
    _is_ytdlp,
    find_external_ytdlp,
)
from livegrab_lib.server import LivegrabHandler


class FolderCreateTests(unittest.TestCase):
    def test_uncreatable_folder_is_a_client_error(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = os.path.join(directory, "not-a-directory")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("x")
            destination = os.path.join(blocker, "videos")
            manager = JobManager(os.path.join(directory, "jobs.json"))
            body = json.dumps(
                {
                    "url": "https://example.invalid/livegrab-not-a-stream",
                    "destination": destination,
                    "template": "%(id)s.%(ext)s",
                    "audio_source": "live",
                }
            ).encode("utf-8")

            class Capture(LivegrabHandler):
                def __init__(self):
                    self.path = "/api/jobs"
                    self.headers = {"Content-Length": str(len(body))}
                    self.rfile = io.BytesIO(body)
                    self.sent = None

                def _send_json(self, status, payload):
                    self.sent = (status, payload)

            handler = Capture()
            previous = LivegrabHandler.manager
            LivegrabHandler.manager = manager
            try:
                with mock.patch(
                    "livegrab_lib.jobs.subprocess.Popen",
                    side_effect=AssertionError("yt-dlp was started"),
                ):
                    handler.do_POST()
            finally:
                LivegrabHandler.manager = previous

        self.assertIsNotNone(handler.sent)
        status, payload = handler.sent
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": FOLDER_UNCREATABLE})
        self.assertNotIn("internal error", payload["error"])
        self.assertNotIn("Traceback", payload["error"])
        self.assertFalse(os.path.isdir(destination))

    def test_start_job_raises_folder_error_without_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = os.path.join(directory, "not-a-directory")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("x")
            destination = os.path.join(blocker, "videos")
            manager = JobManager(os.path.join(directory, "jobs.json"))
            with mock.patch(
                "livegrab_lib.jobs.subprocess.Popen",
                side_effect=AssertionError("yt-dlp was started"),
            ), self.assertRaises(JobError) as caught:
                manager.start_job(
                    "https://example.invalid/livegrab-not-a-stream",
                    destination=destination,
                    template="%(id)s.%(ext)s",
                    audio_source="live",
                )
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(str(caught.exception), "Could not create that folder")


class YtDlpExeTests(unittest.TestCase):
    def test_exe_names_count_as_ytdlp(self):
        self.assertTrue(_is_ytdlp(["yt-dlp"]))
        self.assertTrue(_is_ytdlp(["/usr/bin/yt-dlp", "--no-download"]))
        self.assertTrue(_is_ytdlp([r"C:\Livegrab\yt-dlp.exe"]))
        self.assertTrue(_is_ytdlp(["YT-DLP.EXE"]))
        self.assertTrue(_is_ytdlp(["yt-dlp-nightly"]))
        self.assertTrue(_is_ytdlp([r"D:\tools\yt-dlp-nightly.exe"]))
        self.assertFalse(_is_ytdlp(["ffmpeg.exe"]))
        self.assertFalse(_is_ytdlp([r"C:\Livegrab\python.exe", "-m", "yt_dlp"]))
        self.assertFalse(_is_ytdlp([]))

    def test_windows_discovery_feeds_the_existing_check(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        argv = [r"C:\Livegrab\yt-dlp.exe", "--live-from-start", url]
        with mock.patch("livegrab_lib.jobs.os.name", "nt"), mock.patch(
            "livegrab_lib.jobs._windows_ytdlp_argvs",
            return_value=[(4242, argv)],
        ):
            found = find_external_ytdlp(url)
        self.assertEqual([item["pid"] for item in found], [4242])
        self.assertTrue(found[0]["exact_url"])
        self.assertTrue(found[0]["live_from_start"])

        other = [r"C:\Livegrab\yt-dlp.exe", "https://example.invalid/other"]
        with mock.patch("livegrab_lib.jobs.os.name", "nt"), mock.patch(
            "livegrab_lib.jobs._windows_ytdlp_argvs",
            return_value=[(7, other), (8, ["ffmpeg.exe", url])],
        ):
            missed = find_external_ytdlp(url)
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()
