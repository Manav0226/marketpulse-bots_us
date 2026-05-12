"""
MarketPulse Pre-flight Auditor — Layer 1
=========================================
Run this every morning at 8:30 AM IST (GitHub Actions or manually).
Scans each bot file for known bugs, missing safety checks, and dangerous patterns.
Sends a pass/fail Telegram report so you know before the market opens.

Usage:
    python preflight.py                          # check all bots
    python preflight.py --bot bot_india_v5.py    # check one bot
    python preflight.py --dry-run                # print report, skip Telegram
"""

import os, sys, re, ast, subprocess, datetime, argparse, json, traceback
import importlib.util
import requests
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_DIR = Path(os.environ.get("BOT_DIR", r"D:\MarketPulseBot"))
# MarketPulse Monitor bot (separate from main trading bot)
TG_TOKEN = os.environ.get("MONITOR_TG_TOKEN", "")
TG_CHAT  = os.environ.get("MONITOR_TG_CHAT", "")

BOTS = {
    "bot_india_v5": {
        "file": "bot_india_v5.py",
        "label": "🇮🇳 India Equity Bot v5",
        "active": True,
    },
    "bot_fno_v1": {
        "file": "bot_fno_v1.py",
        "label": "📊 FNO Bot v1",
        "active": True,
    },
    # bot_us_crypto.py is retained for regression coverage only.
    # The active optional launcher target is bot_us_crypto_v4.py.
    "bot_us_crypto_v4": {
        "file": "bot_us_crypto_v4.py",
        "label": "🇺🇸 US/Crypto Bot v4",
        "active": False,   # set True when you start using it
    },
}

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg: str, dry_run=False):
    if dry_run:
        print("\n[TELEGRAM]\n" + msg + "\n")
        return
    if not TG_TOKEN or not TG_CHAT:
        print("[WARN] TG_TOKEN or TG_CHAT not set — skipping Telegram")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")


# ── CHECK REGISTRY ────────────────────────────────────────────────────────────
# Each check is (label, severity, test_fn)
# test_fn(src: str) -> bool  (True = PASS, False = FAIL)

SEVERITY_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "INFO": "⚪"}
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "INFO"]

def _has(pattern, src, regex=False):
    if regex:
        return bool(re.search(pattern, src))
    return pattern in src

def _missing(pattern, src, regex=False):
    return not _has(pattern, src, regex)

# Shared checks that apply to any bot
SHARED_CHECKS = [
    # Syntax
    ("Python syntax valid",                     "CRITICAL",
     lambda src, path: _syntax_ok(path)),

    # Order safety
    ("No plain MARKET orders",                  "CRITICAL",
     lambda src, _: _missing("order_type=['\"]MARKET['\"]", src, regex=True) or
                    _has("LIMIT", src)),

    ("Uses buffered LIMIT for exits",           "HIGH",
     lambda src, _: _has("LIMIT", src) and (_has("0.005", src) or _has("0.003", src))),

    ("Fill verification thread exists",         "HIGH",
     lambda src, _: _has("_verify_fill", src) or _has("verify_fill", src)),

    # Position tracking
    ("Position added only after fill confirm",  "HIGH",
     lambda src, _: not (_has("self.pos[", src) and _missing("_verify_fill", src))),

    ("Exit attempted flag exists",              "MEDIUM",
     lambda src, _: _has("_exit_attempted", src) or _has("exit_attempted", src)),

    # Stop loss / targets
    ("SL floor enforced (no 1-point SL)",       "HIGH",
     lambda src, _: _has("min_sl", src) or _has("MIN_SL", src) or _has("sl_floor", src) or
                    _has("0.003", src) or _has("0.0025", src)),

    ("T2 fallback when no pivot beyond T1",     "MEDIUM",
     lambda src, _: _has("T2", src) and (_has("fallback", src.lower()) or _has("0.005", src) or
                                          _has("1.8", src) or _has("ATR", src))),

    # Pivots / direction
    ("nearest_sup below price validated",       "HIGH",
     lambda src, _: _has("nearest_sup", src) and
                    (_has("< current", src) or _has("<current", src) or
                     _has("below", src.lower()) or _has("price >", src))),

    ("nearest_res above price validated",       "HIGH",
     lambda src, _: _has("nearest_res", src) and
                    (_has("> current", src) or _has(">current", src) or
                     _has("above", src.lower()) or _has("price <", src))),

    # Re-entry protection
    ("Re-entry after SL blocked",               "HIGH",
     lambda src, _: _has("sl_today", src) or _has("index_sl_today", src) or
                    _has("sl_hit_today", src)),

    ("Circuit breaker cache set",               "MEDIUM",
     lambda src, _: _has("circuit_today", src) or _has("circuit_cache", src)),

    # Error handling
    ("WebSocket error handling present",        "MEDIUM",
     lambda src, _: _has("on_error", src) or _has("ws_error", src) or
                    _has("reconnect", src.lower())),

    ("Holiday check before trading",            "MEDIUM",
     lambda src, _: _has("holiday", src.lower()) or _has("NSE_HOLIDAYS", src)),

    # Telegram
    ("Telegram alerts wired up",                "INFO",
     lambda src, _: _has("_tg(", src) or _has("send_telegram", src) or
                    _has("telegram", src.lower())),

    ("Daily loss limit present",                "HIGH",
     lambda src, _: _has("loss_limit", src) or _has("DAILY_LOSS", src) or
                    _has("max_loss", src.lower())),

    ("MAX_TRADES or MAX_POSITIONS defined",     "MEDIUM",
     lambda src, _: _has("MAX_TRADES", src) or _has("MAX_POSITIONS", src)),
]

