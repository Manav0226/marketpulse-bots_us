"""
╔══════════════════════════════════════════════════════════════╗
║  MARKETPULSE PRO v4 — TRADING ANALYSIS ENGINE                 ║
║  10-Layer Signal Scoring | Backtest | Forward Test             ║
╚══════════════════════════════════════════════════════════════╝

LAYERS:
  1. RSI (Wilder's smoothing)
  2. MACD (line + signal + histogram momentum)
  3. Moving Averages (SMA20/50 + EMA9, golden/death cross)
  4. ADX Trend Strength (filters trendless markets)
  5. VWAP (institutional fair value)
  6. Candlestick Patterns (hammer, engulfing, doji, stars)
  7. Support/Resistance (pivot + cluster detection)
  8. Volume Confirmation (spike detection)
  9. Multi-Timeframe (daily + hourly alignment)
 10. News Sentiment (Finnhub keyword scoring)

USAGE:
  from trading_engine import TradingEngine
  engine = TradingEngine()
  signal = engine.analyze("AAPL")
  backtest = engine.backtest("RELIANCE.NS", days=365)
"""

import datetime, time, math, logging, requests, json
import numpy as np
import yfinance as yf   # kept as fallback for backtesting only — live uses Kite
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional

log = logging.getLogger("Engine")

# ── Global Kite instance (set after login) ───────────────────
# All scoring functions use this for live data instead of yfinance
_global_kite = None

def set_global_kite(kite_instance):
    """Call once after Kite login. Enables official NSE data for all signals."""
    global _global_kite
    _global_kite = kite_instance
    log.info("[ENGINE] Kite data layer active — official NSE data enabled")


def _normalize_ohlcv(hist, log_warning=None):
    """Return an OHLCV frame with lowercase columns, or None if unusable."""
    if hist is None or getattr(hist, "empty", False):
        return None
    hist = hist.copy()
    if hasattr(hist.columns, "nlevels") and hist.columns.nlevels > 1:
        normalized = []
        for col in hist.columns:
            parts = [str(part).strip().lower() for part in col if str(part).strip()]
            normalized.append(next((part for part in parts if part in {"open", "high", "low", "close", "volume"}), parts[0]))
        hist.columns = normalized
    else:
        hist.columns = [str(c).strip().lower() for c in hist.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = sorted(required - set(hist.columns))
    if missing:
        if log_warning:
            log_warning(f"Backtest data missing columns: {missing}; available={list(hist.columns)}")
        return None
    return hist

# ══════════════════════════════
# SYMBOL FIXES (delisted/renamed) — NSE only
# ══════════════════════════════
SYMBOL_MAP = {}   # NSE symbols don't need remapping; kept for fix_symbol() compatibility

SKIP_SYMBOLS = set()  # Symbols that failed — skip for rest of session

# ── Sector map: NSE symbol → sector name (used by score_sector_momentum) ──
SECTOR_MAP = {
    # Banking
    'AXISBANK':'Banking','HDFCBANK':'Banking','ICICIBANK':'Banking',
    'KOTAKBANK':'Banking','SBIN':'Banking','BANKBARODA':'Banking',
    'FEDERALBNK':'Banking','INDUSINDBK':'Banking','IDFCFIRSTB':'Banking',
    # IT
    'TCS':'IT','INFY':'IT','WIPRO':'IT','HCLTECH':'IT','TECHM':'IT',
    'MPHASIS':'IT','LTIM':'IT','PERSISTENT':'IT','COFORGE':'IT',
    # Metals
    'TATASTEEL':'Metals','JSWSTEEL':'Metals','HINDALCO':'Metals',
    'VEDL':'Metals','SAIL':'Metals','NMDC':'Metals',
    # Energy
    'ONGC':'Energy','RELIANCE':'Energy','BPCL':'Energy','IOC':'Energy',
    'GAIL':'Energy','POWERGRID':'Energy','NTPC':'Energy','ADANIGREEN':'Energy',
    # Auto
    'MARUTI':'Auto','TATAMOTORS':'Auto','M&M':'Auto','BAJAJ-AUTO':'Auto',
    'HEROMOTOCO':'Auto','EICHERMOT':'Auto','ASHOKLEY':'Auto',
    # Pharma
    'SUNPHARMA':'Pharma','DRREDDY':'Pharma','CIPLA':'Pharma',
    'DIVISLAB':'Pharma','AUROPHARMA':'Pharma','LUPIN':'Pharma',
    # FMCG
    'HINDUNILVR':'FMCG','ITC':'FMCG','NESTLEIND':'FMCG',
    'BRITANNIA':'FMCG','DABUR':'FMCG','MARICO':'FMCG',
    # Finance
    'BAJFINANCE':'Finance','BAJAJFINSV':'Finance','CHOLAFIN':'Finance',
    'MUTHOOTFIN':'Finance','LICHSGFIN':'Finance','SBILIFE':'Finance',
    # Realty
    'DLF':'Realty','GODREJPROP':'Realty','OBEROIRLTY':'Realty',
    # Logistics/Others
    'DELHIVERY':'Logistics','ZOMATO':'Consumer','NYKAA':'Consumer',
    'PAYTM':'Fintech','IRCTC':'PSU','HAL':'Defence','BEL':'Defence',
    'BOSCHLTD':'Auto','UBL':'FMCG','ASIANPAINT':'Consumer',
}


def fix_symbol(sym):
    return SYMBOL_MAP.get(sym, sym)

# ══════════════════════════════
# DATA CLASSES
# ══════════════════════════════
@dataclass
class Signal:
    symbol: str; price: float; signal: str; confidence: float
    entry: float; stop_loss: float; target1: float; target2: float
    risk_reward: float; quantity: int; risk_amount: float
    total_score: int; layer_scores: Dict[str, int] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    supports: List[float] = field(default_factory=list)
    resistances: List[float] = field(default_factory=list)
    tf_signals: Dict[str, str] = field(default_factory=dict)
    timestamp: str = ""

@dataclass
class BacktestTrade:
    date: str; direction: str; entry: float; exit: float
    pnl: float; pnl_pct: float; bars_held: int; exit_reason: str

@dataclass
class BacktestResult:
    symbol: str; days: int; total_trades: int; wins: int; losses: int
    win_rate: float; total_pnl: float; total_pnl_pct: float
    avg_win: float; avg_loss: float; profit_factor: float
    max_drawdown_pct: float; sharpe_ratio: float
    best_trade: float; worst_trade: float
    trades: List[BacktestTrade] = field(default_factory=list)


# ══════════════════════════════
# INDICATOR CALCULATIONS
# ══════════════════════════════

def calc_rsi(c, period=14):
    if len(c) < period + 1: return None
    d = np.diff(c)
    g = np.where(d > 0, d, 0); l = np.where(d < 0, -d, 0)
    ag = np.mean(g[:period]); al = np.mean(l[:period])
    for i in range(period, len(g)):
        ag = (ag * (period-1) + g[i]) / period
        al = (al * (period-1) + l[i]) / period
    if al == 0: return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)

def calc_ema(data, period):
    if len(data) < period: return None
    e = np.zeros(len(data)); e[period-1] = np.mean(data[:period])
    k = 2.0 / (period + 1)
    for i in range(period, len(data)): e[i] = data[i]*k + e[i-1]*(1-k)
    return e

def calc_macd(c, fast=12, slow=26, sig=9):
    if len(c) < slow + sig: return None, None, None
    ef = calc_ema(c, fast); es = calc_ema(c, slow)
    if ef is None or es is None: return None, None, None
    ml = ef - es
    # Signal line from the valid MACD portion
    valid = ml[slow-1:]
    if len(valid) < sig: return ml[-1], None, None
    sl = calc_ema(valid, sig)
    if sl is None: return ml[-1], None, None
    return ml[-1], sl[-1], ml[-1] - sl[-1]

def calc_adx(h, l, c, period=14):
    n = len(c)
    if n < period * 2: return None, None, None
    tr=[]; pdm=[]; mdm=[]
    for i in range(1, n):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
        hd = h[i]-h[i-1]; ld = l[i-1]-l[i]
        pdm.append(hd if hd > ld and hd > 0 else 0)
        mdm.append(ld if ld > hd and ld > 0 else 0)
    atr=sum(tr[:period])/period; ps=sum(pdm[:period])/period; ms=sum(mdm[:period])/period
    for i in range(period, len(tr)):
        atr=(atr*(period-1)+tr[i])/period
        ps=(ps*(period-1)+pdm[i])/period
        ms=(ms*(period-1)+mdm[i])/period
    if atr==0: return None, None, None
    pdi=100*ps/atr; mdi=100*ms/atr
    ds=pdi+mdi
    if ds==0: return None, pdi, mdi
    return 100*abs(pdi-mdi)/ds, pdi, mdi

def calc_vwap(h, l, c, v, period=20):
    n = min(period, len(c))
    if n < 5: return None
    tp = (h[-n:] + l[-n:] + c[-n:]) / 3
    vol = v[-n:]
    tv = np.sum(vol)
    if tv == 0: return None
    return float(np.sum(tp * vol) / tv)

