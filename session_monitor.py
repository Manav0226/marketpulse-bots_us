"""
MarketPulse Live Session Monitor — Layer 2
============================================
Run this ALONGSIDE your trading bot during market hours.
It tails the bot's log file in real time, detects known error patterns,
and fires Telegram alerts with a fix hint so you don't have to stare at logs.

Usage:
    python session_monitor.py                          # watch all active bots
    python session_monitor.py --bot india              # watch india bot only
    python session_monitor.py --dry-run                # print alerts, skip Telegram

Works on Windows (no 'tail' command needed — pure Python file polling).
"""

import os, sys, re, time, threading, datetime, argparse, collections
import requests
from pathlib import Path
from marketpulse_runtime import now_market, resolve_log_dir

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_DIR = Path(os.environ.get("BOT_DIR", r"D:\MarketPulseBot"))
LOG_DIR = resolve_log_dir(os.environ.get("MARKETPULSE_LOG_DIR") or os.environ.get("LOG_DIR") or r"D:\MarketPulseBot\logs")
# MarketPulse Monitor bot (separate from main trading bot)
TG_TOKEN = os.environ.get("MONITOR_TG_TOKEN", "")
TG_CHAT  = os.environ.get("MONITOR_TG_CHAT", "")

POLL_INTERVAL = 2   # seconds between log reads
ALERT_COOLDOWN = 120  # seconds — don't repeat same alert class within this window

# ── BOT LOG FILE MAP ──────────────────────────────────────────────────────────
# Each bot writes to its own log file. Adjust patterns to match your actual filenames.
BOT_LOGS = {
    "india": {
        "label": "🇮🇳 India Equity",
        "pattern": "india_v5_*.log",   # glob pattern inside LOG_DIR
        "active": True,
    },
    "fno": {
        "label": "📊 FNO",
        "pattern": "fno_*.log",
        "active": True,
    },
    "us": {
        "label": "🇺🇸 US/Crypto",
        "pattern": "uscrp4_*.log",
        "active": True,
    },
}

# ── ERROR PATTERN REGISTRY ────────────────────────────────────────────────────
# Each entry: (name, severity, regex_pattern, fix_hint)
# The monitor fires a Telegram alert when any pattern matches a new log line.

def missing_log_message(is_github_runner: bool, bot_key: str) -> str:
    if is_github_runner:
        return (
            f"{bot_key}: local live logs unavailable from GitHub runner. "
            "Use uploaded workflow artifacts or synced briefing state for the latest available snapshot."
        )
    return f"{bot_key}: log file not found - is the bot running?"


