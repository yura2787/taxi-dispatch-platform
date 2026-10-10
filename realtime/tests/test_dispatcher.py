"""The dispatcher process: its entry point, the metrics port and a clean stop on
SIGTERM, in-process and as a real `python -m app.dispatcher`."""

import asyncio
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app import config, dispatcher

SERVICE_ROOT = Path(dispatcher.__file__).resolve().parents[1]


async def wait_for_log(caplog, text: str) -> None:
    async with asyncio.timeout(2):
        while text not in caplog.text:
            await asyncio.sleep(0.005)


async def test_sigterm_finishes_the_pass_and_stops(redis, caplog):
    # `redis` makes sure the dispatcher talks to the test DB, not to dev data.
    task = asyncio.create_task(dispatcher.serve())
    # The signal handlers are installed before this line is logged.
    await wait_for_log(caplog, "Dispatcher started")

    os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(task, timeout=2)
    assert "Dispatcher stopped" in caplog.text
    # The handlers are removed again: SIGTERM is back to its default.
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_main_serves_metrics_on_the_dispatcher_port(monkeypatch):
    ports, served = [], []

    async def fake_serve():
        served.append(True)

    monkeypatch.setattr(dispatcher, "start_http_server", ports.append)
    monkeypatch.setattr(dispatcher, "serve", fake_serve)

    dispatcher.main()

    assert ports == [config.DISPATCHER_METRICS_PORT]
    assert served == [True]


def read_metrics(port: int, *, timeout: float = 5) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=1) as reply:
                text = reply.read().decode()
            if 'drivers_online{status="online"}' in text:  # after the first pass
                return text
        except OSError:
            pass  # not listening yet
        if time.monotonic() > deadline:
            pytest.fail(f"no dispatcher metrics on port {port}")
        time.sleep(0.05)


def test_real_process_serves_metrics_and_exits_cleanly_on_sigterm(redis):
    process = subprocess.Popen(
        [sys.executable, "-m", "app.dispatcher"],
        cwd=SERVICE_ROOT,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        metrics = read_metrics(config.DISPATCHER_METRICS_PORT)

        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=5)
    finally:
        process.kill()

    assert "reaper_iteration_seconds_count 1.0" in metrics
    assert "ws_connections" not in metrics  # gateway metrics are not imported here
    assert process.returncode == 0
    assert "Dispatcher stopped" in stderr
