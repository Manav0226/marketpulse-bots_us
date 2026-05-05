from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class FatherBrainStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS brain_sources (
                    source TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_memory (
                    source TEXT NOT NULL,
                    market_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    bias TEXT NOT NULL DEFAULT 'NEUTRAL',
                    sector TEXT NOT NULL DEFAULT 'OTHER',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, market_date, symbol)
                );

                CREATE TABLE IF NOT EXISTS market_regimes (
                    source TEXT NOT NULL,
                    market_date TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, market_date)
                );

                CREATE TABLE IF NOT EXISTS brain_snapshots (
                    snapshot_name TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def save_source_payload(self, source: str, payload: dict[str, Any]) -> None:
        now_iso = utc_now_iso()
        generated_at = str(payload.get("generated_at") or now_iso)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO brain_sources (source, generated_at, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    generated_at=excluded.generated_at,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (source, generated_at, json.dumps(payload, default=str), now_iso),
            )

    def save_ranked_symbols(
        self,
        source: str,
        market_date: str,
        symbols: list[dict[str, Any]],
    ) -> None:
        now_iso = utc_now_iso()
        rows = []
        for item in symbols:
            rows.append(
                (
                    source,
                    market_date,
                    str(item.get("symbol", "")).upper(),
                    float(item.get("score", 0.0) or 0.0),
                    float(item.get("confidence", 0.0) or 0.0),
                    str(item.get("bias", "NEUTRAL") or "NEUTRAL").upper(),
                    str(item.get("sector", "OTHER") or "OTHER"),
                    json.dumps(item.get("metadata", {}), default=str),
                    now_iso,
                )
            )
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM stock_memory WHERE source = ? AND market_date = ?",
                (source, market_date),
            )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO stock_memory (
                        source, market_date, symbol, score, confidence, bias, sector, meta_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def save_market_regime(
        self,
        source: str,
        market_date: str,
        regime: str,
        confidence: float,
        meta: dict[str, Any] | None = None,
    ) -> None:
        now_iso = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO market_regimes (source, market_date, regime, confidence, meta_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, market_date) DO UPDATE SET
                    regime=excluded.regime,
                    confidence=excluded.confidence,
                    meta_json=excluded.meta_json,
                    updated_at=excluded.updated_at
                """,
                (
                    source,
                    market_date,
                    regime,
                    float(confidence),
                    json.dumps(meta or {}, default=str),
                    now_iso,
                ),
            )

    def save_snapshot(self, snapshot_name: str, payload: dict[str, Any]) -> None:
        now_iso = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO brain_snapshots (snapshot_name, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(snapshot_name) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (snapshot_name, json.dumps(payload, indent=2, default=str), now_iso),
            )

