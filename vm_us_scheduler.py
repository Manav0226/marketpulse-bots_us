from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from marketpulse_runtime import resolve_state_dir

UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parent
STATE_PATH = resolve_state_dir() / "vm_scheduler_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vm_us_scheduler")


def build_us_vm_jobs() -> list[dict]:
    python = sys.executable
    return [
        {
            "name": "us_research_weekly",
            "kind": "weekly",
            "weekday": 6,
            "time_utc": "13:00",
            "command": [python, str(ROOT / "bot_us_research.py")],
        },
        {
            "name": "us_research_daily",
            "kind": "daily",
            "weekdays": [0, 1, 2, 3, 4],
            "time_utc": "11:00",
            "command": [python, str(ROOT / "bot_us_research.py")],
        },
        {
            "name": "us_supervision_refresh",
            "kind": "interval",
            "every_minutes": 10,
            "command": [python, str(ROOT / "us_supervisor.py")],
        },
        {
            "name": "us_eod_workbook",
            "kind": "interval",
            "every_minutes": 15,
            "command": [python, str(ROOT / "bot_us_eod_report.py")],
        },
    ]


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return None


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_job_due(job: dict, now: dt.datetime, last_run: str | None) -> bool:
    current = now.astimezone(UTC)
    previous = _parse_iso(last_run)
    kind = job.get("kind")

    if kind == "interval":
        if previous is None:
            return True
        elapsed = (current - previous).total_seconds() / 60.0
        return elapsed >= int(job.get("every_minutes", 0))

    hour, minute = _parse_hhmm(str(job.get("time_utc", "00:00")))
    if current.hour != hour or current.minute != minute:
        return False

    if kind == "weekly":
        if current.weekday() != int(job.get("weekday", -1)):
            return False
    elif kind == "daily":
        allowed = set(job.get("weekdays", []))
        if allowed and current.weekday() not in allowed:
            return False

    if previous is None:
        return True
    return previous.date() != current.date()


def _load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {"jobs": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"jobs": {}}


def _save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_due_jobs(now: dt.datetime | None = None) -> list[str]:
    current = now or dt.datetime.now(UTC)
    state = _load_state()
    jobs_state = state.setdefault("jobs", {})
    ran: list[str] = []
    for job in build_us_vm_jobs():
        name = job["name"]
        last_run = jobs_state.get(name, {}).get("last_run")
        if not is_job_due(job, current, last_run):
            continue
        log.info("Running job: %s", name)
        proc = subprocess.run(job["command"], cwd=str(ROOT), capture_output=True, text=True)
        jobs_state[name] = {
            "last_run": current.isoformat(),
            "exit_code": int(proc.returncode),
        }
        if proc.returncode == 0:
            ran.append(name)
        else:
            log.error("Job failed: %s\n%s", name, proc.stderr[-400:])
    _save_state(state)
    return ran


def main():
    while True:
        try:
            run_due_jobs()
        except Exception as exc:
            log.error("Scheduler loop failed: %s", exc)
        time.sleep(60)


if __name__ == "__main__":
    main()
