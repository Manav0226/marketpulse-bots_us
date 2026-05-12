"""
bot_father.py - Shared father bot orchestrator and live opinion writer.

Runs alongside the execution bots. It does not place trades.
It refreshes the shared father-brain outputs and publishes a compact
opinion snapshot for India equity and FNO so the system can consume
guidance without slowing live execution.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

from core.india_market_scheduler import india_window_label, is_india_market_open
from core.config_loader import FATHER_TG_CHAT, FATHER_TG_TOKEN
from core.father_brain import FatherBrain
from core.us_market_scheduler import is_us_market_open, market_window_label
from marketpulse_runtime import market_tz, now_market, resolve_log_dir, resolve_state_dir
from us_supervisor import refresh_us_supervision


IST = market_tz("Asia/Kolkata")
LOG_DIR = resolve_log_dir()
STATE_DIR = resolve_state_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
TODAY = now_market("Asia/Kolkata").date().isoformat()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"father_{TODAY}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("father_bot")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_father_opinion_payload(state_dir: Path, opinion: dict) -> None:
    payload = json.dumps(opinion, indent=2, default=str)
    paths = [
        Path(state_dir) / "father_opinion.json",
        Path("briefings") / "father_opinion.json",
    ]
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")


def build_father_opinion(
    brain_brief: dict,
    brain_state: dict,
    bot_state: dict,
    council_state: dict,
    risk_status: dict,
    now: dt.datetime | None = None,
) -> dict:
    current = now or dt.datetime.now(dt.timezone.utc)
    bots = bot_state.get("bots", {}) if isinstance(bot_state, dict) else {}
    fno = bots.get("fno", {}) if isinstance(bots, dict) else {}
    india = bots.get("india", {}) if isinstance(bots, dict) else {}
    us = bots.get("us_v4", bots.get("us", {})) if isinstance(bots, dict) else {}
    equity_focus = brain_brief.get("equity_focus", []) or []
    market_regime = str(brain_brief.get("market_regime", "NEUTRAL")).upper()
    council_bias = (
        council_state.get("cross_market_bias", {}).get("verdict", "NEUTRAL")
        if isinstance(council_state, dict)
        else "NEUTRAL"
    )

    top_equity = [row.get("symbol") for row in equity_focus[:3] if row.get("symbol")]
    avoid_symbols = brain_brief.get("avoid_symbols", []) or []
    avoid_set = {str(symbol).upper() for symbol in avoid_symbols if symbol}
    fno_rejections = len(fno.get("rejections", []) or [])
    india_signals = len(india.get("signals", []) or [])
    risk_hold = bool(risk_status.get("hold")) if isinstance(risk_status, dict) else False
    us_positions = us.get("positions", {}) if isinstance(us, dict) else {}
    us_bets = us.get("bets", {}) if isinstance(us, dict) else {}
    us_safe_mode = us.get("safe_mode", {}) if isinstance(us, dict) else {}
    us_health = us.get("health", {}) if isinstance(us, dict) else {}
    us_perf = us.get("performance", {}) if isinstance(us, dict) else {}
    india_open = is_india_market_open(current)
    us_open = is_us_market_open(current)
    crypto_disabled_reason = str(us_health.get("crypto_disabled_reason", "") or "")

    fno_mode = "supervised"
    if risk_hold:
        fno_mode = "risk_hold"
    elif fno_rejections >= 10:
        fno_mode = "cautious"

    pause_new_entries = bool(us_safe_mode.get("global_pause_new_entries"))
    if not pause_new_entries and market_regime in {"RISK_OFF", "BEARISH"} and str(council_bias).upper() == "BEARISH":
        pause_new_entries = True
        us_safe_mode = {
            "global_pause_new_entries": True,
            "reason": "brain_risk_off",
        }
    us_mode = "paused" if pause_new_entries else brain_state.get("bot_modes", {}).get("us", "active_light_guidance")
    if not pause_new_entries and not us_open:
        us_mode = "waiting_session"

    india_mode = brain_state.get("bot_modes", {}).get("india", "active_fast_path")
    if not india_open:
        india_mode = "waiting_session"

    if crypto_disabled_reason:
        crypto_mode = "disabled"
        crypto_note = f"Crypto path disabled on this host: {crypto_disabled_reason}."
    else:
        crypto_mode = "always_on"
        crypto_note = "Crypto supervision stays active across all time zones."

    fno_candidates = []
    for row in equity_focus:
        symbol = str(row.get("symbol", "") or "").upper()
        if not symbol or symbol in avoid_set:
            continue
        fno_candidates.append(
            {
                "symbol": symbol,
                "bias": str(row.get("bias", "NEUTRAL") or "NEUTRAL").upper(),
                "composite_score": float(row.get("composite_score", 0.0) or 0.0),
                "execution_mode": str(row.get("execution_mode", "fast_path") or "fast_path"),
                "reason": "father_equity_focus",
            }
        )
        if len(fno_candidates) >= 3:
            break
    fno_candidate_symbols = [row["symbol"] for row in fno_candidates]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": brain_brief.get("date") or now_market("Asia/Kolkata").date().isoformat(),
        "market_regime": market_regime,
        "council_bias": str(council_bias).upper(),
        "capabilities": {
            "mode": "rule_memory",
            "intraday_llm": False,
            "can_rank_equities_fast": True,
            "can_supervise_fno": True,
            "can_place_orders": False,
        },
        "india": {
            "mode": india_mode,
            "top_focus": top_equity,
            "signals_seen": india_signals,
            "note": (
                "India session active; use precomputed equity focus list for fast execution."
                if top_equity
                else ("India session active, but no strong prepared equity focus yet." if india_open else "Waiting for India market session.")
            ),
        },
        "fno": {
            "mode": fno_mode,
            "recent_rejections": fno_rejections,
            "candidate_symbols": fno_candidate_symbols,
            "candidates": fno_candidates,
            "note": (
                "FNO should stay supervised until rejection quality improves."
                if fno_mode != "risk_hold"
                else "Risk hold is active; new FNO exposure should stay defensive."
            ),
        },
        "us": {
            "mode": us_mode,
            "open_positions": len(us_positions),
            "safe_mode": us_safe_mode,
            "llm_supervisor": us_health.get("llm_supervisor", "disabled"),
            "note": (
                "Pause new entries until supervision is healthy again."
                if pause_new_entries
                else ("US execution may continue with venue-level risk checks." if us_open else "Waiting for US market session.")
            ),
        },
        "crypto": {
            "mode": crypto_mode,
            "signals_seen": int((us_perf.get("crypto", {}) or {}).get("signals", 0)),
            "pnl": float((us_perf.get("crypto", {}) or {}).get("pnl", 0.0) or 0.0),
            "note": crypto_note,
        },
        "polymarket": {
            "mode": "paper_supervised",
            "open_bets": len(us_bets),
            "note": "Prediction-market ideas stay paper-only until live promotion gates are met.",
        },
        "sessions": {
            "india": {"is_open": india_open, "window": india_window_label(current)},
            "us": {"is_open": us_open, "window": market_window_label(current)},
            "crypto": {"is_open": True, "window": "always_on"},
        },
        "avoid_symbols": avoid_symbols[:12],
    }


class FatherBot:
    def __init__(self, state_dir: Path | None = None, interval_seconds: int = 60):
        from notifier import Notifier

        self.state_dir = Path(state_dir or STATE_DIR)
        self.interval_seconds = max(15, int(interval_seconds))
        self.brain = FatherBrain(self.state_dir)
        self.last_signature = ""
        self.notify = Notifier(FATHER_TG_TOKEN, FATHER_TG_CHAT)

    def _write_opinion(self) -> dict:
        refreshed = self.brain.refresh_outputs()
        opinion = build_father_opinion(
            refreshed.get("brain_brief", {}),
            refreshed.get("brain_state", {}),
            _read_json(self.state_dir / "bot_state.json"),
            _read_json(self.state_dir / "council_state.json"),
            _read_json(self.state_dir / "risk_status.json"),
        )
        write_father_opinion_payload(self.state_dir, opinion)
        refresh_us_supervision(self.state_dir)
        return opinion

    def run(self) -> None:
        log.info("=" * 60)
        log.info("Father Bot started")
        log.info("Purpose: refresh shared brain and publish live opinion snapshots")
        log.info("=" * 60)
        while True:
            try:
                opinion = self._write_opinion()
                signature = json.dumps(
                    {
                        "market_regime": opinion.get("market_regime"),
                        "india_focus": opinion.get("india", {}).get("top_focus", []),
                        "fno_mode": opinion.get("fno", {}).get("mode"),
                        "india_mode": opinion.get("india", {}).get("mode"),
                        "us_mode": opinion.get("us", {}).get("mode"),
                        "crypto_mode": opinion.get("crypto", {}).get("mode"),
                    },
                    sort_keys=True,
                )
                if signature != self.last_signature:
                    log.info(
                        "Opinion updated | regime=%s | india=%s | fno=%s | fno_candidates=%s",
                        opinion.get("market_regime"),
                        ",".join(opinion.get("india", {}).get("top_focus", [])[:3]) or "none",
                        opinion.get("fno", {}).get("mode"),
                        ",".join(opinion.get("fno", {}).get("candidate_symbols", [])[:3]) or "none",
                    )
                    self.notify.alert(
                        "Father opinion updated\n"
                        f"Regime: {opinion.get('market_regime')}\n"
                        f"India mode: {opinion.get('india', {}).get('mode')}\n"
                        f"FNO mode: {opinion.get('fno', {}).get('mode')}\n"
                        f"US mode: {opinion.get('us', {}).get('mode', 'n/a')}\n"
                        f"Crypto mode: {opinion.get('crypto', {}).get('mode', 'n/a')}",
                        silent=True,
                    )
                    self.last_signature = signature
            except Exception as exc:
                log.error("Father bot cycle failed: %s", exc)
            time.sleep(self.interval_seconds)


if __name__ == "__main__":
    FatherBot().run()
