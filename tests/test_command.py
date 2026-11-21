"""Tests for livegrab_lib.command.build_args."""

import inspect
import os
import tempfile
import unittest
from unittest import mock

from livegrab_lib.command import build_args

URL = "https://www.youtube.com/watch?v=example"
DESTINATION = "/tmp/livegrab-videos"
TEMPLATE = "%(title)s [%(id)s].%(ext)s"
YTDLP = "/opt/livegrab-test/yt-dlp"
WINDOWS_DESTINATION = "C:\\Users\\someone\\Videos"
JS_RUNTIME_TOKENS = ("node", "--js-runtimes", "ejs:github", "deno", "bun", "quickjs")


def call_build_args(live_from_start, destination=DESTINATION, ytdlp_path=YTDLP, node_path=""):
    """Call build_args without requiring a Node.js parameter."""
    kwargs = {}
    parameters = inspect.signature(build_args).parameters
    if "ytdlp_path" in parameters:
        kwargs["ytdlp_path"] = ytdlp_path
    if node_path is not _OMIT and "node_path" in parameters:
        kwargs["node_path"] = node_path
    return build_args(URL, destination, TEMPLATE, live_from_start, **kwargs)


class _Omit:
    pass


_OMIT = _Omit()


class BuildArgsTests(unittest.TestCase):
    def setUp(self):
        self._ffmpeg = mock.patch(
            "livegrab_lib.command.platform_ffmpeg_path",
            return_value=None,
        )
        self._ffmpeg.start()

    def tearDown(self):
        self._ffmpeg.stop()

    def assert_no_javascript_runtime(self, args):
        blob = "\n".join(str(part) for part in args)
        for token in JS_RUNTIME_TOKENS:
            self.assertNotIn(token, blob, msg=f"{token!r} found in {args}")

    def test_starts_with_ytdlp_newline_and_no_update(self):
        args = call_build_args(False)
        self.assertEqual(args[:3], [YTDLP, "--newline", "--no-update"])
        self.assertNotIn("--ffmpeg-location", args)
        self.assert_no_javascript_runtime(args)

    def test_ffmpeg_location_is_the_ffmpeg_directory(self):
        ffmpeg = "/opt/livegrab-test/bin/ffmpeg"
        with mock.patch(
            "livegrab_lib.command.platform_ffmpeg_path",
            return_value=ffmpeg,
        ):
            disabled = call_build_args(False)
            enabled = call_build_args(True)
        self.assertEqual(
            disabled[disabled.index("--ffmpeg-location") + 1],
            "/opt/livegrab-test/bin",
        )
        self.assertNotIn("--live-from-start", disabled)
        self.assertIn("--live-from-start", enabled)
        self.assertEqual(
            enabled[enabled.index("--ffmpeg-location") + 1],
            "/opt/livegrab-test/bin",
        )
        self.assertIn("--no-js-runtimes", disabled)
        self.assert_no_javascript_runtime(disabled)
        self.assert_no_javascript_runtime(enabled)

    def test_live_from_start_only_when_enabled(self):
        disabled = call_build_args(False)
        enabled = call_build_args(True)
        self.assertNotIn("--live-from-start", disabled)
        self.assertIn("--live-from-start", enabled)
        self.assert_no_javascript_runtime(disabled)
        self.assert_no_javascript_runtime(enabled)

    def test_live_choice_omits_live_from_start(self):
        args = call_build_args("live")
        self.assertNotIn("--live-from-start", args)
        self.assert_no_javascript_runtime(args)

    def test_rewind_choice_includes_live_from_start(self):
        args = call_build_args("rewind")
        self.assertIn("--live-from-start", args)
        self.assert_no_javascript_runtime(args)

    def test_windows_destination_is_joined(self):
        args = call_build_args(False, destination=WINDOWS_DESTINATION)
        self.assertIsInstance(args, list)
        output = args[args.index("-o") + 1]
        self.assertEqual(output, os.path.join(WINDOWS_DESTINATION, TEMPLATE))
        self.assertNotIn("--live-from-start", args)
        self.assert_no_javascript_runtime(args)
        enabled = call_build_args(True, destination=WINDOWS_DESTINATION)
        self.assertIn("--live-from-start", enabled)
        self.assertEqual(
            enabled[enabled.index("-o") + 1],
            os.path.join(WINDOWS_DESTINATION, TEMPLATE),
        )
        self.assert_no_javascript_runtime(enabled)

    def test_build_args_omits_javascript_runtimes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = os.path.join(directory, "runtime")
            with open(runtime, "w", encoding="utf-8") as handle:
                handle.write("x")
            destination = os.path.join(directory, "out")
            parameters = inspect.signature(build_args).parameters
            node_path = runtime if "node_path" in parameters else _OMIT
            args = call_build_args(True, destination=destination, node_path=node_path)
        self.assert_no_javascript_runtime(args)
        self.assertNotIn("/home/random", "\n".join(str(part) for part in args))
        self.assertNotIn("/usr/bin/yt-dlp", "\n".join(str(part) for part in args))

    def test_remux_output_template_and_url(self):
        args = call_build_args(True)
        remux = args.index("--remux-video")
        self.assertEqual(args[remux + 1], "mp4")
        output = args.index("-o")
        self.assertEqual(args[output + 1], os.path.join(DESTINATION, TEMPLATE))
        self.assertIn(URL, args)
        self.assertEqual(args[-1], URL)
        self.assert_no_javascript_runtime(args)


if __name__ == "__main__":
    unittest.main()
