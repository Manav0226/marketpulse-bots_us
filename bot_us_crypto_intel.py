from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

from core.config_loader import US_INTEL_TG_CHAT, US_INTEL_TG_TOKEN
from core.us_market_scheduler import is_us_trading_day, market_window_label
from marketpulse_runtime import resolve_state_dir
from marketpulse_state import read_bot_state
from notifier import Notifier


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("USIntel")
STATE_DIR = resolve_state_dir()
INTEL_STATE_PATH = STATE_DIR / "us_intel_state.json"


class USCryptoIntelBot:
    def __init__(self):
        self.notify = Notifier(US_INTEL_TG_TOKEN, US_INTEL_TG_CHAT)
        self.last_sent = self._load_last_sent()

    def _load_last_sent(self):
        default = {"premarket": "", "open": "", "midday": "", "eod": "", "crypto": ""}
        if not INTEL_STATE_PATH.exists():
            return default
        try:
            payload = json.loads(INTEL_STATE_PATH.read_text(encoding="utf-8"))
            default.update(payload.get("last_sent", {}) or {})
        except Exception:
            pass
        return default

    def _save_last_sent(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            INTEL_STATE_PATH.write_text(json.dumps({"last_sent": self.last_sent}, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _state(self):
        return read_bot_state().get("bots", {}).get("us_v4", {})

    def _brain(self):
        path = STATE_DIR / "father_opinion.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def send_market_brief(self):
        now = dt.datetime.now(dt.timezone.utc)
        window = market_window_label(now)
        if window == "closed" or not is_us_trading_day(now):
            return
        stamp = now.date().isoformat() + ":" + window
        if self.last_sent.get(window) == stamp:
            return

        state = self._state()
        brain = self._brain()
        us = brain.get("us", {})
        self.notify.alert(
            f"US/Crypto Intel {window.upper()}\n"
            f"Regime: {brain.get('market_regime', 'UNKNOWN')}\n"
            f"US mode: {us.get('mode', 'unknown')}\n"
            f"Open positions: {len(state.get('positions', {}) or {})}\n"
            f"Open bets: {len(state.get('bets', {}) or {})}\n"
            f"Safe mode: {bool((state.get('safe_mode') or {}).get('global_pause_new_entries'))}"
        )
        self.last_sent[window] = stamp
        self._save_last_sent()

    def send_crypto_heartbeat(self):
        now = dt.datetime.now(dt.timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H")
        if self.last_sent.get("crypto") == stamp:
            return
        state = self._state()
        perf = state.get("performance", {}).get("crypto", {})
        self.notify.alert(
            f"Crypto heartbeat\n"
            f"Signals today: {perf.get('signals', 0)}\n"
            f"PnL: {perf.get('pnl', 0.0)}\n"
            f"Safe mode: {bool((state.get('safe_mode') or {}).get('global_pause_new_entries'))}"
        )
        self.last_sent["crypto"] = stamp
        self._save_last_sent()

    def run(self):
        while True:
            try:
                self.send_market_brief()
                self.send_crypto_heartbeat()
            except Exception as exc:
                log.error("Intel cycle failed: %s", exc)
            time.sleep(60)


if __name__ == "__main__":
    USCryptoIntelBot().run()
