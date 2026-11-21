"""In-memory grab jobs, persisted to jobs.json.

On POSIX, yt-dlp is spawned in its own session so Stop can signal ffmpeg
with it. Stop sends SIGINT to that group first and waits up to about 30
seconds so yt-dlp can merge and remux. A process that is still exiting on
its own is not force-killed during that window, including while the
recording is still making progress toward a finished file. SIGKILL follows
only if the group is still alive at the deadline. On Windows, Stop breaks
the hidden process group when that does not open a console, waits the same
way, then ``taskkill /F /T`` removes whatever remains.

If the recording is still a ``.part``, ``.ytdl``, or ``.temp`` file after
the process is gone, Stop remuxes it to the intended ``.mp4`` with ffmpeg.
A PID Livegrab did not spawn is never signaled, including an already-running
external grab.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from livegrab_lib.command import build_args
from livegrab_lib.config import ensure_destination, load_config
from livegrab_lib.platform import config_dir, ffmpeg_path, subprocess_kwargs
from livegrab_lib.platform import ytdlp_path as platform_ytdlp_path
from livegrab_lib.progress import parse_progress_line

JOBS_PATH = config_dir() / "jobs.json"
LOG_TAIL = 30
ACTIVE_STATES = {"starting", "downloading", "merging", "external"}
RUNNING_STATES = {"starting", "downloading", "merging"}
# Historical pid. Never signal it.
NEVER_SIGNAL_PIDS = {149396}
# Two seconds is not enough for yt-dlp to merge and remux a live grab.
# Wait this long after the graceful signal. Force-kill only at the deadline.
_STOP_GRACE = 30.0
# POSIX stop: SIGINT, wait for a clean remux, then SIGKILL the group.
_POSIX_STOP_GRACE = _STOP_GRACE
_POSIX_STOP_POLL = 0.1
_POSIX_STOP_KILL_WAIT = 1.0
# Windows stop: CTRL_BREAK when it can be delivered, same grace, then taskkill.
_WINDOWS_STOP_GRACE = _STOP_GRACE
_WINDOWS_STOP_POLL = 0.1
_WINDOWS_STOP_KILL_WAIT = 2.0
_PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp")
FOLDER_REQUIRED = "Choose a folder before starting"
FOLDER_UNCREATABLE = "Could not create that folder"

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ID_PATTERNS = (
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"/v/([A-Za-z0-9_-]{11})"),
)

_PUBLIC_FIELDS = (
    "id",
    "url",
    "destination",
    "template",
    "live_from_start",
    "audio_source",
    "state",
    "owner",
    "pid",
    "size",
    "speed",
    "fragment",
    "fragment_total",
    "percent",
    "video",
    "audio",
    "log_tail",
    "output_path",
    "error",
    "started_at",
)


class JobError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def extract_video_id(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    for pattern in _ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    if _VIDEO_ID.fullmatch(text):
        return text
    return None


def read_argv(pid: int) -> list[str] | None:
    """Read /proc/<pid>/cmdline. Does not signal the process."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]


