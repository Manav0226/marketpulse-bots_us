"""
launch_us_cloud.py - Railway/worker-friendly launcher for the US cloud stack.

Runs the US execution bot, US intel bot, father bot, and VM scheduler together
inside a single long-running worker so they can share one persistent state path.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from marketpulse_runtime import resolve_log_dir, resolve_state_dir


ROOT = Path(__file__).parent.parent
PYTHON = sys.executable
LOG_DIR = resolve_log_dir()
STATE_DIR = resolve_state_dir()
MAX_RESTARTS = 5

BOTS = {
    "father": {"script": "bot_father.py", "enabled": True},
    "us": {"script": "bot_us_crypto_v4.py", "enabled": True},
    "us_intel": {"script": "bot_us_crypto_intel.py", "enabled": True},
    "us_scheduler": {"script": "vm_us_scheduler.py", "enabled": True},
}

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [US_CLOUD] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "launch_us_cloud.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("launch_us_cloud")


def _bot_env() -> dict:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("MARKETPULSE_LOG_DIR", str(LOG_DIR))
    env.setdefault("MARKETPULSE_STATE_DIR", str(STATE_DIR))
    env.setdefault("MARKETPULSE_TZ", env.get("MARKETPULSE_TZ", "UTC"))
    return env


def _start_bot(name: str, script: str) -> subprocess.Popen:
    log_path = LOG_DIR / f"{name}_railway.log"
    log_fh = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [PYTHON, str(ROOT / script)],
        cwd=str(ROOT),
        env=_bot_env(),
        stdout=log_fh,
        stderr=log_fh,
    )
    log.info("Started %s (pid %s) -> %s", name, proc.pid, script)
    return proc


def should_restart(exit_code: int | None, restart_count: int) -> bool:
    return exit_code not in (0, None) and restart_count < MAX_RESTARTS


def restart_delay_seconds(restart_count: int) -> int:
    return min(20 * (2 ** restart_count), 300)


def main() -> None:
    log.info("=" * 60)
    log.info("US cloud launcher started")
    log.info("State dir: %s", STATE_DIR)
    log.info("Log dir: %s", LOG_DIR)
    log.info("=" * 60)

    procs: dict[str, subprocess.Popen] = {}
    restart_counts: dict[str, int] = {}

    for name, cfg in BOTS.items():
        if cfg.get("enabled"):
            procs[name] = _start_bot(name, cfg["script"])

    try:
        while True:
            for name, cfg in BOTS.items():
                if not cfg.get("enabled") or name not in procs:
                    continue
                proc = procs[name]
                if proc.poll() is None:
                    continue
                exit_code = proc.returncode
                count = restart_counts.get(name, 0)
                if not should_restart(exit_code, count):
                    log.error("%s exited with code %s and will not be restarted", name, exit_code)
                    continue
                delay = restart_delay_seconds(count)
                restart_counts[name] = count + 1
                log.warning("%s exited with code %s; restarting in %ss", name, exit_code, delay)
                time.sleep(delay)
                procs[name] = _start_bot(name, cfg["script"])
            time.sleep(15)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received, shutting down")
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        time.sleep(3)
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    main()