def calc_atr(h, l, c, period=14):
    n = min(period, len(c)-1)
    if n < 2: return float(c[-1]) * 0.02
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(len(c)-n, len(c))]
    return sum(tr)/len(tr)


# ══════════════════════════════
# SCORING FUNCTIONS
# ══════════════════════════════

def score_rsi(rsi):
    if rsi is None: return 0, []
    if rsi < 20: return 8, [f"RSI extremely oversold ({rsi:.0f})"]
    if rsi < 30: return 5, [f"RSI oversold ({rsi:.0f})"]
    if rsi < 40: return 2, []
    if rsi > 80: return -8, [f"RSI extremely overbought ({rsi:.0f})"]
    if rsi > 70: return -5, [f"RSI overbought ({rsi:.0f})"]
    if rsi > 60: return -2, []
    return 0, []

def score_macd(mv, sv, hv):
    if mv is None: return 0, []
    s = 0; r = []
    s += (2 if mv > 0 else -2)
    if sv is not None:
        if mv > sv: s += 3; r.append("MACD > Signal (bullish)")
        else: s -= 3; r.append("MACD < Signal (bearish)")
    if hv is not None and abs(hv) > abs(mv)*0.1:
        if hv > 0: s += 2; r.append("MACD histogram expanding")
        else: s -= 2
    return s, r

def score_ma(price, c):
    if len(c) < 50: return 0, []
    s = 0; r = []
    sma20 = np.mean(c[-20:]); sma50 = np.mean(c[-50:])
    e9 = calc_ema(c, 9)
    if price > sma20: s += 1
    else: s -= 1
    if price > sma50: s += 1; r.append("Above SMA50 (uptrend)")
    else: s -= 1; r.append("Below SMA50 (downtrend)")
    if sma20 > sma50: s += 2; r.append("Golden alignment (SMA20>50)")
    else: s -= 2; r.append("Death alignment (SMA20<50)")
    if e9 is not None:
        if price > e9[-1]: s += 1
        else: s -= 1
    return s, r

def score_adx(adx, pdi, mdi):
    if adx is None: return 0, []
    if adx < 20: return 0, ["ADX<20: No trend — SKIP"]
    bull = pdi > mdi
    if adx > 40:
        s = 4 if bull else -2  # strong downtrend still penalised but less aggressively
        return s, [f"ADX {adx:.0f}: Strong {'up' if bull else 'down'}trend"]
    s = 2 if bull else -1  # weak downtrend: mild penalty only
    return s, [f"ADX {adx:.0f}: Moderate trend"]

def score_vwap(price, vwap):
    if vwap is None: return 0, []
    pct = ((price - vwap) / vwap) * 100
    if price > vwap:
        return min(3, max(1, int(pct))), [f"Price {pct:+.1f}% above VWAP"]
    return max(-3, min(-1, int(pct))), [f"Price {pct:+.1f}% below VWAP"]

def score_volume(volumes, chg_pct):
    if len(volumes) < 21: return 0, []
    avg = np.mean(volumes[-21:-1])
    if avg == 0: return 0, []
    ratio = volumes[-1] / avg
    if ratio > 2.0:
        return (5 if chg_pct > 0 else -5), [f"Volume spike {ratio:.1f}x (strong)"]
    if ratio > 1.5:
        return (3 if chg_pct > 0 else -3), [f"High volume {ratio:.1f}x"]
    if ratio < 0.5:
        return 0, ["Low volume — weak conviction"]
    return 0, []

def detect_patterns(o, h, l, c):
    if len(c) < 3: return 0, []
    s = 0; pats = []
    # Current bar
    body = abs(c[-1] - o[-1]); rng = h[-1] - l[-1]
    if rng == 0: rng = 0.001
    upper = h[-1] - max(o[-1], c[-1]); lower = min(o[-1], c[-1]) - l[-1]
    prev_body = abs(c[-2] - o[-2])

    # Hammer
    if lower > body * 2 and upper < body * 0.5 and c[-2] < o[-2]:
        pats.append("Hammer"); s += 4
    # Shooting Star
    if upper > body * 2 and lower < body * 0.5 and c[-2] > o[-2]:
        pats.append("Shooting Star"); s -= 4
    # Doji
    if body < rng * 0.1:
        pats.append("Doji")
        if c[-2] > c[-3]: s -= 2
        elif c[-2] < c[-3]: s += 2
    # Bullish Engulfing
    if c[-2] < o[-2] and c[-1] > o[-1] and c[-1] > o[-2] and o[-1] < c[-2] and body > prev_body:
        pats.append("Bullish Engulfing"); s += 5
    # Bearish Engulfing
    if c[-2] > o[-2] and c[-1] < o[-1] and c[-1] < o[-2] and o[-1] > c[-2] and body > prev_body:
        pats.append("Bearish Engulfing"); s -= 5
    # Morning Star
    if (c[-3] < o[-3] and abs(c[-2]-o[-2]) < abs(c[-3]-o[-3])*0.3 and
        c[-1] > o[-1] and c[-1] > (o[-3]+c[-3])/2):
        pats.append("Morning Star"); s += 5
    # Evening Star
    if (c[-3] > o[-3] and abs(c[-2]-o[-2]) < abs(c[-3]-o[-3])*0.3 and
        c[-1] < o[-1] and c[-1] < (o[-3]+c[-3])/2):
        pats.append("Evening Star"); s -= 5
    # Marubozu
    if body > rng * 0.8:
        if c[-1] > o[-1]: pats.append("Bullish Marubozu"); s += 3
        else: pats.append("Bearish Marubozu"); s -= 3

    return s, pats

def find_sr(h, l, c, n_levels=3):
    if len(c) < 20: return [], []
    sups = []; ress = []
    for i in range(2, min(len(l)-2, 60)):
        if l[i] <= min(l[i-1], l[i-2], l[i+1], l[i+2]):
            sups.append(float(l[i]))
        if h[i] >= max(h[i-1], h[i-2], h[i+1], h[i+2]):
            ress.append(float(h[i]))
    def cluster(lvls, tol=1.0):
        if not lvls: return []
        lvls = sorted(set(lvls)); out = [lvls[0]]
        for x in lvls[1:]:
            if abs(x - out[-1]) / max(out[-1], 0.01) * 100 > tol: out.append(x)
        return out
    return cluster(sorted(sups, reverse=True))[:n_levels], cluster(sorted(ress))[:n_levels]

def score_sr(price, sups, ress):
    s = 0; r = []
    if sups:
        ns = min(sups, key=lambda x: abs(price-x))
        d = ((price-ns)/price)*100
        if 0 < d < 2: s += 3; r.append(f"Near support {ns:.1f}")
        elif -1 < d < 0: s -= 2; r.append(f"Support {ns:.1f} broken")
    if ress:
        nr = min(ress, key=lambda x: abs(price-x))
        d = ((nr-price)/price)*100
        if 0 < d < 2: s -= 2; r.append(f"Near resistance {nr:.1f}")
        elif d < 0: s += 3; r.append(f"Broke resistance {nr:.1f}!")
    return s, r

def score_multi_tf(symbol, kite=None):
    """
    Multi-timeframe confirmation using Kite official data.
    Checks 5min, 15min, 1hr, daily all aligned in same direction.
    More confirming timeframes = higher confidence score.
    """
    try:
        if kite:
            from data_provider import get_ohlcv_multi_tf
            tfs = get_ohlcv_multi_tf(kite, symbol)
            h   = tfs.get('daily', __import__('pandas').DataFrame())
        else:
            _is_index = any(x in symbol.upper() for x in ['NIFTY', 'SENSEX', 'INDIA VIX', 'MIDCAP'])
            if _is_index: return 0, {}, []
            import logging as _lg
            _yfl2 = _lg.getLogger('yfinance')
            _yfl2.setLevel(_lg.CRITICAL)
            import yfinance as yf
            h = yf.Ticker(symbol).history(period="3mo", interval="1d")
        if h is None or len(h) < 30: return 0, {}, []
        # Normalise column names (Kite returns lowercase, yfinance returns Title case)
        if hasattr(h, 'columns'):
            h.columns = [c.lower() for c in h.columns]
        c = (h.get('close') or h.get('Close', h.iloc[:,3])).values.astype(float)
        # Daily trend
        sma20_d = np.mean(c[-20:]); sma50_d = np.mean(c[-50:]) if len(c)>=50 else sma20_d
        d_bull = c[-1] > sma20_d and sma20_d > sma50_d
        d_bear = c[-1] < sma20_d and sma20_d < sma50_d
        # Short-term (last 5 days)
        st_bull = c[-1] > c[-5] if len(c) >= 5 else False
        st_bear = c[-1] < c[-5] if len(c) >= 5 else False
        # Medium (last 20 days)
        mt_bull = c[-1] > c[-20] if len(c) >= 20 else False

        tf = {}
        tf["Short(5d)"] = "bullish" if st_bull else "bearish" if st_bear else "neutral"
        tf["Medium(20d)"] = "bullish" if mt_bull else "bearish"
        tf["Daily"] = "bullish" if d_bull else "bearish" if d_bear else "neutral"

        aligned_bull = all(v == "bullish" for v in tf.values())
        aligned_bear = all(v == "bearish" for v in tf.values())
        s = 0; r = []
        if aligned_bull: s = 5; r.append("All timeframes BULLISH aligned")
        elif aligned_bear: s = -5; r.append("All timeframes BEARISH aligned")
        else: r.append(f"Mixed: {tf}")
        return s, tf, r
    except: return 0, {}, []

