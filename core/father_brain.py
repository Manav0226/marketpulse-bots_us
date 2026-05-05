from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from core.brain_models import BrainBrief, RankedSymbol
from core.brain_store import FatherBrainStore
from core.config_loader import FNO_BASE_CAPITAL, INDIA_CAPITAL, US_CAPITAL
from marketpulse_runtime import resolve_state_dir


class FatherBrain:
    def __init__(self, state_dir: str | Path | None = None):
        self.state_dir = resolve_state_dir(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = FatherBrainStore(self.state_dir / "father_brain.db")

    def ingest_research_brief(self, brief: dict[str, Any]) -> dict[str, Any]:
        market_date = str(brief.get("date") or dt.date.today().isoformat())
        bullish = self._coerce_symbols(brief.get("top_bullish"), "BULLISH", "research")
        bearish = self._coerce_symbols(brief.get("top_bearish"), "BEARISH", "research")
        ranked = bullish + bearish

        self._write_json("fundamental_brief.json", brief)
        self.store.save_source_payload("research", brief)
        self.store.save_ranked_symbols("research", market_date, [item.to_dict() for item in ranked])
        self.store.save_market_regime(
            "research",
            market_date,
            str(brief.get("nifty_bias", "NEUTRAL")).upper(),
            confidence=min(95.0, 45.0 + len(bullish) + len(bearish)),
            meta={
                "signal_count": int(brief.get("signal_count", 0) or 0),
                "avg_score": float(brief.get("avg_score", 0) or 0),
                "sector_rotation": brief.get("sector_rotation", {}),
            },
        )
        return self.refresh_outputs(research_brief=brief)

    def ingest_premarket_brief(self, brief: dict[str, Any]) -> dict[str, Any]:
        market_date = str(brief.get("date") or dt.date.today().isoformat())
        ranked: list[RankedSymbol] = []
        for item in brief.get("top_watchlist", []) or []:
            ranked.append(
                RankedSymbol(
                    symbol=str(item.get("symbol", "")).upper(),
                    score=float(item.get("score", 0) or 0),
                    confidence=min(99.0, 50.0 + abs(float(item.get("score", 0) or 0)) * 5),
                    bias=str(item.get("bias", "NEUTRAL") or "NEUTRAL").upper(),
                    sector=str(item.get("sector", "OTHER") or "OTHER"),
                    source="premarket",
                    metadata={
                        "price": item.get("price"),
                        "change_pct": item.get("change_pct"),
                        "key_level": item.get("key_level"),
                    },
                )
            )

        self._write_json("daily_brief.json", brief)
        self.store.save_source_payload("premarket", brief)
        self.store.save_ranked_symbols("premarket", market_date, [item.to_dict() for item in ranked])
        self.store.save_market_regime(
            "premarket",
            market_date,
            str(brief.get("global_sentiment", "NEUTRAL")).upper(),
            confidence=min(95.0, 55.0 + len(ranked) * 2),
            meta={
                "avoid_symbols": brief.get("avoid_symbols", []),
                "high_risk_today": bool(brief.get("high_risk_today")),
                "dynamic_watchlist_size": len(brief.get("dynamic_watchlist", []) or []),
            },
        )
        return self.refresh_outputs(premarket_brief=brief)

    def ingest_us_research_brief(self, brief: dict[str, Any]) -> dict[str, Any]:
        market_date = str(brief.get("date") or dt.date.today().isoformat())
        bullish = self._coerce_symbols(brief.get("top_bullish"), "BULLISH", "us_research")
        bearish = self._coerce_symbols(brief.get("top_bearish"), "BEARISH", "us_research")
        ranked = bullish + bearish

        self._write_json("us_weekly_brief.json", brief)
        self.store.save_source_payload("us_research", brief)
        self.store.save_ranked_symbols("us_research", market_date, [item.to_dict() for item in ranked])
        self.store.save_market_regime(
            "us_research",
            market_date,
            str(brief.get("market_bias", "NEUTRAL")).upper(),
            confidence=min(95.0, 45.0 + len(bullish) + len(bearish)),
            meta={
                "signal_count": int(brief.get("signal_count", 0) or 0),
                "avg_score": float(brief.get("avg_score", 0) or 0),
                "sector_rotation": brief.get("sector_rotation", {}),
                "earnings_setups": brief.get("earnings_setups", []),
            },
        )
        return self.refresh_outputs(us_research_brief=brief)

    def refresh_outputs(
        self,
        research_brief: dict[str, Any] | None = None,
        premarket_brief: dict[str, Any] | None = None,
        us_research_brief: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        research_brief = research_brief or self._read_json("fundamental_brief.json")
        premarket_brief = premarket_brief or self._read_json("daily_brief.json")
        us_research_brief = us_research_brief or self._read_json("us_weekly_brief.json")

        generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        market_date = str(
            premarket_brief.get("date")
            or research_brief.get("date")
            or dt.date.today().isoformat()
        )
        market_regime = str(
            premarket_brief.get("global_sentiment")
            or research_brief.get("nifty_bias")
            or "NEUTRAL"
        ).upper()

        research_focus = {
            item.symbol: item
            for item in self._coerce_symbols(research_brief.get("top_bullish"), "BULLISH", "research")
        }
        us_research_focus = {
            item.symbol: item
            for item in self._coerce_symbols(us_research_brief.get("top_bullish"), "BULLISH", "us_research")
        }
        us_research_focus.update(
            {
                item.symbol: item
                for item in self._coerce_symbols(us_research_brief.get("top_bearish"), "BEARISH", "us_research")
            }
        )
        avoid_symbols = {
            str(symbol).upper()
            for symbol in (premarket_brief.get("avoid_symbols", []) or [])
        }
        equity_focus = []
        for row in premarket_brief.get("top_watchlist", []) or []:
            symbol = str(row.get("symbol", "")).upper()
            research_boost = 1 if symbol in research_focus else 0
            equity_focus.append(
                {
                    "symbol": symbol,
                    "bias": str(row.get("bias", "NEUTRAL")).upper(),
                    "sector": row.get("sector", "OTHER"),
                    "premarket_score": float(row.get("score", 0) or 0),
                    "research_boost": research_boost,
                    "composite_score": round(float(row.get("score", 0) or 0) + research_boost * 2, 2),
                    "execution_mode": "fast_path",
                }
            )
        equity_focus.sort(key=lambda item: item["composite_score"], reverse=True)

        us_earnings = {
            str(item.get("symbol", "")).upper(): item
            for item in (us_research_brief.get("earnings_setups", []) or [])
            if item.get("symbol")
        }
        us_equity_focus = []
        for row in us_research_brief.get("weekly_candidates", []) or []:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            ranked = us_research_focus.get(symbol)
            earnings = us_earnings.get(symbol, {})
            raw_score = float(row.get("score", 0) or 0)
            bias = "BULLISH" if raw_score > 0 else "BEARISH" if raw_score < 0 else "NEUTRAL"
            us_equity_focus.append(
                {
                    "symbol": symbol,
                    "bias": bias,
                    "sector": str((ranked.sector if ranked else row.get("sector", "OTHER")) or "OTHER"),
                    "weekly_score": raw_score,
                    "confidence": float(row.get("confidence", row.get("conf", 0)) or 0),
                    "research_boost": 1 if symbol in us_research_focus else 0,
                    "pre_result_bias": earnings.get("pre_result_bias", "NONE"),
                    "result_day_bias": earnings.get("result_day_bias", "NONE"),
                    "earnings_date": earnings.get("earnings_date"),
                    "execution_mode": "swing_supervised",
                }
            )
        us_equity_focus.sort(
            key=lambda item: (item["research_boost"], abs(item["weekly_score"]), item["confidence"]),
            reverse=True,
        )

        sector_heatmap = premarket_brief.get("sector_heatmap", {}) or {}
        sector_leaders = []
        for sector, payload in sector_heatmap.items():
            sector_leaders.append(
                {
                    "sector": sector,
                    "bias": str(payload.get("bias", "MIXED")).upper(),
                    "top_bull": payload.get("top_bull"),
                    "top_bear": payload.get("top_bear"),
                }
            )

        notes = [
            "Equity bots must read prepared picks only; no heavy recompute at entry time.",
            "FNO remains supervised until rejection and contract-resolution quality improve.",
        ]
        if premarket_brief.get("high_risk_today"):
            notes.append("High-impact calendar risk is active today; new entries should be more selective.")

        source_health = {
            "research_loaded": bool(research_brief),
            "premarket_loaded": bool(premarket_brief),
            "us_research_loaded": bool(us_research_brief),
            "top_watchlist_count": len(premarket_brief.get("top_watchlist", []) or []),
            "research_bullish_count": len(research_brief.get("top_bullish", []) or []),
            "us_weekly_candidate_count": len(us_research_brief.get("weekly_candidates", []) or []),
        }

        brain_brief = BrainBrief(
            date=market_date,
            generated_at=generated_at,
            market_regime=market_regime,
            equity_focus=equity_focus[:12],
            avoid_symbols=sorted(avoid_symbols),
            sector_leaders=sector_leaders[:12],
            source_health=source_health,
            us_equity_focus=us_equity_focus[:12],
            notes=notes,
        ).to_dict()

        wallets = self._default_wallets(generated_at)
        brain_state = {
            "generated_at": generated_at,
            "date": market_date,
            "market_regime": market_regime,
            "bot_modes": {
                "research": "learning",
                "premarket": "precompute",
                "india": "active_fast_path",
                "fno": "supervised",
                "us": "active_light_guidance",
                "risk": "guardrail",
            },
        }
        brain_scorecard = {
            "generated_at": generated_at,
            "date": market_date,
            "focus_count": len(brain_brief["equity_focus"]),
            "avoid_count": len(brain_brief["avoid_symbols"]),
            "bullish_focus_count": sum(1 for row in brain_brief["equity_focus"] if row["bias"] == "BULLISH"),
            "research_overlap_count": sum(1 for row in brain_brief["equity_focus"] if row["research_boost"] > 0),
            "market_regime": market_regime,
        }

        self._write_json("brain_brief.json", brain_brief)
        self._write_json("wallets.json", wallets)
        self._write_json("brain_state.json", brain_state)
        self._write_json("brain_scorecard.json", brain_scorecard)
        self.store.save_snapshot("brain_brief", brain_brief)
        self.store.save_snapshot("wallets", wallets)
        self.store.save_snapshot("brain_state", brain_state)
        self.store.save_snapshot("brain_scorecard", brain_scorecard)

        return {
            "brain_brief": brain_brief,
            "wallets": wallets,
            "brain_state": brain_state,
            "brain_scorecard": brain_scorecard,
        }

    def _default_wallets(self, generated_at: str) -> dict[str, Any]:
        path = self.state_dir / "wallets.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                existing["updated_at"] = generated_at
                return existing
            except Exception:
                pass
        return {
            "updated_at": generated_at,
            "treasury": {"balance": 0.0, "currency": "INR"},
            "bots": {
                "india": {"balance": float(INDIA_CAPITAL), "mode": "compounding"},
                "fno": {"balance": float(FNO_BASE_CAPITAL), "mode": "protected"},
                "us": {"balance": float(US_CAPITAL), "mode": "compounding"},
            },
        }

    def _coerce_symbols(
        self,
        raw_items: list[Any] | None,
        default_bias: str,
        source: str,
    ) -> list[RankedSymbol]:
        rows: list[RankedSymbol] = []
        for index, item in enumerate(raw_items or []):
            if isinstance(item, dict):
                symbol = str(item.get("symbol", "")).upper()
                score = float(item.get("score", 0) or 0)
                confidence = float(item.get("confidence", item.get("conf", 0)) or 0)
                sector = str(item.get("sector", "OTHER") or "OTHER")
            else:
                symbol = str(item).upper()
                score = float(max(0, 20 - index))
                confidence = 50.0
                sector = "OTHER"
            if not symbol:
                continue
            rows.append(
                RankedSymbol(
                    symbol=symbol,
                    score=score,
                    confidence=confidence,
                    bias=default_bias,
                    sector=sector,
                    source=source,
                )
            )
        return rows

    def _read_json(self, filename: str) -> dict[str, Any]:
        path = self.state_dir / filename
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_json(self, filename: str, payload: dict[str, Any]) -> None:
        path = self.state_dir / filename
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
