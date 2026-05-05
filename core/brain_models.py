from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class RankedSymbol:
    symbol: str
    score: float = 0.0
    confidence: float = 0.0
    bias: str = "NEUTRAL"
    sector: str = "OTHER"
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbol"] = (self.symbol or "").upper()
        payload["bias"] = (self.bias or "NEUTRAL").upper()
        payload["sector"] = self.sector or "OTHER"
        payload["source"] = self.source or ""
        return payload


@dataclass(slots=True)
class BrainBrief:
    date: str
    generated_at: str
    market_regime: str
    equity_focus: list[dict[str, Any]]
    avoid_symbols: list[str]
    sector_leaders: list[dict[str, Any]]
    source_health: dict[str, Any]
    us_equity_focus: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["avoid_symbols"] = [str(symbol).upper() for symbol in self.avoid_symbols]
        return payload
