"""Follow parsing and the auto-start decision.

These tests do not start yt-dlp. The checker is given a fake probe.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from livegrab_lib.config import load_config
from livegrab_lib.follows import (
    FollowChecker,
    FollowStore,
    check_follows,
    parse_twitch_login,
    probe_args,
    should_start_grab,
)

LIVE_ID = "v2932301927"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def blank_follow(login="example"):
    return {
        "login": login,
        "url": "https://www.twitch.tv/" + login,
        "enabled": True,
        "live_status": None,
        "title": None,
        "video_id": None,
        "last_checked": None,
        "last_started_id": None,
        "error": None,
    }


def live_probe(video_id=LIVE_ID):
    return {
        "ok": True,
        "live_status": "is_live",
        "is_live": True,
        "video_id": video_id,
        "title": "Example stream",
    }


class ParseTwitchLoginTests(unittest.TestCase):
    def test_bare_name(self):
        self.assertEqual(parse_twitch_login("kelsaco"), "kelsaco")

    def test_twitch_url(self):
        self.assertEqual(parse_twitch_login("https://www.twitch.tv/kelsaco"), "kelsaco")
        self.assertEqual(parse_twitch_login("https://twitch.tv/kelsaco/"), "kelsaco")

    def test_twitch_url_with_query_string(self):
        self.assertEqual(
            parse_twitch_login("https://www.twitch.tv/kelsaco?filter=all"),
            "kelsaco",
        )
        self.assertEqual(
            parse_twitch_login("https://twitch.tv/kelsaco/about?lang=en"),
            "kelsaco",
        )

    def test_rejects_youtube_url(self):
        with self.assertRaises(ValueError):
            parse_twitch_login(YOUTUBE)
        with self.assertRaises(ValueError):
            parse_twitch_login("https://youtu.be/dQw4w9WgXcQ")

    def test_hyphenated_name(self):
        self.assertEqual(parse_twitch_login("livegrab-test-offline"), "livegrab-test-offline")


class ShouldStartGrabTests(unittest.TestCase):
    def test_live_new_id_starts_once_then_same_id_does_not(self):
        follow = {"enabled": True, "last_started_id": None}
        probe = {"live_status": "is_live", "video_id": LIVE_ID}
        self.assertTrue(should_start_grab(follow, probe))
        follow["last_started_id"] = probe["video_id"]
        self.assertFalse(should_start_grab(follow, probe))

    def test_is_live_false_does_not_start(self):
        follow = {"enabled": True, "last_started_id": None}
        self.assertFalse(
            should_start_grab(follow, {"is_live": False, "video_id": LIVE_ID})
        )
        self.assertFalse(
            should_start_grab(
                follow,
                {"live_status": "is_offline", "is_live": False, "video_id": LIVE_ID},
            )
        )
        self.assertFalse(
            should_start_grab(follow, {"live_status": "was_live", "video_id": LIVE_ID})
        )


class CheckFollowsTests(unittest.TestCase):
    def test_fake_probe_starts_once_for_a_new_id(self):
        calls = []

        def start(follow, probed):
            calls.append((follow["url"], probed["video_id"]))
            return True

        probe = live_probe()
        first = check_follows(
            [blank_follow()],
            lambda login: probe,
            start=start,
            active=lambda url, video_id: False,
        )
        self.assertEqual(calls, [("https://www.twitch.tv/example", LIVE_ID)])
        self.assertEqual(first[0]["last_started_id"], LIVE_ID)
        self.assertFalse(should_start_grab(first[0], probe))

        calls.clear()
        second = check_follows(
            first,
            lambda login: probe,
            start=start,
            active=lambda url, video_id: False,
        )
        self.assertEqual(calls, [])
        self.assertEqual(second[0]["last_started_id"], LIVE_ID)

    def test_is_live_false_does_not_call_start(self):
        calls = []

        def start(follow, probed):
            calls.append(follow["login"])
            return True

        offline = {"ok": True, "live_status": "is_offline", "is_live": False, "video_id": LIVE_ID}
        result = check_follows([blank_follow()], lambda login: offline, start=start)
        self.assertEqual(calls, [])
        self.assertIsNone(result[0]["last_started_id"])
        self.assertEqual(result[0]["live_status"], "is_offline")

    def test_active_grab_skips_start(self):
        calls = []
        result = check_follows(
            [blank_follow()],
            lambda login: live_probe(),
            start=lambda follow, probed: calls.append(follow["login"]) or True,
            active=lambda url, video_id: True,
        )
        self.assertEqual(calls, [])
        self.assertIsNone(result[0]["last_started_id"])

    def test_failed_probe_keeps_previous_status(self):
        follow = blank_follow()
        follow["live_status"] = "is_offline"
        follow["title"] = "Old title"
        follow["video_id"] = "v1"
        result = check_follows([follow], lambda login: {"ok": False, "error": "timeout"})
        self.assertEqual(result[0]["live_status"], "is_offline")
        self.assertEqual(result[0]["title"], "Old title")
        self.assertEqual(result[0]["video_id"], "v1")
        self.assertIsNone(result[0]["last_started_id"])
        self.assertEqual(result[0]["error"], "timeout")

    def test_disabled_follow_is_not_probed(self):
        follow = blank_follow()
        follow["enabled"] = False
        called = []

        def probe(login):
            called.append(login)
            return live_probe()

        result = check_follows([follow], probe, start=lambda follow, probed: True)
        self.assertEqual(called, [])
        self.assertFalse(should_start_grab(follow, live_probe()))
        self.assertIsNone(result[0]["last_checked"])


class FollowStoreTests(unittest.TestCase):
    def test_pathlib_path_can_be_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "follows.json"
            store = FollowStore(path)
            view = store.add("example")
            self.assertEqual(view["follows"][0]["login"], "example")
            self.assertTrue(path.is_file())
            stored = path.read_text(encoding="utf-8")
            self.assertIn("example", stored)


class ProbeArgsTests(unittest.TestCase):
    def test_metadata_only_and_no_live_from_start(self):
        args = probe_args("kelsaco")
        self.assertIn(Path(args[0]).name.lower(), ("yt-dlp", "yt-dlp.exe"))
        self.assertIn("--no-download", args)
        self.assertIn("--dump-single-json", args)
        self.assertIn("--no-js-runtimes", args)
        self.assertNotIn("--live-from-start", args)
        self.assertNotIn("--no-simulate", args)
        self.assertNotIn("-o", args)
        self.assertFalse(any("Videos" in arg for arg in args))
        self.assertEqual(args[-1], "https://www.twitch.tv/kelsaco")


class FakeManager:
    def __init__(self):
        self.calls = []
        self.jobs = []

    def list_jobs(self):
        return [dict(job) for job in self.jobs]

    def start_job(
        self,
        url,
        destination=None,
        template=None,
        live_from_start=None,
        audio_source=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "url": url,
                "destination": destination,
                "template": template,
                "live_from_start": live_from_start,
                "audio_source": audio_source,
            }
        )
        job = {
            "id": f"job-{len(self.calls)}",
            "url": url,
            "state": "starting",
            "owner": "livegrab",
            "pid": None,
            "output_path": None,
            "log_tail": [],
        }
        self.jobs.append(job)
        return dict(job)


class FollowCheckerFakeProbeTests(unittest.TestCase):
    def test_checker_starts_once_without_spawning_ytdlp(self):
        config = load_config()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "follows.json")
            manager = FakeManager()
            checker = FollowChecker(
                manager,
                path=path,
                probe=lambda login: live_probe(),
            )
            checker.add("example")
            with mock.patch(
                "livegrab_lib.follows.subprocess.run",
                side_effect=AssertionError("yt-dlp was started"),
            ):
                checker.check_once()
                self.assertEqual(len(manager.calls), 1)
                manager.jobs.clear()
                checker.check_once()
            self.assertEqual(len(manager.calls), 1)
        call = manager.calls[0]
        self.assertEqual(call["url"], "https://www.twitch.tv/example")
        self.assertEqual(call["destination"], config["destination"])
        self.assertEqual(call["template"], config["template"])
        self.assertEqual(call["audio_source"], "live")
        self.assertIs(call["live_from_start"], False)
        stored = checker.view()["follows"][0]
        self.assertEqual(stored["last_started_id"], LIVE_ID)
        self.assertEqual(checker.view()["interval_seconds"], 180)


if __name__ == "__main__":
    unittest.main()