FINNHUB_KEY = "d6s0cj1r01qpss2hsb00d6s0cj1r01qpss2hsb0g"

def score_sentiment(symbol):
    clean = symbol.replace(".NS","").replace(".BO","").replace("-USD","").replace("/USDT","")
    try:
        now = datetime.date.today().isoformat()
        ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        r = requests.get(f"https://finnhub.io/api/v1/company-news?symbol={clean}&from={ago}&to={now}&token={FINNHUB_KEY}", timeout=8)
        news = r.json()
        if not isinstance(news, list) or not news: return 0, ["No news"]
        pos_words = ['upgrade','beat','growth','profit','surge','rally','strong','record','bullish','buy','outperform','raises']
        neg_words = ['downgrade','miss','loss','decline','crash','weak','cut','bearish','sell','underperform','layoff','warning']
        pc = nc = 0
        for a in news[:20]:
            txt = (a.get('headline','')+' '+a.get('summary','')).lower()
            pc += sum(1 for w in pos_words if w in txt)
            nc += sum(1 for w in neg_words if w in txt)
        t = pc + nc
        if t == 0: return 0, [f"{len(news)} articles, neutral"]
        return int(((pc-nc)/t)*5), [f"News: {pc} positive, {nc} negative ({len(news)} articles)"]
    except: return 0, ["Sentiment unavailable"]


# ══════════════════════════════════════════════════════════════════
#  NEW SKILLS — Quant-grade signal improvements
# ══════════════════════════════════════════════════════════════════

def score_relative_strength(stock_chg: float, nifty_chg: float) -> tuple:
    """RS = stock % change - NIFTY % change. Positive = outperforming."""
    if nifty_chg == 0: return 0, []
    rs = stock_chg - nifty_chg
    if   rs >  3.0: return  4, [f"Strong RS +{rs:.1f}% vs NIFTY (institutional buying)"]
    elif rs >  1.5: return  3, [f"Outperforming NIFTY by +{rs:.1f}%"]
    elif rs >  0.5: return  2, []
    elif rs > -0.5: return  0, []
    elif rs > -1.5: return -2, [f"Lagging NIFTY by {rs:.1f}%"]
    elif rs > -3.0: return -3, [f"Weak vs NIFTY {rs:.1f}% — distribution"]
    else:           return -5, [f"Severely underperforming NIFTY {rs:.1f}%"]


def score_intraday_trend(symbol: str, live_price: float, kite=None) -> tuple:
    """5-min trend confirmation using Kite or yfinance fallback."""
    # Skip yfinance for index symbols — they don't exist on Yahoo Finance
    _is_index = any(x in symbol.upper() for x in ['NIFTY', 'SENSEX', 'INDIA VIX', 'MIDCAP'])
    try:
        h = None
        if kite or _global_kite:
            from data_provider import get_ohlcv
            h = get_ohlcv(kite or _global_kite, symbol.replace('.NS',''), '5minute', 3)
        if (h is None or len(h) < 6) and not _is_index:
            import yfinance as yf
            h = yf.Ticker(symbol if symbol.endswith('.NS') else f"{symbol}.NS").history(period='1d', interval='5m')
            if h is not None and not h.empty:
                h.columns = [c.lower() for c in h.columns]
        if h is None or len(h) < 6: return 0, []
        c5 = h['close'].values.astype(float) if 'close' in h.columns else h.iloc[:,3].values.astype(float)
        trend = (c5[-1] - c5[-10]) / c5[-10] * 100 if len(c5) >= 10 else (c5[-1] - c5[0]) / c5[0] * 100
        bull  = float(sum(c5[-6:])) / 6 > float(sum(c5[-15:])) / 15 if len(c5) >= 15 else trend > 0
        v5 = h['volume'].values.astype(float) if 'volume' in h.columns else None
        vol_amp = 1.3 if (v5 is not None and len(v5) >= 10 and float(v5[-1]) > float(sum(v5[-11:-1]))/10 * 2) else 1.0
        if trend > 1.0 and bull:    s =  int(3 * vol_amp)
        elif trend > 0 and bull:    s =  2
        elif trend < -1.0 and not bull: s = int(-3 * vol_amp)
        elif trend < 0 and not bull:    s = -2
        else:                           s =  0
        r = [f"5-min {'uptrend' if s>0 else 'downtrend'} {trend:+.1f}%"] if s != 0 else []
        return s, r
    except Exception: return 0, []


def detect_regime(nifty_adx: float = None, nifty_chg: float = None) -> str:
    """TRENDING (ADX≥25) / RANGING (ADX<20) / MIXED / VOLATILE."""
    if nifty_adx is None: return "UNKNOWN"
    if abs(nifty_chg or 0) > 3.0: return "VOLATILE"
    if nifty_adx >= 25: return "TRENDING"
    if nifty_adx < 20:  return "RANGING"
    return "MIXED"


def score_regime_filter(total: int, regime: str, is_buy: bool) -> tuple:
    """Reduce score 20% in RANGING market — trend signals less reliable."""
    if regime == "RANGING":
        adj = int(round(total * 0.80))
        return adj, [f"RANGING mkt: score {total}→{adj}"]
    return total, []


def detect_divergence(c: list) -> tuple:
    """RSI divergence: bearish=-4, bullish=+4."""
    if len(c) < 10: return 0, []
    try:
        rsis = [calc_rsi(c[:i+1]) for i in range(max(0,len(c)-10), len(c))]
        rsis = [r for r in rsis if r is not None]
        if len(rsis) < 5: return 0, []
        pa = list(c[-10:])
        peaks   = [(i,pa[i]) for i in range(1,len(pa)-1) if pa[i]>pa[i-1] and pa[i]>pa[i+1]]
        troughs = [(i,pa[i]) for i in range(1,len(pa)-1) if pa[i]<pa[i-1] and pa[i]<pa[i+1]]
        if len(peaks) >= 2:
            (i1,p1),(i2,p2) = peaks[-2], peaks[-1]
            r1 = rsis[i1] if i1 < len(rsis) else 50
            r2 = rsis[i2] if i2 < len(rsis) else 50
            if p2 > p1 and r2 < r1-3: return -4, ["BEARISH DIVERGENCE: price high, RSI declining"]
        if len(troughs) >= 2:
            (i1,t1),(i2,t2) = troughs[-2], troughs[-1]
            r1 = rsis[i1] if i1 < len(rsis) else 50
            r2 = rsis[i2] if i2 < len(rsis) else 50
            if t2 < t1 and r2 > r1+3: return  4, ["BULLISH DIVERGENCE: price low, RSI rising"]
    except Exception: pass
    return 0, []


def score_weekly_sr(symbol: str, price: float, kite=None) -> tuple:
    """Weekly pivot S/R: stronger walls than daily."""
    _is_index = any(x in symbol.upper() for x in ['NIFTY', 'SENSEX', 'INDIA VIX', 'MIDCAP'])
    try:
        h = None
        if kite or _global_kite:
            from data_provider import get_ohlcv
            h = get_ohlcv(kite or _global_kite, symbol.replace('.NS',''), 'week', 90)
        if (h is None or len(h) < 4) and not _is_index:
            import yfinance as yf
            h = yf.Ticker(symbol if symbol.endswith('.NS') else f"{symbol}.NS").history(period="3mo", interval="1wk")
            if h is not None and not h.empty:
                h.columns = [c.lower() for c in h.columns]
        if h is None or len(h) < 4: return 0, []
        whi = float(h['high'].iloc[-2]); wlo = float(h['low'].iloc[-2]); wc = float(h['close'].iloc[-2])
        wp  = (whi+wlo+wc)/3; wr1 = 2*wp-wlo; ws1 = 2*wp-whi
        tol = price*0.005; s = 0; r = []
        if abs(price-wr1)<tol: s-=3; r.append(f"Weekly R1 {wr1:.1f} — strong resistance")
        elif price>wr1: s+=3; r.append(f"Broke weekly R1 {wr1:.1f}")
        if abs(price-ws1)<tol: s+=3; r.append(f"Weekly S1 support {ws1:.1f}")
        elif price<ws1: s-=3; r.append(f"Below weekly S1 {ws1:.1f}")
        return s, r
    except Exception: return 0, []


