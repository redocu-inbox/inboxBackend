"""Run child processes and stop all of them when one exits or a signal arrives.

This is a process supervisor. It is not started from the API lifespan.
"""

import signal
import subprocess
import time


def run_processes(commands: list[list[str]]) -> int:
    """Start each command. Return 0 after SIGTERM or SIGINT. Return 1 if a child exits first.

    Children are reaped. A child that is still running when another exits, or
    when a signal arrives, receives SIGTERM and then SIGKILL if it does not stop.
    """
    if not commands:
        raise ValueError("run_processes requires at least one command")
    processes = [subprocess.Popen(command) for command in commands]
    stopping = False

    def _stop(signum: int, _frame) -> None:
        del signum
        nonlocal stopping
        stopping = True
        _terminate(processes)

    previous_term = signal.signal(signal.SIGTERM, _stop)
    previous_int = signal.signal(signal.SIGINT, _stop)
    try:
        while True:
            if any(process.poll() is not None for process in processes):
                if not stopping:
                    _terminate(processes)
                _wait(processes)
                return 0 if stopping else 1
            if stopping:
                _wait(processes)
                return 0
            time.sleep(0.2)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def _terminate(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()


def _wait(processes: list[subprocess.Popen]) -> None:
    deadline = time.monotonic() + 10
    for process in processes:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
