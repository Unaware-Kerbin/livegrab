"""Quit Livegrab. These tests do not start a download."""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from livegrab_lib.server import (
    LivegrabHandler,
    exit_process,
    schedule_process_exit,
    stop_running_jobs,
)

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 8765


def _port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _handler(manager=None):
    handler = LivegrabHandler.__new__(LivegrabHandler)
    handler.path = "/api/shutdown"
    handler.headers = {"Content-Length": "0"}
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    handler.server = mock.Mock()
    handler.client_address = (HOST, 0)
    handler.requestline = "POST /api/shutdown HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.close_connection = False
    handler.manager = manager
    return handler


class StopRunningJobsTests(unittest.TestCase):
    def test_stops_each_running_job_and_logs_a_failure(self):
        stopped = []

        class Manager:
            def list_jobs(self):
                return [
                    {"id": "running-1", "state": "downloading"},
                    {"id": "done", "state": "finished"},
                    {"id": "other", "state": "external"},
                    {"id": "boom", "state": "starting"},
                    {"id": "running-2", "state": "merging"},
                    {"id": "", "state": "downloading"},
                    "not-a-job",
                ]

            def stop_job(self, job_id):
                if job_id == "boom":
                    raise RuntimeError("remux failed")
                stopped.append(job_id)
                return 200, {"id": job_id, "state": "stopped"}

        with self.assertLogs("livegrab", level="ERROR") as logs:
            stop_running_jobs(Manager())

        self.assertEqual(stopped, ["running-1", "running-2"])
        self.assertIn("boom", "\n".join(logs.output))
        self.assertIn("remux failed", "\n".join(logs.output))

    def test_list_failure_does_not_raise(self):
        class Manager:
            def list_jobs(self):
                raise RuntimeError("jobs unavailable")

            def stop_job(self, job_id):
                raise AssertionError(job_id)

        with self.assertLogs("livegrab", level="ERROR") as logs:
            stop_running_jobs(Manager())

        self.assertIn("could not list jobs", "\n".join(logs.output))

    def test_missing_manager_is_a_no_op(self):
        stop_running_jobs(None)


class ExitProcessTests(unittest.TestCase):
    def test_shutdown_then_exit_this_process(self):
        server = mock.Mock()
        order = []
        server.shutdown.side_effect = lambda: order.append("shutdown")
        server.server_close.side_effect = lambda: order.append("close")

        def _exit(code):
            order.append(code)

        with mock.patch("livegrab_lib.server.os._exit", side_effect=_exit):
            exit_process(server)

        self.assertEqual(order, ["shutdown", "close", 0])

    def test_exit_continues_when_shutdown_fails(self):
        server = mock.Mock()
        server.shutdown.side_effect = RuntimeError("socket")
        with mock.patch("livegrab_lib.server.os._exit") as exit_fn, self.assertLogs(
            "livegrab", level="ERROR"
        ):
            exit_process(server)
        exit_fn.assert_called_once_with(0)
        server.server_close.assert_called_once_with()

    def test_schedule_flushes_then_exits(self):
        server = mock.Mock()
        with mock.patch("livegrab_lib.server.exit_process") as exit_fn, mock.patch(
            "livegrab_lib.server.time.sleep"
        ) as sleep:
            thread = schedule_process_exit(server, delay=0.5)
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        sleep.assert_called_once_with(0.5)
        exit_fn.assert_called_once_with(server)


class ShutdownHandlerTests(unittest.TestCase):
    def setUp(self):
        self._previous = LivegrabHandler.manager

    def tearDown(self):
        LivegrabHandler.manager = self._previous

    def test_post_answers_ok_and_stops_running_jobs(self):
        stopped = []

        class Manager:
            def list_jobs(self):
                return [
                    {"id": "running-1", "state": "downloading"},
                    {"id": "done", "state": "finished"},
                    {"id": "boom", "state": "merging"},
                ]

            def stop_job(self, job_id):
                if job_id == "boom":
                    raise RuntimeError("remux failed")
                stopped.append(job_id)
                return 200, {"id": job_id, "state": "stopped"}

        LivegrabHandler.manager = Manager()
        handler = _handler(LivegrabHandler.manager)
        with mock.patch("livegrab_lib.server.schedule_process_exit") as schedule, self.assertLogs(
            "livegrab", level="ERROR"
        ) as logs:
            handler.do_POST()

        self.assertEqual(stopped, ["running-1"])
        self.assertIn("boom", "\n".join(logs.output))
        schedule.assert_called_once_with(handler.server)
        raw = handler.wfile.getvalue()
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 200"))
        self.assertIn(b"Connection: close", head)
        self.assertEqual(json.loads(body.decode("utf-8")), {"ok": True})
        self.assertTrue(handler.close_connection)

    def test_post_still_answers_when_listing_jobs_fails(self):
        class Manager:
            def list_jobs(self):
                raise RuntimeError("jobs unavailable")

        LivegrabHandler.manager = Manager()
        handler = _handler(LivegrabHandler.manager)
        with mock.patch("livegrab_lib.server.schedule_process_exit") as schedule, self.assertLogs(
            "livegrab", level="ERROR"
        ):
            handler.do_POST()
        schedule.assert_called_once_with(handler.server)
        self.assertIn(b'{"ok": true}', handler.wfile.getvalue())


class PageTests(unittest.TestCase):
    def test_quit_control_is_in_the_header(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        header = html.split("<header", 1)[1].split("</header>", 1)[0]
        self.assertIn(">Quit Livegrab<", header)
        self.assertIn('id="quit-livegrab"', header)
        self.assertNotIn(">Stop<", header)
        self.assertIn("window.confirm", script)
        self.assertIn('fetch("/api/shutdown", { method: "POST" })', script)


class ServerExitTests(unittest.TestCase):
    def test_post_shutdown_exits_the_process(self):
        if not _port_free(PORT):
            self.skipTest("127.0.0.1:8765 is already in use")
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = directory
            env["APPDATA"] = directory
            env["PYTHONUNBUFFERED"] = "1"
            stderr_path = Path(directory) / "stderr.txt"
            with open(stderr_path, "wb") as stderr:
                proc = subprocess.Popen(
                    [sys.executable, str(ROOT / "livegrab.py")],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                )
            try:
                ready = _wait_for_listen(proc, 10)
                if not ready:
                    code = proc.poll()
                    err = stderr_path.read_text(encoding="utf-8", errors="replace")
                    if code is not None and "could not bind" in err:
                        self.skipTest("127.0.0.1:8765 was taken before Livegrab bound it")
                    self.fail(f"server did not listen (exit {code}): {err}")
                started = time.monotonic()
                request = urllib.request.Request(
                    f"http://{HOST}:{PORT}/api/shutdown",
                    data=b"",
                    method="POST",
                    headers={"Content-Length": "0"},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    status = response.status
                code = proc.wait(timeout=5)
                elapsed = time.monotonic() - started
            except subprocess.TimeoutExpired:
                self.fail("process was still running 5s after POST /api/shutdown")
            finally:
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"ok": True})
            self.assertEqual(code, 0)
            self.assertLess(elapsed, 5)


def _wait_for_listen(proc: subprocess.Popen, timeout: float) -> bool:
    found = threading.Event()

    def _read() -> None:
        stream = proc.stdout
        if stream is None:
            return
        while True:
            line = stream.readline()
            if not line:
                return
            if b"Livegrab listening on http://127.0.0.1:8765" in line:
                found.set()
                return

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    if found.wait(timeout):
        return True
    return proc.poll() is None and found.is_set()


if __name__ == "__main__":
    unittest.main()