def pid_alive(pid) -> bool:
    """True when the process is still running.

    POSIX reads /proc and treats a zombie as gone. Windows reads the
    process exit code. Signal 0 is never sent.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(number)
    try:
        with open(f"/proc/{number}/stat", "r", encoding="utf-8") as handle:
            stat = handle.read()
    except OSError:
        return False
    match = re.search(r"\)\s+([A-Za-z])", stat)
    if not match:
        return True
    return match.group(1) != "Z"


def _windows_pid_alive(pid: int) -> bool:
    """True when Windows still has this process. Does not signal it."""
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, OSError):
        return False
    # PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE
    query = 0x1000
    still_active = 259
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return False
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(query, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_ytdlp(argv: list[str]) -> bool:
    """True when argv names yt-dlp, including a Windows yt-dlp.exe."""
    return any(_ytdlp_name(part) for part in argv)


def _ytdlp_name(part: str) -> bool:
    """True for yt-dlp, yt-dlp-…, and those names with a .exe suffix.

    The comparison is case-insensitive. Backslashes count as separators so
    a Windows path is recognized on any host.
    """
    if not isinstance(part, str) or not part:
        return False
    base = ntpath.basename(part)
    stem, ext = ntpath.splitext(base)
    if ext.casefold() == ".exe":
        base = stem
    folded = base.casefold()
    return folded == "yt-dlp" or folded.startswith("yt-dlp-")


def _mentions_target(argv: list[str], url: str, video_id: str | None) -> bool:
    for part in argv:
        if url and (part == url or url in part):
            return True
        if video_id and re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(video_id)}(?![A-Za-z0-9_-])",
            part,
        ):
            return True
    return False


def find_external_ytdlp(url: str, skip_pids: set[int] | None = None) -> list[dict]:
    """Return yt-dlp processes already grabbing this URL or video id.

    POSIX reads /proc cmdline. Windows reads the yt-dlp.exe image name and
    that process's command line. Nothing is signaled and no download starts.
    """
    skip = skip_pids or set()
    video_id = extract_video_id(url)
    found = []
    for pid, argv in _external_candidates():
        if pid in skip or pid == os.getpid():
            continue
        if not argv or not _is_ytdlp(argv):
            continue
        if not _mentions_target(argv, url, video_id):
            continue
        destination, template = _output_arg(argv)
        found.append(
            {
                "pid": pid,
                "argv": argv,
                "destination": destination,
                "template": template,
                "live_from_start": "--live-from-start" in argv,
                "exact_url": any(url and (part == url or url in part) for part in argv),
            }
        )
    found.sort(key=lambda item: (not item["exact_url"], item["pid"]))
    return found


def _external_candidates():
    """Yield (pid, argv) for processes the external-grab check may inspect."""
    if os.name == "nt":
        # A failed Windows read must not block a grab. Detection is best-effort.
        try:
            yield from _windows_ytdlp_argvs()
        except Exception: # noqa
            return
        return
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        argv = read_argv(pid)
        if argv:
            yield pid, argv


def _windows_ytdlp_argvs():
    """Yield (pid, argv) for a running yt-dlp.exe. Does not signal it."""
    for pid, image in _windows_process_names():
        argv = None
        if pid <= 0 or pid == os.getpid() or not _ytdlp_name(image):
            continue
        try:
            argv = _windows_command_argv(pid)
        except Exception: # noqa
            sys.stderr.write("Problem with yt-dlp on Windows\n")
        if not argv:
            continue
        if not _is_ytdlp(argv):
            argv = [image, *argv]
        yield pid, argv


def _windows_process_names():
    """Yield (pid, executable file name) without opening other processes."""
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, OSError):
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    # TH32CS_SNAPPROCESS
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid:
        yield from _windows_process_names_by_image()
        return
    found = False
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            found = False
        else:
            while True:
                name = entry.szExeFile
                pid = int(entry.th32ProcessID)
                if name and pid > 0:
                    found = True
                    yield pid, name
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    if not found:
        yield from _windows_process_names_by_image()


def _windows_process_names_by_image():
    """Yield (pid, full image path) when the process snapshot is unavailable."""
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, OSError):
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return
    enum = getattr(kernel32, "K32EnumProcesses", None)
    if enum is None:
        try:
            enum = ctypes.WinDLL("psapi", use_last_error=True).EnumProcesses
        except OSError:
            return
    enum.argtypes = (
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    enum.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    count = 512
    pids = []
    while count <= 65536:
        array = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not enum(array, ctypes.sizeof(array), ctypes.byref(needed)):
            return
        if needed.value < ctypes.sizeof(array):
            width = ctypes.sizeof(wintypes.DWORD)
            total = needed.value // width
            pids = [int(array[index]) for index in range(total) if int(array[index]) > 0]
            break
        count *= 2
    # PROCESS_QUERY_LIMITED_INFORMATION. Enough to read the image path.
    query = 0x1000
    for pid in pids:
        handle = kernel32.OpenProcess(query, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                image = buf.value
                if image:
                    yield pid, image
        finally:
            kernel32.CloseHandle(handle)


def _windows_command_argv(pid: int) -> list[str] | None:
    """Return a yt-dlp process command line. Does not signal the process."""
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, OSError):
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    except OSError:
        return None
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    ntdll.NtQueryInformationProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long

    # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, then limited + VM_READ.
    handle = kernel32.OpenProcess(0x0410, False, pid)
    if not handle:
        handle = kernel32.OpenProcess(0x1010, False, pid)
    if not handle:
        return None
    try:
        command = _windows_command_line(kernel32, ntdll, handle)
    finally:
        kernel32.CloseHandle(handle)
    if not command:
        return None
    return _split_command_line(command)


def _windows_command_line(kernel32, ntdll, handle) -> str | None:
    """Read CommandLine from the process PEB, including a 32-bit yt-dlp.exe."""
    wow64 = _query_pointer(ntdll, handle, 26)
    if wow64:
        return _command_line_at(kernel32, handle, wow64, 4)
    peb = _native_peb(ntdll, handle)
    if not peb:
        return None
    return _command_line_at(kernel32, handle, peb, _native_pointer_size())


def _native_pointer_size() -> int:
    import ctypes

    return ctypes.sizeof(ctypes.c_void_p)


def _native_peb(ntdll, handle) -> int | None:
    import ctypes

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    info = PROCESS_BASIC_INFORMATION()
    status = ntdll.NtQueryInformationProcess(
        handle,
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    )
    if status != 0 or not info.PebBaseAddress:
        return None
    return int(info.PebBaseAddress)


def _query_pointer(ntdll, handle, information_class: int) -> int | None:
    import ctypes

    value = ctypes.c_void_p()
    status = ntdll.NtQueryInformationProcess(
        handle,
        information_class,
        ctypes.byref(value),
        ctypes.sizeof(value),
        None,
    )
    if status != 0 or not value.value:
        return None
    return int(value.value)


def _command_line_at(kernel32, handle, peb: int, pointer_size: int) -> str | None:
    if pointer_size == 8:
        params_offset = 0x20
        command_offset = 0x70
        header_size = 16
        buffer_at = 8
    elif pointer_size == 4:
        params_offset = 0x10
        command_offset = 0x40
        header_size = 8
        buffer_at = 4
    else:
        return None
    params = _read_pointer(kernel32, handle, peb + params_offset, pointer_size)
    if not params:
        return None
    header = _read_bytes(kernel32, handle, params + command_offset, header_size)
    if header is None or len(header) < header_size:
        return None
    length = int.from_bytes(header[0:2], "little")
    if length <= 0 or length > 32767 or length % 2:
        return None
    buffer = int.from_bytes(header[buffer_at : buffer_at + pointer_size], "little")
    if not buffer:
        return None
    raw = _read_bytes(kernel32, handle, buffer, length)
    if raw is None or len(raw) != length:
        return None
    return raw.decode("utf-16-le", "replace")


def _read_pointer(kernel32, handle, address: int, pointer_size: int) -> int | None:
    raw = _read_bytes(kernel32, handle, address, pointer_size)
    if raw is None or len(raw) != pointer_size:
        return None
    value = int.from_bytes(raw, "little")
    return value or None


def _read_bytes(kernel32, handle, address: int, size: int) -> bytes | None:
    import ctypes

    if not address or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t()
    ok = kernel32.ReadProcessMemory(
        handle,
        ctypes.c_void_p(address),
        ctypes.cast(buffer, ctypes.c_void_p),
        size,
        ctypes.byref(read),
    )
    if not ok or read.value != size:
        return None
    return buffer.raw


def _split_command_line(command: str) -> list[str] | None:
    """Split a Windows command line the way CreateProcess does."""
    import ctypes
    from ctypes import wintypes

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None
    shell32.CommandLineToArgvW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    )
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    parsed = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not parsed or count.value <= 0:
        return None
    try:
        return [parsed[index] for index in range(count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(parsed, ctypes.c_void_p))


def _output_arg(argv: list[str]) -> tuple[str | None, str | None]:
    if "-o" not in argv:
        return None, None
    index = argv.index("-o")
    if index + 1 >= len(argv):
        return None, None
    output = argv[index + 1]
    return os.path.dirname(output), os.path.basename(output)


def _empty_stream() -> dict:
    return {
        "size": None,
        "speed": None,
        "fragment": None,
        "fragment_total": None,
        "percent": None,
    }


def _blank_job() -> dict:
    return {
        "id": uuid.uuid4().hex,
        "url": "",
        "destination": "",
        "template": "",
        "live_from_start": True,
        "audio_source": "rewind",
        "state": "starting",
        "owner": "livegrab",
        "pid": None,
        "size": None,
        "speed": None,
        "fragment": None,
        "fragment_total": None,
        "percent": None,
        "video": _empty_stream(),
        "audio": _empty_stream(),
        "log_tail": [],
        "output_path": None,
        "error": None,
        "started_at": _now(),
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def public_job(job: dict) -> dict:
    view = {}
    for key in _PUBLIC_FIELDS:
        value = job.get(key)
        if key in ("video", "audio"):
            stream = _empty_stream()
            if isinstance(value, dict):
                for field in stream:
                    if field in value:
                        stream[field] = value[field]
            view[key] = stream
        elif key == "log_tail":
            if isinstance(value, list):
                view[key] = [str(line) for line in value[-LOG_TAIL:]]
            else:
                view[key] = []
        else:
            view[key] = value
    source = view.get("audio_source")
    if source not in ("live", "rewind"):
        source = "live" if view.get("live_from_start") is False else "rewind"
    view["audio_source"] = source
    view["live_from_start"] = source == "rewind"
    return view


class JobManager:
    def __init__(self, path: Path = JOBS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._jobs: list[dict] = []
        self._spawned: set[int] = set()
        self._stop_requested: set[str] = set()
        self._hard_merged: set[str] = set()
        self._remux_done: set[str] = set()
        self._load()

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [public_job(job) for job in self._jobs]

    def start_job(
        self,
        url,
        destination=None,
        template=None,
        live_from_start=None,
        audio_source=None,
        ytdlp_path=None,
    ) -> dict:
        if not isinstance(url, str) or not url.strip():
            raise JobError("url is required")
        url = url.strip()
        if isinstance(audio_source, str):
            audio_source = audio_source.strip().lower()
        if audio_source is not None and audio_source not in ("live", "rewind"):
            raise JobError("audio_source must be 'live' or 'rewind'")
        if live_from_start is not None and not isinstance(live_from_start, bool):
            raise JobError("live_from_start must be a boolean")
        config = load_config()
        destination = _resolve_folder(destination, config.get("destination"))
        template = _choose_text(template, config["template"])
        if audio_source is not None:
            live_from_start = audio_source == "rewind"
        elif live_from_start is not None:
            audio_source = "rewind" if live_from_start else "live"
        else:
            audio_source = config.get("audio_source") or (
                "rewind" if config["live_from_start"] else "live"
            )
            live_from_start = audio_source == "rewind"
        if not template:
            raise JobError("template must be a non-empty string")

        video_id = extract_video_id(url)
        with self._lock:
            existing = self._active_match(url, video_id)
            if existing is not None:
                return public_job(existing)
            skip = set(self._spawned)
        external = find_external_ytdlp(url, skip_pids=skip)
        if external:
            return self._record_external(url, destination, template, live_from_start, external[0])

        try:
            ensure_destination(destination)
        except OSError:
            raise JobError(FOLDER_UNCREATABLE) from None
        binary = ytdlp_path if isinstance(ytdlp_path, str) and ytdlp_path.strip() else platform_ytdlp_path()
        args = build_args(
            url,
            destination,
            template,
            audio_source,
            binary,
        )
        job = _blank_job()
        job.update(
            {
                "url": url,
                "destination": destination,
                "template": template,
                "live_from_start": live_from_start,
                "audio_source": audio_source,
                "state": "starting",
                "owner": "livegrab",
            }
        )
        try:
            proc = subprocess.Popen(args, **_popen_kwargs())
        except OSError as exc:
            job["state"] = "error"
            job["error"] = f"Could not start yt-dlp: {exc}"
            with self._lock:
                self._jobs.append(job)
                self._save_locked()
            return public_job(job)

        job["pid"] = proc.pid
        with self._lock:
            self._spawned.add(proc.pid)
            self._jobs.append(job)
            self._save_locked()
            snapshot = public_job(job)
        thread = threading.Thread(
            target=self._pump,
            args=(job["id"], proc),
            name=f"livegrab-{job['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return snapshot

    def stop_job(self, job_id: str):
        """Stop a grab this process spawned. Leave every other pid alone."""
        with self._lock:
            job = self._find(job_id)
            if job is None:
                return 404, {"error": "job not found"}
            if job.get("owner") != "livegrab":
                return 409, {"error": "Livegrab will not stop a download it did not start"}
            if job.get("state") not in RUNNING_STATES:
                return 200, public_job(job)
            pid = job.get("pid")
            if not self._safe_to_signal(pid, job.get("url") or ""):
                return 409, {"error": "Livegrab will not stop a download it did not start"}
            self._stop_requested.add(job_id)
            job["state"] = "stopped"
            _remember_output(job)
            self._save_locked()
            watched = job
        if pid in NEVER_SIGNAL_PIDS:
            return 409, {"error": "Livegrab will not stop a download it did not start"}
        try:
            _signal_spawned(pid, lambda: _output_signature(watched))
        except ProcessLookupError:
            pass
        except (PermissionError, OSError):
            with self._lock:
                self._stop_requested.discard(job_id)
                job = self._find(job_id)
                if job is not None and job.get("state") == "stopped" and pid_alive(pid):
                    job["state"] = "downloading"
                    self._save_locked()
            return 409, {"error": "Livegrab will not stop a download it did not start"}
        with self._lock:
            job = self._find(job_id)
            if job is None:
                return 404, {"error": "job not found"}
            job["state"] = "stopped"
            self._finalize_stopped_locked(job)
            self._save_locked()
            return 200, public_job(job)

    def _safe_to_signal(self, pid, url: str) -> bool:
        try:
            number = int(pid)
        except (TypeError, ValueError):
            return False
        if number in NEVER_SIGNAL_PIDS or number not in self._spawned:
            return False
        argv = read_argv(number)
        if argv:
            if not _is_ytdlp(argv):
                return False
            return _mentions_target(argv, url, extract_video_id(url))
        # No /proc cmdline on Windows. The pid was spawned here, and the
        # historical pid was already refused above.
        if os.name == "nt":
            return pid_alive(number)
        return False

    def _record_external(self, url, destination, template, live_from_start, match) -> dict:
        external_destination = match.get("destination") or destination
        external_template = match.get("template") or template
        job = _blank_job()
        job.update(
            {
                "url": url,
                "destination": external_destination,
                "template": external_template,
                "live_from_start": bool(match.get("live_from_start", live_from_start)),
                "audio_source": "rewind" if match.get("live_from_start", live_from_start) else "live",
                "state": "external",
                "owner": "external",
                "pid": match.get("pid"),
            }
        )
        with self._lock:
            existing = self._active_match(url, extract_video_id(url))
            if existing is not None:
                return public_job(existing)
            self._jobs.append(job)
            self._save_locked()
            return public_job(job)

    def _active_match(self, url: str, video_id: str | None) -> dict | None:
        for job in reversed(self._jobs):
            if job.get("state") not in ACTIVE_STATES:
                continue
            if job.get("state") == "external" and not pid_alive(job.get("pid")):
                continue
            if _same_target(job.get("url"), url, video_id):
                return job
        return None

    def _find(self, job_id: str) -> dict | None:
        for job in self._jobs:
            if job.get("id") == job_id:
                return job
        return None

    def _pump(self, job_id: str, proc: subprocess.Popen) -> None:
        tail: list[str] = []
        try:
            stream = proc.stdout
            if stream is not None:
                for line in stream:
                    text = line.rstrip("\r\n")
                    tail.append(text)
                    if len(tail) > LOG_TAIL:
                        del tail[:-LOG_TAIL]
                    self._on_line(job_id, list(tail), parse_progress_line(text))
        finally:
            rc = proc.wait()
            stream = proc.stdout
            if stream is not None:
                stream.close()
            self._on_exit(job_id, rc)

    def _on_line(self, job_id: str, tail: list[str], parsed) -> None:
        with self._lock:
            job = self._find(job_id)
            if job is None or job.get("owner") != "livegrab":
                return
            job["log_tail"] = tail[-LOG_TAIL:]
            if parsed:
                self._apply_progress(job, parsed)
            self._save_locked()

    def _apply_progress(self, job: dict, parsed: dict) -> None:
        if job.get("state") in ("finished", "interrupted", "stopped", "external"):
            return
        phase = parsed.get("phase")
        if phase == "downloading":
            if job["id"] not in self._hard_merged and job.get("state") in (
                "starting",
                "downloading",
                "merging",
            ):
                job["state"] = "downloading"
            if job.get("state") in ("starting", "downloading", "merging"):
                _copy_metrics(job, parsed)
                _copy_stream(job, parsed)
        elif phase == "merging":
            if parsed.get("output_path"):
                job["output_path"] = parsed["output_path"]
            if parsed.get("reason") == "merge" and job.get("state") not in (
                "finished",
                "error",
                "interrupted",
                "stopped",
                "external",
            ):
                self._hard_merged.add(job["id"])
                job["state"] = "merging"
        elif phase == "error":
            job["state"] = "error"
            job["error"] = parsed.get("message") or "yt-dlp reported an error"

    def _finalize_stopped_locked(self, job: dict) -> None:
        """Remux a leftover partial once. Caller holds ``self._lock``."""
        job_id = job.get("id")
        if isinstance(job_id, str) and job_id in self._remux_done:
            if not _is_ready_mp4(job.get("output_path")):
                _remember_output(job)
            return
        if isinstance(job_id, str):
            self._remux_done.add(job_id)
        _finalize_partial(job)

    def _on_exit(self, job_id: str, rc: int) -> None:
        with self._lock:
            job = self._find(job_id)
            if job is None:
                return
            pid = job.get("pid")
            if isinstance(pid, int):
                self._spawned.discard(pid)
            stopped = job_id in self._stop_requested
            if stopped:
                job["state"] = "stopped"
            elif isinstance(rc, int) and rc < 0:
                job["state"] = "interrupted"
            elif rc == 0 and job.get("state") != "error":
                job["state"] = "finished"
            else:
                job["state"] = "error"
                if not job.get("error"):
                    job["error"] = f"yt-dlp exited with status {rc}"
            if stopped:
                self._finalize_stopped_locked(job)
            else:
                _remember_output(job)
            self._save_locked()

    def _load(self) -> None:
        raw_jobs = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError):
            stored = None
        if isinstance(stored, dict) and isinstance(stored.get("jobs"), list):
            raw_jobs = stored["jobs"]
        elif isinstance(stored, list):
            raw_jobs = stored
        changed = False
        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            job = public_job(item)
            if not job.get("id"):
                job["id"] = uuid.uuid4().hex
                changed = True
            if job.get("state") in RUNNING_STATES and not pid_alive(job.get("pid")):
                job["state"] = "interrupted"
                changed = True
            self._jobs.append(job)
        if changed:
            self._save_locked()

    def _save_locked(self) -> None:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        payload = {"jobs": [public_job(job) for job in self._jobs]}
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def _popen_kwargs() -> dict:
    """Argv spawn options.

    ``shell`` stays off, so POSIX does not open a terminal. A new session
    is POSIX-only and lets Stop signal yt-dlp and its children together.
    Windows console flags come from ``subprocess_kwargs``.
    """
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "bufsize": 1,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    kwargs.update(subprocess_kwargs())
    kwargs["shell"] = False
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    return kwargs


def _signal_spawned(pid: int, progress=None) -> None:
    """Stop a process this app spawned.

    Windows hides the console, breaks that process group when it can do so
    without opening a window, waits for yt-dlp to merge and remux, then
    ``taskkill /F /T`` removes the parent and every child that is still
    recording. POSIX sends SIGINT to its session, waits the same way, then
    SIGKILL the group if anything is still alive. ``progress`` is polled
    during the grace window. A changing recording is progress toward exit
    and is never a reason to force-kill before the deadline.
    """
    number = int(pid)
    if number in NEVER_SIGNAL_PIDS or number <= 0:
        raise PermissionError("refusing to signal this pid")
    if sys.platform == "win32":
        _signal_spawned_windows(number, progress)
        return
    _signal_spawned_posix(number, progress)


def _signal_spawned_windows(pid: int, progress=None) -> None:
    """Kill a Windows yt-dlp tree without opening a cmd window.

    ``os.kill(..., SIGTERM)`` ends only the parent. The ffmpeg or sleeper
    child keeps recording. A break is attempted first, and only through the
    process that was already started hidden. When the break is delivered,
    Stop waits for yt-dlp to exit on its own. Then ``taskkill /F /T /PID``
    force-kills that pid and its descendants. The parent is still the
    taskkill target when the break could not be delivered, which is the
    usual case with ``CREATE_NO_WINDOW``.
    """
    if _windows_ctrl_break(pid):
        _wait_pid_exit(pid, _WINDOWS_STOP_GRACE, progress)
    _windows_taskkill_tree(pid)
    _wait_pid_exit(pid, _WINDOWS_STOP_KILL_WAIT)


def _windows_ctrl_break(pid: int) -> bool:
    """Send CTRL_BREAK to a hidden process group. Never creates a console."""
    event = getattr(signal, "CTRL_BREAK_EVENT", None)
    if event is None or pid in NEVER_SIGNAL_PIDS or pid <= 0:
        return False
    try:
        os.kill(pid, event)
    except OSError:
        return False
    return True


def _windows_taskkill_tree(pid: int) -> None:
    """``taskkill /F /T /PID``. The window for taskkill itself stays hidden."""
    number = int(pid)
    if number in NEVER_SIGNAL_PIDS or number <= 0:
        raise PermissionError("refusing to signal this pid")
    command = ["taskkill", "/F", "/T", "/PID", str(number)]
    kwargs = subprocess_kwargs()
    kwargs["stdin"] = subprocess.DEVNULL
    kwargs["stdout"] = subprocess.DEVNULL
    kwargs["stderr"] = subprocess.DEVNULL
    kwargs["shell"] = False
    kwargs["check"] = False
    subprocess.run(command, check=False, **kwargs)


def _wait_pid_exit(pid: int, timeout: float, progress=None) -> bool:
    return _wait_while_exiting(
        lambda: pid_alive(pid),
        timeout,
        _WINDOWS_STOP_POLL,
        progress,
    )


def _signal_spawned_posix(pid: int, progress=None) -> None:
    _kill_process_group(pid, signal.SIGINT)
    if _wait_for_group_exit(pid, _POSIX_STOP_GRACE, progress):
        return
    _kill_process_group(pid, signal.SIGKILL)
    _wait_for_group_exit(pid, _POSIX_STOP_KILL_WAIT)


def _kill_process_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return


def _wait_for_group_exit(pgid: int, timeout: float, progress=None) -> bool:
    return _wait_while_exiting(
        lambda: _group_has_live_members(pgid),
        timeout,
        _POSIX_STOP_POLL,
        progress,
    )


def _wait_while_exiting(still_alive, timeout: float, poll: float, progress=None) -> bool:
    """Wait until the process is gone, or until ``timeout`` seconds pass.

    Returns True when it has exited. Force-kill is the caller's job and
    only after this returns False. While the deadline has not been reached,
    progress toward a finished file does not end the wait early with a kill.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    previous = _call_progress(progress)
    while True:
        if not still_alive():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        current = _call_progress(progress)
        if current != previous:
            # The recording is still moving (partial growing, mp4 appearing).
            # That is progress toward exit. Do not force-kill in this window.
            previous = current
        time.sleep(min(poll, remaining))


