"""Optional single-container demo: migrate once, then run the API and the worker.

This is not the normal deployment. Use it only where the host gives you one
web process and no separate worker. The API lifespan does not start the worker.

Set RUN_MIGRATIONS=0 to skip the locked upgrade. The default for this command
is to migrate, because a free web service has no separate release step.
Do not run more than one copy. Two copies would each run a worker.
"""

import os
import sys

from app.deploy.migrate import upgrade_head_with_lock
from app.deploy.supervise import run_processes


def main() -> int:
    if os.environ.get("RUN_MIGRATIONS", "1") != "0":
        upgrade_head_with_lock()
    port = os.environ.get("PORT", "8000")
    if not port.isdigit():
        raise SystemExit("PORT must be a number")
    return run_processes(
        [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                port,
            ],
            [sys.executable, "-m", "arq", "app.worker.settings.WorkerSettings"],
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
