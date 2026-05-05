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

from core.config_loader import FATHER_TG_CHAT, FATHER_TG_TOKEN
from core.father_brain import FatherBrain
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


def build_father_opinion(
    brain_brief: dict,
    brain_state: dict,
    bot_state: dict,
    council_state: dict,
    risk_status: dict,
) -> dict:
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
    fno_rejections = len(fno.get("rejections", []) or [])
    india_signals = len(india.get("signals", []) or [])
    risk_hold = bool(risk_status.get("hold")) if isinstance(risk_status, dict) else False
    us_positions = us.get("positions", {}) if isinstance(us, dict) else {}
    us_bets = us.get("bets", {}) if isinstance(us, dict) else {}
    us_safe_mode = us.get("safe_mode", {}) if isinstance(us, dict) else {}
    us_health = us.get("health", {}) if isinstance(us, dict) else {}

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
            "mode": brain_state.get("bot_modes", {}).get("india", "active_fast_path"),
            "top_focus": top_equity,
            "signals_seen": india_signals,
            "note": (
                "Use precomputed equity focus list for fast execution."
                if top_equity
                else "No strong prepared equity focus yet."
            ),
        },
        "fno": {
            "mode": fno_mode,
            "recent_rejections": fno_rejections,
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
                else "US execution may continue with venue-level risk checks."
            ),
        },
        "polymarket": {
            "mode": "paper_supervised",
            "open_bets": len(us_bets),
            "note": "Prediction-market ideas stay paper-only until live promotion gates are met.",
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
        path = self.state_dir / "father_opinion.json"
        path.write_text(json.dumps(opinion, indent=2, default=str), encoding="utf-8")
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
                    },
                    sort_keys=True,
                )
                if signature != self.last_signature:
                    log.info(
                        "Opinion updated | regime=%s | india=%s | fno=%s",
                        opinion.get("market_regime"),
                        ",".join(opinion.get("india", {}).get("top_focus", [])[:3]) or "none",
                        opinion.get("fno", {}).get("mode"),
                    )
                    self.notify.alert(
                        "Father opinion updated\n"
                        f"Regime: {opinion.get('market_regime')}\n"
                        f"FNO mode: {opinion.get('fno', {}).get('mode')}\n"
                        f"US mode: {opinion.get('us', {}).get('mode', 'n/a')}",
                        silent=True,
                    )
                    self.last_signature = signature
            except Exception as exc:
                log.error("Father bot cycle failed: %s", exc)
            time.sleep(self.interval_seconds)


if __name__ == "__main__":
    FatherBot().run()