def _call_progress(progress):
    if progress is None:
        return None
    try:
        return progress()
    except Exception: # noqa
        return None


def _group_has_live_members(pgid: int) -> bool:
    """True when a non-zombie process still belongs to ``pgid``."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pid_alive(pgid)
    for entry in entries:
        if not entry.isdigit():
            continue
        fields = _proc_stat_fields(int(entry))
        if fields is None:
            continue
        state, pgrp = fields
        if pgrp == pgid and state != "Z":
            return True
    return False


def _proc_stat_fields(pid: int):
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            stat = handle.read()
    except OSError:
        return None
    end = stat.rfind(")")
    if end < 0:
        return None
    tail = stat[end + 1 :].split()
    if len(tail) < 4:
        return None
    try:
        pgrp = int(tail[2])
    except ValueError:
        return None
    return tail[0], pgrp


def _remember_output(job: dict) -> None:
    found = _locate_output(job)
    if found:
        job["output_path"] = found


def _locate_output(job: dict) -> str | None:
    """Return a recording path that exists. Do not delete the file."""
    recorded = job.get("output_path")
    if isinstance(recorded, str) and recorded.strip():
        recorded_path = recorded.strip()
        ready = _ready_mp4_near(Path(recorded_path))
        if ready is not None:
            return str(ready)
        if os.path.isfile(recorded_path):
            return recorded_path
        part = (
            recorded_path
            if recorded_path.casefold().endswith(".part")
            else recorded_path + ".part"
        )
        if os.path.isfile(part):
            return part
        if recorded_path.casefold().endswith(".part"):
            finished = recorded_path[: -len(".part")]
            if finished and os.path.isfile(finished):
                return finished
    destination = job.get("destination")
    if not isinstance(destination, str) or not destination.strip():
        return None
    folder = Path(destination)
    try:
        entries = [entry for entry in folder.iterdir() if entry.is_file()]
    except OSError:
        return None
    started = _started_timestamp(job.get("started_at"))
    fresh = []
    for entry in entries:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if started is not None and mtime < started - 2:
            continue
        if started is None and not _is_partial_name(entry.name):
            continue
        fresh.append((mtime, entry))
    if not fresh:
        return None
    finished = [item for item in fresh if _is_ready_mp4_path(item[1])]
    partials = [item for item in fresh if _is_partial_name(item[1].name)]
    media = [
        item
        for item in partials
        if not item[1].name.casefold().endswith(".ytdl")
    ]
    if finished:
        pool = finished
    elif media:
        pool = media
    elif partials:
        pool = partials
    else:
        pool = fresh
    pool.sort(key=lambda item: item[0])
    return str(pool[-1][1])


def _output_signature(job: dict):
    """Fingerprint the recording files. A change is progress toward exit."""
    paths = []
    recorded = job.get("output_path")
    if isinstance(recorded, str) and recorded.strip():
        paths.append(recorded.strip())
    destination = job.get("destination")
    if isinstance(destination, str) and destination.strip():
        try:
            for entry in Path(destination).iterdir():
                if entry.is_file():
                    paths.append(str(entry))
        except OSError:
            pass
    signature = []
    seen = set()
    for text in paths:
        if text in seen:
            continue
        seen.add(text)
        try:
            st = os.stat(text)
        except OSError:
            signature.append((text, None))
            continue
        signature.append((text, st.st_size, getattr(st, "st_mtime_ns", st.st_mtime)))
    signature.sort()
    return tuple(signature)


def _finalize_partial(job: dict) -> None:
    """Turn a leftover partial into the intended mp4 after the process is gone."""
    try:
        _finalize_partial_work(job)
    except OSError as exc:
        if not job.get("error"):
            job["error"] = f"Could not remux the partial recording: {exc}"
        if not _is_ready_mp4(job.get("output_path")):
            _remember_output(job)


def _finalize_partial_work(job: dict) -> None:
    partial = _find_partial_media(job)
    if partial is None:
        ready = _find_ready_output(job)
        if ready is not None:
            job["output_path"] = str(ready)
            return
        _remember_output(job)
        located = job.get("output_path")
        if (isinstance(located, str) and _is_partial_name(os.path.basename(located))) and not job.get("error"):
            job["error"] = (
                "Partial recording was left in place; no media file could be remuxed"
            )
        return
    final = _intended_mp4(partial)
    if os.path.normcase(str(final)) == os.path.normcase(str(partial)):
        job["output_path"] = str(partial)
        job["error"] = "Could not choose an mp4 path for the partial recording"
        return
    if _file_size(final) > 0:
        job["output_path"] = str(final)
        _delete_partial_after_success(partial, final)
        return
    if _file_size(partial) <= 0:
        job["output_path"] = str(partial)
        job["error"] = "Partial recording is empty; left it in place"
        return
    binary = ffmpeg_path()
    if not binary:
        job["output_path"] = str(partial)
        job["error"] = "ffmpeg was not found; partial recording was left in place"
        return
    error = _remux_partial(binary, partial, final)
    if error is None and _file_size(final) > 0:
        job["output_path"] = str(final)
        _delete_partial_after_success(partial, final)
        return
    _unlink_quiet(final)
    job["output_path"] = str(partial)
    job["error"] = error or "Could not remux the partial recording"


def _find_ready_output(job: dict) -> Path | None:
    recorded = job.get("output_path")
    if isinstance(recorded, str) and recorded.strip():
        ready = _ready_mp4_near(Path(recorded.strip()))
        if ready is not None:
            return ready
    located = _locate_output(job)
    if located:
        ready = _ready_mp4_near(Path(located))
        if ready is not None:
            return ready
    return None


def _find_partial_media(job: dict) -> Path | None:
    candidates = []
    recorded = job.get("output_path")
    if isinstance(recorded, str) and recorded.strip():
        candidates.append(Path(recorded.strip()))
    located = _locate_output(job)
    if located:
        candidates.append(Path(located))
    candidates.extend(_recent_partials(job))
    media = []
    seen = set()
    for candidate in candidates:
        for path in _media_variants(candidate):
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            if path.is_file() and _is_media_partial(path):
                media.append(path)
    if not media:
        return None

    def sort_key(path: Path):
        name = path.name.casefold()
        size = _file_size(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        return (not name.endswith(".part"), -size, -mtime)

    media.sort(key=sort_key)
    return media[0]


def _recent_partials(job: dict) -> list[Path]:
    destination = job.get("destination")
    if not isinstance(destination, str) or not destination.strip():
        return []
    started = _started_timestamp(job.get("started_at"))
    found = []
    try:
        entries = list(Path(destination).iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_file() or not _is_partial_name(entry.name):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if started is not None and mtime < started - 2:
            continue
        found.append(entry)
    return found


def _media_variants(path: Path):
    name = path.name.casefold()
    if name.endswith(".ytdl"):
        base = str(path)[: -len(".ytdl")]
        yield Path(base + ".part")
        yield Path(base + ".temp")
        return
    if name.endswith((".part", ".temp")):
        yield path
        return
    yield Path(str(path) + ".part")
    yield Path(str(path) + ".temp")


def _remux_partial(ffmpeg: str, partial: Path, final: Path) -> str | None:
    """Return None when ``final`` is a non-empty mp4. Otherwise an error string.

    A clean exit is the normal success. A stopped live stream is often
    truncated, so ffmpeg can also exit non-zero after writing a real mp4.
    That file is kept when it has an ``ftyp`` header and a size above zero.
    MPEG-TS inputs get one retry with the AAC bitstream filter when the
    first copy does not exit cleanly.
    """
    mpegts = _looks_like_mpegts(partial)
    filters = [False]
    if mpegts:
        filters.append(True)
    errors = []
    stash = final.with_name(final.name + ".livegrab-stash")
    _unlink_quiet(stash)
    try:
        for index, bitstream_filter in enumerate(filters):
            _unlink_quiet(final)
            result = _run_ffmpeg(
                _ffmpeg_command(ffmpeg, partial, final, bitstream_filter)
            )
            if _ffmpeg_succeeded(result, final):
                _unlink_quiet(stash)
                return None
            if _file_size(final) > 0 and _has_ftyp(final):
                _unlink_quiet(stash)
                os.replace(final, stash)
            else:
                _unlink_quiet(final)
            errors.append(_ffmpeg_failure_text(result))
            if index == 0 and not mpegts:
                break
        if _file_size(stash) > 0:
            os.replace(stash, final)
            return None
    finally:
        _unlink_quiet(stash)
    return "; ".join(error for error in errors if error) or "ffmpeg remux failed"


def _ffmpeg_command(ffmpeg: str, partial: Path, final: Path, bitstream_filter: bool) -> list[str]:
    command = [ffmpeg, "-y", "-i", str(partial), "-c", "copy"]
    if bitstream_filter:
        command.extend(["-bsf:a", "aac_adtstoasc"])
    command.extend(["-movflags", "+faststart", str(final)])
    return command


def _run_ffmpeg(command: list[str]):
    """Run ffmpeg. On Windows the console stays hidden, same as yt-dlp."""
    kwargs = subprocess_kwargs()
    kwargs["stdin"] = subprocess.DEVNULL
    kwargs["stdout"] = subprocess.PIPE
    kwargs["stderr"] = subprocess.PIPE
    kwargs["shell"] = False
    kwargs["check"] = False
    kwargs["text"] = True
    kwargs["encoding"] = "utf-8"
    kwargs["errors"] = "replace"
    return subprocess.run(command, check=False, **kwargs)


def _ffmpeg_succeeded(result, final: Path) -> bool:
    if result is None or getattr(result, "returncode", 1) != 0:
        return False
    return _file_size(final) > 0


def _has_ftyp(path: Path) -> bool:
    """True when the file starts like an MPEG-4 container."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(64)
    except OSError:
        return False
    return b"ftyp" in header