def score_gap_fill_risk(gap_pct: float, vol_ratio: float,
                         has_news: bool = False, vix: float = 20) -> tuple:
    """Penalise gap-ups without news: 60-70% fill same day."""
    if abs(gap_pct) < 0.5 or (has_news and vol_ratio > 1.5): return 0, []
    s = 0; r = []
    if abs(gap_pct) > 3.0 and not has_news and vol_ratio < 1.3:
        s = -3; r.append(f"Gap {gap_pct:+.1f}% no news — 65% fill probability")
    elif abs(gap_pct) > 1.5 and not has_news:
        s = -2; r.append(f"Gap {gap_pct:+.1f}% — moderate fill risk")
    if vix > 22 and abs(gap_pct) > 2.0: s -= 1
    return s, r


def score_volume_enhanced(volumes, chg_pct: float, live_vol: float = None) -> tuple:
    """Enhanced volume: extrapolates live intraday vol to projected daily."""
    if len(volumes) < 21: return 0, []
    avg = float(sum(volumes[-21:-1])) / 20
    if avg == 0: return 0, []
    if live_vol and live_vol > 0:
        import datetime as _dt, zoneinfo as _zi
        _ist = _dt.datetime.now(_zi.ZoneInfo("Asia/Kolkata"))
        _open = _ist.replace(hour=9, minute=15, second=0)
        elapsed = max(0.5, (_ist - _open).total_seconds() / 3600)
        ratio = (live_vol * (6.25 / elapsed)) / avg
        src = "projected"
    else:
        # Use yesterday's completed candle ([-2]), not today's partial candle ([-1])
        # Today's partial volume is always < daily average and gives false -2 penalty
        ratio = volumes[-2] / avg if len(volumes) >= 2 else volumes[-1] / avg
        src = "historical"
    if ratio > 2.5: s = 6 if chg_pct>0 else -6; r=[f"Exceptional vol {ratio:.1f}x ({src})"]
    elif ratio>2.0: s = 5 if chg_pct>0 else -5; r=[f"Vol spike {ratio:.1f}x ({src})"]
    elif ratio>1.5: s = 3 if chg_pct>0 else -3; r=[f"High vol {ratio:.1f}x ({src})"]
    elif ratio<0.5: s = -2;                      r=[f"Low vol {ratio:.1f}x — weak conviction"]
    else:           s = 0;                        r=[]
    return s, r


def apply_slippage(price: float, action: str, is_dry_run: bool = True,
                   is_option: bool = False) -> float:
    """Realistic slippage for DRY_RUN: 0.05% equity, 0.3% options."""
    if not is_dry_run: return price
    slip = 0.003 if is_option else 0.0005
    return round(price * (1 + slip) if action == 'BUY' else price * (1 - slip), 2)


_BETA_MAP = {
    'TATASTEEL':1.4,'JSWSTEEL':1.5,'AXISBANK':1.3,'HDFCBANK':0.9,'RELIANCE':0.8,
    'TCS':0.7,'ONGC':1.1,'DELHIVERY':1.6,'UBL':0.8,'ASIANPAINT':0.7,
    'ADANIGREEN':1.8,'BOSCHLTD':0.9,'BAJAJFINSV':1.2,'INFY':0.8,'WIPRO':0.9,
}

def calc_portfolio_heat(positions: dict, live_prices: dict, capital: float) -> tuple:
    """Beta-weighted directional exposure. Returns (heat, overheated, reasons)."""
    long_exp = short_exp = 0.0
    for sym, pos in positions.items():
        cp   = live_prices.get(sym, pos.get('entry', 0))
        qty  = pos.get('remaining_qty', pos.get('qty', 0))
        beta = _BETA_MAP.get(sym.replace('.NS',''), 1.0)
        exp  = cp * qty * beta
        if pos.get('action') == 'BUY': long_exp  += exp
        else:                           short_exp += exp
    net = (long_exp - short_exp) / max(capital, 1)
    over = net > 2.5 or (-net) > 2.5
    r    = [f"Portfolio {'over-long' if net>2.5 else 'over-short'} {abs(net):.1f}x — avoid more same-dir"] if over else []
    return round(net, 2), over, r


def check_peak_drawdown(session_pnl: float, peak_pnl: float,
                         limit: float = 0.30) -> bool:
    """True if P&L fell >limit% from session peak."""
    if peak_pnl <= 0: return False
    return (peak_pnl - session_pnl) / peak_pnl > limit


_fii_cache: dict = {}
def fetch_fii_flow() -> dict:
    """NSE FII/DII net flow. Cached 1hr."""
    import time as _t
    if _fii_cache.get('ts') and _t.time() - _fii_cache['ts'] < 3600:
        return _fii_cache['data']
    try:
        import requests as _r
        hdrs = {'User-Agent':'Mozilla/5.0','Accept':'application/json','Referer':'https://www.nseindia.com/'}
        resp = _r.get('https://www.nseindia.com/api/fiidiiTradeReact', headers=hdrs, timeout=8)
        if resp.status_code == 200:
            raw = resp.json()
            if raw and isinstance(raw, list):
                l = raw[0]
                net = float(l.get('disposalValue', 0))
                d   = {'fii_net': net, 'dii_net': 0,
                       'date': l.get('date',''),
                       'fii_bias': 'BULLISH' if net>1500 else ('BEARISH' if net<-1500 else 'NEUTRAL')}
                _fii_cache.update({'ts': _t.time(), 'data': d})
                return d
    except Exception: pass
    return {'fii_net':0,'dii_net':0,'date':'','fii_bias':'NEUTRAL'}


_sector_etf_cache: dict = {}
def score_sector_momentum(sector: str, market_move_pct: float,
                           kite=None) -> tuple:
    """Sector vs NIFTY today. Leading=+3, Lagging=-3."""
    SECTOR_INDICES = {
        'Banking':'NIFTY BANK','IT':'NIFTY IT','Pharma':'NIFTY PHARMA',
        'Auto':'NIFTY AUTO','Metals':'NIFTY METAL','FMCG':'NIFTY FMCG',
        'Energy':'NIFTY ENERGY','Realty':'NIFTY REALTY','Finance':'NIFTY FIN SERVICE',
    }
    idx = SECTOR_INDICES.get(sector)
    if not idx: return 0, []
    try:
        chg = _sector_etf_cache.get(idx)
        if chg is None:
            _k = kite or _global_kite
            if _k:
                from data_provider import get_ohlcv
                h = get_ohlcv(_k, idx, 'day', 5)
                if h is not None and not h.empty and len(h) >= 2:
                    h.columns = [c.lower() for c in h.columns]
                    chg = (float(h['close'].iloc[-1]) - float(h['close'].iloc[-2])) / float(h['close'].iloc[-2]) * 100
                    _sector_etf_cache[idx] = chg
                else:
                    log.debug(f"[SECTOR] {idx}: no OHLCV data")
            if chg is None: return 0, []
        rs = chg - market_move_pct
        if rs >  1.5: return  3, [f"{sector} outperforming NIFTY {rs:+.1f}%"]
        elif rs >  0.5: return  1, []
        elif rs < -1.5: return -3, [f"{sector} lagging NIFTY {rs:.1f}%"]
        elif rs < -0.5: return -1, []
    except Exception: pass
    return 0, []


_earn_cache: dict = {}
def check_earnings_proximity(symbol: str, days_ahead: int = 2) -> tuple:
    """True if earnings within days_ahead. Uses NSE calendar."""
    clean = symbol.replace('.NS','').replace('.BO','')
    if clean in _earn_cache: return _earn_cache[clean]
    result = (False, 99, '')
    try:
        from data_provider import get_earnings_calendar
        d = get_earnings_calendar(clean, days_ahead=10)
        days = d.get('days', 99)
        near = days <= days_ahead and d.get('near_earnings', False)
        msg  = d.get('event','')
        if near:
            result = (True, days, f"EARNINGS in {days}d ({d.get('date','')}) — BLOCKED")
        elif days <= 5:
            result = (False, days, f"Earnings in {days}d — caution")
    except Exception:
        try:
            import yfinance as yf, datetime
            cal = yf.Ticker(f"{clean}.NS").calendar
            if cal and isinstance(cal, dict):
                for k in ('Earnings Date',):
                    val = cal.get(k)
                    if val:
                        dates = val if isinstance(val,(list,tuple)) else [val]
                        for d in dates:
                            ed = d.date() if hasattr(d,'date') else d
                            days = (ed - datetime.date.today()).days
                            if 0 <= days <= days_ahead:
                                result = (True, days, f"EARNINGS {days}d ({ed}) — BLOCKED")
                                break
        except Exception: pass
    _earn_cache[clean] = result
    return result


