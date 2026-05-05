from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from marketpulse_runtime import resolve_state_dir


def _father_pause_is_actionable(father_opinion: dict, source_warnings: list[str], critical_source_warnings: set[str]) -> bool:
    us = father_opinion.get("us", {}) if isinstance(father_opinion, dict) else {}
    safe_mode = us.get("safe_mode", {}) if isinstance(us, dict) else {}
    if not safe_mode.get("global_pause_new_entries"):
        return False

    reason = str(safe_mode.get("reason") or "").strip().lower()
    if not reason:
        return True

    critical_matches = [warning for warning in source_warnings if warning in critical_source_warnings]
    if critical_matches:
        return True

    normalized_reason = {part.strip() for part in reason.split(",") if part.strip()}
    noncritical_source_set = {str(item).strip().lower() for item in source_warnings if str(item).strip()}
    known_source_warning_set = noncritical_source_set | set(critical_source_warnings) | {
        "earnings calendar unavailable",
    }
    if normalized_reason and normalized_reason.issubset(noncritical_source_set):
        return False
    if normalized_reason and normalized_reason.issubset(known_source_warning_set):
        return False
    return True


def build_us_supervision(
    father_opinion: dict,
    weekly_brief: dict,
    bot_state: dict,
    now: dt.datetime | None = None,
) -> dict:
    current = now or dt.datetime.now(dt.timezone.utc)
    weekly = weekly_brief.get("weekly_candidates", []) or []
    earnings = weekly_brief.get("earnings_setups", []) or []
    source_health = weekly_brief.get("source_health", {}) or {}

    size_multipliers: dict[str, float] = {}
    blocked_symbols: list[str] = []
    event_risk_symbols: list[str] = []
    source_warnings = list(source_health.get("warnings", []) or [])
    critical_source_warnings = {
        "news unavailable",
        "price data stale",
        "fundamental data partial",
    }

    for item in earnings:
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        event_risk_symbols.append(symbol)
        earnings_date = str(item.get("earnings_date") or "")
        result_bias = str(item.get("result_day_bias", "NONE")).upper()
        pre_bias = str(item.get("pre_result_bias", "NONE")).upper()
        if earnings_date == current.date().isoformat():
            size_multipliers[symbol] = 0.5
            if "BEARISH" in result_bias:
                blocked_symbols.append(symbol)
        elif "BEARISH" in pre_bias:
            blocked_symbols.append(symbol)
        else:
            size_multipliers[symbol] = 0.75

    allow_new_entries = not _father_pause_is_actionable(
        father_opinion=father_opinion,
        source_warnings=source_warnings,
        critical_source_warnings=critical_source_warnings,
    )
    forced_safe_mode = any(warning in critical_source_warnings for warning in source_warnings)
    if forced_safe_mode:
        allow_new_entries = False

    return {
        "generated_at": current.isoformat(),
        "allow_new_entries": allow_new_entries,
        "blocked_symbols": sorted(set(blocked_symbols)),
        "size_multipliers": size_multipliers,
        "event_risk_symbols": sorted(set(event_risk_symbols)),
        "forced_safe_mode": forced_safe_mode,
        "source_warnings": source_warnings,
        "weekly_focus": [str(item.get("symbol", "")).upper() for item in weekly[:8] if item.get("symbol")],
        "llm_supervisor": bot_state.get("health", {}).get("llm_supervisor", "disabled"),
    }


def refresh_us_supervision(state_dir: str | Path | None = None) -> dict:
    root = Path(state_dir) if state_dir else resolve_state_dir()
    def _read(name: str) -> dict:
        path = root / name
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    supervision = build_us_supervision(
        father_opinion=_read("father_opinion.json"),
        weekly_brief=_read("us_weekly_brief.json"),
        bot_state=_read("bot_state.json").get("bots", {}).get("us_v4", {}),
    )
    (root / "us_supervision.json").write_text(json.dumps(supervision, indent=2), encoding="utf-8")
    return supervision


if __name__ == "__main__":
    refresh_us_supervision()