def _ffmpeg_failure_text(result) -> str:
    if result is None:
        return "ffmpeg remux failed"
    code = getattr(result, "returncode", 1)
    err = getattr(result, "stderr", "") or ""
    out = getattr(result, "stdout", "") or ""
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    if not isinstance(err, str):
        err = str(err)
    if not isinstance(out, str):
        out = str(out)
    detail = (err or out).strip()
    if not detail:
        return f"ffmpeg remux failed ({code})"
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    detail = " | ".join(lines[-4:])
    if len(detail) > 400:
        detail = detail[-400:]
    return f"ffmpeg remux failed ({code}): {detail}"


def _looks_like_mpegts(path: Path) -> bool:
    base = _strip_partial_suffix(path.name)
    if base.casefold().endswith(".ts"):
        return True
    try:
        with open(path, "rb") as handle:
            blob = handle.read(188 * 64)
    except OSError:
        return False
    if len(blob) < 188:
        return False
    limit = min(len(blob) - 188, 188 * 8)
    for offset in range(limit + 1):
        if blob[offset] != 0x47 or blob[offset + 188] != 0x47:
            continue
        if offset + 376 < len(blob) and blob[offset + 376] != 0x47:
            continue
        return True
    return False


def _delete_partial_after_success(partial: Path, final: Path) -> None:
    """Delete the partial only after the mp4 exists and is non-empty."""
    if not _is_media_partial(partial):
        return
    if _file_size(final) <= 0:
        return
    if os.path.normcase(str(partial)) == os.path.normcase(str(final)):
        return
    _unlink_quiet(partial)