def classify_event_risk(symbol: str = "", headline: str = "", days_ahead: int = 2) -> dict:
    """
    Shared graded event-risk classifier for both India and F&O bots.
    Returns:
      risk_level, risk_reason, position_size_multiplier, score_bump,
      target_multiplier, stop_multiplier, max_hold_multiplier, entry_blocked
    """
    risk = {
        "risk_level": "normal",
        "risk_reason": "",
        "position_size_multiplier": 1.0,
        "score_bump": 0,
        "target_multiplier": 1.0,
        "stop_multiplier": 1.0,
        "max_hold_multiplier": 1.0,
        "entry_blocked": False,
    }
    text = " ".join(part for part in [symbol or "", headline or ""] if part).lower()
    extreme_keywords = (
        "nse halt", "bse halt", "market circuit", "trading halt",
        "nuclear", "martial law", "terror attack india",
    )
    caution_keywords = (
        "india election", "lok sabha", "union budget", "budget",
        "rbi rate", "rbi policy", "repo rate", "india war",
        "india pakistan", "india attack", "war", "attack",
        "results day", "earnings", "board meeting",
    )
    if any(word in text for word in extreme_keywords):
        risk.update({
            "risk_level": "block",
            "risk_reason": headline or symbol or "extreme_event",
            "position_size_multiplier": 0.0,
            "score_bump": 99,
            "target_multiplier": 0.7,
            "stop_multiplier": 0.7,
            "max_hold_multiplier": 0.5,
            "entry_blocked": True,
        })
        return risk

    brief_path = Path("briefings") / "daily_brief.json"
    if brief_path.exists():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
            if brief.get("high_risk_today"):
                risk.update({
                    "risk_level": "caution",
                    "risk_reason": "daily_brief_high_risk",
                    "position_size_multiplier": 0.8,
                    "score_bump": max(risk["score_bump"], 1),
                    "target_multiplier": 0.92,
                    "stop_multiplier": 0.88,
                    "max_hold_multiplier": 0.85,
                })
            clean = (symbol or "").replace(".NS", "").replace(".BO", "").upper()
            for item in brief.get("calendar_events", []):
                name = str(item.get("name", "")).upper()
                impact = str(item.get("impact", "")).upper()
                if clean and clean in name and str(item.get("type", "")).upper() in {"EARNINGS", "RESULTS"}:
                    risk.update({
                        "risk_level": "caution",
                        "risk_reason": item.get("name", clean),
                        "position_size_multiplier": 0.7 if impact == "HIGH" else 0.8,
                        "score_bump": max(risk["score_bump"], 2 if impact == "HIGH" else 1),
                        "target_multiplier": 0.9,
                        "stop_multiplier": 0.85,
                        "max_hold_multiplier": 0.75,
                    })
                    break
        except Exception:
            pass

    near_earn, days, msg = check_earnings_proximity(symbol, days_ahead=days_ahead) if symbol else (False, 99, "")
    if near_earn:
        risk.update({
            "risk_level": "caution",
            "risk_reason": msg or "near_earnings",
            "position_size_multiplier": min(risk["position_size_multiplier"], 0.7),
            "score_bump": max(risk["score_bump"], 2),
            "target_multiplier": min(risk["target_multiplier"], 0.9),
            "stop_multiplier": min(risk["stop_multiplier"], 0.85),
            "max_hold_multiplier": min(risk["max_hold_multiplier"], 0.75),
        })
    elif any(word in text for word in caution_keywords):
        risk.update({
            "risk_level": "caution",
            "risk_reason": headline or symbol or "macro_event",
            "position_size_multiplier": min(risk["position_size_multiplier"], 0.8),
            "score_bump": max(risk["score_bump"], 2),
            "target_multiplier": min(risk["target_multiplier"], 0.92),
            "stop_multiplier": min(risk["stop_multiplier"], 0.88),
            "max_hold_multiplier": min(risk["max_hold_multiplier"], 0.85),
        })
    return risk


