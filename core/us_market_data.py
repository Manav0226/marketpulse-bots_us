from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import requests


UTC = dt.timezone.utc


def fetch_alpaca_bars(
    symbol: str,
    api_key: str,
    api_secret: str,
    *,
    timeframe: str = "1Day",
    days: int = 90,
    base_url: str = "https://data.alpaca.markets",
    session: requests.Session | None = None,
) -> pd.DataFrame:
    if not api_key or not api_secret:
        return pd.DataFrame()

    current = dt.datetime.now(UTC)
    start = current - dt.timedelta(days=max(days + 10, 30))
    client = session or requests.Session()
    url = f"{base_url.rstrip('/')}/v2/stocks/{symbol}/bars"
    response = client.get(
        url,
        params={
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": current.isoformat().replace("+00:00", "Z"),
            "limit": max(days + 10, 60),
            "adjustment": "raw",
            "feed": "iex",
        },
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        },
        timeout=20,
    )
    if response.status_code != 200:
        return pd.DataFrame()

    payload = response.json() or {}
    bars = payload.get("bars") or []
    if not bars:
        return pd.DataFrame()

    rows = []
    for bar in bars:
        rows.append(
            {
                "timestamp": bar.get("t"),
                "open": float(bar.get("o", 0) or 0),
                "high": float(bar.get("h", 0) or 0),
                "low": float(bar.get("l", 0) or 0),
                "close": float(bar.get("c", 0) or 0),
                "volume": float(bar.get("v", 0) or 0),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.tail(days).reset_index(drop=True)
