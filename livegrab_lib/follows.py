"""Twitch channel follows and the live checker.

The checker reads metadata with yt-dlp --no-download. It never passes
--live-from-start on that check, and it never starts a grab itself.
A grab starts only through JobManager.start_job when the decision function
says the channel just went live on a video id that has not been started.
That call passes the audio_source stored on the follow. "live" is live
player audio and omits --live-from-start. "rewind" passes --live-from-start.
A missing or unknown audio_source is treated as "live".
Each follow can store its own destination folder. An empty destination
uses the global folder from config. This module does not create that folder.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from livegrab_lib.config import AUDIO_SOURCES, load_config, normalize_audio_source
from livegrab_lib.jobs import ACTIVE_STATES, JobError, pid_alive
from livegrab_lib.platform import config_dir, subprocess_kwargs, ytdlp_path

FOLLOWS_PATH = config_dir() / "follows.json"
CHECK_INTERVAL_SECONDS = 180
STARTUP_DELAY_SECONDS = 5

_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,24}$")
_TWITCH_HOSTS = {
    "twitch.tv",
    "www.twitch.tv",
    "m.twitch.tv",
    "go.twitch.tv",
    "player.twitch.tv",
}
_NOT_LOGINS = {
    "admin",
    "clip",
    "clips",
    "directory",
    "downloads",
    "drops",
    "inventory",
    "moderator",
    "popout",
    "products",
    "search",
    "settings",
    "subscriptions",
    "videos",
    "wallet",
}
_STATUS_FIELDS = (
    "live_status",
    "title",
    "video_id",
    "last_checked",
    "last_started_id",
    "error",
)
_PUBLIC_FIELDS = ("login", "url", "enabled", "destination", "audio_source") + _STATUS_FIELDS
_OFFLINE_MARKERS = (
    "not currently live",
    "is offline",
    "user is offline",
    "channel is offline",
)
_ID_BOUNDARY = r"(?<![A-Za-z0-9_-]){value}(?![A-Za-z0-9_-])"


class FollowError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def channel_url(login: str) -> str:
    return "https://www.twitch.tv/" + login


def parse_twitch_login(channel) -> str:
    """Return a Twitch login from a bare name or a twitch.tv URL.

    YouTube and any other host are rejected. The login is stored lowercase.
    """
    if not isinstance(channel, str):
        raise ValueError("channel is required")
    text = channel.strip()
    if not text:
        raise ValueError("channel is required")
    if _looks_like_url(text):
        return _login_from_url(text)
    if _LOGIN.fullmatch(text) and text.lower() not in _NOT_LOGINS:
        return text.lower()
    raise ValueError("enter a Twitch channel name or URL")


def should_start_grab(follow, probe) -> bool:
    """True when this probe should start one automatic grab.

    ``live_status`` must be ``is_live`` and the video id must be new.
    ``is_live`` false, offline, and was_live do not start. The same id as
    ``last_started_id`` does not start again. Disabled follows do not start.
    """
    if not isinstance(follow, dict) or not isinstance(probe, dict):
        return False
    if follow.get("enabled", True) is False:
        return False
    if not _probe_is_live(probe):
        return False
    video_id = _probe_video_id(probe)
    if not video_id:
        return False
    if video_id == follow.get("last_started_id"):
        return False
    return True


def check_follows(follows, probe, start=None, active=None):
    """Run one check cycle. ``probe`` is called instead of yt-dlp.

    ``probe(login)`` returns a metadata dict. ``start(follow, probe)`` is
    called only when :func:`should_start_grab` is true and ``active`` does
    not report a grab already running. Return True from ``start`` after a
    successful start so the video id is recorded. A failed probe keeps the
    previous live status, title, and video id.
    """
    if not isinstance(follows, list):
        raise ValueError("follows must be a list")
    checked = []
    for item in follows:
        if not isinstance(item, dict):
            continue
        checked.append(_check_one(dict(item), probe, start, active))
    return checked


def probe_args(login: str) -> list[str]:
    """Argv for a metadata check.

    No download flags and no --live-from-start. ``--no-js-runtimes`` keeps
    the check from launching a JavaScript runtime.
    """
    url = channel_url(login.strip().lower())
    args = [
        ytdlp_path(),
        "--no-download",
        "--no-update",
        "--no-warnings",
        "--ignore-no-formats-error",
        "--no-js-runtimes",
        "--dump-single-json",
        url,
    ]
    if "--live-from-start" in args or "--no-simulate" in args:
        raise RuntimeError("live check must not download")
    return args


def probe_channel(login: str, timeout: float = 60) -> dict:
    """Read live_status and the video id. Does not download the stream.

    On timeout, only the process started here is stopped. Other yt-dlp and
    ffmpeg processes are left alone.
    """
    args = probe_args(login)
    directory = tempfile.mkdtemp(prefix="livegrab-probe-")
    try:
        run_kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "stdin": subprocess.DEVNULL,
            "cwd": directory,
            "check": False,
        }
        run_kwargs.update(subprocess_kwargs())
        run_kwargs["shell"] = False
        completed = subprocess.run(args, **run_kwargs)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "live check timed out"}
    except OSError as exc:
        return {"ok": False, "error": "live check failed: %s" % exc}
    finally:
        _remove_tree(directory)
    return _probe_from_process(completed.returncode, completed.stdout or "", completed.stderr or "")


class FollowStore:
    def __init__(self, path: str = FOLLOWS_PATH):
        # The default path is a pathlib.Path. Later saves concatenate a suffix.
        self.path = os.fspath(path)
        self._lock = threading.Lock()
        self._follows: list[dict] = []
        self._load()

    def view(self) -> dict:
        with self._lock:
            return self._view_locked()

    def items(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._follows]

    def get(self, login: str):
        key = login.strip().lower()
        with self._lock:
            item = self._find_locked(key)
            return dict(item) if item else None

    def add(self, channel, destination=None, audio_source=None) -> dict:
        login = parse_twitch_login(channel)
        folder = "" if destination is None else _require_destination_text(destination)
        source = None if audio_source is None else normalize_audio_source(audio_source)
        with self._lock:
            if self._find_locked(login) is not None:
                raise ValueError("that channel is already followed")
            follow = _blank(login)
            follow["destination"] = folder
            if source is not None:
                follow["audio_source"] = source
            self._follows.append(follow)
            self._save_locked()
            return self._view_locked()

    def set_enabled(self, login: str, enabled) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        key = _lookup_key(login)
        with self._lock:
            item = self._find_locked(key)
            if item is None:
                raise FollowError("follow not found", 404)
            item["enabled"] = enabled
            self._save_locked()
            return self._view_locked()

    def set_destination(self, login: str, destination) -> dict:
        folder = _require_destination_text(destination)
        key = _lookup_key(login)
        with self._lock:
            item = self._find_locked(key)
            if item is None:
                raise FollowError("follow not found", 404)
            item["destination"] = folder
            self._save_locked()
            return self._view_locked()

    def set_audio_source(self, login: str, audio_source) -> dict:
        source = normalize_audio_source(audio_source)
        key = _lookup_key(login)
        with self._lock:
            item = self._find_locked(key)
            if item is None:
                raise FollowError("follow not found", 404)
            item["audio_source"] = source
            self._save_locked()
            return self._view_locked()

    def remove(self, login: str) -> dict:
        key = _lookup_key(login)
        with self._lock:
            kept = [item for item in self._follows if item["login"] != key]
            if len(kept) == len(self._follows):
                raise FollowError("follow not found", 404)
            self._follows = kept
            self._save_locked()
            return self._view_locked()

    def merge_status(self, updated: list[dict]) -> None:
        """Copy check results onto follows that are still stored.

        Enabled flags, destinations, audio choices, new follows, and
        deletions made during the probe are left as they are. Missing
        follows are not recreated.
        """
        by_login = {}
        for item in updated:
            if isinstance(item, dict) and isinstance(item.get("login"), str):
                by_login[item["login"]] = item
        with self._lock:
            changed = False
            for item in self._follows:
                fresh = by_login.get(item["login"])
                if fresh is None:
                    continue
                for key in _STATUS_FIELDS:
                    if item.get(key) != fresh.get(key):
                        item[key] = fresh.get(key)
                        changed = True
            if changed:
                self._save_locked()

    def _find_locked(self, login: str):
        for item in self._follows:
            if item.get("login") == login:
                return item
        return None

    def _view_locked(self) -> dict:
        return {
            "follows": [_public(item) for item in self._follows],
            "interval_seconds": CHECK_INTERVAL_SECONDS,
        }

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        raw = []
        if isinstance(stored, dict) and isinstance(stored.get("follows"), list):
            raw = stored["follows"]
        elif isinstance(stored, list):
            raw = stored
        for item in raw:
            follow = _normalize(item)
            if follow is None:
                continue
            if any(existing["login"] == follow["login"] for existing in self._follows):
                continue
            self._follows.append(follow)

    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.path + ".tmp"
        payload = {"follows": [_public(item) for item in self._follows]}
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class FollowChecker:
    """Background poll. Pass ``probe`` to avoid the network in tests."""

    def __init__(
        self,
        manager,
        path: str = FOLLOWS_PATH,
        probe=None,
        interval_seconds: int = CHECK_INTERVAL_SECONDS,
        startup_delay: float = STARTUP_DELAY_SECONDS,
    ):
        self.manager = manager
        self.store = FollowStore(path)
        self.probe = probe or probe_channel
        self.interval_seconds = interval_seconds
        self.startup_delay = startup_delay
        self._cycle = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="livegrab-follows",
            daemon=True,
        )
        self._thread.start()

    def view(self) -> dict:
        return self.store.view()

    def add(self, channel, destination=None, audio_source=None) -> dict:
        return self.store.add(channel, destination=destination, audio_source=audio_source)

    def set_enabled(self, login: str, enabled) -> dict:
        return self.store.set_enabled(login, enabled)

    def set_destination(self, login: str, destination) -> dict:
        return self.store.set_destination(login, destination)

    def set_audio_source(self, login: str, audio_source) -> dict:
        return self.store.set_audio_source(login, audio_source)

    def remove(self, login: str) -> dict:
        return self.store.remove(login)

    def check_once(self) -> dict:
        """One poll. Uses ``self.probe``, which tests replace with a fake."""
        with self._cycle:
            updated = check_follows(
                self.store.items(),
                self.probe,
                start=self._start_grab,
                active=self._grab_active,
            )
            self.store.merge_status(updated)
            return self.view()

    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay):
            return
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:
                sys.stderr.write("livegrab: follow check failed: %s\n" % exc)
            if self._stop.wait(self.interval_seconds):
                return

    def _start_grab(self, follow, probed) -> bool:
        login = follow.get("login")
        if not isinstance(login, str):
            return False
        current = self.store.get(login)
        if current is None or current.get("enabled") is False:
            return False
        url = channel_url(login)
        video_id = follow.get("video_id")
        if self._grab_active(url, video_id):
            return False
        config = load_config()
        # A blank per-channel folder keeps the global destination. start_job
        # reports "Choose a folder before starting" when that is blank too.
        chosen = _follow_destination(current)
        if not chosen:
            fallback = config.get("destination")
            chosen = fallback if isinstance(fallback, str) else ""
        source = _follow_audio_source(current)
        before = {job.get("id") for job in self._jobs()}
        try:
            job = self.manager.start_job(
                url,
                destination=chosen,
                template=config["template"],
                audio_source=source,
                live_from_start=source == "rewind",
            )
        except JobError as exc:
            raise FollowError(str(exc)) from exc
        if not isinstance(job, dict):
            return False
        if job.get("id") in before:
            return False
        if job.get("owner") != "livegrab":
            return False
        if job.get("state") == "error":
            raise FollowError(job.get("error") or "could not start grab")
        sys.stderr.write("livegrab: auto-started %s (%s)\n" % (url, video_id or ""))
        return True

    def _grab_active(self, url, video_id) -> bool:
        for job in self._jobs():
            if job.get("state") not in ACTIVE_STATES:
                continue
            if job.get("state") == "external" and not pid_alive(job.get("pid")):
                continue
            job_url = job.get("url") if isinstance(job.get("url"), str) else ""
            if _same_channel(job_url, url):
                return True
            if video_id and _job_mentions(job, video_id):
                return True
        return False

    def _jobs(self) -> list:
        listed = self.manager.list_jobs()
        return listed if isinstance(listed, list) else []


def _check_one(follow, probe, start, active) -> dict:
    if follow.get("enabled", True) is False:
        return follow
    login = follow.get("login")
    if not isinstance(login, str) or not login:
        follow["error"] = "channel is missing a login"
        follow["last_checked"] = _now()
        return follow
    try:
        probed = probe(login)
    except Exception as exc:
        probed = {"ok": False, "error": str(exc)}
    if _probe_failed(probed):
        follow["error"] = _error_text(probed)
        follow["last_checked"] = _now()
        return follow
    follow["live_status"] = _live_status(probed)
    follow["title"] = _title(probed)
    follow["video_id"] = _probe_video_id(probed)
    follow["last_checked"] = _now()
    follow["error"] = None
    if not should_start_grab(follow, probed):
        return follow
    url = follow.get("url") or channel_url(login)
    video_id = follow.get("video_id")
    try:
        if active is not None and active(url, video_id):
            return follow
        if start is not None and start(follow, probed):
            follow["last_started_id"] = video_id
    except Exception as exc:
        follow["error"] = _short(exc)
    return follow


def _probe_failed(probed) -> bool:
    return not isinstance(probed, dict) or probed.get("ok") is False


def _probe_is_live(probe: dict) -> bool:
    if probe.get("is_live") is False or probe.get("ok") is False:
        return False
    if probe.get("live_status") == "is_live":
        return True
    return probe.get("is_live") is True and probe.get("live_status") in (None, "", "is_live")


def _probe_video_id(probe: dict):
    if not isinstance(probe, dict):
        return None
    for key in ("video_id", "id"):
        value = probe.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _live_status(probe: dict):
    status = probe.get("live_status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    if probe.get("is_live") is True:
        return "is_live"
    if probe.get("is_live") is False:
        return "is_offline"
    return None


def _title(probe: dict):
    title = probe.get("title")
    if not isinstance(title, str):
        return None
    text = " ".join(title.split())
    if not text:
        return None
    return text[:500]


def _error_text(probed) -> str:
    if isinstance(probed, dict):
        return _short(probed.get("error") or "live check failed")
    return "live check failed"


def _short(value) -> str:
    text = " ".join(str(value).split())
    if len(text) > 300:
        return text[:300]
    return text or "live check failed"


def _looks_like_url(text: str) -> bool:
    lowered = text.lower()
    if "://" in text or "/" in text or "?" in text or "#" in text:
        return True
    if lowered.startswith("www.") or "twitch.tv" in lowered:
        return True
    if "youtube" in lowered or "youtu.be" in lowered:
        return True
    return "." in text


def _login_from_url(text: str) -> str:
    raw = text.strip()
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if host not in _TWITCH_HOSTS:
        raise ValueError("only Twitch channels can be followed")
    segments = [part for part in parsed.path.split("/") if part]
    login = ""
    if segments:
        login = segments[0]
    else:
        query = parse_qs(parsed.query)
        picked = query.get("channel") or []
        if picked and isinstance(picked[0], str):
            login = picked[0]
    if not login or login.lower() in _NOT_LOGINS or not _LOGIN.fullmatch(login):
        raise ValueError("only Twitch channels can be followed")
    return login.lower()


def _blank(login: str) -> dict:
    return {
        "login": login,
        "url": channel_url(login),
        "enabled": True,
        "destination": "",
        "audio_source": "live",
        "live_status": None,
        "title": None,
        "video_id": None,
        "last_checked": None,
        "last_started_id": None,
        "error": None,
    }


def _public(item: dict) -> dict:
    view = {}
    for key in _PUBLIC_FIELDS:
        if key == "enabled":
            view[key] = item.get(key) is not False
        elif key == "url":
            login = item.get("login")
            view[key] = item.get("url") or (channel_url(login) if isinstance(login, str) else None)
        elif key == "destination":
            view[key] = _follow_destination(item)
        elif key == "audio_source":
            view[key] = _follow_audio_source(item)
        else:
            view[key] = item.get(key)
    return view


def _normalize(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    login = item.get("login")
    if not isinstance(login, str) or not _LOGIN.fullmatch(login):
        return None
    follow = _blank(login.lower())
    follow["enabled"] = item.get("enabled") is not False
    follow["destination"] = _follow_destination(item)
    follow["audio_source"] = _follow_audio_source(item)
    for key in _STATUS_FIELDS:
        if key in item:
            follow[key] = item.get(key)
    if not isinstance(follow.get("title"), str):
        follow["title"] = None
    for key in ("live_status", "video_id", "last_checked", "last_started_id", "error"):
        value = follow.get(key)
        if value is not None and not isinstance(value, str):
            follow[key] = None
    return follow


def _follow_audio_source(follow) -> str:
    """Return ``live`` or ``rewind``. Missing and unknown values are ``live``."""
    if not isinstance(follow, dict):
        return "live"
    source = follow.get("audio_source")
    if isinstance(source, str):
        choice = source.strip().lower()
        if choice in AUDIO_SOURCES:
            return choice
    return "live"


def _follow_destination(follow) -> str:
    """Return a per-channel folder, or "" when the follow does not set one."""
    if not isinstance(follow, dict):
        return ""
    destination = follow.get("destination")
    if not isinstance(destination, str):
        return ""
    return destination.strip()


def _require_destination_text(destination) -> str:
    if not isinstance(destination, str):
        raise ValueError("destination must be a string")
    return destination.strip()


def _lookup_key(login: str) -> str:
    if not isinstance(login, str) or not login.strip() or "/" in login.strip():
        raise FollowError("follow not found", 404)
    return login.strip().lower()


def _same_channel(left: str, right: str) -> bool:
    if isinstance(left, str) and isinstance(right, str) and left.strip() == right.strip():
        return True
    try:
        return parse_twitch_login(left) == parse_twitch_login(right)
    except ValueError:
        return False


def _job_mentions(job: dict, video_id: str) -> bool:
    haystacks = [job.get("url"), job.get("output_path")]
    tail = job.get("log_tail")
    if isinstance(tail, list):
        haystacks.extend(tail)
    pattern = _ID_BOUNDARY.format(value=re.escape(video_id))
    for item in haystacks:
        if isinstance(item, str) and re.search(pattern, item):
            return True
    return False


def _probe_from_process(returncode: int, stdout: str, stderr: str) -> dict:
    payload = _parse_json(stdout)
    if isinstance(payload, dict):
        return _probe_from_json(payload)
    combined = "%s\n%s" % (stderr, stdout)
    if _text_says_offline(combined):
        return {
            "ok": True,
            "live_status": "is_offline",
            "video_id": None,
            "title": None,
            "is_live": False,
        }
    message = _last_error_line(stderr) or _last_error_line(stdout)
    if not message:
        message = "yt-dlp exited with status %s" % returncode
    return {"ok": False, "error": message}


def _probe_from_json(payload: dict) -> dict:
    entries = payload.get("entries")
    if payload.get("_type") == "playlist" or isinstance(entries, list):
        videos = [entry for entry in entries or [] if isinstance(entry, dict)]
        live = None
        for entry in videos:
            if entry.get("live_status") == "is_live" or entry.get("is_live") is True:
                live = entry
                break
        chosen = live if live is not None else (videos[0] if videos else None)
        if chosen is None:
            return {
                "ok": True,
                "live_status": "is_offline",
                "video_id": None,
                "title": None,
                "is_live": False,
            }
        return _probe_from_video(chosen)
    return _probe_from_video(payload)


def _probe_from_video(payload: dict) -> dict:
    status = payload.get("live_status")
    is_live = payload.get("is_live")
    if not isinstance(status, str) or not status.strip():
        if is_live is True:
            status = "is_live"
        elif is_live is False or payload.get("was_live") is True:
            status = "was_live" if payload.get("was_live") is True else "is_offline"
        else:
            status = None
    else:
        status = status.strip()
    video_id = payload.get("id")
    if isinstance(video_id, str) and video_id.strip():
        video_id = video_id.strip()
    else:
        video_id = None
    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    return {
        "ok": True,
        "live_status": status,
        "video_id": video_id,
        "title": title,
        "is_live": status == "is_live",
    }


def _parse_json(stdout: str):
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return payload


def _text_says_offline(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _OFFLINE_MARKERS)


def _last_error_line(text: str) -> str | None:
    chosen = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            chosen = stripped
    if not chosen:
        return None
    if chosen.lower().startswith("error:"):
        chosen = chosen.split(":", 1)[1].strip() or chosen
    return chosen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remove_tree(directory: str) -> None:
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if os.path.isdir(path):
                    _remove_tree(path)
                else:
                    os.remove(path)
            except OSError:
                pass
        os.rmdir(directory)
    except OSError:
        pass
