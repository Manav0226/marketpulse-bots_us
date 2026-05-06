"""
launch_all.py — Start all MarketPulse bots, monitor them, auto-restart on crash.
Shuts all bots down cleanly at MARKET_CLOSE + 30min IST.
"""
import sys, os, time, datetime, subprocess, logging, zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from kite_auth import AUTH_REQUIRED_EXIT_CODE

log = logging.getLogger("launcher")
log.addHandler(logging.NullHandler())

IST          = zoneinfo.ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = datetime.time(16, 0)   # 3:30 PM + 30 min buffer
ROOT         = Path(__file__).parent.parent
PYTHON       = sys.executable
MAX_RESTARTS = 3
_LOGGING_READY = False
AUTH_CACHE_PATH = ROOT / ".auth" / "kite_access_token.json"

# Which bots to launch
# interactive=True  → opens a NEW console window so you can see the Kite login prompt
#                     and type the redirect URL. Required for India + FNO bots.
# interactive=False → output goes to logs/<name>_launcher.log (background)
# enabled=False     → bot is skipped entirely
BOTS = {
    "father": {"script": "bot_father.py",       "enabled": False, "interactive": False},
    "india":  {"script": "bot_india_v5.py",    "enabled": True,  "interactive": True},
    "fno":    {"script": "bot_fno_v1.py",       "enabled": True,  "interactive": True},
    "us":     {"script": "bot_us_crypto_v4.py", "enabled": False, "interactive": False},
    "us_intel": {"script": "bot_us_crypto_intel.py", "enabled": False, "interactive": False},
    "us_research": {"script": "bot_us_research.py", "enabled": False, "interactive": False},
    "us_supervisor": {"script": "us_supervisor.py", "enabled": False, "interactive": False},
    "risk":   {"script": "bot_risk.py",         "enabled": True,  "interactive": False},
}

_procs: dict = {}
_restart_counts: dict[str, int] = {}
_disabled: set[str] = set()

# Windows flag to open a new visible console window
_CREATE_NEW_CONSOLE = 0x00000010


def _auth_cache_ready() -> bool:
    return AUTH_CACHE_PATH.exists()


def _wait_for_auth(proc: subprocess.Popen, timeout: int = 90, poll_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _auth_cache_ready():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(poll_seconds)
    return _auth_cache_ready()


def start_enabled_bots(
    bots: dict | None = None,
    start_bot=None,
    wait_for_auth=_wait_for_auth,
    auth_cache_ready=_auth_cache_ready,
) -> dict:
    procs = {}
    start_bot = start_bot or _start_bot
    for name, cfg in (bots or BOTS).items():
        if not cfg["enabled"]:
            continue
        proc = start_bot(name, cfg["script"], cfg.get("interactive", False))
        procs[name] = proc
        if cfg.get("interactive", False) and name in {"india", "fno"} and not auth_cache_ready():
            wait_for_auth(proc)
    return procs


def _configure_logging() -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [LAUNCHER] %(message)s")

    file_handler = logging.FileHandler(log_dir / "launcher.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers = [file_handler, stream_handler]
    _LOGGING_READY = True


def _start_bot(name: str, script: str, interactive: bool = False) -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("MARKETPULSE_TZ", "Asia/Kolkata")
    env.setdefault("MARKETPULSE_LOG_DIR", "logs")
    env.setdefault("MARKETPULSE_STATE_DIR", "briefings")
    if interactive:
        # Open in a new console window — user can see login prompt and type
        proc = subprocess.Popen(
            [PYTHON, str(ROOT / script)],
            cwd=str(ROOT),
            env=env,
            creationflags=_CREATE_NEW_CONSOLE,
        )
        log.info(f"Started {name} (pid {proc.pid}) → {script}  [new window — complete login there]")
    else:
        log_path = ROOT / "logs" / f"{name}_launcher.log"
        log_fh   = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [PYTHON, str(ROOT / script)],
            stdout=log_fh, stderr=log_fh,
            cwd=str(ROOT),
            env=env,
        )
        log.info(f"Started {name} (pid {proc.pid}) → {script}")
    return proc


def should_restart(name: str, exit_code: int | None, restart_count: int) -> bool:
    if exit_code in (0, AUTH_REQUIRED_EXIT_CODE):
        return False
    return restart_count < MAX_RESTARTS


def restart_delay_seconds(restart_count: int) -> int:
    return min(30 * (2 ** restart_count), 300)


def _stop_all():
    log.info("Shutting down all bots...")
    for name, proc in _procs.items():
        if proc.poll() is None:
            proc.terminate()
            log.info(f"  Terminated {name} (pid {proc.pid})")
    time.sleep(3)
    for name, proc in _procs.items():
        if proc.poll() is None:
            proc.kill()
            log.info(f"  Force-killed {name}")


def main():
    _configure_logging()
    log.info("=" * 60)
    log.info(f"MarketPulse Launcher — {datetime.date.today()}")
    log.info("=" * 60)

    _procs.update(start_enabled_bots())

    try:
        while True:
            now_ist = datetime.datetime.now(IST).time()
            if now_ist >= MARKET_CLOSE:
                log.info(f"Market close + buffer reached ({MARKET_CLOSE}) — shutting down")
                _stop_all()
                break

            for name, cfg in BOTS.items():
                if not cfg["enabled"] or name not in _procs:
                    continue
                proc = _procs[name]
                if name in _disabled:
                    continue
                if proc.poll() is not None:   # process exited unexpectedly
                    exit_code = proc.returncode
                    restart_count = _restart_counts.get(name, 0)
                    if not should_restart(name, exit_code, restart_count):
                        log.warning(f"{name} exited (code {exit_code}) — not restarting")
                        _disabled.add(name)
                        continue
                    delay = restart_delay_seconds(restart_count)
                    _restart_counts[name] = restart_count + 1
                    log.warning(f"{name} exited (code {exit_code}) — restarting in {delay}s")
                    time.sleep(delay)
                    _procs[name] = _start_bot(name, cfg["script"], cfg.get("interactive", False))

            time.sleep(15)

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down")
        _stop_all()

    log.info("Launcher exited.")


if __name__ == "__main__":
    main()
