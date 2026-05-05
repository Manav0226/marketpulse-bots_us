"""
US market weekly research and earnings setup bot.

Focus:
- weekend/weekly candidate ideas
- pre-earnings swing bias
- result-day directional framing
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import time
from pathlib import Path

try:
    import pandas as pd
except Exception:  # pragma: no cover - fallback for stripped test runtime
    pd = None

try:
    import requests
except Exception:  # pragma: no cover - fallback for stripped test runtime
    requests = None

try:
    import yfinance as yf
except Exception:  # pragma: no cover - fallback for stripped test runtime
    yf = None

from core.config_loader import (
    FINNHUB_KEY,
    GITHUB_REPO,
    GITHUB_TOKEN,
    GITHUB_USER,
    US_INTEL_TG_CHAT,
    US_RESEARCH_TG_CHAT,
    US_RESEARCH_TG_TOKEN,
)
from core.father_brain import FatherBrain
from marketpulse_runtime import resolve_log_dir, resolve_state_dir
from us_supervisor import refresh_us_supervision

LOG_DIR = resolve_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = resolve_state_dir()
STATE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"us_research_{dt.date.today()}.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("us_research")

TG_TOKEN = US_RESEARCH_TG_TOKEN
TG_CHAT = US_RESEARCH_TG_CHAT or US_INTEL_TG_CHAT

US_SAMPLE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "NFLX", "AVGO", "PLTR", "SHOP", "UBER", "CRWD", "SNOW", "COIN",
    "JPM", "GS", "XOM", "LLY", "UNH", "COST", "WMT", "BA",
]

SECTOR_MAP = {
    "TECH": ["AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMD", "PLTR", "CRWD", "SNOW"],
    "CONSUMER": ["AMZN", "NFLX", "SHOP", "UBER", "COST", "WMT", "TSLA"],
    "FINANCE": ["JPM", "GS", "COIN"],
    "ENERGY": ["XOM"],
    "HEALTHCARE": ["LLY", "UNH"],
    "INDUSTRIAL": ["BA"],
}


def _tg(msg: str):
    if not TG_TOKEN or not TG_CHAT or requests is None:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def _commit_to_github(filename: str) -> bool:
    if not GITHUB_TOKEN or requests is None:
        return False
    path = STATE_DIR / filename
    if not path.exists():
        return False
    content_b64 = base64.b64encode(path.read_bytes()).decode()
    api = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/briefings/{filename}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    r = requests.get(api, headers=headers, timeout=10)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {
        "message": f"research: update {filename} [{dt.date.today()}]",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(api, json=payload, headers=headers, timeout=15)
    return resp.status_code in (200, 201)


def fetch_ohlcv(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    if yf is None or pd is None:
        return pd.DataFrame() if pd is not None else []
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
        df.columns = [str(c).lower() for c in df.columns]
        return df
    except Exception:
        return pd.DataFrame()


def fetch_news_score(symbol: str) -> int:
    if not FINNHUB_KEY or requests is None:
        return 0
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=7)
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": FINNHUB_KEY},
            timeout=10,
        )
        items = r.json()
        if not isinstance(items, list):
            return 0
        pos = ("beat", "growth", "upgrade", "record", "outperform", "strong")
        neg = ("miss", "cut", "downgrade", "warning", "weak", "underperform")
        score = 0
        for item in items[:10]:
            text = f"{item.get('headline', '')} {item.get('summary', '')}".lower()
            score += sum(word in text for word in pos)
            score -= sum(word in text for word in neg)
        return max(-5, min(5, score))
    except Exception:
        return 0


def detect_sector_rotation(results: list[dict]) -> dict:
    symbol_to_sector = {}
    for sector, symbols in SECTOR_MAP.items():
        for symbol in symbols:
            symbol_to_sector[symbol] = sector
    buckets: dict[str, list[float]] = {}
    for row in results:
        sector = symbol_to_sector.get(row["symbol"], "OTHER")
        buckets.setdefault(sector, []).append(float(row["score"]))
    rotation = {}
    for sector, scores in buckets.items():
        avg = sum(scores) / max(len(scores), 1)
        rotation[sector] = "BULLISH" if avg >= 8 else "BEARISH" if avg <= -8 else "NEUTRAL"
    return rotation


def build_weekly_candidates(results: list[dict], limit: int = 8) -> list[dict]:
    ranked = sorted(
        results,
        key=lambda row: (abs(float(row.get("score", 0))), float(row.get("conf", 0))),
        reverse=True,
    )
    return ranked[:limit]


def classify_pre_earnings_bias(
    technical_score: float,
    news_score: int,
    revenue_growth: float,
    earnings_growth: float,
) -> dict:
    composite = technical_score + news_score + (revenue_growth * 12.0) + (earnings_growth * 14.0)
    if composite >= 7:
        bias = "BULLISH"
        result_day_bias = "VOLATILE_BULLISH"
    elif composite <= -7:
        bias = "BEARISH"
        result_day_bias = "VOLATILE_BEARISH"
    else:
        bias = "NEUTRAL"
        result_day_bias = "VOLATILE_NEUTRAL"
    confidence = max(35.0, min(88.0, 50.0 + abs(composite) * 2.2))
    return {
        "bias": bias,
        "result_day_bias": result_day_bias,
        "confidence": round(confidence, 1),
        "composite": round(composite, 2),
    }


def build_agent_consensus(
    symbol: str,
    technical_score: float,
    news_score: int,
    revenue_growth: float,
    earnings_growth: float,
) -> dict:
    bias = classify_pre_earnings_bias(technical_score, news_score, revenue_growth, earnings_growth)
    tech_view = "BULLISH" if technical_score >= 6 else "BEARISH" if technical_score <= -6 else "NEUTRAL"
    sentiment_view = "BULLISH" if news_score >= 2 else "BEARISH" if news_score <= -2 else "NEUTRAL"
    fundamental_score = revenue_growth + earnings_growth
    fundamental_view = "BULLISH" if fundamental_score > 0.12 else "BEARISH" if fundamental_score < -0.08 else "NEUTRAL"
    risk_level = "HIGH_EVENT_RISK" if "VOLATILE" in bias["result_day_bias"] else "NORMAL"
    bull_case = (
        f"Technical trend and earnings trajectory support upside in {symbol}."
        if bias["bias"] == "BULLISH"
        else f"Positive signals exist, but upside case for {symbol} is not dominant."
    )
    bear_case = (
        f"Event volatility and weak directional evidence can pressure {symbol}."
        if bias["bias"] != "BULLISH"
        else f"Earnings-day volatility can still hit {symbol} even with bullish setup."
    )
    tie_breaker = (
        "Favor trend-following direction when technical and fundamental views agree."
        if tech_view == fundamental_view and tech_view != "NEUTRAL"
        else "Reduce conviction and wait for post-event confirmation when views disagree."
    )
    return {
        "symbol": symbol,
        "final_bias": bias["bias"],
        "result_day_bias": bias["result_day_bias"],
        "confidence": bias["confidence"],
        "analyst_views": {
            "technical": {"view": tech_view, "score": round(float(technical_score), 2)},
            "sentiment": {"view": sentiment_view, "score": int(news_score)},
            "fundamental": {
                "view": fundamental_view,
                "revenue_growth": round(float(revenue_growth), 4),
                "earnings_growth": round(float(earnings_growth), 4),
            },
        },
        "risk": {
            "level": risk_level,
            "reason": "earnings_window" if risk_level == "HIGH_EVENT_RISK" else "normal_window",
        },
        "bull_case": bull_case,
        "bear_case": bear_case,
        "tie_breaker": tie_breaker,
    }


def apply_source_health_adjustment(confidence: float, source_health: dict) -> dict:
    adjusted = float(confidence)
    warnings: list[str] = []
    if not source_health.get("news_available", True):
        adjusted -= 8.0
        warnings.append("news unavailable")
    if not source_health.get("earnings_calendar_available", True):
        adjusted -= 10.0
        warnings.append("earnings calendar unavailable")
    if not source_health.get("price_data_fresh", True):
        adjusted -= 7.0
        warnings.append("price data stale")
    if source_health.get("fundamental_data_partial", False):
        adjusted -= 5.0
        warnings.append("fundamental data partial")
    adjusted = max(25.0, min(95.0, adjusted))
    return {
        "confidence": round(adjusted, 1),
        "degraded": bool(warnings),
        "warnings": warnings,
    }


def build_us_weekly_brief(
    results: list[dict],
    sector_rotation: dict,
    earnings_setups: list[dict],
    generated_at: str,
    market_date: str,
    source_health: dict | None = None,
) -> dict:
    bullish = sorted([row for row in results if row["signal"] == "BUY"], key=lambda row: row["score"], reverse=True)
    bearish = sorted([row for row in results if row["signal"] == "SELL"], key=lambda row: row["score"])
    candidates = build_weekly_candidates(results)
    all_scores = [float(row["score"]) for row in results]
    avg_score = sum(all_scores) / max(len(all_scores), 1)
    return {
        "date": market_date,
        "generated_at": generated_at,
        "top_bullish": [row["symbol"] for row in bullish[:12]],
        "top_bearish": [row["symbol"] for row in bearish[:12]],
        "weekly_candidates": candidates,
        "earnings_setups": earnings_setups,
        "sector_rotation": sector_rotation,
        "market_bias": "BULLISH" if avg_score >= 5 else "BEARISH" if avg_score <= -5 else "NEUTRAL",
        "signal_count": len(results),
        "avg_score": round(avg_score, 2),
        "source_health": source_health or {},
    }


def score_symbol(symbol: str, engine) -> dict | None:
    try:
        sig = engine.analyze(symbol, record_signal=False)
        if sig is None or sig.signal in ("NO TRADE", "NEUTRAL"):
            return None
        return {
            "symbol": symbol,
            "signal": sig.signal,
            "score": sig.total_score,
            "conf": round(sig.confidence, 1),
        }
    except Exception as exc:
        log.warning("score failed for %s: %s", symbol, exc)
        return None


def _extract_growth(info: dict, *keys: str) -> float:
    for key in keys:
        value = info.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def build_earnings_setup(symbol: str, technical_score: float) -> dict | None:
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info if isinstance(ticker.info, dict) else {}
        cal = ticker.calendar
        if cal is None or getattr(cal, "empty", True):
            return None
        earnings_date = None
        try:
            earnings_date = str(cal.iloc[0, 0]).split(" ")[0]
        except Exception:
            pass
        news_score = fetch_news_score(symbol)
        revenue_growth = _extract_growth(info, "revenueGrowth", "quarterlyRevenueGrowth")
        earnings_growth = _extract_growth(info, "earningsGrowth", "earningsQuarterlyGrowth")
        bias = classify_pre_earnings_bias(technical_score, news_score, revenue_growth, earnings_growth)
        consensus = build_agent_consensus(symbol, technical_score, news_score, revenue_growth, earnings_growth)
        source_health = {
            "news_available": FINNHUB_KEY and requests is not None,
            "earnings_calendar_available": bool(earnings_date),
            "price_data_fresh": True,
            "fundamental_data_partial": not bool(info),
        }
        adjusted = apply_source_health_adjustment(bias["confidence"], source_health)
        return {
            "symbol": symbol,
            "earnings_date": earnings_date,
            "pre_result_bias": bias["bias"],
            "result_day_bias": bias["result_day_bias"],
            "confidence": adjusted["confidence"],
            "news_score": news_score,
            "revenue_growth": revenue_growth,
            "earnings_growth": earnings_growth,
            "consensus": consensus,
            "source_health": source_health,
            "source_warnings": adjusted["warnings"],
        }
    except Exception as exc:
        log.debug("earnings setup failed for %s: %s", symbol, exc)
        return None


def main():
    from trading_engine import TradingEngine

    log.info("=" * 60)
    log.info("US weekly research bot")
    log.info("=" * 60)
    engine = TradingEngine(capital=100_000, max_risk_pct=0.02)
    results: list[dict] = []
    earnings_setups: list[dict] = []

    for index, symbol in enumerate(US_SAMPLE):
        row = score_symbol(symbol, engine)
        if row:
            results.append(row)
            earnings_setup = build_earnings_setup(symbol, float(row["score"]))
            if earnings_setup:
                earnings_setups.append(earnings_setup)
        if index and index % 8 == 0:
            log.info("[PROGRESS] Scored %s/%s", index, len(US_SAMPLE))
        time.sleep(0.2)

    sector_rotation = detect_sector_rotation(results)
    generated_at = dt.datetime.utcnow().isoformat() + "Z"
    market_date = dt.date.today().isoformat()
    source_health = {
        "news_available": bool(FINNHUB_KEY and requests is not None),
        "earnings_calendar_available": bool(earnings_setups),
        "price_data_fresh": bool(results),
        "fundamental_data_partial": False,
        "symbols_requested": len(US_SAMPLE),
        "symbols_scored": len(results),
        "symbols_with_earnings_setups": len(earnings_setups),
    }
    source_adjustment = apply_source_health_adjustment(80.0, source_health)
    source_health["degraded"] = source_adjustment["degraded"]
    source_health["warnings"] = source_adjustment["warnings"]
    log.info(
        "[SUMMARY] scored=%s/%s | earnings=%s | warnings=%s",
        len(results),
        len(US_SAMPLE),
        len(earnings_setups),
        ", ".join(source_health["warnings"]) or "none",
    )
    brief = build_us_weekly_brief(results, sector_rotation, earnings_setups, generated_at, market_date, source_health=source_health)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "us_weekly_brief.json"
    path.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    _commit_to_github("us_weekly_brief.json")
    FatherBrain(STATE_DIR).ingest_us_research_brief(brief)
    refresh_us_supervision(STATE_DIR)

    top_bulls = ", ".join(brief["top_bullish"][:5]) or "none"
    top_bears = ", ".join(brief["top_bearish"][:5]) or "none"
    earnings_lines = []
    for item in brief["earnings_setups"][:5]:
        earnings_lines.append(
            f"{item['symbol']} {item['pre_result_bias']} -> {item['result_day_bias']} ({item['confidence']:.0f}%)"
        )
    _tg(
        f"📊 <b>US Weekly Research — {market_date}</b>\n\n"
        f"🟢 Top Bullish: {top_bulls}\n"
        f"🔴 Top Bearish: {top_bears}\n"
        f"📈 Market Bias: {brief['market_bias']}\n\n"
        f"🗓 Earnings Setups:\n" + ("\n".join(earnings_lines) if earnings_lines else "No near-term setups found")
    )
    log.info("[DONE] US research bot complete")


if __name__ == "__main__":
    main()
