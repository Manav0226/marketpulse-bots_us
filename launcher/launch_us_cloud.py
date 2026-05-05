"""
launch_us_cloud.py - Railway/worker-friendly launcher for the US cloud stack.

Runs the US execution bot, US intel bot, father bot, and VM scheduler together
inside a single long-running worker so they can share one persistent state path.
"""

from __future__ import annotations

import logging
import os
import json
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from marketpulse_runtime import resolve_log_dir, resolve_state_dir

PYTHON = sys.executable
LOG_DIR = resolve_log_dir()
STATE_DIR = resolve_state_dir()
MAX_RESTARTS = 5
WEEKLY_BRIEF_PATH = STATE_DIR / "us_weekly_brief.json"

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


def _weekly_brief_stale(now: dt.datetime | None = None) -> bool:
    current = now or dt.datetime.now(dt.timezone.utc)
    if not WEEKLY_BRIEF_PATH.exists():
        return True
    try:
        payload = json.loads(WEEKLY_BRIEF_PATH.read_text(encoding="utf-8"))
        generated_at = str(payload.get("generated_at") or "")
        if not generated_at:
            return True
        parsed = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        age_hours = (current - parsed.astimezone(dt.timezone.utc)).total_seconds() / 3600.0
        return age_hours >= 12 or not payload.get("weekly_candidates")
    except Exception:
        return True


def _run_startup_catchup() -> None:
    env = _bot_env()
    commands: list[list[str]] = [[PYTHON, str(ROOT / "us_supervisor.py")]]
    if _weekly_brief_stale():
        commands.append([PYTHON, str(ROOT / "bot_us_research.py")])
        commands.append([PYTHON, str(ROOT / "us_supervisor.py")])
    for command in commands:
        try:
            proc = subprocess.run(command, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
            log.info("Startup catch-up ran: %s (exit %s)", Path(command[-1]).name, proc.returncode)
            if proc.returncode != 0 and proc.stderr:
                log.warning("Catch-up stderr for %s: %s", Path(command[-1]).name, proc.stderr[-400:])
        except Exception as exc:
            log.warning("Startup catch-up failed for %s: %s", Path(command[-1]).name, exc)


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
    _run_startup_catchup()

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