# FNO-specific additional checks
FNO_CHECKS = [
    ("Black-Scholes / Greeks defined",          "HIGH",
     lambda src, _: _has("bs_greeks", src) or _has("black_scholes", src.lower()) or
                    _has("implied_vol", src)),

    ("Delta filter enforced",                   "HIGH",
     lambda src, _: _has("MIN_DELTA", src) or _has("delta", src.lower())),

    ("Theta decay filter enforced",             "HIGH",
     lambda src, _: _has("MAX_THETA", src) or _has("theta", src.lower())),

    ("Expiry day cutoff time set",              "HIGH",
     lambda src, _: _has("EXPIRY_CUTOFF", src) or _has("expiry", src.lower())),

    ("Partial exit (half qty) at T1",           "MEDIUM",
     lambda src, _: _has("half_qty", src) or _has("partial", src.lower())),

    ("half_qty floor (min 1 lot)",              "MEDIUM",
     lambda src, _: _has("max(1", src) or _has("max(1,", src)),

    ("3-loss daily stop",                       "HIGH",
     lambda src, _: _has("stop_day", src) or _has("daily_stop", src) or
                    _has("loss_count", src)),

    ("Option symbol lookup from instrument list", "HIGH",
     lambda src, _: _has("instruments", src) and
                    (_has("NFO", src) or _has("nfo", src))),

    ("Position reconcile on restart",           "MEDIUM",
     lambda src, _: _has("reconcile", src.lower()) or _has("existing_positions", src) or
                    _has("open_positions", src)),

    ("Stall exit (time-based)",                 "MEDIUM",
     lambda src, _: _has("mins_held", src) or _has("time_exit", src) or
                    _has("stall", src.lower())),

    ("Learning / performance tracking",         "INFO",
     lambda src, _: _has("iv_performance", src) or _has("hour_performance", src) or
                    _has("learning", src.lower())),
]

# US/Crypto checks
US_CHECKS = [
    ("Alpaca paper trading mode set",           "HIGH",
     lambda src, _: _has("paper", src.lower()) or _has("PAPER", src)),

    ("Crypto exchange (CCXT/Binance) wired",    "INFO",
     lambda src, _: _has("ccxt", src.lower()) or _has("binance", src.lower())),
]

BOT_EXTRA_CHECKS = {
    "bot_fno_v1":  FNO_CHECKS,
    "bot_us_crypto_v4": US_CHECKS,
}


