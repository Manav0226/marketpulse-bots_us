"""
dashboard/server.py - MarketPulse Pro Live Dashboard
Deploy to Render.com. Reads briefing JSONs from GitHub with local fallback.
Password-gated via DASHBOARD_PASSWORD env var.
"""
import base64
import datetime
import json
import os
from functools import wraps
from pathlib import Path

import requests
from bot_intelligence_store import BotIntelligenceStore
from flask import Flask, redirect, render_template, request, session, url_for
from marketpulse_runtime import resolve_state_dir


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "changeme_in_prod")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "marketpulse2026")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "Manav-Deakin-23")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "marketpulse-bots")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/briefings"
LOCAL_BRIEFINGS = Path(__file__).resolve().parents[1] / "briefings"
INTELLIGENCE_STATE_DIR = resolve_state_dir()

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


def _load_local_json(filename: str) -> dict:
    path = LOCAL_BRIEFINGS / filename
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fetch_json(filename: str) -> dict:
    """Fetch a JSON file from GitHub with local fallback for workspace usage."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    try:
        response = requests.get(f"{GITHUB_API}/{filename}", headers=headers, timeout=8)
        if response.status_code == 200:
            content = base64.b64decode(response.json()["content"]).decode("utf-8")
            return json.loads(content)
    except Exception:
        pass
    return _load_local_json(filename)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _bot_label(bot_id: str) -> str:
    return {
        "india": "India Bot",
        "fno": "F&O Bot",
        "us": "US Bot",
        "us_v4": "US Bot V4",
    }.get(bot_id, bot_id.replace("_", " ").title())


def _recent_items(items: list[dict], limit: int = 8) -> list[dict]:
    def sort_key(item):
        return item.get("time") or item.get("updated_at") or ""

    return sorted(items, key=sort_key, reverse=True)[:limit]


def _nav_items(active: str) -> list[dict]:
    items = [
        {"label": "Live Dashboard", "href": url_for("index"), "key": "live"},
        {"label": "Bot Intelligence", "href": url_for("bot_intelligence"), "key": "intelligence"},
    ]
    for item in items:
        item["active"] = item["key"] == active
    return items


def _summarize_bot_states(bot_state: dict, capital: dict, fno_capital: dict) -> list[dict]:
    bots = bot_state.get("bots", {})
    specs = [
        ("india", "India Bot", capital.get("session_pnl", 0), capital.get("current_capital", capital.get("capital", 0))),
        ("fno", "F&O Bot", fno_capital.get("session_pnl", 0), fno_capital.get("capital", 0)),
        ("us", "US Bot", 0, 0),
        ("us_v4", "US Bot V4", 0, 0),
    ]
    cards = []
    for bot_id, label, fallback_pnl, fallback_capital in specs:
        payload = bots.get(bot_id, {})
        health = payload.get("health", {}) if isinstance(payload, dict) else {}
        cards.append(
            {
                "id": bot_id,
                "label": label,
                "pnl": _safe_float(payload.get("pnl", fallback_pnl)),
                "capital": _safe_float(payload.get("capital", fallback_capital)),
                "stale": payload.get("stale", True),
                "age_minutes": payload.get("age_minutes"),
                "connected": health.get("connected"),
                "dry_run": health.get("dry_run"),
                "trades": health.get("trades", 0),
                "wins": health.get("wins", 0),
                "losses": health.get("losses", 0),
                "positions": payload.get("positions", {}),
                "signals": payload.get("signals", []),
                "rejections": payload.get("rejections", []),
            }
        )
    return cards


def _bot_overview_metrics(bot_cards: list[dict]) -> dict:
    active_bots = [
        card
        for card in bot_cards
        if card["trades"] or card["signals"] or card["rejections"] or card["positions"]
    ]
    return {
        "tracked_bots": len(bot_cards),
        "active_bots": len(active_bots),
        "connected_bots": sum(1 for card in bot_cards if card.get("connected")),
        "stale_bots": sum(1 for card in bot_cards if card.get("stale")),
        "total_pnl": sum(card.get("pnl", 0) for card in bot_cards),
    }


def _build_live_outcomes(bot_cards: list[dict]) -> list[dict]:
    outcomes = []
    for card in bot_cards:
        for signal in card.get("signals", [])[-12:]:
            if isinstance(signal, dict):
                symbol = signal.get("underlying") or signal.get("symbol") or "-"
                direction = signal.get("direction", "WATCH")
                belief = f"score {signal.get('score', 0)} | conf {signal.get('confidence', 0)}%"
                status = signal.get("status", "signal observed")
                timestamp = signal.get("time", "")
            else:
                symbol = str(signal)
                direction = "WATCH"
                belief = "pending confirmation"
                status = "signal staged"
                timestamp = ""
            outcomes.append(
                {
                    "bot": card["label"],
                    "category": "signal",
                    "symbol": symbol,
                    "headline": f"{direction} signal",
                    "belief": belief,
                    "status": status,
                    "time": timestamp,
                }
            )
        for symbol, pos in (card.get("positions") or {}).items():
            outcomes.append(
                {
                    "bot": card["label"],
                    "category": "position",
                    "symbol": symbol,
                    "headline": f"{pos.get('direction', pos.get('action', 'OPEN'))} open position",
                    "belief": f"entry {pos.get('entry_premium', pos.get('entry', 0))}",
                    "status": "open position",
                    "time": pos.get("entry_time") or pos.get("time", ""),
                }
            )
    return _recent_items(outcomes, limit=12)


def _classify_rejection(reason: str) -> str:
    if reason in RUNTIME_REASONS:
        return "runtime"
    if reason in TRADING_REASONS:
        return "trading"
    return "analysis"


def _load_store_records() -> dict:
    try:
        store = BotIntelligenceStore(INTELLIGENCE_STATE_DIR)
        return {
            "outcomes": store.list_outcomes(),
            "beliefs": store.list_beliefs(),
            "learning": store.list_learning(),
            "proofs": store.list_proofs(),
        }
    except Exception:
        return {"outcomes": [], "beliefs": [], "learning": [], "proofs": []}


def _build_store_outcomes(records: dict) -> list[dict]:
    belief_map = {}
    for belief in records.get("beliefs", []):
        belief_map.setdefault(belief.event_id, belief)

    items = []
    for outcome in records.get("outcomes", []):
        belief = belief_map.get(outcome.event_id)
        belief_text = "pending evidence"
        if belief is not None:
            belief_text = f"{belief.direction} | score {belief.score:g} | conf {belief.confidence:g}%"
        status = outcome.runtime_impact or outcome.action_taken.replace("_", " ").lower()
        items.append(
            {
                "bot": _bot_label(outcome.bot_id),
                "bot_id": outcome.bot_id,
                "category": outcome.category,
                "symbol": outcome.symbol or "-",
                "headline": outcome.category.replace("_", " ").title(),
                "belief": belief_text,
                "status": status,
                "time": outcome.timestamp,
            }
        )
    return _recent_items(items, limit=18)


def _build_store_learning(records: dict) -> list[dict]:
    outcome_map = {item.event_id: item for item in records.get("outcomes", [])}
    belief_map = {item.event_id: item for item in records.get("beliefs", [])}
    proof_by_learning = {}
    for proof in records.get("proofs", []):
        proof_by_learning[proof.learning_id] = proof

    ledger = []
    for learning in records.get("learning", []):
        outcome = outcome_map.get(learning.event_id)
        belief = belief_map.get(learning.event_id)
        proof = proof_by_learning.get(learning.learning_id)
        symbol = outcome.symbol if outcome else "-"
        evidence = "No belief snapshot"
        if belief is not None:
            evidence = f"{belief.direction} | score {belief.score:g} | conf {belief.confidence:g}%"
        outcome_text = outcome.action_taken.replace("_", " ").title() if outcome else "No linked outcome"
        status = proof.proof_status if proof is not None else learning.status
        confidence = proof.cases_seen * 10 if proof is not None else learning.confidence
        if proof is not None:
            outcome_text = f"{outcome_text} | {proof.cases_seen} follow-up case(s)"
        ledger.append(
            {
                "bot": _bot_label(outcome.bot_id) if outcome else learning.scope.split(":")[0].upper(),
                "bot_id": outcome.bot_id if outcome else learning.scope.split(":")[0],
                "symbol": symbol or "-",
                "category": learning.learning_type,
                "claim": learning.claim,
                "evidence": evidence,
                "outcome": outcome_text,
                "learning": learning.suggested_change,
                "confidence": round(confidence, 1),
                "status": status,
                "time": learning.created_at,
            }
        )
    return _recent_items(ledger, limit=24)


def _build_store_runtime(records: dict) -> list[dict]:
    incidents = []
    for outcome in records.get("outcomes", []):
        if outcome.category != "runtime_incident":
            continue
        incidents.append(
            {
                "bot": _bot_label(outcome.bot_id),
                "bot_id": outcome.bot_id,
                "symbol": outcome.symbol or "-",
                "reason": outcome.runtime_impact or outcome.category,
                "time": outcome.timestamp,
            }
        )
    return _recent_items(incidents, limit=16)


def _filter_items(items: list[dict], filters: dict) -> list[dict]:
    filtered = items
    if filters.get("bot"):
        filtered = [item for item in filtered if item.get("bot_id") == filters["bot"]]
    if filters.get("category"):
        filtered = [item for item in filtered if str(item.get("category", "")) == filters["category"]]
    if filters.get("status"):
        filtered = [item for item in filtered if str(item.get("status", "")) == filters["status"]]
    if filters.get("symbol"):
        symbol = filters["symbol"].upper()
        filtered = [item for item in filtered if str(item.get("symbol", "")).upper() == symbol]
    return filtered


def _filter_options(bot_cards: list[dict], live_outcomes: list[dict], learning_ledger: list[dict]) -> dict:
    return {
        "bots": [{"value": card["id"], "label": card["label"]} for card in bot_cards],
        "categories": sorted({str(item.get("category", "")) for item in [*live_outcomes, *learning_ledger] if item.get("category")}),
        "statuses": sorted({str(item.get("status", "")) for item in learning_ledger if item.get("status")}),
    }


def _build_learning_ledger(bot_cards: list[dict]) -> list[dict]:
    grouped = {}
    for card in bot_cards:
        for rejection in card.get("rejections", [])[-60:]:
            if not isinstance(rejection, dict):
                continue
            key = (card["label"], rejection.get("reason", "unknown"), rejection.get("symbol", ""))
            grouped.setdefault(key, []).append(rejection)

    ledger = []
    for (bot_label, reason, symbol), rows in grouped.items():
        category = _classify_rejection(reason)
        count = len(rows)
        if category == "runtime":
            learning = "Fix execution/runtime path before trusting strategy quality here."
        elif category == "trading":
            learning = "Check whether this filter is protecting capital or hiding valid trades."
        else:
            learning = "Investigate whether this repeated analysis pattern should be promoted or ignored."
        ledger.append(
            {
                "bot": bot_label,
                "symbol": symbol or "-",
                "category": category,
                "claim": f"{symbol or bot_label} repeatedly triggered `{reason}`.",
                "evidence": f"{count} recent cases",
                "outcome": f"Last seen at {rows[-1].get('time', 'unknown time')}",
                "learning": learning,
                "confidence": min(95, 35 + count * 8),
                "status": "watching" if count >= 2 else "new",
                "time": rows[-1].get("time", ""),
            }
        )
    return _recent_items(ledger, limit=18)


def _build_runtime_incidents(bot_cards: list[dict]) -> list[dict]:
    incidents = []
    for card in bot_cards:
        for rejection in card.get("rejections", [])[-80:]:
            if not isinstance(rejection, dict):
                continue
            reason = rejection.get("reason", "unknown")
            if _classify_rejection(reason) != "runtime":
                continue
            incidents.append(
                {
                    "bot": card["label"],
                    "symbol": rejection.get("symbol", "-"),
                    "reason": reason,
                    "time": rejection.get("time", ""),
                }
            )
    return _recent_items(incidents, limit=16)


def _proof_metrics(ledger: list[dict], runtime_incidents: list[dict]) -> dict:
    metrics = {"new": 0, "watching": 0, "proven": 0, "rejected": 0}
    for item in ledger:
        metrics[item["status"]] = metrics.get(item["status"], 0) + 1
    metrics["runtime_incidents"] = len(runtime_incidents)
    return metrics


def _build_context(active_page: str, filters: dict | None = None) -> dict:
    filters = filters or {}
    positions = _fetch_json("positions.json")
    risk_status = _fetch_json("risk_status.json")
    capital = _fetch_json("capital.json")
    daily_brief = _fetch_json("daily_brief.json")
    fno_capital = _fetch_json("fno_capital.json")
    council_state = _fetch_json("council_state.json")
    bot_state = _fetch_json("bot_state.json")
    us_runtime_status = _fetch_json("us_runtime_status.json")
    us_report_status = _fetch_json("us_report_status.json")
    father_opinion = _fetch_json("father_opinion.json")
    us_supervision = _fetch_json("us_supervision.json")

    open_positions = {
        sym: pos
        for sym, pos in positions.items()
        if isinstance(pos, dict) and not pos.get("closed")
    }
    bot_cards = _summarize_bot_states(bot_state, capital, fno_capital)
    records = _load_store_records()
    live_outcomes = _build_store_outcomes(records)
    learning_ledger = _build_store_learning(records)
    runtime_incidents = _build_store_runtime(records)
    if not live_outcomes:
        live_outcomes = _build_live_outcomes(bot_cards)
    if not learning_ledger:
        learning_ledger = _build_learning_ledger(bot_cards)
    if not runtime_incidents:
        runtime_incidents = _build_runtime_incidents(bot_cards)
    filtered_live_outcomes = _filter_items(live_outcomes, filters)
    filtered_learning_ledger = _filter_items(learning_ledger, filters)
    filtered_runtime_incidents = _filter_items(runtime_incidents, filters)
    us_live_positions = us_runtime_status.get("position_snapshot", {}) or {}
    us_live_pnl = sum(_safe_float(pos.get("pnl")) for pos in us_live_positions.values())
    us_live_position_count = len(us_live_positions)
    polymarket_bets = (((bot_state.get("bots", {}) or {}).get("us_v4", {}) or {}).get("bets", {}) or {})

    return {
        "nav_items": _nav_items(active_page),
        "india_pnl": capital.get("session_pnl", 0),
        "india_cap": capital.get("current_capital", capital.get("capital", 0)),
        "open_positions": open_positions,
        "sentiment": daily_brief.get("global_sentiment", "-"),
        "brief_date": daily_brief.get("date", "-"),
        "risk_msg": risk_status.get("reason", "-"),
        "risk_ok": risk_status.get("status", "") == "OK",
        "fno_capital": fno_capital,
        "council_direction": council_state.get("cross_market_bias", {}).get("verdict", "NEUTRAL"),
        "council_confidence": council_state.get("confidence", 0),
        "council_watchlist": council_state.get("watchlist", []),
        "council_avoid_list": council_state.get("avoid_list", []),
        "council_markets": council_state.get("market_verdicts", {}),
        "now_ist": datetime.datetime.utcnow().strftime("%d %b %Y %H:%M UTC"),
        "bot_cards": bot_cards,
        "bot_metrics": _bot_overview_metrics(bot_cards),
        "live_outcomes": filtered_live_outcomes,
        "learning_ledger": filtered_learning_ledger,
        "runtime_incidents": filtered_runtime_incidents,
        "proof_metrics": _proof_metrics(filtered_learning_ledger, filtered_runtime_incidents),
        "intelligence_filters": filters,
        "filter_options": _filter_options(bot_cards, live_outcomes, learning_ledger),
        "us_runtime_status": us_runtime_status,
        "us_report_status": us_report_status,
        "father_opinion": father_opinion,
        "us_supervision": us_supervision,
        "us_live_positions": us_live_positions,
        "us_live_pnl": us_live_pnl,
        "us_live_position_count": us_live_position_count,
        "polymarket_bets": polymarket_bets,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Wrong password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", **_build_context("live"))


@app.route("/bot-intelligence")
@login_required
def bot_intelligence():
    filters = {
        "bot": request.args.get("bot", "").strip(),
        "category": request.args.get("category", "").strip(),
        "status": request.args.get("status", "").strip(),
        "symbol": request.args.get("symbol", "").strip(),
    }
    filters = {key: value for key, value in filters.items() if value}
    return render_template("bot_intelligence.html", **_build_context("intelligence", filters))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