ERROR_PATTERNS = [
    # ── ORDER / FILL ERRORS ──────────────────────────────────────────────
    ("Order rejected",          "CRITICAL",
     r"(order.*reject|reject.*order|InputException|Error placing)",
     "Zerodha rejected the order. Check: instrument token valid? Margin enough? NSE open?"),

    ("Fill verify failed",      "CRITICAL",
     r"(fill.*fail|_verify_fill.*fail|Order not filled after)",
     "Limit order didn't fill after timeout. Price moved away. Bot should have re-tried — check if position is tracked."),

    ("MARKET order blocked",    "CRITICAL",
     r"(market.*order.*not allowed|MarketOrderException|market orders.*API)",
     "Zerodha blocks API MARKET orders. Switch to buffered LIMIT (0.5% offset). Check exit logic."),

    ("Ghost position",          "HIGH",
     r"(position.*already|duplicate.*position|pos.*exists)",
     "Bot tried to open a position that's already tracked. Check self.pos and _verify_fill flow."),

    # ── STOP LOSS / TARGET ERRORS ────────────────────────────────────────
    ("SL too tight / 1-point",  "HIGH",
     r"(sl.*1\b|stop.*loss.*0\.0|sl_dist.*0\b|sl.*=.*price)",
     "Stop-loss distance is near zero. Enforce MIN_SL floor (0.3%). Check pivot validation."),

    ("T2 is None",              "HIGH",
     r"(T2.*None|target2.*None|t2.*=.*None)",
     "T2 target not set. Apply 1.8×ATR fallback when no pivot beyond T1."),

    ("Re-entry after SL",       "HIGH",
     r"(re.?entry.*SL|sl.*hit.*reent|index_sl_today.*already)",
     "Bot tried to re-enter after SL was hit today. Check index_sl_today blocking logic."),

    # ── DATA / PRICE ERRORS ──────────────────────────────────────────────
    ("yfinance unavailable",    "MEDIUM",
     r"(yfinance.*fail|No data.*yfinance|KNOWN_YF_UNAVAILABLE|Delisted)",
     "yfinance has no data for this symbol. Add to KNOWN_YF_UNAVAILABLE list."),

    ("Kite LTP failed",         "HIGH",
     r"(kite\.ltp.*error|LTP.*fail|NetworkException|TokenException)",
     "Kite live price fetch failed. Check API token, internet, Kite server status."),

    ("Stale index levels",      "MEDIUM",
     r"(index.*stale|last_index.*None|index.*scan.*fail)",
     "Index levels not refreshed. Market move filter may use wrong data. Check _scan_indices()."),

    ("Sentiment hard block",    "MEDIUM",
     r"(HARD BLOCK|sentiment.*block|nifty.*>.*2\.5|market.*crash)",
     "Sentiment filter blocked all trades today. If intentional — fine. If not, check NIFTY move calc."),

    # ── LOOP / STATE ERRORS ──────────────────────────────────────────────
    ("Exit loop detected",      "HIGH",
     r"(exit.*loop|_exit_attempted.*True.*exit|exit.*called.*again)",
     "Exit function called multiple times for same position. Check _exit_attempted flag."),

    ("Circuit scan loop",       "MEDIUM",
     r"(circuit.*scan.*again|circuit_today.*miss|circuit.*repeat)",
     "Circuit breaker check running repeatedly for same symbol. Check circuit_today cache."),

    ("WebSocket dropped",       "HIGH",
     r"(websocket.*disconnect|ws.*closed|on_close.*kite|WebSocket.*error)",
     "Kite WebSocket dropped. Bot should auto-reconnect in 5s. If it doesn't, restart the bot."),

    ("Infinite loop suspected", "CRITICAL",
     r"(RecursionError|maximum recursion|while True.*stuck|loop.*timeout)",
     "Possible infinite loop. Stop the bot immediately and review the scan/exit logic."),

    # ── FNO SPECIFIC ─────────────────────────────────────────────────────
    ("Delta out of range",      "MEDIUM",
     r"(delta.*out of range|delta.*<.*MIN_DELTA|delta.*>.*0\.9)",
     "Option delta is outside acceptable range. Check strike selection and MIN_DELTA filter."),

    ("Expiry day cutoff",       "INFO",
     r"(expiry.*cutoff|past.*EXPIRY_CUTOFF|no trading.*expiry)",
     "Bot correctly stopped trading near expiry. Normal behaviour."),

    ("Premium too wide",        "MEDIUM",
     r"(spread.*too wide|bid.*ask.*wide|premium.*slippage)",
     "Option spread too wide — excessive slippage risk. Bot should skip, verify it did."),

    # ── SYSTEM ───────────────────────────────────────────────────────────
    ("Python exception",        "HIGH",
     r"(Traceback \(most recent|Exception:|Error:|raise )",
     "Unhandled exception in bot. Read the full traceback to identify root cause."),

    ("API rate limit",          "MEDIUM",
     r"(rate.limit|429|too many requests|throttl)",
     "API rate limit hit. Add sleep between rapid calls. Check scan frequency."),

    ("Margin insufficient",     "HIGH",
     r"(margin.*insufficient|insufficient.*margin|margin.*fail)",
     "Not enough margin for the order. Check available funds in Zerodha console."),

    ("Daily loss limit hit",    "INFO",
     r"(daily.*loss.*limit|loss_limit.*reached|stop.*trading.*today)",
     "Daily loss limit triggered. Bot should stop for the day — verify it did."),

    ("Max positions reached",   "INFO",
     r"(max.*positions|MAX_POSITIONS.*reached|position.*limit)",
     "Max positions reached — normal. No new trades until a position closes."),
]