# ══════════════════════════════
# MAIN ENGINE CLASS
# ══════════════════════════════
class TradingEngine:
    def __init__(self, capital=10000, max_risk_pct=0.02, kite=None):
        self.capital   = capital
        self.max_risk  = max_risk_pct
        self._kite     = kite          # Kite Connect instance — set after login
        self.market_regime   = 'UNKNOWN'
        self._current_sector = None
        self.recent_signals  = {}  # sym → timestamp — prevents duplicates

    def _is_duplicate(self, sym, direction, fast_mode=False):
        """Prevent same signal firing repeatedly. fast_mode=1hr, standard=4hr."""
        key = f"{sym}_{direction}"
        if key in self.recent_signals:
            last_time = self.recent_signals[key]
            window = 3600 if fast_mode else 4 * 3600
            if (datetime.datetime.now() - last_time).seconds < window:
                return True
        return False

    def _record_signal(self, sym, direction):
        self.recent_signals[f"{sym}_{direction}"] = datetime.datetime.now()

    def _fetch_ohlcv(self, symbol, interval='day', days=80):
        """
        Fetch OHLCV via data_provider (Kite-native, zero yfinance).
        Primary: kite.historical_data() — official NSE exchange data.
        Falls back to NSE free API for daily data.
        Returns DataFrame with open/high/low/close/volume.
        """
        external_fetcher = getattr(self, "_ohlcv_fetcher", None)
        if callable(external_fetcher):
            try:
                external = external_fetcher(symbol, interval=interval, days=days)
                if external is not None and not external.empty:
                    external.columns = [c.lower() for c in external.columns]
                    return external.reset_index(drop=True)
            except Exception:
                pass
        if self._kite is None:
            # No kite connection — this shouldn't happen in live
            # but allow for backtesting mode via yfinance fallback
            import logging as _lg
            _yfl = _lg.getLogger('yfinance')
            _yfl.setLevel(_lg.CRITICAL)
            try:
                import yfinance as yf
                sym = symbol if symbol.endswith('.NS') else f"{symbol}.NS"
                h   = yf.Ticker(sym).history(period=f"{days+10}d", interval="1d")
                if h.empty:
                    h = yf.Ticker(symbol).history(period=f"{days+10}d", interval="1d")
                if not h.empty:
                    h.columns = [c.lower() for c in h.columns]
                    return h.tail(days).reset_index(drop=True)
            except Exception:
                pass
            return __import__('pandas').DataFrame()

        # Live mode: use Kite via data_provider
        from data_provider import get_smart_ohlcv
        # Map yfinance/legacy index symbols → Kite NSE names
        INDEX_NAME_MAP = {
            '^NSEI':    'NIFTY 50',
            '^NSEBANK': 'NIFTY BANK',
            '^NSMIDCP': 'NIFTY MIDCAP 50',
            '^INDIAVIX': 'INDIA VIX',
            '^BSESN':   'SENSEX',
        }
        clean = INDEX_NAME_MAP.get(symbol, symbol.replace('.NS', '').replace('.BO', '').upper())
        df    = get_smart_ohlcv(self._kite, clean, interval=interval, days=days)
        if not df.empty:
            df.columns = [c.lower() for c in df.columns]
        return df


    def analyze(self, symbol, verbose=False, live_price=None, market_move_pct=0.0, skip_adx=False,
                record_signal=True):
        """Full 10-layer analysis. Returns Signal or None.
        market_move_pct: today's NIFTY % change (signed).
        big_day (abs>2.5%): news-day mode — reduce lagging indicator weights.
        very big day (abs>3.0%) + stock gap >3%: fast-mode gap trade.
        """
        big_day      = abs(market_move_pct) > 2.5
        big_day_bull = big_day and market_move_pct > 0
        symbol = fix_symbol(symbol)
        if symbol in SKIP_SYMBOLS:
            log.debug(f"  [SKIP] {symbol}: in SKIP_SYMBOLS (banned this session)")
            return None

        try:
            h = self._fetch_ohlcv(symbol)
            if h.empty or len(h) < 30:
                _is_index = any(x in symbol.upper() for x in ['NIFTY', 'SENSEX', 'INDIA VIX'])
                if not _is_index:  # never ban index symbols — token issues are transient
                    SKIP_SYMBOLS.add(symbol)
                log.warning(f"  [NO DATA] {symbol}: insufficient data (empty={h.empty}, rows={len(h)}) — will retry next scan")
                return None

            c = h['close'].values.astype(float)
            o = h['open'].values.astype(float)
            hi = h['high'].values.astype(float)
            lo = h['low'].values.astype(float)
            v = h['volume'].values.astype(float)
            price = float(c[-1])
            chg   = ((price - c[-2]) / c[-2]) * 100
            atr   = calc_atr(hi, lo, c)          # computed once — used for fast-mode & SL/targets

            # ── Fast-mode gap trade ──────────────────────────────────────
            # On NIFTY >3% days, daily indicators (MACD/MA) measure yesterday.
            # When a stock also gaps >3% with 1.5x+ volume, bypass the 10-layer
            # engine and trade the gap momentum directly. Tighter SL (0.5×ATR),
            # wider T1 (1.5×ATR) to maintain 3:1 R:R on a confirmed gap.
            if big_day and live_price is not None and atr and atr > 0:
                prev_close = float(c[-2])
                gap_pct    = (live_price - prev_close) / prev_close * 100
                vol_avg    = float(sum(v[-21:-1])) / 20 if len(v) >= 21 else float(v[-1])
                vol_ratio  = float(v[-1]) / max(vol_avg, 1)
                vwap_q     = calc_vwap(hi, lo, c, v)
                above_vwap = (vwap_q is None) or (live_price > vwap_q)
                below_vwap = (vwap_q is not None) and (live_price < vwap_q)

                if (market_move_pct > 3.0 and gap_pct > 3.0 and vol_ratio > 1.5
                        and above_vwap
                        and not self._is_duplicate(symbol, 'BUY', fast_mode=True)):
                    fast_sl = round(live_price - atr * 0.5, 2)
                    fast_t1 = round(live_price + atr * 1.5, 2)
                    fast_t2 = round(live_price + atr * 2.5, 2)
                    rps     = max(live_price - fast_sl, 0.01)
                    qty     = max(1, min(int(self.capital * self.max_risk / rps),
                                        int(self.capital * 0.35 / live_price)))
                    score   = int(round(gap_pct * 2 + vol_ratio * 1.5 + 4))
                    conf    = min(100.0, round(score / 20 * 100, 1))
                    if record_signal:
                        self._record_signal(symbol, 'BUY')
                    log.info(f"  [FAST-MODE BUY] {symbol}: gap+{gap_pct:.1f}% vol{vol_ratio:.1f}x NIFTY{market_move_pct:+.1f}%")
                    return Signal(symbol=symbol, price=round(live_price,2), signal='BUY',
                                  confidence=conf, entry=round(live_price,2),
                                  stop_loss=fast_sl, target1=fast_t1, target2=fast_t2,
                                  risk_reward=round((fast_t1-live_price)/rps,2), quantity=qty,
                                  risk_amount=round(rps*qty,2), total_score=score,
                                  layer_scores={'Gap':int(round(gap_pct*2)),'Volume':int(round(vol_ratio*1.5)),
                                    'VWAP':2,'RSI':0,'MACD':0,'MA':0,'ADX':0,'Pattern':0,'S/R':0,'MTF':0,'Sentiment':0},
                                  reasons=[f'FAST-MODE: gap+{gap_pct:.1f}% vol{vol_ratio:.1f}x NIFTY{market_move_pct:+.1f}%'],
                                  patterns=[], supports=[], resistances=[], tf_signals=[],
                                  timestamp=datetime.datetime.now().isoformat())

                if (market_move_pct < -3.0 and gap_pct < -3.0 and vol_ratio > 1.5
                        and below_vwap
                        and not self._is_duplicate(symbol, 'SELL', fast_mode=True)):
                    fast_sl = round(live_price + atr * 0.5, 2)
                    fast_t1 = round(live_price - atr * 1.5, 2)
                    fast_t2 = round(live_price - atr * 2.5, 2)
                    rps     = max(fast_sl - live_price, 0.01)
                    qty     = max(1, min(int(self.capital * self.max_risk / rps),
                                        int(self.capital * 0.35 / live_price)))
                    score   = -int(round(abs(gap_pct)*2 + vol_ratio*1.5 + 4))
                    conf    = min(100.0, round(abs(score)/20*100, 1))
                    if record_signal:
                        self._record_signal(symbol, 'SELL')
                    log.info(f"  [FAST-MODE SELL] {symbol}: gap{gap_pct:.1f}% vol{vol_ratio:.1f}x NIFTY{market_move_pct:+.1f}%")
                    return Signal(symbol=symbol, price=round(live_price,2), signal='SELL',
                                  confidence=conf, entry=round(live_price,2),
                                  stop_loss=fast_sl, target1=fast_t1, target2=fast_t2,
                                  risk_reward=round((live_price-fast_t1)/rps,2), quantity=qty,
                                  risk_amount=round(rps*qty,2), total_score=score,
                                  layer_scores={'Gap':-int(round(abs(gap_pct)*2)),'Volume':-int(round(vol_ratio*1.5)),
                                    'VWAP':-2,'RSI':0,'MACD':0,'MA':0,'ADX':0,'Pattern':0,'S/R':0,'MTF':0,'Sentiment':0},
                                  reasons=[f'FAST-MODE: gap{gap_pct:.1f}% vol{vol_ratio:.1f}x NIFTY{market_move_pct:+.1f}%'],
                                  patterns=[], supports=[], resistances=[], tf_signals=[],
                                  timestamp=datetime.datetime.now().isoformat())

            # ── Standard 10-layer analysis ───────────────────────────────
            rsi = calc_rsi(c)
            rsi_s, rsi_r = score_rsi(rsi)

            mv, sv, hv = calc_macd(c)
            macd_s, macd_r = score_macd(mv, sv, hv)

            ma_s, ma_r = score_ma(price, c)

            adx, pdi, mdi = calc_adx(hi, lo, c)
            adx_s, adx_r = score_adx(adx, pdi, mdi)

            vwap = calc_vwap(hi, lo, c, v)
            vwap_s, vwap_r = score_vwap(price, vwap)

            pat_s, pats = detect_patterns(o, hi, lo, c)

            sups, ress = find_sr(hi, lo, c)
            sr_s, sr_r = score_sr(price, sups, ress)

            vol_s, vol_r = score_volume(v, chg)

            mtf_s, tf_sigs, mtf_r = score_multi_tf(symbol, kite=self._kite)

            sent_s, sent_r = score_sentiment(symbol)

            # ── TOTAL ──
            # ── Gap momentum layer (11th) ─────────────────────────
            # Scores today's intraday gap vs previous close.
            gap_s = 0; gap_r = []
            if live_price is not None and len(c) >= 2:
                prev_close = float(c[-2])
                gap_pct    = (live_price - prev_close) / prev_close * 100
                if   gap_pct >  3.0: gap_s =  3; gap_r.append(f"Gap up +{gap_pct:.1f}% (momentum BUY)")
                elif gap_pct >  1.5: gap_s =  2; gap_r.append(f"Gap up +{gap_pct:.1f}%")
                elif gap_pct >  0.5: gap_s =  1
                elif gap_pct < -3.0: gap_s = -3; gap_r.append(f"Gap down {gap_pct:.1f}% (momentum SELL)")
                elif gap_pct < -1.5: gap_s = -2; gap_r.append(f"Gap down {gap_pct:.1f}%")
                elif gap_pct < -0.5: gap_s = -1

            # ── News-day mode: reduce weight of lagging indicators ───────
            # On big NIFTY moves (>2.5%), MACD and MA reflect last week.
            # Halve their weight; double gap layer so current price action wins.
            if big_day:
                macd_adj = int(round(macd_s * 0.5))
                ma_adj   = int(round(ma_s   * 0.5))
                adx_adj  = int(round(adx_s  * 0.6))
                gap_adj  = gap_s * 2
                gap_r.insert(0, f"NEWS-DAY mode (NIFTY {market_move_pct:+.1f}%): MACD/MA/ADX weights reduced, Gap doubled")
            else:
                macd_adj = macd_s; ma_adj = ma_s; adx_adj = adx_s; gap_adj = gap_s

            # ── NEW SKILLS: integrate additional scoring layers ────────

            # Skill 1: Relative strength vs NIFTY
            rs_s, rs_r = score_relative_strength(chg, market_move_pct)

            # Skill 2: Intraday 5-min trend confirmation
            tf5_s, tf5_r = score_intraday_trend(symbol, live_price or price)

            # Skill 4: RSI divergence detection
            div_s, div_r = detect_divergence(list(c))

            # Skill 5: Weekly S/R context
            wsr_s, wsr_r = score_weekly_sr(symbol, price)

            # Skill 6: Gap fill risk
            _has_news  = bool(sent_r)  # if sentiment layer fired, there's news context
            _vix_level = 20.0          # default; real VIX passed in via market_move_pct context
            gfr_s, gfr_r = (0, []) if not big_day else score_gap_fill_risk(
                gap_pct if 'gap_pct' in dir() else 0, vol_ratio if 'vol_ratio' in dir() else 1.0,
                _has_news, _vix_level)

            # ── New quant skills ───────────────────────────────────────
            # Skill 1: Relative strength vs NIFTY
            rs_s,  rs_r  = score_relative_strength(chg, market_move_pct)

            # Skill 2: 5-min intraday trend
            tf5_s, tf5_r = score_intraday_trend(symbol, live_price or price, self._kite)

            # Skill 4: Divergence
            div_s, div_r = detect_divergence(list(c))

            # Skill 5: Weekly S/R
            wsr_s, wsr_r = score_weekly_sr(symbol, price, self._kite)

            # Skill 6: Gap fill risk
            _has_news = bool(sent_r)
            # Gap fill risk only on actual gap opens (>3% gap from prev close)
            # A normal -1.6% intraday move is NOT a gap — don't penalise
            gfr_s, gfr_r = 0, []
            if live_price and len(c) >= 2:
                _gpct = (live_price - float(c[-2])) / float(c[-2]) * 100
                if abs(_gpct) > 3.0:   # only apply on genuine gap opens
                    _vol_avg = float(sum(v[-21:-1])) / 20 if len(v) >= 21 else float(v[-1])
                    _vratio  = float(v[-1]) / max(_vol_avg, 1)
                    gfr_s, gfr_r = score_gap_fill_risk(_gpct, _vratio, _has_news, 20.0)

            # Skill 7: Enhanced volume — pass live_vol if available from quote
            # live_vol comes from kite.quote() which updates in real-time
            _live_vol = getattr(self, '_last_live_vol', {}).get(symbol, None)
            vol_s, vol_r = score_volume_enhanced(v, chg, live_vol=_live_vol)

            # Skill 12: Sector momentum
            _sector = getattr(self, '_current_sector', None)
            sec_s, sec_r = score_sector_momentum(_sector, market_move_pct, self._kite) if _sector else (0, [])

            total = (rsi_s + macd_adj + ma_adj + adx_adj + vwap_s + pat_s + sr_s
                     + vol_s + mtf_s + sent_s + gap_adj
                     + rs_s + tf5_s + div_s + wsr_s + gfr_s + sec_s)

            scores = {
                'RSI':rsi_s, 'MACD':macd_adj, 'MA':ma_adj, 'ADX':adx_adj,
                'VWAP':vwap_s, 'Pattern':pat_s, 'S/R':sr_s, 'Volume':vol_s,
                'MTF':mtf_s, 'Sentiment':sent_s, 'Gap':gap_adj,
                'RelStr':rs_s, '5min':tf5_s, 'Diverge':div_s,
                'WeeklyS/R':wsr_s, 'GapFill':gfr_s, 'Sector':sec_s,
            }
            reasons = (rsi_r + macd_r + ma_r + adx_r + vwap_r + sr_r + vol_r
                       + mtf_r + sent_r + gap_r
                       + rs_r + tf5_r + div_r + wsr_r + gfr_r + sec_r)
            if pats: reasons.extend([f"Pattern: {p}" for p in pats])

            # Regime adjustment (Skill 3)
            _regime    = getattr(self, 'market_regime', 'UNKNOWN')
            total, reg_r = score_regime_filter(total, _regime, total > 0)
            reasons   += reg_r

            # ── FILTERS ──
            # ADX filter: no trade in trendless market.
            # skip_adx=True bypasses this for F&O/options context: after a crash+recovery
            # whipsaw, ADX collapses to 1-4 on daily charts even when the market is moving
            # strongly. Options can be traded based on direction even in low-ADX regimes.
            if not skip_adx and adx is not None and adx < 20 and not big_day:
                if verbose: log.info(f"  {symbol}: ADX {adx:.0f} < 20 — no trend, skip")
                return None

            # Minimum score threshold — log actual scores so we can see why stocks are skipped
            if abs(total) < 8:
                log.info(f"  {symbol}: score={total} (RSI={rsi_s} MACD={macd_adj} MA={ma_adj} ADX={adx_adj} VWAP={vwap_s} Vol={vol_s} MTF={mtf_s}) — below floor 8")
                return None

            # Duplicate check
            direction = "BUY" if total > 0 else "SELL"
            if self._is_duplicate(symbol, direction):
                if verbose: log.info(f"  {symbol}: Duplicate {direction} within 4h — skip")
                return None

            is_buy = total > 0
            sig_type = ("STRONG BUY" if total >= 15 else "BUY") if is_buy else ("STRONG SELL" if total <= -15 else "SELL")

            # Confidence = % of realistic max score (active layers only, not theoretical 50)
            # Count layers that actually contributed a non-zero score
            active_layers = sum(1 for v in scores.values() if v != 0)
            # Each active layer can contribute max ±2 to ±7 (MACD up to -7)
            # Practical max for Indian stocks with typical data: ~22-25
            # Use active layers × avg max per layer (3.5) as denominator
            realistic_max = max(15, active_layers * 3.5)
            confidence = min(100, round(abs(total) / realistic_max * 100, 1))

            # ── Levels ──
            # atr already computed above (before layers)
            # Late-day tighter targets: if < 90 min to close, use 0.5x ATR for T1
            # so positions have a realistic chance of hitting target before 15:10 squareoff
            import datetime as _dt, zoneinfo as _zi
            _ist_now = _dt.datetime.now(_zi.ZoneInfo('Asia/Kolkata')).time()
            _late_day = _ist_now >= _dt.time(13, 30)
            _t1_mult = 0.5 if _late_day else 1.0
            _t2_mult = 1.0 if _late_day else 1.8
            sl = (price - atr*0.8) if is_buy else (price + atr*0.8)
            t1 = (price + atr*_t1_mult) if is_buy else (price - atr*_t1_mult)
            t2 = (price + atr*_t2_mult) if is_buy else (price - atr*_t2_mult)
            rps = abs(price - sl)
            if rps <= 0: return None
            qty = max(1, int(self.capital * self.max_risk / rps))
            if qty * price > self.capital * 0.35:
                qty = max(1, int(self.capital * 0.35 / price))
            rr = abs(t1 - price) / rps if rps > 0 else 0

            if record_signal:
                self._record_signal(symbol, direction)

            sig = Signal(
                symbol=symbol, price=round(price,2), signal=sig_type,
                confidence=round(confidence,1), entry=round(price,2),
                stop_loss=round(sl,2), target1=round(t1,2), target2=round(t2,2),
                risk_reward=round(rr,2), quantity=qty,
                risk_amount=round(rps*qty,2), total_score=total,
                layer_scores=scores, reasons=reasons, patterns=pats,
                supports=[round(x,2) for x in sups],
                resistances=[round(x,2) for x in ress],
                tf_signals=tf_sigs, timestamp=datetime.datetime.now().isoformat()
            )

            if verbose:
                log.info(f"\n  {sig_type} {symbol} | Score:{total} Conf:{confidence:.0f}%")
                log.info(f"  Price:{price:.2f} Entry:{price:.2f} SL:{sl:.2f} T1:{t1:.2f} T2:{t2:.2f}")
                log.info(f"  Scores: {scores}")
                if pats: log.info(f"  Patterns: {pats}")
                log.info(f"  S:{sups} R:{ress}")

            return sig

        except Exception as e:
            if "No data found" in str(e) or "delisted" in str(e):
                SKIP_SYMBOLS.add(symbol)
                log.warning(f"  {symbol}: Delisted/unavailable — skipped for session")
            else:
                # Changed from debug→warning so silent engine crashes are visible in logs
                import traceback as _tb
                log.warning(f"  [ENGINE ERROR] {symbol}: {e} | {_tb.format_exc().splitlines()[-1]}")
            return None

    # ══════════════════════════════
    # BACKTESTER
    # ══════════════════════════════
    def backtest(self, symbol, days=365, verbose=True):
        """Backtest strategy on historical data."""
        symbol = fix_symbol(symbol)
        if verbose: log.info(f"\n{'='*55}\nBACKTEST: {symbol} | {days} days\n{'='*55}")

        # Use Kite data if available, else yfinance for backtesting
        if _global_kite:
            hist = self._fetch_ohlcv(symbol, interval='day', days=days+30)
        else:
            hist = yf.Ticker(symbol).history(period=f"{days+90}d", interval="1d")
        hist = _normalize_ohlcv(hist, log.warning)
        if hist is None or len(hist) < 90:
            log.error("Insufficient data"); return None

        c  = hist['close'].values.astype(float)
        o  = hist['open'].values.astype(float)
        hi = hist['high'].values.astype(float)
        lo = hist['low'].values.astype(float)
        v  = hist['volume'].values.astype(float)
        dates = hist.index

        trades = []; equity = [self.capital]; cap = self.capital
        in_trade = False; t_entry = t_sl = t_tgt = 0; t_qty = 0; t_dir = 0; t_idx = 0
        warmup = 60

        for i in range(warmup, len(c)):
            price = c[i]

            # ── CHECK EXIT ──
            if in_trade:
                if t_dir == 1:
                    if price <= t_sl:
                        pnl = (t_sl - t_entry) * t_qty
                        trades.append(BacktestTrade(str(dates[i].date()),'LONG',round(t_entry,2),round(t_sl,2),round(pnl,2),round(pnl/t_entry/t_qty*100,2),i-t_idx,'STOPLOSS'))
                        cap += pnl; in_trade = False
                    elif price >= t_tgt:
                        pnl = (t_tgt - t_entry) * t_qty
                        trades.append(BacktestTrade(str(dates[i].date()),'LONG',round(t_entry,2),round(t_tgt,2),round(pnl,2),round(pnl/t_entry/t_qty*100,2),i-t_idx,'TARGET'))
                        cap += pnl; in_trade = False
                    elif i - t_idx > 10:
                        pnl = (price - t_entry) * t_qty
                        trades.append(BacktestTrade(str(dates[i].date()),'LONG',round(t_entry,2),round(price,2),round(pnl,2),round(pnl/t_entry/t_qty*100,2),10,'TIMESTOP'))
                        cap += pnl; in_trade = False
                elif t_dir == -1:
                    if price >= t_sl:
                        pnl = (t_entry - t_sl) * t_qty
                        trades.append(BacktestTrade(str(dates[i].date()),'SHORT',round(t_entry,2),round(t_sl,2),round(pnl,2),round(pnl/t_entry/t_qty*100,2),i-t_idx,'STOPLOSS'))
                        cap += pnl; in_trade = False
                    elif price <= t_tgt:
                        pnl = (t_entry - t_tgt) * t_qty
                        trades.append(BacktestTrade(str(dates[i].date()),'SHORT',round(t_entry,2),round(t_tgt,2),round(pnl,2),round(pnl/t_entry/t_qty*100,2),i-t_idx,'TARGET'))
                        cap += pnl; in_trade = False
                    elif i - t_idx > 10:
                        pnl = (t_entry - price) * t_qty
                        trades.append(BacktestTrade(str(dates[i].date()),'SHORT',round(t_entry,2),round(price,2),round(pnl,2),round(pnl/t_entry/t_qty*100,2),10,'TIMESTOP'))
                        cap += pnl; in_trade = False

            # ── CHECK ENTRY ──
            if not in_trade and i > warmup + 5:
                sub_c = c[:i+1]; sub_o = o[:i+1]; sub_h = hi[:i+1]; sub_l = lo[:i+1]; sub_v = v[:i+1]
                rsi = calc_rsi(sub_c)
                rs, _ = score_rsi(rsi)
                mv, sv, hv = calc_macd(sub_c)
                ms, _ = score_macd(mv, sv, hv)
                mas, _ = score_ma(price, sub_c)
                adx_v, pdi, mdi = calc_adx(sub_h, sub_l, sub_c)
                adxs, _ = score_adx(adx_v, pdi, mdi)
                ps, _ = detect_patterns(sub_o, sub_h, sub_l, sub_c)
                vs, _ = score_volume(sub_v, ((price-c[i-1])/c[i-1])*100 if c[i-1] != 0 else 0)

                total = rs + ms + mas + adxs + ps + vs

                # ADX filter
                if adx_v is not None and adx_v < 20: total = 0

                if abs(total) >= 8:
                    is_buy = total > 0
                    atr = calc_atr(sub_h, sub_l, sub_c)
                    t_entry = price
                    t_sl = (price - atr*1.5) if is_buy else (price + atr*1.5)
                    t_tgt = (price + atr*2.0) if is_buy else (price - atr*2.0)
                    rps = abs(t_entry - t_sl)
                    if rps > 0:
                        t_qty = max(1, int(cap * self.max_risk / rps))
                        if t_qty * price > cap * 0.35:
                            t_qty = max(1, int(cap * 0.35 / price))
                        t_dir = 1 if is_buy else -1
                        t_idx = i
                        in_trade = True

            equity.append(cap)

        # ── RESULTS ──
        if not trades:
            if verbose: log.info("No trades generated in backtest period.")
            return BacktestResult(symbol=symbol, days=days, total_trades=0, wins=0, losses=0,
                win_rate=0, total_pnl=0, total_pnl_pct=0, avg_win=0, avg_loss=0,
                profit_factor=0, max_drawdown_pct=0, sharpe_ratio=0, best_trade=0, worst_trade=0)

        wins = [t for t in trades if t.pnl > 0]
        losses_list = [t for t in trades if t.pnl <= 0]
        pnls = [t.pnl for t in trades]
        total_pnl = sum(pnls)
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses_list)) if losses_list else 0.01

        # Max drawdown
        peak = equity[0]; max_dd = 0
        for e in equity:
            if e > peak: peak = e
            dd = (peak - e) / peak * 100
            if dd > max_dd: max_dd = dd

        # Sharpe ratio (annualized, assuming 252 trading days)
        if len(pnls) > 1:
            returns = np.array(pnls) / self.capital
            sharpe = np.mean(returns) / (np.std(returns) + 1e-10) * np.sqrt(252 / max(1, days/len(pnls)))
        else: sharpe = 0

        result = BacktestResult(
            symbol=symbol, days=days, total_trades=len(trades),
            wins=len(wins), losses=len(losses_list),
            win_rate=round(len(wins)/len(trades)*100, 1),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl/self.capital*100, 2),
            avg_win=round(np.mean([t.pnl for t in wins]),2) if wins else 0,
            avg_loss=round(np.mean([t.pnl for t in losses_list]),2) if losses_list else 0,
            profit_factor=round(gross_profit/gross_loss, 2),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            best_trade=round(max(pnls), 2),
            worst_trade=round(min(pnls), 2),
            trades=trades
        )

        if verbose:
            log.info(f"\n{'─'*55}")
            log.info(f"BACKTEST RESULTS: {symbol}")
            log.info(f"{'─'*55}")
            log.info(f"  Period:        {days} days")
            log.info(f"  Total Trades:  {result.total_trades}")
            log.info(f"  Win Rate:      {result.win_rate}%")
            log.info(f"  Total P&L:     ${result.total_pnl:,.2f} ({result.total_pnl_pct:+.2f}%)")
            log.info(f"  Avg Win:       ${result.avg_win:,.2f}")
            log.info(f"  Avg Loss:      ${result.avg_loss:,.2f}")
            log.info(f"  Profit Factor: {result.profit_factor}")
            log.info(f"  Max Drawdown:  {result.max_drawdown_pct}%")
            log.info(f"  Sharpe Ratio:  {result.sharpe_ratio}")
            log.info(f"  Best Trade:    ${result.best_trade:,.2f}")
            log.info(f"  Worst Trade:   ${result.worst_trade:,.2f}")
            log.info(f"{'─'*55}")
            if result.win_rate >= 55 and result.profit_factor >= 1.5:
                log.info(f"  ✅ STRATEGY VIABLE — Win rate and profit factor look good")
            elif result.win_rate >= 45:
                log.info(f"  ⚠️ MARGINAL — Needs tuning or more data")
            else:
                log.info(f"  ❌ STRATEGY WEAK — Do not trade live with these parameters")

        return result

    def scan_watchlist(self, watchlist, verbose=False):
        """Scan a list of symbols, return signals sorted by strength."""
        signals = []
        for sym in watchlist:
            sym = fix_symbol(sym)
            if sym in SKIP_SYMBOLS: continue
            sig = self.analyze(sym, verbose=verbose)
            if sig: signals.append(sig)
            time.sleep(0.4)
        signals.sort(key=lambda x: abs(x.total_score), reverse=True)
        return signals

    def batch_backtest(self, symbols, days=365):
        """Backtest multiple symbols and show summary."""
        results = []
        for sym in symbols:
            sym = fix_symbol(sym)
            log.info(f"\nBacktesting {sym}...")
            r = self.backtest(sym, days=days, verbose=False)
            if r and r.total_trades > 0:
                results.append(r)
                log.info(f"  {sym}: {r.total_trades} trades | WR:{r.win_rate}% | PnL:{r.total_pnl_pct:+.1f}% | PF:{r.profit_factor} | Sharpe:{r.sharpe_ratio}")

        if results:
            avg_wr = np.mean([r.win_rate for r in results])
            avg_pf = np.mean([r.profit_factor for r in results])
            total_pnl = sum(r.total_pnl for r in results)
            log.info(f"\n{'='*55}\nBATCH SUMMARY ({len(results)} stocks)")
            log.info(f"  Avg Win Rate:     {avg_wr:.1f}%")
            log.info(f"  Avg Profit Factor:{avg_pf:.2f}")
            log.info(f"  Total P&L:        ${total_pnl:,.2f}")
            log.info(f"{'='*55}")
        return results