# ── SYNTAX CHECK ──────────────────────────────────────────────────────────────
def _syntax_ok(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        ast.parse(source)
        return True
    except SyntaxError as e:
        return False

def _get_syntax_error(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        ast.parse(source)
        return None
    except SyntaxError as e:
        return f"Line {e.lineno}: {e.msg}"


# ── IMPORT CHECK ──────────────────────────────────────────────────────────────
def _check_imports(src: str) -> list[str]:
    """Return list of imports that look broken or missing."""
    issues = []
    # Check for obviously broken relative imports
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("from .") or line.startswith("import ."):
            issues.append(f"Relative import may break: {line}")
    return issues


# ── RUN CHECKS FOR ONE BOT ────────────────────────────────────────────────────
def audit_bot(bot_key: str, bot_cfg: dict, dry_run=False) -> dict:
    path = BOT_DIR / bot_cfg["file"]
    result = {
        "key":    bot_key,
        "label":  bot_cfg["label"],
        "path":   str(path),
        "exists": path.exists(),
        "checks": [],    # (label, severity, passed, detail)
        "passed": 0,
        "failed": 0,
        "critical_fails": [],
        "high_fails": [],
    }

    if not path.exists():
        result["checks"].append(("File exists on disk", "CRITICAL", False,
                                 f"Not found: {path}"))
        result["failed"] = 1
        result["critical_fails"].append("File not found")
        return result

    src = path.read_text(encoding="utf-8", errors="replace")

    # Syntax error detail
    syn_err = _get_syntax_error(path)

    # Gather all checks for this bot
    all_checks = list(SHARED_CHECKS)
    all_checks += BOT_EXTRA_CHECKS.get(bot_key, [])

    for label, severity, test_fn in all_checks:
        try:
            passed = test_fn(src, path)
        except Exception as e:
            passed = False
            label += f" [check error: {e}]"

        detail = ""
        if not passed and label == "Python syntax valid" and syn_err:
            detail = syn_err

        result["checks"].append((label, severity, passed, detail))
        if passed:
            result["passed"] += 1
        else:
            result["failed"] += 1
            if severity == "CRITICAL":
                result["critical_fails"].append(label)
            elif severity == "HIGH":
                result["high_fails"].append(label)

    # Import issues
    import_issues = _check_imports(src)
    for iss in import_issues:
        result["checks"].append((iss, "MEDIUM", False, ""))
        result["failed"] += 1

    return result


# ── FORMAT REPORT ─────────────────────────────────────────────────────────────
def format_report(results: list[dict], elapsed_sec: float) -> str:
    now = datetime.datetime.now().strftime("%d %b %Y %H:%M IST")
    lines = [f"<b>🛫 MarketPulse Pre-flight Report</b>"]
    lines.append(f"<i>{now} · {elapsed_sec:.1f}s</i>")
    lines.append("─" * 30)

    overall_ok = True

    for r in results:
        total = r["passed"] + r["failed"]
        pct = int(100 * r["passed"] / total) if total else 0

        if not r["exists"]:
            lines.append(f"\n🔴 {r['label']}")
            lines.append(f"   ✗ File not found: {r['path']}")
            overall_ok = False
            continue

        # Decide emoji
        if r["critical_fails"]:
            icon = "🔴"
            verdict = "DO NOT TRADE"
            overall_ok = False
        elif r["high_fails"]:
            icon = "🟠"
            verdict = "TRADE WITH CAUTION"
            overall_ok = False
        elif r["failed"] > 0:
            icon = "🟡"
            verdict = "MINOR ISSUES"
        else:
            icon = "🟢"
            verdict = "READY"

        lines.append(f"\n{icon} <b>{r['label']}</b>")
        lines.append(f"   {r['passed']}/{total} checks · {pct}% · {verdict}")

        # Show all failures grouped by severity
        for sev in SEVERITY_ORDER:
            fails = [(lbl, det) for lbl, sv, ok, det in r["checks"]
                     if sv == sev and not ok]
            if fails:
                lines.append(f"   {SEVERITY_ICON[sev]} {sev}:")
                for lbl, det in fails:
                    lines.append(f"      ✗ {lbl}")
                    if det:
                        lines.append(f"        → {det}")

    lines.append("\n" + "─" * 30)
    if overall_ok:
        lines.append("✅ All active bots are ready. Good hunting.")
    else:
        lines.append("⛔ Fix the above before trading. Log issues at: vault/bugs/")

    return "\n".join(lines)


def format_console_report(results: list[dict]) -> str:
    """Plain text for terminal output."""
    lines = []
    for r in results:
        total = r["passed"] + r["failed"]
        lines.append(f"\n{'='*60}")
        lines.append(f"  {r['label']}  ({r['passed']}/{total} passed)")
        lines.append(f"{'='*60}")
        for label, severity, passed, detail in r["checks"]:
            icon = "✓" if passed else "✗"
            lines.append(f"  {icon} [{severity[:3]}] {label}")
            if not passed and detail:
                lines.append(f"        → {detail}")
    return "\n".join(lines)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MarketPulse Pre-flight Auditor")
    parser.add_argument("--bot",     help="Audit only this bot file (e.g. bot_fno_v1.py)")
    parser.add_argument("--dry-run", action="store_true", help="Don't send Telegram")
    parser.add_argument("--bot-dir", help="Override BOT_DIR")
    args = parser.parse_args()

    global BOT_DIR
    if args.bot_dir:
        BOT_DIR = Path(args.bot_dir)

    t0 = datetime.datetime.now()

    bots_to_check = {}
    if args.bot:
        # Find matching key
        for k, cfg in BOTS.items():
            if cfg["file"] == args.bot or k == args.bot:
                bots_to_check[k] = cfg
        if not bots_to_check:
            print(f"[ERROR] Bot '{args.bot}' not found in registry.")
            sys.exit(1)
    else:
        bots_to_check = {k: v for k, v in BOTS.items() if v["active"]}

    print(f"[Pre-flight] Auditing {len(bots_to_check)} bot(s) from {BOT_DIR}")

    results = []
    for key, cfg in bots_to_check.items():
        print(f"  Checking {cfg['file']}...")
        r = audit_bot(key, cfg, dry_run=args.dry_run)
        results.append(r)

    elapsed = (datetime.datetime.now() - t0).total_seconds()

    # Console output
    print(format_console_report(results))

    # Telegram report
    report = format_report(results, elapsed)
    tg(report, dry_run=args.dry_run)

    # Exit code: 0 = all green, 1 = failures found
    any_fail = any(r["critical_fails"] or r["high_fails"] for r in results)
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