# Compile patterns once
_COMPILED = [(name, sev, re.compile(pat, re.IGNORECASE), hint)
             for name, sev, pat, hint in ERROR_PATTERNS]

SEV_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "INFO": "ℹ️"}


# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg: str, dry_run=False):
    if dry_run:
        print(f"\n[TELEGRAM ALERT]\n{msg}\n")
        return
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Telegram failed: {e}")


# ── FILE TAILER ───────────────────────────────────────────────────────────────
class LogTailer:
    """Watches a log file and yields new lines as they appear (Windows-safe)."""

    def __init__(self, path: Path):
        self.path = path
        self._pos = 0
        self._inode = None
        # Start from end of file so we don't re-process history on launch
        if path.exists():
            self._pos = path.stat().st_size

    def new_lines(self):
        if not self.path.exists():
            return
        try:
            stat = self.path.stat()
            # File was rotated / truncated
            if stat.st_size < self._pos:
                self._pos = 0

            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()

            for line in data.splitlines():
                if line.strip():
                    yield line
        except (OSError, PermissionError):
            pass


# ── ALERT STATE ───────────────────────────────────────────────────────────────
class AlertState:
    """Tracks per-bot, per-error-class cooldown to avoid alert spam."""

    def __init__(self):
        self._last: dict[tuple, float] = {}

    def should_alert(self, bot_key: str, error_name: str) -> bool:
        k = (bot_key, error_name)
        now = time.time()
        last = self._last.get(k, 0)
        if now - last > ALERT_COOLDOWN:
            self._last[k] = now
            return True
        return False


# ── MONITOR ONE BOT ───────────────────────────────────────────────────────────
class BotMonitor:
    def __init__(self, bot_key: str, cfg: dict, alert_state: AlertState,
                 dry_run: bool = False, log_dir: Path | None = None):
        self.key = bot_key
        self.label = cfg["label"]
        self.pattern = cfg["pattern"]
        self.log_dir = Path(log_dir) if log_dir else LOG_DIR
        self.dry_run = dry_run
        self.alert_state = alert_state
        self.tailer: LogTailer | None = None
        self._error_counts: collections.Counter = collections.Counter()
        self._session_start = now_market()

    def _find_log(self) -> Path | None:
        matches = sorted(self.log_dir.glob(self.pattern), key=lambda p: p.stat().st_mtime
                         if p.exists() else 0, reverse=True)
        return matches[0] if matches else None

    def _ensure_tailer(self):
        log_path = self._find_log()
        if log_path and (self.tailer is None or self.tailer.path != log_path):
            print(f"[Monitor] {self.label}: watching {log_path.name}")
            self.tailer = LogTailer(log_path)
        elif self.tailer is None:
            print(f"[Monitor] {missing_log_message(os.environ.get('GITHUB_ACTIONS') == 'true', self.key)}")

    def process_line(self, line: str):
        for name, severity, pattern, hint in _COMPILED:
            if pattern.search(line):
                self._error_counts[name] += 1
                if self.alert_state.should_alert(self.key, name):
                    self._fire_alert(name, severity, hint, line)
                break  # one alert per line

    def _fire_alert(self, error_name: str, severity: str, hint: str, raw_line: str):
        icon = SEV_ICON.get(severity, "⚠️")
        ts = now_market().strftime("%H:%M:%S")
        # Truncate the raw log line to avoid massive messages
        snippet = raw_line.strip()[:200]
        msg = (
            f"{icon} <b>{error_name}</b> [{severity}]\n"
            f"Bot: {self.label}\n"
            f"Time: {ts}\n\n"
            f"<code>{snippet}</code>\n\n"
            f"💡 <i>{hint}</i>"
        )
        print(f"[{ts}] ALERT [{self.key}] {error_name}")
        tg(msg, dry_run=self.dry_run)

    def tick(self):
        self._ensure_tailer()
        if self.tailer:
            for line in self.tailer.new_lines():
                self.process_line(line)

    def session_summary(self) -> str:
        elapsed = (now_market() - self._session_start)
        mins = int(elapsed.total_seconds() / 60)
        if not self._error_counts:
            return f"✅ {self.label}: no errors in {mins}m"
        lines = [f"⚠️ {self.label}: {sum(self._error_counts.values())} errors in {mins}m"]
        for name, count in self._error_counts.most_common(5):
            lines.append(f"  · {name}: {count}×")
        return "\n".join(lines)