# ══════════════════════════════
# STANDALONE USAGE
# ══════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    engine = TradingEngine(capital=10000)

    print("\n" + "="*60)
    print("  MARKETPULSE PRO v4 — TRADING ENGINE")
    print("="*60)
    print("\nOptions:")
    print("  1. Analyze a single stock")
    print("  2. Backtest a stock (1 year)")
    print("  3. Scan Indian watchlist")
    print("  4. Scan US watchlist")
    print("  5. Batch backtest Indian stocks")
    print("  6. Batch backtest US stocks")

    choice = input("\nChoice (1-6): ").strip()

    INDIA = ['RELIANCE.NS','TCS.NS','INFY.NS','HDFCBANK.NS','ICICIBANK.NS',
             'SBIN.NS','BHARTIARTL.NS','ITC.NS','LT.NS','BAJFINANCE.NS',
             'TATAMOTORS.NS','ADANIENT.NS','TATASTEEL.NS','WIPRO.NS','MARUTI.NS']

    US = ['AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','AMD',
          'NFLX','COIN','PLTR','SOFI','CRWD','UBER','SHOP']

    if choice == '1':
        sym = input("Symbol (e.g. AAPL, RELIANCE.NS): ").strip()
        engine.analyze(sym, verbose=True)
    elif choice == '2':
        sym = input("Symbol: ").strip()
        engine.backtest(sym, days=365)
    elif choice == '3':
        sigs = engine.scan_watchlist(INDIA, verbose=True)
        print(f"\n{len(sigs)} signals found")
    elif choice == '4':
        sigs = engine.scan_watchlist(US, verbose=True)
        print(f"\n{len(sigs)} signals found")
    elif choice == '5':
        engine.batch_backtest(INDIA)
    elif choice == '6':
        engine.batch_backtest(US)
