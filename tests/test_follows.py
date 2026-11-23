"""Follow parsing and the auto-start decision.

These tests do not start yt-dlp. The checker is given a fake probe.
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from livegrab_lib.config import load_config
from livegrab_lib.follows import (
    FollowChecker,
    FollowError,
    FollowStore,
    check_follows,
    parse_twitch_login,
    probe_args,
    should_start_grab,
)
from livegrab_lib.jobs import JobError, JobManager
from livegrab_lib.server import LivegrabHandler

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

    def test_start_failure_keeps_destination_and_does_not_record_id(self):
        follow = blank_follow()
        folder = os.path.join("channel-recordings", "example")
        follow["destination"] = folder

        def start(item, probed):
            raise RuntimeError("Choose a folder before starting")

        result = check_follows([follow], lambda login: live_probe(), start=start)
        self.assertEqual(result[0]["destination"], folder)
        self.assertEqual(result[0]["error"], "Choose a folder before starting")
        self.assertIsNone(result[0]["last_started_id"])


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

    def test_old_file_without_destination_loads_blank(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "follows.json"
            path.write_text(
                json.dumps(
                    {
                        "follows": [
                            {
                                "login": "example",
                                "url": "https://www.twitch.tv/example",
                                "enabled": True,
                                "live_status": "is_offline",
                            },
                            {
                                "login": "other",
                                "enabled": True,
                                "destination": None,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store = FollowStore(path)
            follows = {item["login"]: item for item in store.view()["follows"]}
            self.assertEqual(follows["example"]["destination"], "")
            self.assertEqual(follows["example"]["live_status"], "is_offline")
            self.assertEqual(follows["example"]["audio_source"], "live")
            self.assertEqual(follows["other"]["destination"], "")
            self.assertEqual(follows["other"]["audio_source"], "live")

    def test_destination_is_saved_and_does_not_create_a_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "follows.json"
            folder = os.path.join(directory, "channel-folder")
            store = FollowStore(path)
            created = store.add("example", destination="  " + folder + "  ")
            self.assertEqual(created["follows"][0]["destination"], folder)
            self.assertFalse(os.path.exists(folder))
            reloaded = FollowStore(path)
            self.assertEqual(reloaded.get("example")["destination"], folder)
            cleared = store.set_destination("example", "   ")
            self.assertEqual(cleared["follows"][0]["destination"], "")
            self.assertEqual(FollowStore(path).get("example")["destination"], "")
            with self.assertRaises(ValueError):
                store.set_destination("example", None)
            with self.assertRaises(FollowError) as caught:
                store.set_destination("missing", folder)
            self.assertEqual(caught.exception.status, 404)
            self.assertFalse(os.path.exists(folder))

    def test_enabled_and_status_leave_destination_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "follows.json"
            folder = os.path.join(directory, "kept")
            store = FollowStore(path)
            store.add("example", destination=folder)
            store.set_enabled("example", False)
            self.assertEqual(store.get("example")["destination"], folder)
            updated = store.items()
            updated[0]["live_status"] = "is_offline"
            updated[0]["destination"] = os.path.join(directory, "ignored")
            store.merge_status(updated)
            self.assertEqual(store.get("example")["destination"], folder)
            self.assertEqual(store.get("example")["live_status"], "is_offline")
            self.assertFalse(os.path.isdir(folder))


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
            "id": "job-%d" % len(self.calls),
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
        self.assertEqual(stored["destination"], "")
        self.assertEqual(checker.view()["interval_seconds"], 180)


class FolderErrorManager(FakeManager):
    def start_job(
        self,
        url,
        destination=None,
        template=None,
        live_from_start=None,
        audio_source=None,
        **kwargs,
    ):
        if not isinstance(destination, str) or not destination.strip():
            raise JobError("Choose a folder before starting")
        return super().start_job(
            url,
            destination=destination,
            template=template,
            live_from_start=live_from_start,
            audio_source=audio_source,
            **kwargs,
        )


class FollowDestinationTests(unittest.TestCase):
    def test_channel_folder_is_used_and_not_created_until_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "follows.json")
            folder = os.path.join(directory, "channel")
            manager = FakeManager()
            checker = FollowChecker(manager, path=path, probe=lambda login: live_probe())
            checker.add("example", destination="  " + folder + "  ")
            config = {
                "destination": os.path.join(directory, "global"),
                "template": "%(id)s.%(ext)s",
            }
            with mock.patch("livegrab_lib.follows.load_config", return_value=config):
                checker.check_once()
            self.assertEqual(manager.calls[0]["destination"], folder)
            self.assertEqual(manager.calls[0]["audio_source"], "live")
            self.assertIs(manager.calls[0]["live_from_start"], False)
            self.assertFalse(os.path.exists(folder))
            self.assertFalse(os.path.exists(config["destination"]))

    def test_blank_channel_folder_uses_global_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "follows.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"follows": [{"login": "example", "enabled": True}]}, handle)
            manager = FakeManager()
            checker = FollowChecker(manager, path=path, probe=lambda login: live_probe())
            self.assertEqual(checker.view()["follows"][0]["destination"], "")
            global_folder = os.path.join(directory, "global")
            config = {"destination": global_folder, "template": "%(id)s.%(ext)s"}
            with mock.patch("livegrab_lib.follows.load_config", return_value=config):
                checker.check_once()
            self.assertEqual(manager.calls[0]["destination"], global_folder)
            self.assertEqual(manager.calls[0]["audio_source"], "live")
            self.assertFalse(os.path.isdir(global_folder))

    def test_empty_folders_surface_choose_a_folder_without_stopping_the_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "follows.json")
            kept = os.path.join(directory, "kept")
            manager = FolderErrorManager()
            checker = FollowChecker(manager, path=path, probe=lambda login: live_probe())
            checker.add("blank")
            checker.add("kept", destination=kept)
            config = {"destination": "", "template": "%(id)s.%(ext)s"}
            with mock.patch("livegrab_lib.follows.load_config", return_value=config), mock.patch(
                "livegrab_lib.jobs.subprocess.Popen",
                side_effect=AssertionError("yt-dlp was started"),
            ):
                view = checker.check_once()
            by_login = {item["login"]: item for item in view["follows"]}
            self.assertEqual(by_login["blank"]["error"], "Choose a folder before starting")
            self.assertIsNone(by_login["blank"]["last_started_id"])
            self.assertEqual(by_login["kept"]["last_started_id"], LIVE_ID)
            self.assertIsNone(by_login["kept"]["error"])
            self.assertEqual(manager.calls[0]["destination"], kept)
            self.assertEqual(manager.calls[0]["audio_source"], "live")
            self.assertFalse(os.path.isdir(kept))

    def test_real_start_job_reports_folder_error_without_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            follows_path = os.path.join(directory, "follows.json")
            jobs_path = os.path.join(directory, "jobs.json")
            manager = JobManager(jobs_path)
            checker = FollowChecker(manager, path=follows_path, probe=lambda login: live_probe())
            checker.add("example")
            config = {
                "destination": "",
                "template": "%(id)s.%(ext)s",
                "audio_source": "rewind",
                "live_from_start": True,
            }
            with mock.patch("livegrab_lib.follows.load_config", return_value=config), mock.patch(
                "livegrab_lib.jobs.load_config", return_value=config
            ), mock.patch(
                "livegrab_lib.jobs.subprocess.Popen",
                side_effect=AssertionError("yt-dlp was started"),
            ):
                view = checker.check_once()
            follow = view["follows"][0]
            self.assertEqual(follow["error"], "Choose a folder before starting")
            self.assertIsNone(follow["last_started_id"])
            self.assertEqual(manager.list_jobs(), [])


def _post_json(path, payload):
    body = json.dumps(payload).encode("utf-8")

    class Capture(LivegrabHandler):
        def __init__(self):
            self.path = path
            self.headers = {"Content-Length": str(len(body))}
            self.rfile = io.BytesIO(body)
            self.sent = None

        def _send_json(self, status, response):
            self.sent = (status, response)

    handler = Capture()
    handler.do_POST()
    return handler.sent


class FollowDestinationApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "follows.json")
        self.checker = FollowChecker(FakeManager(), path=path, probe=lambda login: live_probe())
        self._previous = LivegrabHandler.follows
        LivegrabHandler.follows = self.checker

    def tearDown(self):
        LivegrabHandler.follows = self._previous
        self._tmp.cleanup()

    def test_create_and_update_return_destination(self):
        folder = os.path.join(self._tmp.name, "from-api")
        status, created = _post_json("/api/follows", {"channel": "example"})
        self.assertEqual(status, 200)
        self.assertEqual(created["follows"][0]["destination"], "")
        status, updated = _post_json("/api/follows/example", {"destination": "  " + folder + "  "})
        self.assertEqual(status, 200)
        self.assertEqual(updated["follows"][0]["destination"], folder)
        self.assertFalse(os.path.exists(folder))
        status, toggled = _post_json("/api/follows/example", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(toggled["follows"][0]["enabled"])
        self.assertEqual(toggled["follows"][0]["destination"], folder)
        status, both = _post_json(
            "/api/follows/example",
            {"enabled": True, "destination": ""},
        )
        self.assertEqual(status, 200)
        self.assertTrue(both["follows"][0]["enabled"])
        self.assertEqual(both["follows"][0]["destination"], "")
        self.assertEqual(self.checker.view()["follows"][0]["destination"], "")

    def test_create_accepts_destination(self):
        folder = os.path.join(self._tmp.name, "created")
        status, created = _post_json(
            "/api/follows",
            {"channel": "https://www.twitch.tv/example", "destination": folder},
        )
        self.assertEqual(status, 200)
        self.assertEqual(created["follows"][0]["login"], "example")
        self.assertEqual(created["follows"][0]["destination"], folder)
        self.assertFalse(os.path.isdir(folder))

    def test_bad_destination_is_rejected(self):
        _post_json("/api/follows", {"channel": "example"})
        status, payload = _post_json("/api/follows/example", {"destination": 12})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "destination must be a string")
        self.assertEqual(self.checker.store.get("example")["destination"], "")
        status, missing = _post_json("/api/follows/missing", {"destination": "videos"})
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "follow not found")


class FollowAudioTests(unittest.TestCase):
    def test_saved_rewind_follow_auto_starts_with_rewind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "follows.json")
            folder = os.path.join(directory, "global")
            FollowChecker(FakeManager(), path=path, probe=lambda login: live_probe()).add(
                "example",
                audio_source="rewind",
            )
            manager = FakeManager()
            checker = FollowChecker(manager, path=path, probe=lambda login: live_probe())
            self.assertEqual(checker.view()["follows"][0]["audio_source"], "rewind")
            self.assertEqual(checker.view()["follows"][0]["destination"], "")
            config = {
                "destination": folder,
                "template": "%(id)s.%(ext)s",
                "audio_source": "live",
                "live_from_start": False,
            }
            with mock.patch("livegrab_lib.follows.load_config", return_value=config), mock.patch(
                "livegrab_lib.follows.subprocess.run",
                side_effect=AssertionError("yt-dlp was started"),
            ):
                checker.check_once()
            self.assertEqual(manager.calls[0]["url"], "https://www.twitch.tv/example")
            self.assertEqual(manager.calls[0]["destination"], folder)
            self.assertEqual(manager.calls[0]["audio_source"], "rewind")
            self.assertIs(manager.calls[0]["live_from_start"], True)
            self.assertFalse(os.path.isdir(folder))
            self.assertEqual(FollowStore(path).get("example")["audio_source"], "rewind")

    def test_follow_without_audio_source_auto_starts_with_live(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "follows.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "follows": [
                            {
                                "login": "example",
                                "enabled": True,
                                "destination": "",
                            }
                        ]
                    },
                    handle,
                )
            manager = FakeManager()
            checker = FollowChecker(manager, path=path, probe=lambda login: live_probe())
            self.assertEqual(checker.view()["follows"][0]["audio_source"], "live")
            global_folder = os.path.join(directory, "global")
            config = {
                "destination": global_folder,
                "template": "%(id)s.%(ext)s",
                "audio_source": "rewind",
                "live_from_start": True,
            }
            with mock.patch("livegrab_lib.follows.load_config", return_value=config), mock.patch(
                "livegrab_lib.follows.subprocess.run",
                side_effect=AssertionError("yt-dlp was started"),
            ):
                checker.check_once()
            self.assertEqual(manager.calls[0]["destination"], global_folder)
            self.assertEqual(manager.calls[0]["audio_source"], "live")
            self.assertIs(manager.calls[0]["live_from_start"], False)
            self.assertFalse(os.path.isdir(global_folder))

    def test_unknown_audio_source_loads_as_live_and_choice_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "follows.json"
            folder = os.path.join(directory, "channel")
            path.write_text(
                json.dumps(
                    {
                        "follows": [
                            {"login": "example", "enabled": True, "audio_source": "broadcast"},
                            {"login": "blank", "enabled": True, "audio_source": ""},
                            {"login": "missing", "enabled": True},
                            {"login": "number", "enabled": True, "audio_source": 1},
                            {"login": "spaced", "enabled": True, "audio_source": " REWIND "},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store = FollowStore(path)
            follows = {item["login"]: item for item in store.view()["follows"]}
            self.assertEqual(follows["example"]["audio_source"], "live")
            self.assertEqual(follows["blank"]["audio_source"], "live")
            self.assertEqual(follows["missing"]["audio_source"], "live")
            self.assertEqual(follows["number"]["audio_source"], "live")
            self.assertEqual(follows["spaced"]["audio_source"], "rewind")
            store.add("kept", destination=folder, audio_source="rewind")
            store.set_destination("kept", "")
            self.assertEqual(store.get("kept")["audio_source"], "rewind")
            self.assertEqual(store.get("kept")["destination"], "")
            store.set_audio_source("kept", "live")
            self.assertEqual(store.get("kept")["destination"], "")
            updated = store.items()
            updated[0]["audio_source"] = "rewind"
            updated[0]["live_status"] = "is_offline"
            store.merge_status(updated)
            self.assertEqual(store.get("example")["audio_source"], "live")
            self.assertEqual(store.get("example")["live_status"], "is_offline")
            with self.assertRaises(ValueError):
                store.set_audio_source("kept", "music")
            self.assertEqual(store.get("kept")["audio_source"], "live")
            with self.assertRaises(FollowError) as caught:
                store.set_audio_source("absent", "rewind")
            self.assertEqual(caught.exception.status, 404)
            reloaded = FollowStore(path)
            self.assertEqual(reloaded.get("kept")["audio_source"], "live")
            self.assertEqual(reloaded.get("spaced")["audio_source"], "rewind")
            self.assertFalse(os.path.isdir(folder))


class FollowAudioApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "follows.json")
        self.checker = FollowChecker(FakeManager(), path=path, probe=lambda login: live_probe())
        self._previous = LivegrabHandler.follows
        LivegrabHandler.follows = self.checker

    def tearDown(self):
        LivegrabHandler.follows = self._previous
        self._tmp.cleanup()

    def test_create_and_update_return_audio_source(self):
        folder = os.path.join(self._tmp.name, "from-api")
        status, created = _post_json("/api/follows", {"channel": "example"})
        self.assertEqual(status, 200)
        self.assertEqual(created["follows"][0]["audio_source"], "live")
        self.assertEqual(created["follows"][0]["destination"], "")
        status, updated = _post_json("/api/follows/example", {"audio_source": " ReWind "})
        self.assertEqual(status, 200)
        self.assertEqual(updated["follows"][0]["audio_source"], "rewind")
        self.assertEqual(updated["follows"][0]["destination"], "")
        status, both = _post_json(
            "/api/follows/example",
            {"destination": folder, "audio_source": "live", "enabled": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(both["follows"][0]["audio_source"], "live")
        self.assertEqual(both["follows"][0]["destination"], folder)
        self.assertFalse(both["follows"][0]["enabled"])
        self.assertFalse(os.path.isdir(folder))
        status, created_rewind = _post_json(
            "/api/follows",
            {"channel": "other", "audio_source": "rewind"},
        )
        self.assertEqual(status, 200)
        by_login = {item["login"]: item for item in created_rewind["follows"]}
        self.assertEqual(by_login["other"]["audio_source"], "rewind")
        self.assertEqual(by_login["example"]["audio_source"], "live")

    def test_bad_audio_source_is_rejected_without_other_changes(self):
        folder = os.path.join(self._tmp.name, "kept")
        _post_json("/api/follows", {"channel": "example", "destination": folder})
        status, payload = _post_json(
            "/api/follows/example",
            {"enabled": False, "audio_source": "music"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "audio_source must be 'live' or 'rewind'")
        stored = self.checker.store.get("example")
        self.assertTrue(stored["enabled"])
        self.assertEqual(stored["audio_source"], "live")
        self.assertEqual(stored["destination"], folder)
        status, missing = _post_json("/api/follows/missing", {"audio_source": "rewind"})
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "follow not found")


if __name__ == "__main__":
    unittest.main()