# ── SESSION HEARTBEAT ─────────────────────────────────────────────────────────
def heartbeat_loop(monitors: list[BotMonitor], dry_run: bool):
    """Every 30 minutes during trading hours, send a health pulse."""
    MARKET_OPEN  = datetime.time(9, 15)
    MARKET_CLOSE = datetime.time(15, 30)

    while True:
        time.sleep(1800)  # 30 minutes
        now = now_market().time()
        if not (MARKET_OPEN <= now <= MARKET_CLOSE):
            continue
        ts = now_market().strftime("%H:%M")
        lines = [f"💓 <b>Monitor heartbeat</b> — {ts}"]
        for m in monitors:
            lines.append(m.session_summary())
        tg("\n".join(lines), dry_run=dry_run)


# ── EOD SUMMARY ───────────────────────────────────────────────────────────────
def eod_summary(monitors: list[BotMonitor], dry_run: bool):
    ts = now_market().strftime("%d %b %Y %H:%M")
    lines = [f"📋 <b>Session Monitor — EOD Summary</b>\n{ts}"]
    for m in monitors:
        lines.append(m.session_summary())
    tg("\n".join(lines), dry_run=dry_run)


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MarketPulse Live Session Monitor")
    parser.add_argument("--bot",     help="Watch only this bot key (india / fno / us)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-dir", help="Override LOG_DIR")
    args = parser.parse_args()

    global LOG_DIR
    if args.log_dir:
        LOG_DIR = Path(args.log_dir)

    bots_to_watch = {}
    if args.bot:
        if args.bot in BOT_LOGS:
            bots_to_watch[args.bot] = BOT_LOGS[args.bot]
        else:
            print(f"[ERROR] Unknown bot key: {args.bot}. Options: {list(BOT_LOGS)}")
            sys.exit(1)
    else:
        bots_to_watch = {k: v for k, v in BOT_LOGS.items() if v["active"]}

    print(f"[Monitor] Starting — watching {len(bots_to_watch)} bot(s)")
    print(f"[Monitor] Log dir: {LOG_DIR}")
    print(f"[Monitor] Poll interval: {POLL_INTERVAL}s | Alert cooldown: {ALERT_COOLDOWN}s")

    alert_state = AlertState()
    monitors = [BotMonitor(k, cfg, alert_state, dry_run=args.dry_run)
                for k, cfg in bots_to_watch.items()]

    # Fire startup message
    ts = now_market().strftime("%H:%M")
    tg(f"👁️ <b>Session monitor started</b> — {ts}\n"
       f"Watching: {', '.join(m.label for m in monitors)}",
       dry_run=args.dry_run)

    # Start heartbeat in background thread
    hb = threading.Thread(target=heartbeat_loop, args=(monitors, args.dry_run),
                          daemon=True)
    hb.start()

    # Market close time for EOD summary
    MARKET_CLOSE_DT = now_market().replace(hour=15, minute=35, second=0)
    eod_fired = False

    try:
        while True:
            for m in monitors:
                m.tick()

            # EOD summary at 15:35 IST
            now = now_market()
            if not eod_fired and now >= MARKET_CLOSE_DT:
                eod_summary(monitors, dry_run=args.dry_run)
                eod_fired = True

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[Monitor] Stopped by user.")
        eod_summary(monitors, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
