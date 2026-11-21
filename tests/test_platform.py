"""Cross-platform config directory and yt-dlp lookup.

sys.platform, os.environ, and shutil.which are patched so these assertions
do not depend on this Linux machine's real home or PATH.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from livegrab_lib.platform import config_dir, ffmpeg_path, ffprobe_path, ytdlp_path


class ConfigDirTests(unittest.TestCase):
    def test_win32_uses_appdata(self):
        appdata = "C:\\Users\\someone\\AppData\\Roaming"
        with mock.patch("livegrab_lib.platform.sys.platform", "win32"), mock.patch.dict(
            os.environ, {"APPDATA": appdata}
        ):
            result = config_dir()
        self.assertEqual(Path(result), Path(appdata) / "livegrab")

    def test_darwin_uses_application_support(self):
        home = "/Users/someone"
        with mock.patch("livegrab_lib.platform.sys.platform", "darwin"), mock.patch.dict(
            os.environ, {"HOME": home}
        ):
            result = config_dir()
        self.assertEqual(
            Path(result),
            Path(home) / "Library" / "Application Support" / "livegrab",
        )

    def test_linux_uses_xdg_config_home(self):
        xdg = "/tmp/livegrab-xdg-config"
        with mock.patch("livegrab_lib.platform.sys.platform", "linux"), mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": xdg}
        ):
            result = config_dir()
        self.assertEqual(Path(result), Path(xdg) / "livegrab")

    def test_linux_uses_home_config_without_xdg(self):
        home = "/tmp/livegrab-home"
        with (
            mock.patch("livegrab_lib.platform.sys.platform", "linux"),
            mock.patch.dict(os.environ, {"HOME": home}, clear=False)
        ):
            os.environ.pop("XDG_CONFIG_HOME", None)
            result = config_dir()
        self.assertEqual(Path(result), Path(home) / ".config" / "livegrab")


class YtdlpPathTests(unittest.TestCase):
    def test_which_result_is_not_a_home_random_path(self):
        found = "/opt/livegrab-test/yt-dlp"
        with mock.patch("livegrab_lib.platform.shutil.which", return_value=found):
            path = ytdlp_path()
        self.assertEqual(path, found)
        self.assertNotIn("/home/random", path)

    def test_missing_which_result_is_not_a_home_random_path(self):
        with mock.patch("livegrab_lib.platform.shutil.which", return_value=None):
            path = ytdlp_path()
        self.assertIsInstance(path, str)
        self.assertNotIn("/home/random", path)

    def test_missing_tools_fall_back(self):
        with (
            mock.patch("livegrab_lib.platform._program_dirs", return_value=[]),
            mock.patch("livegrab_lib.platform.shutil.which", return_value=None),
            mock.patch("livegrab_lib.platform._install_dirs", return_value=[]),
        ):
            self.assertEqual(ytdlp_path(), "yt-dlp")
            self.assertIsNone(ffmpeg_path())
            self.assertIsNone(ffprobe_path())
            self.assertNotIn("/home/random", ytdlp_path())

    def test_tools_and_bin_beat_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "tools"
            bindir = root / "bin"
            tools.mkdir()
            bindir.mkdir()
            ffmpeg = tools / "ffmpeg"
            ffprobe = bindir / "ffprobe"
            _write_exec(ffmpeg)
            _write_exec(ffprobe)
            with (
                mock.patch("livegrab_lib.platform._source_root", return_value=root),
                mock.patch("livegrab_lib.platform._is_frozen", return_value=False),
                mock.patch(
                    "livegrab_lib.platform.shutil.which",
                    return_value="/usr/bin/ffmpeg",
                ),
            ):
                self.assertEqual(ffmpeg_path(), str(ffmpeg))
                self.assertEqual(ffprobe_path(), str(ffprobe))
                self.assertNotIn("/home/random", ffmpeg_path())

    def test_frozen_executable_directory_beats_meipass_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            exe_dir = base / "app"
            meipass = base / "mei"
            exe_dir.mkdir()
            meipass.mkdir()
            bundled = exe_dir / "yt-dlp.exe"
            extracted = meipass / "yt-dlp.exe"
            _write_exec(bundled)
            _write_exec(extracted)
            with (
                mock.patch("livegrab_lib.platform._is_frozen", return_value=True),
                mock.patch(
                    "livegrab_lib.platform.sys.executable",
                    str(exe_dir / "Livegrab.exe"),
                ),
                mock.patch(
                    "livegrab_lib.platform.sys._MEIPASS",
                    str(meipass),
                    create=True,
                ),
                mock.patch("livegrab_lib.platform._source_root", return_value=None),
                mock.patch(
                    "livegrab_lib.platform.shutil.which",
                    return_value="/usr/bin/yt-dlp",
                ),
            ):
                self.assertEqual(ytdlp_path(), str(bundled))

    def test_frozen_meipass_when_executable_dir_has_no_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            exe_dir = base / "app"
            meipass = base / "mei"
            exe_dir.mkdir()
            meipass.mkdir()
            extracted = meipass / "ffmpeg.exe"
            _write_exec(extracted)
            with (
                mock.patch("livegrab_lib.platform._is_frozen", return_value=True),
                mock.patch(
                    "livegrab_lib.platform.sys.executable",
                    str(exe_dir / "Livegrab.exe"),
                ),
                mock.patch(
                    "livegrab_lib.platform.sys._MEIPASS",
                    str(meipass),
                    create=True,
                ),
                mock.patch("livegrab_lib.platform._source_root", return_value=None),
                mock.patch("livegrab_lib.platform.shutil.which", return_value=None),
                mock.patch("livegrab_lib.platform._install_dirs", return_value=[]),
            ):
                self.assertEqual(ffmpeg_path(), str(extracted))

    def test_windows_exe_without_unix_mode_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ffmpeg.exe"
            binary.write_bytes(b"MZ")
            binary.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with (
                mock.patch("livegrab_lib.platform.os.name", "nt"),
                mock.patch("livegrab_lib.platform._source_root", return_value=root),
                mock.patch("livegrab_lib.platform._is_frozen", return_value=False),
                mock.patch("livegrab_lib.platform.shutil.which", return_value=None),
                mock.patch("livegrab_lib.platform._install_dirs", return_value=[]),
            ):
                self.assertEqual(ffmpeg_path(), str(binary))


def _write_exec(path: Path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
