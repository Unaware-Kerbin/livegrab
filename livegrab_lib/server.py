"""Local HTTP server for Livegrab. Binds to 127.0.0.1 only."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from livegrab_lib.config import config_view, normalize_audio_source, update_config
from livegrab_lib.follows import FollowChecker, FollowError
from livegrab_lib.jobs import RUNNING_STATES, JobError, JobManager
from livegrab_lib.platform import ffmpeg_path, ytdlp_path

HOST = "127.0.0.1"
PORTS = (8765,)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_BODY = 1_000_000
# Let the shutdown response leave the socket before this process ends.
_SHUTDOWN_FLUSH_SECONDS = 0.5

log = logging.getLogger("livegrab")

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


def stop_running_jobs(manager) -> None:
    """Stop each running grab. One failure is logged and does not skip the rest."""
    if manager is None:
        return
    try:
        jobs = list(manager.list_jobs())
    except Exception:
        log.exception("Livegrab shutdown could not list jobs")
        return
    for job in jobs:
        if not isinstance(job, dict) or job.get("state") not in RUNNING_STATES:
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            continue
        try:
            manager.stop_job(job_id)
        except Exception:
            log.exception("Livegrab shutdown failed to stop job %s", job_id)


def exit_process(server) -> None:
    """Stop the HTTP server, then end this Livegrab process.

    ``os._exit`` ends the Windows console and the Linux process. It does
    not signal any other process.
    """
    try:
        server.shutdown()
    except Exception:
        log.exception("Livegrab could not shut down the HTTP server")
    try:
        server.server_close()
    except Exception:
        log.exception("Livegrab could not close the HTTP server")
    os._exit(0)


def schedule_process_exit(server, delay: float = _SHUTDOWN_FLUSH_SECONDS):
    """Give the shutdown response time to flush, then exit this process."""

    def _exit() -> None:
        time.sleep(delay)
        exit_process(server)

    thread = threading.Thread(target=_exit, name="livegrab-exit", daemon=False)
    thread.start()
    return thread


class LivegrabServer(ThreadingHTTPServer):
    # Windows treats SO_REUSEADDR as permission to bind a port that is
    # already listening. Leave it off there so an existing listener keeps
    # 8765. POSIX still allows a quick restart without taking that port.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


class LivegrabHandler(BaseHTTPRequestHandler):
    manager: JobManager | None = None
    follows: FollowChecker | None = None
    project_root = PROJECT_ROOT

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        try:
            self._handle_get()
        except Exception:
            self._send_json(500, {"error": "internal error"})

    def do_POST(self):
        try:
            self._handle_post()
        except FollowError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except JobError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
        except Exception:
            self._send_json(500, {"error": "internal error"})

    def do_DELETE(self):
        try:
            self._handle_delete()
        except FollowError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            self._send_json(500, {"error": "internal error"})

    def _handle_get(self):
        path = urlparse(self.path).path
        if path == "/":
            self._serve_index()
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path == "/api/config":
            self._send_json(200, config_view())
            return
        if path == "/api/jobs":
            self._require_manager()
            self._send_json(200, {"jobs": self.manager.list_jobs()})
            return
        if path == "/api/follows":
            self._require_follows()
            self._send_json(200, self.follows.view())
            return
        self._send_json(404, {"error": "not found"})

    def _handle_post(self):
        path = urlparse(self.path).path
        if path == "/api/shutdown":
            self._begin_shutdown()
            return
        if path == "/api/config":
            self._send_json(200, update_config(self._read_json()))
            return
        if path == "/api/jobs":
            self._require_manager()
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise JobError("job must be a JSON object")
            job = self.manager.start_job(
                payload.get("url"),
                destination=payload.get("destination") if "destination" in payload else None,
                template=payload.get("template") if "template" in payload else None,
                live_from_start=payload.get("live_from_start") if "live_from_start" in payload else None,
                audio_source=payload.get("audio_source") if "audio_source" in payload else None,
            )
            self._send_json(200, job)
            return
        if path.startswith("/api/jobs/") and path.endswith("/stop"):
            self._require_manager()
            job_id = unquote(path[len("/api/jobs/") : -len("/stop")]).strip("/")
            if not job_id or "/" in job_id:
                self._send_json(404, {"error": "job not found"})
                return
            status, body = self.manager.stop_job(job_id)
            self._send_json(status, body)
            return
        if path == "/api/follows":
            self._require_follows()
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ValueError("follow must be a JSON object")
            destination = payload.get("destination") if "destination" in payload else None
            audio_source = payload.get("audio_source") if "audio_source" in payload else None
            self._send_json(
                200,
                self.follows.add(
                    payload.get("channel"),
                    destination=destination,
                    audio_source=audio_source,
                ),
            )
            return
        if path == "/api/follows/check":
            self._require_follows()
            self._send_json(200, self.follows.check_once())
            return
        if path.startswith("/api/follows/"):
            self._require_follows()
            login = self._follow_login(path)
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ValueError("follow must be a JSON object")
            has_enabled = "enabled" in payload
            has_destination = "destination" in payload
            has_audio = "audio_source" in payload
            if not has_enabled and not has_destination and not has_audio:
                raise ValueError("enabled must be a boolean")
            if has_enabled and not isinstance(payload.get("enabled"), bool):
                raise ValueError("enabled must be a boolean")
            if has_destination and not isinstance(payload.get("destination"), str):
                raise ValueError("destination must be a string")
            if has_audio:
                normalize_audio_source(payload.get("audio_source"))
            view = None
            if has_enabled:
                view = self.follows.set_enabled(login, payload.get("enabled"))
            if has_destination:
                view = self.follows.set_destination(login, payload.get("destination"))
            if has_audio:
                view = self.follows.set_audio_source(login, payload.get("audio_source"))
            self._send_json(200, view)
            return
        self._send_json(404, {"error": "not found"})

    def _handle_delete(self):
        path = urlparse(self.path).path
        if path.startswith("/api/follows/"):
            self._require_follows()
            login = self._follow_login(path)
            self._send_json(200, self.follows.remove(login))
            return
        self._send_json(404, {"error": "not found"})

    def _begin_shutdown(self) -> None:
        stop_running_jobs(self.manager)
        self.close_connection = True
        try:
            self._send_json(200, {"ok": True}, close=True)
            try:
                self.wfile.flush()
            except Exception:
                log.exception("Livegrab could not flush the shutdown response")
        except Exception:
            log.exception("Livegrab could not send the shutdown response")
        schedule_process_exit(self.server)

    def _require_manager(self):
        if self.manager is None:
            raise JobError("server is not ready", 500)

    def _require_follows(self):
        if self.follows is None:
            raise FollowError("server is not ready", 500)

    def _follow_login(self, path: str) -> str:
        login = unquote(path[len("/api/follows/") :]).strip("/")
        if not login or "/" in login or login == "check":
            raise FollowError("follow not found", 404)
        return login

    def _serve_index(self):
        index = self.project_root / "static" / "index.html"
        if index.is_file():
            self._send_bytes(200, index.read_bytes(), "text/html; charset=utf-8")
            return
        self._send_bytes(200, b"Livegrab API is up", "text/plain; charset=utf-8")

    def _serve_static(self, relative: str):
        relative = unquote(relative)
        if not relative or relative.startswith("/") or "\\" in relative:
            self._send_json(404, {"error": "not found"})
            return
        root = (self.project_root / "static").resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            self._send_json(404, {"error": "not found"})
            return
        if not candidate.is_file():
            self._send_json(404, {"error": "not found"})
            return
        content_type = _MIME.get(candidate.suffix.lower(), "application/octet-stream")
        self._send_bytes(200, candidate.read_bytes(), content_type)

    def _read_json(self):
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: int, payload, close: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", close=close)

    def _send_bytes(self, status: int, body: bytes, content_type: str, close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def bind_server(host: str = HOST, ports=PORTS):
    if host != HOST or tuple(ports) != (8765,):
        raise RuntimeError("Livegrab binds to 127.0.0.1:8765 only")
    last_error = None
    for port in ports:
        try:
            return LivegrabServer((host, port), LivegrabHandler), port
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"could not bind {host} on {ports}: {last_error}")


def main() -> int:
    manager = JobManager()
    follows = FollowChecker(manager)
    LivegrabHandler.manager = manager
    LivegrabHandler.follows = follows
    httpd, port = bind_server()
    follows.start()
    print(f"Livegrab listening on http://127.0.0.1:{port}", flush=True)
    print(
        "Livegrab tools yt-dlp=%s ffmpeg=%s" % (ytdlp_path(), ffmpeg_path() or ""),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