def _unlink_quiet(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        return


def _is_partial_name(name: str) -> bool:
    if not isinstance(name, str) or not name:
        return False
    lowered = name.casefold()
    return any(lowered.endswith(suffix) for suffix in _PARTIAL_SUFFIXES)


def _is_media_partial(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith((".part", ".temp"))


def _strip_partial_suffix(name: str) -> str:
    lowered = name.casefold()
    for suffix in _PARTIAL_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _intended_mp4(partial: Path) -> Path:
    base = _strip_partial_suffix(partial.name)
    if not base:
        base = partial.name + ".mp4"
    intended = partial.with_name(base)
    if intended.suffix.casefold() != ".mp4":
        intended = intended.with_suffix(".mp4")
    if intended.name.casefold() == partial.name.casefold():
        intended = partial.with_name(base + ".mp4")
    return intended


def _ready_mp4_near(path: Path) -> Path | None:
    if _is_ready_mp4_path(path):
        return path
    if _is_partial_name(path.name):
        final = _intended_mp4(path)
        if _is_ready_mp4_path(final):
            return final
    return None


def _is_ready_mp4(path) -> bool:
    if not isinstance(path, str) or not path.strip():
        return False
    return _is_ready_mp4_path(Path(path.strip()))


def _is_ready_mp4_path(path: Path) -> bool:
    if _is_partial_name(path.name):
        return False
    if path.suffix.casefold() != ".mp4":
        return False
    return _file_size(path) > 0


def _file_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    return 0


def _started_timestamp(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _resolve_folder(value, fallback) -> str:
    """Return the folder to record into.

    A missing value uses the saved folder. A blank value is refused.
    """
    if value is None:
        chosen = fallback
    else:
        if not isinstance(value, str):
            raise JobError("expected a string")
        chosen = value
        if not chosen.strip():
            raise JobError(FOLDER_REQUIRED)
    if not isinstance(chosen, str) or not chosen.strip():
        raise JobError(FOLDER_REQUIRED)
    return chosen.strip()


def _choose_text(value, fallback: str) -> str:
    if value is None:
        return fallback if isinstance(fallback, str) else ""
    if not isinstance(value, str):
        raise JobError("expected a string")
    text = value.strip()
    return text or (fallback if isinstance(fallback, str) else "")


def _same_target(left, right, video_id: str | None) -> bool:
    if isinstance(left, str) and isinstance(right, str) and left.strip() == right.strip():
        return True
    if not video_id:
        return False
    return extract_video_id(left if isinstance(left, str) else "") == video_id


def _copy_metrics(job: dict, parsed: dict) -> None:
    for key in ("size", "speed", "fragment", "fragment_total", "percent"):
        if parsed.get(key) is not None:
            job[key] = parsed[key]


def _copy_stream(job: dict, parsed: dict) -> None:
    name = parsed.get("stream")
    if name not in ("video", "audio"):
        return
    stream = job.get(name)
    if not isinstance(stream, dict):
        stream = _empty_stream()
        job[name] = stream
    for key in ("size", "speed", "fragment", "fragment_total", "percent"):
        if parsed.get(key) is not None:
            stream[key] = parsed[key]
