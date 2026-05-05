import datetime as dt
import json
from pathlib import Path
from typing import Callable

from bot_intelligence_store import BeliefSnapshot, BotIntelligenceStore, LearningRecord, OutcomeEvent
from marketpulse_runtime import market_date, resolve_state_dir


BOT_STATE_FILE = resolve_state_dir() / "bot_state.json"
TRADING_REASONS = {
    "below_confidence",
    "below_score",
    "duplicate_cooldown",
    "brief_blocked",
    "iv_rejected",
    "iv_learned_range",
    "delta_rejected",
    "theta_rejected",
    "learned_hour_rejected",
    "neutral",
    "engine_none",
    "ranging_same_direction",
}
RUNTIME_REASONS = {
    "no_contract",
    "slot_full",
    "risk_hold",
    "stale_scan",
    "auth_failure",
    "restart",
    "login_collision",
    "data_feed",
    "lookup_failure",
}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse_iso(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _empty_state(now_value: dt.datetime) -> dict:
    return {
        "generated_at": _iso(now_value),
        "market_date": market_date(now_value).isoformat(),
        "bots": {},
    }


def _canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def _bot_type(bot_id: str) -> str:
    mapping = {
        "india": "equity",
        "fno": "options",
        "us": "us_equity",
        "us_v4": "us_equity",
    }
    return mapping.get(bot_id, bot_id)


def _learning_category(reason: str) -> str:
    if reason in RUNTIME_REASONS:
        return "runtime_incident"
    if reason in TRADING_REASONS:
        return "trade_filter"
    return "analysis_review"


def _suggested_change(reason: str) -> str:
    if reason in RUNTIME_REASONS:
        return "Stabilize execution/runtime path before trusting this behavior."
    if reason in TRADING_REASONS:
        return "Review whether this filter is protecting capital or suppressing valid setups."
    return "Review this repeated pattern before promoting it into bot learning."


def _append_intelligence_records(
    source_bot: str,
    previous_payload: dict,
    current_payload: dict,
    state_dir: Path,
) -> None:
    store = BotIntelligenceStore(state_dir)
    bot_type = _bot_type(source_bot)

    prev_signals = previous_payload.get("signals", []) if isinstance(previous_payload, dict) else []
    curr_signals = current_payload.get("signals", []) if isinstance(current_payload, dict) else []
    seen_signal_keys = {_canonical(item) for item in prev_signals}
    for signal in curr_signals:
        signal_key = _canonical(signal)
        if signal_key in seen_signal_keys:
            continue
        seen_signal_keys.add(signal_key)
        payload = signal if isinstance(signal, dict) else {"raw_signal": str(signal)}
        symbol = payload.get("underlying") or payload.get("symbol") or payload.get("raw_signal", "")
        event = store.append_outcome(
            OutcomeEvent(
                bot_id=source_bot,
                bot_type=bot_type,
                category="signal",
                action_taken="SIGNAL_OBSERVED",
                symbol=symbol,
                regime=str(payload.get("regime", "")),
                position_context={"status": payload.get("status", "")},
                runtime_impact="",
            )
        )
        store.append_belief(
            BeliefSnapshot(
                event_id=event.event_id,
                direction=str(payload.get("direction", payload.get("action", "WATCH"))),
                score=float(payload.get("score", 0) or 0),
                confidence=float(payload.get("confidence", 0) or 0),
                market_context={"dry_run": bool(payload.get("dry_run", False))},
                risk_context={},
                signal_components=[],
                reason_text=str(payload.get("status", payload.get("raw_signal", "signal observed"))),
            )
        )

    prev_rejections = previous_payload.get("rejections", []) if isinstance(previous_payload, dict) else []
    curr_rejections = current_payload.get("rejections", []) if isinstance(current_payload, dict) else []
    seen_rejection_keys = {_canonical(item) for item in prev_rejections}
    for rejection in curr_rejections:
        rejection_key = _canonical(rejection)
        if rejection_key in seen_rejection_keys:
            continue
        seen_rejection_keys.add(rejection_key)
        if isinstance(rejection, dict):
            payload = rejection
        else:
            payload = {"reason": "unknown", "symbol": "", "raw_rejection": str(rejection)}
        reason = str(payload.get("reason", "unknown"))
        symbol = str(payload.get("symbol", ""))
        category = _learning_category(reason)
        event = store.append_outcome(
            OutcomeEvent(
                bot_id=source_bot,
                bot_type=bot_type,
                category=category,
                action_taken="SKIP",
                symbol=symbol,
                regime=str(payload.get("regime", "")),
                position_context={"reason": reason, "details": payload},
                runtime_impact=reason if category == "runtime_incident" else "",
            )
        )
        store.append_belief(
            BeliefSnapshot(
                event_id=event.event_id,
                direction=str(payload.get("direction", "WATCH")),
                score=float(payload.get("score", 0) or 0),
                confidence=float(payload.get("confidence", 0) or 0),
                market_context={},
                risk_context={"reason": reason},
                signal_components=[],
                reason_text=str(payload.get("raw_rejection", reason)),
            )
        )
        store.append_learning(
            LearningRecord(
                event_id=event.event_id,
                learning_type=reason,
                claim=f"{symbol or source_bot} triggered `{reason}`.",
                suggested_change=_suggested_change(reason),
                confidence=60.0 if category == "runtime_incident" else 52.0,
                scope=f"{source_bot}:{symbol or 'global'}",
            )
        )

    prev_positions = previous_payload.get("positions", {}) if isinstance(previous_payload, dict) else {}
    curr_positions = current_payload.get("positions", {}) if isinstance(current_payload, dict) else {}
    prev_keys = set(prev_positions.keys())
    curr_keys = set(curr_positions.keys())
    for symbol in sorted(curr_keys - prev_keys):
        pos = curr_positions.get(symbol, {})
        store.append_outcome(
            OutcomeEvent(
                bot_id=source_bot,
                bot_type=bot_type,
                category="position_open",
                action_taken="OPEN_POSITION",
                symbol=symbol,
                regime=str(pos.get("regime", "")),
                position_context=dict(pos) if isinstance(pos, dict) else {"value": pos},
                pnl_impact=float(pos.get("pnl", 0.0) or 0.0) if isinstance(pos, dict) else 0.0,
            )
        )
    for symbol in sorted(prev_keys - curr_keys):
        pos = prev_positions.get(symbol, {})
        store.append_outcome(
            OutcomeEvent(
                bot_id=source_bot,
                bot_type=bot_type,
                category="position_closed",
                action_taken="POSITION_REMOVED",
                symbol=symbol,
                regime=str(pos.get("regime", "")),
                position_context=dict(pos) if isinstance(pos, dict) else {"value": pos},
                pnl_impact=float(pos.get("pnl", 0.0) or 0.0) if isinstance(pos, dict) else 0.0,
            )
        )


def update_bot_state(
    source_bot: str,
    payload: dict,
    path: str | Path | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> dict:
    state_path = Path(path) if path else BOT_STATE_FILE
    now_value = (now or _utc_now)()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = _empty_state(now_value)
    else:
        state = _empty_state(now_value)

    state["generated_at"] = _iso(now_value)
    state["market_date"] = market_date(now_value).isoformat()
    bots = state.setdefault("bots", {})
    previous_payload = bots.get(source_bot, {}) if isinstance(bots.get(source_bot, {}), dict) else {}
    bot_payload = dict(payload or {})
    bot_payload.setdefault("positions", {})
    bot_payload.setdefault("bets", {})
    bot_payload.setdefault("signals", [])
    bot_payload.setdefault("rejections", [])
    bot_payload.setdefault("pnl", 0.0)
    bot_payload.setdefault("health", {})
    bot_payload.setdefault("performance", {})
    bot_payload.setdefault("promotion_status", {})
    bot_payload.setdefault("scheduler_status", {})
    bot_payload.setdefault("safe_mode", {})
    bot_payload["source_bot"] = source_bot
    bot_payload["updated_at"] = _iso(now_value)
    bots[source_bot] = bot_payload
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")
    try:
        _append_intelligence_records(source_bot, previous_payload, bot_payload, state_path.parent)
    except Exception:
        pass
    return state


def read_bot_state(
    path: str | Path | None = None,
    now: Callable[[], dt.datetime] | None = None,
    max_age_minutes: int = 30,
) -> dict:
    state_path = Path(path) if path else BOT_STATE_FILE
    now_value = (now or _utc_now)()
    if not state_path.exists():
        return _empty_state(now_value)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state(now_value)

    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=dt.timezone.utc)
    now_value = now_value.astimezone(dt.timezone.utc)
    for bot in state.get("bots", {}).values():
        updated = _parse_iso(bot.get("updated_at", ""))
        bot["stale"] = True
        if updated is not None:
            age = (now_value - updated).total_seconds() / 60
            bot["age_minutes"] = round(max(age, 0), 1)
            bot["stale"] = age > max_age_minutes
    return state
