"""
╔══════════════════════════════════════════════════════════════════╗
║  MarketPulse — F&O BOT v1                                        ║
║  NIFTY + BankNIFTY Weekly Options                                ║
║  WebSocket prices · Greeks · Adaptive capital · Self-learning    ║
╠══════════════════════════════════════════════════════════════════╣
║  KEY DIFFERENCES FROM EQUITY BOT                                 ║
║  · Prices via WebSocket tick stream (not REST polling)           ║
║  · Only ATM index options — highest liquidity                    ║
║  · Black-Scholes Greeks: delta, theta, vega, IV                  ║
║  · SL = -25% premium, T1 = +50%, T2 = +100%                     ║
║  · Adaptive position sizing — grows on wins, shrinks on losses   ║
║  · 3 consecutive losses → STOP trading for the day              ║
║  · Learns IV sweet spots, best hours, which signals work         ║
║  · News pause: high-impact event → no new entries for 2 min      ║
╠══════════════════════════════════════════════════════════════════╣
║  SETUP                                                           ║
║  pip install kiteconnect yfinance pandas numpy requests scipy    ║
║  Run: python bot_fno_v1.py                                       ║
║  Files needed: trading_engine.py, notifier.py (same folder)     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import sys, os, csv, time, datetime, json, math, logging, traceback, threading
import zoneinfo, requests
from collections import Counter
from pathlib import Path
from scipy.stats import norm         # Black-Scholes
from kiteconnect import KiteConnect, KiteTicker
from core.config_loader import (
    FINNHUB_KEY as CFG_FINNHUB_KEY,
    FNO_TG_CHAT,
    FNO_TG_TOKEN,
    KITE_API_KEY,
    KITE_API_SECRET,
)
from trading_engine import TradingEngine
from kite_auth import AUTH_REQUIRED_EXIT_CODE, KiteAuthManager, KiteAuthRequired
from marketpulse_runtime import resolve_state_dir
from marketpulse_state import update_bot_state

# ── GitHub briefing sync ─────────────────────────────────────────
_GITHUB_USER = "Manav-Deakin-23"
_GITHUB_REPO = "marketpulse-bots"
_GITHUB_API  = f"https://api.github.com/repos/{_GITHUB_USER}/{_GITHUB_REPO}/contents/briefings"

def _sync_briefings_from_github():
    """Pull daily_brief.json + fundamental_brief.json from GitHub repo."""
    import requests as _req, base64 as _b64
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
    }
    Path("briefings").mkdir(exist_ok=True)
    for fname in ["daily_brief.json", "fundamental_brief.json"]:
        try:
            r = _req.get(f"{_GITHUB_API}/{fname}", headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            content = _b64.b64decode(r.json()['content']).decode('utf-8')
            local_p = Path("briefings") / fname
            existing = local_p.read_text(encoding='utf-8') if local_p.exists() else ""
            if content != existing:
                local_p.write_text(content, encoding='utf-8')
                log.info(f"[GITHUB SYNC] {fname} updated from cloud")
        except Exception as _e:
            log.debug(f"[GITHUB SYNC] {fname}: {_e}")

# ── Logging ─────────────────────────────────────────────────────
IST = zoneinfo.ZoneInfo("Asia/Kolkata")
log = logging.getLogger("fno_bot")
log.addHandler(logging.NullHandler())
_LOGGING_READY = False
_OPTION_WARNING_CACHE: set[tuple[str, str, str, str]] = set()


def _configure_logging() -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(
        log_dir / f"fno_{datetime.date.today()}.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers = [file_handler, stream_handler]
    _LOGGING_READY = True


def _warn_option_once(index_name: str, prefix: str, opt_type: str, expiry_str: str, message: str) -> None:
    key = (str(index_name), str(prefix), str(opt_type), str(expiry_str))
    if key in _OPTION_WARNING_CACHE:
        return
    _OPTION_WARNING_CACHE.add(key)
    log.warning(message)

# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════
API_KEY        = KITE_API_KEY
API_SECRET     = KITE_API_SECRET
TG_TOKEN       = FNO_TG_TOKEN
TG_CHAT        = FNO_TG_CHAT
FINNHUB_KEY    = CFG_FINNHUB_KEY
DRY_RUN        = True               # ← Set False for live trading

# ── AGGRESSIVE DRY-RUN CONFIG ────────────────────────────────────────────
# When DRY_RUN=True, bot uses these aggressive parameters to stress-test
# its maximum capability. Tests: best signals, larger sizes, faster exits.
# This shows what the system CAN do, not what is conservative.
# For live: revert to conservative values commented below.
# ─────────────────────────────────────────────────────────────────────────
AGGRESSIVE_DRY_RUN = DRY_RUN   # flip separately if needed

BASE_CAPITAL   = 25_000             # Starting capital (Rs.)
MAX_CAPITAL    = BASE_CAPITAL * 5   # Aggressive: allow 5× growth (live: 3×)
MIN_CAPITAL    = BASE_CAPITAL * 0.4 # Aggressive: floor 40% (live: 50%)
STATE_DIR      = resolve_state_dir()
CAPITAL_FILE   = STATE_DIR / "fno_capital.json"
LEARNING_FILE  = STATE_DIR / "fno_learning.json"

# Entry filters — AGGRESSIVE dry-run values (live values in comments)
ATM_TOLERANCE  = 150        # pts — wider strike range (live: 100)
MIN_DELTA      = 0.30       # lower delta floor — test OTM too (live: 0.35)
MAX_THETA_HR   = 0.012      # allow higher theta — test more setups (live: 0.008)
RANGING_DRY_RUN_MIN_SCORE = 9
RANGING_DRY_RUN_MIN_CONF  = 45
SMALL_ACCOUNT_DRY_RUN_MIN_SCORE = 8
SMALL_ACCOUNT_DRY_RUN_MIN_CONF  = 40
MIN_OI         = 5_000      # dry-run: very low floor so next-week options aren't excluded (live: 100_000)
MIN_IV         = 0.08       # wider IV band (live: 0.10)
MAX_IV         = 0.80       # allow high-IV events (live: 0.60)
MAX_PREMIUM    = 25_000     # dry-run: allow stock-option lots to be tested (live: 8_000)
BRIEF_COUNTER_TREND_SCORE_BUMP = 4  # Dry-run raises conviction needed instead of hard-blocking.
SMALL_ACCOUNT_EST_COST_PCT = 0.01
SMALL_ACCOUNT_SCAN_HEADROOM = 1.5
IV_RELAX_STRONG_SCORE = 18
IV_RELAX_STRONG_CONF = 60.0
IV_RELAX_MAX_BONUS = 0.20
IV_RELAX_LEARNED_BONUS = 0.15

# Exit rules — AGGRESSIVE: faster T1, wider T2 to test full range
SL_PCT         = 0.30       # wider SL — see how drawdowns play out (live: 0.25)
T1_PCT         = 0.40       # faster T1 — lock gains quicker (live: 0.50)
T2_PCT         = 1.20       # let winners run further (live: 1.00)
TIME_EXIT_IST  = datetime.time(15, 10)  # slightly later (live: 15:00)
POSITIONAL_MIN_SCORE = 18
POSITIONAL_MIN_CONF = 74
POSITIONAL_STRONG_SCORE = 24
POSITIONAL_STRONG_CONF = 80
POSITIONAL_MAX_HOLD_DAYS = 2
POSITIONAL_ENTRY_CUTOFF = datetime.time(14, 15)

# Adaptive sizing — AGGRESSIVE: bigger swings to test capital dynamics
WIN_SIZE_BOOST  = 0.15      # +15% after win (live: 0.10)
LOSS_SIZE_CUT   = 0.15      # -15% after loss — less punishing (live: 0.20)
MAX_SIZE_MULT   = 3.0       # allow 3× size to test upside (live: 2.0)
MIN_SIZE_MULT   = 0.40      # lower floor — test recovery (live: 0.50)
DAILY_LOSS_LIMIT_PCT = 0.08 # allow 8% daily loss (live: 5%)
MAX_CONSECUTIVE_LOSSES = 4  # stop after 4 (live: 3)

# Indices
INDICES = {
    # Index options — all NSE weekly expiry (Thursday)
    "NIFTY 50":       {"lot": 75,  "tick": "NSE:NIFTY 50",         "prefix": "NIFTY",     "step": 50,   "expiry": "weekly"},
    "NIFTY BANK":     {"lot": 15,  "tick": "NSE:NIFTY BANK",       "prefix": "BANKNIFTY", "step": 100,  "expiry": "weekly"},
    "NIFTY FIN SERVICE": {"lot": 65, "tick": "NSE:NIFTY FIN SERVICE", "prefix": "FINNIFTY",  "step": 50,   "expiry": "weekly"},
    "NIFTY MIDCAP SELECT": {"lot": 75, "tick": "NSE:NIFTY MIDCAP SELECT", "prefix": "MIDCPNIFTY", "step": 25, "expiry": "weekly"},
    # High-liquidity F&O stocks — monthly expiry, high OI, tight spreads
    "RELIANCE":       {"lot": 250, "tick": "NSE:RELIANCE",         "prefix": "RELIANCE",  "step": 50,   "expiry": "monthly"},
    "HDFCBANK":       {"lot": 550, "tick": "NSE:HDFCBANK",         "prefix": "HDFCBANK",  "step": 50,   "expiry": "monthly"},
    "ICICIBANK":      {"lot": 700, "tick": "NSE:ICICIBANK",        "prefix": "ICICIBANK", "step": 50,   "expiry": "monthly"},
    "SBIN":           {"lot": 1500, "tick": "NSE:SBIN",            "prefix": "SBIN",       "step": 10,   "expiry": "monthly"},
    "TCS":            {"lot": 150, "tick": "NSE:TCS",              "prefix": "TCS",        "step": 100,  "expiry": "monthly"},
    "BHARTIARTL":     {"lot": 950, "tick": "NSE:BHARTIARTL",       "prefix": "BHARTIARTL","step": 50,   "expiry": "monthly"},
    "BAJFINANCE":     {"lot": 125, "tick": "NSE:BAJFINANCE",       "prefix": "BAJFINANCE","step": 100,  "expiry": "monthly"},
    "INFY":           {"lot": 400, "tick": "NSE:INFY",             "prefix": "INFY",       "step": 50,   "expiry": "monthly"},
    "AXISBANK":       {"lot": 625, "tick": "NSE:AXISBANK",         "prefix": "AXISBANK",   "step": 20,   "expiry": "monthly"},
    "KOTAKBANK":      {"lot": 400, "tick": "NSE:KOTAKBANK",        "prefix": "KOTAKBANK",  "step": 20,   "expiry": "monthly"},
    "LT":             {"lot": 175, "tick": "NSE:LT",               "prefix": "LT",         "step": 50,   "expiry": "monthly"},
    "SUNPHARMA":      {"lot": 700, "tick": "NSE:SUNPHARMA",        "prefix": "SUNPHARMA",  "step": 20,   "expiry": "monthly"},
}


def _symbol_cfg(index_name: str, scan_cfg_map: dict | None = None) -> dict:
    if scan_cfg_map and index_name in scan_cfg_map:
        return scan_cfg_map[index_name]
    return INDICES.get(index_name, {})

# ══════════════════════════════════════════════════════════════════
#  BLACK-SCHOLES GREEKS
# ══════════════════════════════════════════════════════════════════
def bs_greeks(S, K, T, r, sigma, option_type='call'):
    """
    S: spot price
    K: strike
    T: time to expiry in years
    r: risk-free rate (use 0.065 for India)
    sigma: implied volatility (annualised, e.g. 0.18)
    Returns: dict with price, delta, gamma, theta (per day), vega, iv
    """
    if T <= 0 or sigma <= 0:
        intrinsic = max(0, S-K) if option_type=='call' else max(0, K-S)
        return {'price':intrinsic,'delta':1.0 if intrinsic>0 else 0,'gamma':0,'theta':0,'vega':0,'iv':sigma}

    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)

    if option_type == 'call':
        price = S*norm.cdf(d1) - K*math.exp(-r*T)*norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K*math.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)
        delta = norm.cdf(d1) - 1

    gamma = norm.pdf(d1) / (S*sigma*math.sqrt(T))
    # theta per calendar day (divided by 365)
    theta = (-(S*norm.pdf(d1)*sigma)/(2*math.sqrt(T)) - r*K*math.exp(-r*T)*norm.cdf(d2 if option_type=='call' else -d2)) / 365
    vega  = S*norm.pdf(d1)*math.sqrt(T) / 100  # per 1% IV change

    return {
        'price': round(price, 2),
        'delta': round(delta, 4),
        'gamma': round(gamma, 6),
        'theta': round(theta, 4),   # Rs. per day
        'vega':  round(vega, 4),
        'iv':    round(sigma, 4),
    }


def implied_vol(market_price, S, K, T, r, option_type='call', tol=1e-5, max_iter=100):
    """Newton-Raphson implied volatility solver."""
    if T <= 0 or market_price <= 0:
        return 0.20  # fallback
    sigma = 0.20
    for _ in range(max_iter):
        g = bs_greeks(S, K, T, r, sigma, option_type)
        diff = g['price'] - market_price
        if abs(diff) < tol:
            break
        vega = g['vega'] * 100  # back to raw vega
        if abs(vega) < 1e-8:
            break
        sigma -= diff / vega
        sigma = max(0.01, min(sigma, 5.0))  # bound sigma
    return round(sigma, 4)


def time_to_expiry_years(expiry_date: datetime.date) -> float:
    """Calendar days to expiry / 365."""
    today = datetime.date.today()
    days  = (expiry_date - today).days
    return max(0.0, days / 365.0)


# ══════════════════════════════════════════════════════════════════
#  CAPITAL MANAGEMENT (ADAPTIVE)
# ══════════════════════════════════════════════════════════════════
def load_fno_capital() -> dict:
    """
    Load adaptive capital state from fno_capital.json.
    Returns: {capital, size_multiplier, consecutive_losses,
              consecutive_wins, cumulative_pnl, recover_mode}
    """
    defaults = {
        'capital':            BASE_CAPITAL,
        'size_multiplier':    1.0,
        'consecutive_losses': 0,
        'consecutive_wins':   0,
        'cumulative_pnl':     0.0,
        'recover_mode':       False,
        'base_capital':       BASE_CAPITAL,
    }
    try:
        Path("briefings").mkdir(exist_ok=True)
        if not CAPITAL_FILE.exists():
            return defaults
        with open(CAPITAL_FILE) as f:
            data = json.load(f)
        saved_date = data.get('date', '')
        today      = datetime.date.today().isoformat()

        if saved_date == today:
            log.info(f"[CAPITAL] Today's state loaded: Rs.{data['capital']:.0f} "
                     f"× {data['size_multiplier']:.2f} | streak: "
                     f"+{data['consecutive_wins']}/-{data['consecutive_losses']}")
            return data

        # Roll forward from previous session
        cap    = float(data.get('capital', BASE_CAPITAL))
        pnl    = float(data.get('session_pnl', 0))
        cumul  = float(data.get('cumulative_pnl', 0))
        c_loss = int(data.get('consecutive_losses', 0))
        c_win  = int(data.get('consecutive_wins', 0))
        mult   = float(data.get('size_multiplier', 1.0))
        recover = bool(data.get('recover_mode', False))

        # Recover mode: after 3 losses in a day, start next day at 60% capital
        if recover:
            cap  = max(MIN_CAPITAL, cap * 0.60)
            mult = 0.60
            recover = False
            log.info(f"[CAPITAL] Recover mode: starting at Rs.{cap:.0f} × {mult:.2f}")
        else:
            # Normal roll: grow base if on a winning streak
            if c_win >= 5:
                new_base = min(cap * 1.10, MAX_CAPITAL)
                log.info(f"[CAPITAL] 5-win streak: raising capital {cap:.0f} → {new_base:.0f}")
                cap = new_base
                c_win = 0  # reset streak counter after raise

        # Reset daily streaks but preserve capital and multiplier
        result = {
            'capital':            round(cap, 0),
            'size_multiplier':    round(mult, 2),
            'consecutive_losses': 0,     # reset each day
            'consecutive_wins':   c_win,
            'cumulative_pnl':     round(cumul, 2),
            'recover_mode':       False,
            'base_capital':       BASE_CAPITAL,
            'date':               today,
        }
        log.info(f"[CAPITAL] Rolled forward: Rs.{cap:.0f} × {mult:.2f} | total P&L: Rs.{cumul:.0f}")
        return result
    except Exception as e:
        log.warning(f"[CAPITAL] Load error: {e} — using defaults")
        return defaults


def save_fno_capital(state: dict, session_pnl: float):
    """Persist capital state after session ends."""
    try:
        Path("briefings").mkdir(exist_ok=True)
        state['session_pnl']   = round(session_pnl, 2)
        state['cumulative_pnl'] = round(state.get('cumulative_pnl', 0) + session_pnl, 2)
        state['date']           = datetime.date.today().isoformat()
        state['updated_at']     = datetime.datetime.now().isoformat()
        with open(CAPITAL_FILE, 'w') as f:
            json.dump(state, f, indent=2)
        log.info(f"[CAPITAL] Saved: session Rs.{session_pnl:+.0f} | "
                 f"cumulative Rs.{state['cumulative_pnl']:+.0f}")
    except Exception as e:
        log.warning(f"[CAPITAL] Save error (non-fatal): {e}")


def adapt_size_after_trade(state: dict, pnl: float) -> dict:
    """
    Update size_multiplier using Quarter-Kelly after every closed trade.
    f* = (p*b - q) / b * KELLY_FRACTION
    p  = win probability proxy based on recent streak (0.50–0.85)
    b  = reward:risk target (1.5 for FNO intraday)
    3+ consecutive losses → stop_day flag (unchanged)
    """
    _KELLY_FRACTION = 0.25   # quarter-Kelly reduces variance
    _KELLY_B        = 1.5    # reward:risk target for FNO
    _KELLY_P_MIN    = 0.50   # base win probability
    _KELLY_P_STEP   = 0.05   # boost per consecutive win
    _KELLY_P_MAX    = 0.85   # cap to prevent over-sizing

    won = pnl > 0
    if won:
        state['consecutive_wins']   += 1
        state['consecutive_losses']  = 0
    else:
        state['consecutive_losses'] += 1
        state['consecutive_wins']    = 0

    _p = min(_KELLY_P_MAX, max(_KELLY_P_MIN, _KELLY_P_MIN + (state['consecutive_wins'] * _KELLY_P_STEP)))
    _b = _KELLY_B
    _q = 1.0 - _p
    _full_kelly = (_p * _b - _q) / _b
    new_mult = max(MIN_SIZE_MULT, min(MAX_SIZE_MULT, round(_full_kelly * _KELLY_FRACTION, 2)))

    state['size_multiplier'] = new_mult

    if state['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES:
        state['stop_day']    = True
        state['recover_mode'] = True

    log.info(f"[CAPITAL] After {'WIN' if won else 'LOSS'}: "
             f"kelly_mult={state['size_multiplier']:.2f} (p={_p:.2f} b={_b}) | "
             f"streak: +{state['consecutive_wins']}/-{state['consecutive_losses']}")
    return state


# ══════════════════════════════════════════════════════════════════
#  LEARNING SYSTEM
# ══════════════════════════════════════════════════════════════════
def load_learning() -> dict:
    defaults = {
        'iv_performance':    {},   # iv_bucket → {wins, losses, avg_pnl}
        'hour_performance':  {},   # hour → {wins, losses, avg_pnl}
        'signal_performance':{},   # signal_type → {wins, losses, avg_pnl}
        'delta_performance': {},   # delta_bucket → {wins, losses}
        'trades_total':      0,
        'model_calibration': [],   # list of {predicted_move, actual_move}
    }
    try:
        if LEARNING_FILE.exists():
            with open(LEARNING_FILE) as f:
                data = json.load(f)
            log.info(f"[LEARN] Loaded: {data.get('trades_total',0)} trades in memory")
            return data
    except Exception:
        pass
    return defaults


def record_learning(learning: dict, trade: dict):
    """
    Record outcome of every closed F&O trade for self-improvement.
    trade dict: {iv_entry, delta_entry, entry_hour, signal_type,
                 premium_entry, premium_exit, index_signal, pnl}
    """
    won  = trade['pnl'] > 0
    pnl  = trade['pnl']
    learning['trades_total'] = learning.get('trades_total', 0) + 1

    # IV bucket (e.g. 0.15 → "15-20%")
    iv   = trade.get('iv_entry', 0.20)
    iv_b = f"{int(iv*100//5)*5}-{int(iv*100//5)*5+5}%"
    d    = learning['iv_performance'].setdefault(iv_b, {'wins':0,'losses':0,'total_pnl':0.0})
    d['wins' if won else 'losses'] += 1
    d['total_pnl'] = round(d['total_pnl'] + pnl, 2)

    # Hour bucket
    hr   = str(trade.get('entry_hour', 9))
    d2   = learning['hour_performance'].setdefault(hr, {'wins':0,'losses':0,'total_pnl':0.0})
    d2['wins' if won else 'losses'] += 1
    d2['total_pnl'] = round(d2['total_pnl'] + pnl, 2)

    # Signal type (NIFTY_BUY, BANKNIFTY_SELL, etc.)
    sig  = trade.get('signal_type', 'UNKNOWN')
    d3   = learning['signal_performance'].setdefault(sig, {'wins':0,'losses':0,'total_pnl':0.0})
    d3['wins' if won else 'losses'] += 1
    d3['total_pnl'] = round(d3['total_pnl'] + pnl, 2)

    # Delta bucket
    delta = trade.get('delta_entry', 0.50)
    db    = f"{int(delta*10)*10//10:.1f}-{int(delta*10)*10//10+0.1:.1f}"
    d4    = learning['delta_performance'].setdefault(db, {'wins':0,'losses':0})
    d4['wins' if won else 'losses'] += 1

    # Model calibration: predicted vs actual premium move
    pred = trade.get('predicted_move', 0)
    act  = trade.get('actual_move', 0)
    if pred and act:
        learning['model_calibration'].append({'pred': pred, 'act': act, 'won': won})
        if len(learning['model_calibration']) > 200:
            learning['model_calibration'] = learning['model_calibration'][-200:]

    # Save
    try:
        Path("briefings").mkdir(exist_ok=True)
        with open(LEARNING_FILE, 'w') as f:
            json.dump(learning, f, indent=2)
    except Exception:
        pass

    log.info(f"[LEARN] Recorded {'WIN' if won else 'LOSS'} | IV:{iv_b} Hr:{hr} Sig:{sig} | "
             f"total trades: {learning['trades_total']}")


def get_best_hours(learning: dict) -> list:
    """Return list of hours with WR ≥ 60% and ≥ 3 trades."""
    good = []
    for hr, d in learning['hour_performance'].items():
        total = d['wins'] + d['losses']
        if total >= 3:
            wr = d['wins'] / total
            if wr >= 0.60:
                good.append(int(hr))
    return sorted(good)


def get_iv_range(learning: dict) -> tuple:
    """Return (min_iv, max_iv) based on winning IV buckets."""
    best = []
    for iv_b, d in learning['iv_performance'].items():
        total = d['wins'] + d['losses']
        if total >= 3 and d['wins']/total >= 0.60:
            # Extract lower bound of bucket e.g. "15-20%" → 0.15
            try:
                lo = int(iv_b.split('-')[0]) / 100
                best.append(lo)
            except Exception:
                pass
    if not best:
        return (MIN_IV, MAX_IV)
    return (max(MIN_IV, min(best) - 0.05), min(MAX_IV, max(best) + 0.10))


# ══════════════════════════════════════════════════════════════════
#  OPTION CHAIN FETCHER
# ══════════════════════════════════════════════════════════════════
def _nfo_name_matches(info_name: str, prefix: str) -> bool:
    name = (info_name or '').upper().replace(' ', '').replace('-', '')
    wanted = (prefix or '').upper().replace(' ', '').replace('-', '')
    aliases = {
        'NIFTY': {'NIFTY', 'NIFTY50'},
        'BANKNIFTY': {'BANKNIFTY', 'NIFTYBANK'},
        'FINNIFTY': {'FINNIFTY', 'NIFTYFINSERVICE', 'NIFTYFINANCIALSERVICES'},
        'MIDCPNIFTY': {'MIDCPNIFTY', 'NIFTYMIDCAPSELECT'},
    }
    return name in aliases.get(wanted, {wanted})

def _fallback_option_symbol(prefix: str, expiry_date: datetime.date,
                            strike: float, opt_type: str, expiry_kind: str) -> str:
    month = (expiry_date.strftime("%y%b") if expiry_kind == "monthly"
             else expiry_date.strftime("%d%b%y")).upper()
    return f"{prefix}{month}{int(strike)}{opt_type}"

def get_atm_option(kite, index_name: str, spot: float,
                   direction: str, expiry_date: datetime.date,
                   lot_size: int, cfg: dict | None = None) -> dict | None:
    """
    Fetch live ATM option from Kite option chain.
    Uses data_provider's cached NFO instrument list (loaded at startup) — avoids
    calling kite.instruments('NFO') on every signal, which downloads 47k contracts.
    direction: 'BUY'→Call, 'SELL'→Put
    Returns: {symbol, strike, type, lot, ltp, oi, bid, ask, expiry}
    """
    from data_provider import _token_cache_nfo, get_nfo_contracts

    cfg      = cfg or _symbol_cfg(index_name)
    prefix   = cfg.get('prefix', index_name.replace(' ', '').upper())
    step     = cfg.get('step', 50)
    opt_type = "CE" if direction == "BUY" else "PE"

    # Round spot to nearest strike step — try ATM ±2 strikes
    atm_strike = round(spot / step) * step
    strikes_to_try = [atm_strike,
                      atm_strike + step, atm_strike - step,
                      atm_strike + 2*step, atm_strike - 2*step,
                      atm_strike + 3*step, atm_strike - 3*step]
    expiry_str = expiry_date.isoformat()  # '2026-04-23'

    # Build lookup from cached NFO instruments — O(n) scan but only in memory
    # _token_cache_nfo[sym] = {token, strike, expiry (str), lot_size, inst_type, name}
    # Match: name==prefix, inst_type==CE/PE, expiry==target, strike==atm
    # Pre-build a filtered candidate list for this expiry+type+prefix
    candidates = {}  # strike -> contract metadata
    try:
        for contract in get_nfo_contracts(prefix, expiry_date, opt_type):
            candidates[float(contract["strike"])] = {
                "symbol": contract["symbol"],
                "lot": int(contract.get("lot", lot_size) or lot_size),
                "verified": True,
            }
    except Exception:
        for sym, info in _token_cache_nfo.items():
            try:
                if (_nfo_name_matches(info.get('name', ''), prefix) and
                        info.get('inst_type') == opt_type and
                        info.get('expiry', '')[:10] == expiry_str):
                    candidates[float(info.get('strike', 0))] = {
                        "symbol": sym,
                        "lot": int(info.get('lot_size', lot_size) or lot_size),
                        "verified": True,
                    }
            except Exception:
                continue

    if not candidates:
        # Fallback only as a last resort. Monthly stock options use e.g.
        # ICICIBANK26APR1400PE; weekly index formats can differ, so cached NFO
        # instruments remain the source of truth whenever available.
        month = (expiry_date.strftime("%y%b") if cfg.get("expiry") == "monthly"
                 else expiry_date.strftime("%d%b%y")).upper()
        _warn_option_once(
            index_name,
            prefix,
            opt_type,
            expiry_str,
            f"[OPTION] {index_name}: no cached contracts for {prefix} {opt_type} expiry={expiry_str} "
            f"(cache has {len(_token_cache_nfo)} NFO entries) — using manual construction",
        )
        for strike in strikes_to_try:
            candidates[float(strike)] = {
                "symbol": _fallback_option_symbol(prefix, expiry_date, strike, opt_type, cfg.get("expiry", "weekly")),
                "lot": int(lot_size),
                "verified": False,
            }

    log.info(f"[OPTION] {index_name}: found {len(candidates)} {opt_type} candidates for {expiry_str}, "
             f"spot={spot:.0f}, ATM={atm_strike:.0f}")

    for strike in strikes_to_try:
        candidate = candidates.get(float(strike))
        if not candidate:
            continue
        symbol = candidate["symbol"]
        contract_lot = int(candidate.get("lot", lot_size) or lot_size)
        verified_contract = bool(candidate.get("verified"))
        if not verified_contract and not DRY_RUN:
            log.warning(f"[OPTION] {symbol}: unverified fallback contract blocked in LIVE mode")
            continue
        try:
            ltp_data = kite.ltp(f"NFO:{symbol}")
            if not ltp_data:
                log.debug(f"[OPTION] {symbol}: no LTP data")
                continue
            ltp = ltp_data.get(f"NFO:{symbol}", {}).get('last_price', 0)
            if ltp <= 0:
                log.debug(f"[OPTION] {symbol}: LTP={ltp} (zero/negative)")
                continue

            # Fetch OI and depth
            quote = kite.quote(f"NFO:{symbol}")
            q     = quote.get(f"NFO:{symbol}", {})
            oi    = q.get('oi', 0)
            bid   = q.get('depth', {}).get('buy', [{}])[0].get('price', ltp)
            ask   = q.get('depth', {}).get('sell', [{}])[0].get('price', ltp)

            if oi < MIN_OI:
                log.info(f"[OPTION] {symbol}: OI={oi:,} < MIN {MIN_OI:,} — skipping")
                continue

            cost = ltp * contract_lot
            if cost > MAX_PREMIUM:
                log.info(f"[OPTION] {symbol}: cost Rs.{cost:.0f} > MAX Rs.{MAX_PREMIUM} — skipping")
                continue

            log.info(
                f"[OPTION] Found: {symbol} LTP={ltp} OI={oi:,} "
                f"Lot={contract_lot} Cost=Rs.{cost:.0f} "
                f"{'VERIFIED' if verified_contract else 'UNVERIFIED'}"
            )
            return {
                'symbol':  symbol,
                'strike':  strike,
                'type':    opt_type,
                'lot':     contract_lot,
                'ltp':     ltp,
                'oi':      oi,
                'bid':     bid,
                'ask':     ask,
                'expiry':  expiry_date,
                'cost':    round(cost, 0),
                'verified_contract': verified_contract,
                'capital_required': round(cost, 2),
            }
        except Exception as e:
            log.info(f"[OPTION] {symbol}: fetch error — {e}")
            continue

    log.warning(f"[OPTION] {index_name}: no valid {opt_type} found near {atm_strike:.0f} expiry={expiry_str} "
                f"(tried {len(strikes_to_try)} strikes, {len(candidates)} candidates)")
    return None


def next_expiry(index_name: str) -> datetime.date:
    """Return next expiry: weekly (Thursday) for indices, monthly (last Thursday) for stocks."""
    cfg = _symbol_cfg(index_name)
    expiry_type = cfg.get('expiry', 'weekly')
    NSE_HOLIDAYS = {
        datetime.date(2026, 4, 14), datetime.date(2026, 4, 18),
        datetime.date(2026, 5, 1),  datetime.date(2026, 8, 15),
        datetime.date(2026, 10, 2),
    }
    today = datetime.date.today()
    if expiry_type == 'monthly':
        # Last Thursday of current month (or next month if < 5 days away)
        import calendar
        year, month = today.year, today.month
        # Find last Thursday of this month
        last_day = calendar.monthrange(year, month)[1]
        for d in range(last_day, 0, -1):
            candidate = datetime.date(year, month, d)
            if candidate.weekday() == 3 and candidate not in NSE_HOLIDAYS:
                if (candidate - today).days >= 5:  # at least 5 days away
                    return candidate
        # Roll to next month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        last_day = calendar.monthrange(year, month)[1]
        for d in range(last_day, 0, -1):
            candidate = datetime.date(year, month, d)
            if candidate.weekday() == 3 and candidate not in NSE_HOLIDAYS:
                return candidate
    # Weekly: next Thursday
    for i in range(1, 14):
        candidate = today + datetime.timedelta(days=i)
        if candidate.weekday() == 3 and candidate not in NSE_HOLIDAYS:
            return candidate
    return today + datetime.timedelta(days=7)


def next_weekly_expiry(index_name: str) -> datetime.date:
    """Alias kept for compatibility."""
    return next_expiry(index_name)


def is_expiry_day() -> bool:
    return datetime.date.today().weekday() == 3  # Thursday


# ══════════════════════════════════════════════════════════════════
#  NEWS MONITOR (Finnhub WebSocket-style polling)
# ══════════════════════════════════════════════════════════════════
# ── HIGH IMPACT KEYWORDS — deliberately narrow to avoid false pauses ─────────
# Finnhub general news contains hundreds of articles per hour.
# Only pause on events that ACTUALLY move NSE 1%+ in minutes.
# Generic words like 'rate', 'loss', 'earnings' fire on US/global news constantly.
HIGH_IMPACT_KEYWORDS = [
    # India monetary policy (very specific)
    'rbi rate', 'repo rate', 'rbi policy', 'monetary policy', 'rbi governor',
    # Indian geopolitical (specific to India market impact)
    'india pakistan', 'indian army', 'border clash', 'surgical strike',
    'india attack', 'india war', 'terror attack india', 'mumbai blast',
    # NSE/Market specific
    'nse halt', 'bse halt', 'trading halt', 'market circuit', 'sebi ban',
    'nifty circuit', 'exchange down', 'market close',
    # Budget/Election (India)
    'india budget', 'union budget', 'india election', 'lok sabha',
    # Severe macro
    'india default', 'rupee crash', 'fii ban', 'fpi ban',
]

# Secondary keywords — only trigger if ALSO mentions India/NSE/market
SECONDARY_KEYWORDS = [
    'collapse', 'crash', 'emergency', 'blast', 'attack',
    'bankruptcy', 'fraud', 'scam', 'halt', 'default',
]
INDIA_CONTEXT_WORDS = ['india', 'nse', 'bse', 'nifty', 'sensex', 'rupee', 'rbi', 'sebi']

# NSE-specific categories for Finnhub
NSE_CATEGORIES = ['general', 'merger', 'top news']


def fetch_gift_nifty() -> float | None:
    """
    Fetch GIFT Nifty futures price — best pre-market indicator for NSE.
    GIFT Nifty trades from 6:30 AM IST, 2h45m before NSE opens.
    A -200pt GIFT Nifty at 8 AM = NSE will likely open -150 to -200.
    Returns: GIFT Nifty price or None
    """
    try:
        # Primary: NSE official GIFT Nifty endpoint
        from data_provider import get_gift_nifty as _get_gift
        price = _get_gift()
        if price and price > 0:
            return price
    except Exception:
        pass
    return None


def fetch_pcr() -> float | None:
    """
    Fetch Put-Call Ratio from NSE.
    PCR > 1.2 = bearish sentiment
    PCR < 0.8 = bullish sentiment
    PCR 0.8-1.2 = neutral
    """
    try:
        from data_provider import get_pcr as _get_pcr
        pcr = _get_pcr('NIFTY')
        if pcr and pcr > 0:
            return round(pcr, 2)
    except Exception:
        pass
    # Fallback: manual session with cookie handshake
    try:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.nseindia.com/',
        })
        s.get('https://www.nseindia.com', timeout=10)  # cookie handshake
        time.sleep(1)
        resp = s.get(
            'https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY',
            timeout=8
        )
        if resp.status_code == 200:
            data     = resp.json()
            filtered = data.get('filtered', {})
            ce_oi    = filtered.get('CE', {}).get('totOI', 0)
            pe_oi    = filtered.get('PE', {}).get('totOI', 0)
            if ce_oi > 0:
                return round(pe_oi / ce_oi, 2)
    except Exception:
        pass
    return None

class NewsMonitor:
    """Polls Finnhub every 30 seconds for market-moving news."""

    def __init__(self, api_key: str, on_high_impact):
        self.api_key       = api_key
        self.on_high_impact = on_high_impact
        self.seen_ids      = set()
        self.running       = False
        self.paused_until  = None   # datetime — no new entries while paused

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        log.info("[NEWS] Monitor started")

    def stop(self):
        self.running = False

    def is_paused(self) -> bool:
        if self.paused_until and datetime.datetime.now(IST) < self.paused_until:
            return True
        self.paused_until = None
        return False

    def pause(self, seconds: int = 120):
        self.paused_until = datetime.datetime.now(IST) + datetime.timedelta(seconds=seconds)
        log.info(f"[NEWS] Entries PAUSED for {seconds}s — high-impact news detected")

    def _is_high_impact(self, headline: str) -> bool:
        hl = headline.lower()
        # ONLY fire on direct India market-impact phrases
        # Do NOT fire on global geopolitical news (Iran war, US Fed, oil, etc.)
        return any(kw in hl for kw in HIGH_IMPACT_KEYWORDS)

    def _loop(self):
        while self.running:
            try:
                url  = f"https://finnhub.io/api/v1/news?category=general&token={self.api_key}"
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    for item in resp.json()[:10]:
                        nid = item.get('id', item.get('headline','')[:30])
                        if nid in self.seen_ids:
                            continue
                        self.seen_ids.add(nid)
                        headline = item.get('headline', '')
                        if self._is_high_impact(headline):
                            log.warning(f"[NEWS] HIGH IMPACT: {headline[:80]}")
                            self.on_high_impact(headline)
            except Exception:
                pass
            time.sleep(30)


# ══════════════════════════════════════════════════════════════════
#  TELEGRAM NOTIFIER
# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════
#  BHAGWAT GITA TRADING WISDOM
#  Karma Yoga: Execute the strategy, release attachment to outcome.
#  Markets are governed by forces beyond any single trader's control.
#  Your duty is to follow your system with discipline and equanimity.
# ══════════════════════════════════════════════════════════════════
import random as _random

_GITA_WIN = [
    "🕉️ \"Let not the fruits of action be your motive.\" — Gita 2.47\nProfit recorded. Now forget it. Next trade, same discipline.",
    "🕉️ \"The wise man lets go of all results.\" — Gita 12.16\nA win is the result of good process. Protect the process.",
    "🕉️ \"He who is not disturbed by happiness, distress, fear or anxiety is firmly established.\" — Gita 2.56\nEven in profit, remain steady. The next trade is a fresh slate.",
    "🕉️ \"Do your duty without attachment.\" — Gita 3.19\nGood trade. System worked. Don't raise size out of excitement.",
]

_GITA_LOSS = [
    "🕉️ \"You have a right to perform your duty, but not to the fruits of your action.\" — Gita 2.47\nLoss taken. Duty fulfilled. The system is intact.",
    "🕉️ \"One who is not disturbed in mind, even amidst the threefold miseries, is called steadfast.\" — Gita 2.56\nA loss is data, not defeat. Review. Improve. Continue.",
    "🕉️ \"Let go of attachment to success and failure. Act with equanimity.\" — Gita 2.48\nThe stop-loss did its job. Capital is protected. That IS a win.",
    "🕉️ \"The soul can never be cut, burned, or drowned — it is eternal.\" — Gita 2.23\nCapital is temporary. Discipline is eternal. Stay the course.",
]

_GITA_START = [
    "🕉️ Today's vow: Execute the strategy. Release the outcome.\n\"Nishkam Karma\" — action without desire for reward.",
    "🕉️ Before the bell rings: your only job is to follow the system.\nThe market owes you nothing. Your edge does.",
    "🕉️ \"Better is one's own duty, though imperfectly performed.\" — Gita 3.35\nTrade your plan. Not someone else's.",
    "🕉️ \"A person who is not disturbed by the incessant flow of desires can alone achieve peace.\" — Gita 2.70\nNo FOMO. No revenge trading. Only the plan.",
]

def gita_wisdom(context: str = 'start') -> str:
    """Return a Bhagwat Gita wisdom message for the given trade context."""
    pool = {'win': _GITA_WIN, 'loss': _GITA_LOSS, 'start': _GITA_START}.get(context, _GITA_START)
    return _random.choice(pool)


def tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


def tg_cmd_check(bot_instance) -> str | None:
    """Poll Telegram for commands. Returns command string or None."""
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?timeout=1&offset=-1"
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return None
        updates = resp.json().get('result', [])
        if not updates:
            return None
        msg = updates[-1].get('message', {}).get('text', '')
        update_id = updates[-1].get('update_id', 0)
        # Mark as read
        requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={update_id+1}&timeout=1", timeout=3)
        return msg.strip() if msg.startswith('/') else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
#  CSV TRADE LOGGER
# ══════════════════════════════════════════════════════════════════
Path("logs").mkdir(exist_ok=True)
_csv_path = Path(f"logs/fno_trades_{datetime.date.today()}.csv")
_csv_headers = [
    'time','index','direction','strike','opt_type','lot','premium_entry',
    'premium_exit','iv_entry','iv_exit','delta','theta_per_hr','oi',
    'signal_score','signal_conf','capital_mult','pnl','exit_reason','mode'
]
if not _csv_path.exists():
    with open(_csv_path, 'w', newline='') as f:
        csv.writer(f).writerow(_csv_headers)

def log_trade(row: list):
    with open(_csv_path, 'a', newline='') as f:
        csv.writer(f).writerow(row)


# ══════════════════════════════════════════════════════════════════
#  MAIN F&O BOT
# ══════════════════════════════════════════════════════════════════
class FNOBot:

    def __init__(self):
        _configure_logging()
        self.kite          = KiteConnect(api_key=API_KEY)
        self.connected     = False
        self.engine        = TradingEngine(capital=BASE_CAPITAL, max_risk_pct=0.02)

        # State
        self.pos           = {}          # index_name → position dict
        self.session_pnl   = 0.0
        self.trades        = 0
        self._last_pcr     = 1.0         # updated at startup and each scan
        self._last_vix     = 20.0        # updated at startup and each scan
        self._pcr_last_refresh = None    # datetime of last successful PCR fetch
        self.wins          = 0
        self.losses        = 0
        self.rejections     = []
        self.active_signals = []

        # Adaptive capital state
        self.cap_state     = load_fno_capital()
        self.capital       = self.cap_state['capital']
        self.size_mult     = self.cap_state['size_multiplier']

        # Learning
        self.learning      = load_learning()

        # WebSocket tick cache {instrument_token: last_price}
        self._ticks           = {}
        self._ticker          = None
        self._tick_lock       = threading.Lock()
        self._last_tick_ts    = {}
        # Token IDs — set in __init__ with hardcoded defaults, verified in start_ticker
        self._NIFTY_TOKEN     = 256265   # NSE:NIFTY 50
        self._BANKNIFTY_TOKEN = 260105   # NSE:NIFTY BANK
        # Session tracking
        self.peak_pnl         = 0.0      # high-watermark for drawdown protection
        self._kite_inst       = None     # alias set after login

        # News monitor
        self.news          = NewsMonitor(FINNHUB_KEY, self._on_news)

        # Last index analysis cache
        self.last_signals  = {}         # index_name → Signal
        self.father_opinion = {}

    # ── LOGIN ─────────────────────────────────────────────────────
    def _record_rejection(self, symbol: str, reason: str, **details):
        item = {
            "time": datetime.datetime.now(IST).isoformat(),
            "symbol": symbol,
            "reason": reason,
        }
        item.update(details)
        self.rejections.append(item)
        self.rejections = self.rejections[-100:]

    def _rejection_summary_lines(self, limit: int = 5) -> list[str]:
        if not getattr(self, "rejections", None):
            return []
        counts = Counter(str(item.get("reason", "unknown")) for item in self.rejections if item.get("reason"))
        return [f"{reason}:{count}" for reason, count in counts.most_common(limit)]

    def _qualify_rejection_reason(self, reason: str, source: str) -> str:
        if source != "father_shortlist":
            return reason
        mapping = {
            "no_contract": "father_pick_no_contract",
            "capital_too_high": "father_pick_over_cap",
            "engine_none": "father_pick_untradeable",
        }
        return mapping.get(reason, reason)

    def _brief_countertrend_bump(self, sentiment: str, direction: str) -> int:
        if sentiment == 'BEARISH' and direction == 'BUY':
            return BRIEF_COUNTER_TREND_SCORE_BUMP
        if sentiment == 'BULLISH' and direction == 'SELL':
            return BRIEF_COUNTER_TREND_SCORE_BUMP
        return 0

    def _event_risk_for_symbol(self, symbol: str) -> dict:
        try:
            from trading_engine import classify_event_risk
            return classify_event_risk(symbol)
        except Exception:
            return {
                "risk_level": "normal",
                "risk_reason": "",
                "position_size_multiplier": 1.0,
                "score_bump": 0,
                "target_multiplier": 1.0,
                "stop_multiplier": 1.0,
                "max_hold_multiplier": 1.0,
                "entry_blocked": False,
            }

    def _build_holding_plan(self, index_name: str, sig, risk: dict, is_expiry: bool) -> dict:
        score = abs(getattr(sig, 'total_score', 0) or 0)
        conf = float(getattr(sig, 'confidence', 0) or 0)
        risk_level = risk.get("risk_level", "normal")
        score_bump = int(risk.get("score_bump", 0) or 0)
        positional_allowed = (
            not is_expiry and
            not risk.get("entry_blocked") and
            score >= (POSITIONAL_MIN_SCORE + score_bump) and
            conf >= POSITIONAL_MIN_CONF and
            risk_level in {"normal", "caution"}
        )
        if risk_level == "caution":
            positional_allowed = positional_allowed and score >= (POSITIONAL_STRONG_SCORE + score_bump) and conf >= POSITIONAL_STRONG_CONF

        planned_hold_days = 0
        if positional_allowed:
            planned_hold_days = 2 if score >= (POSITIONAL_STRONG_SCORE + score_bump) and conf >= POSITIONAL_STRONG_CONF else 1
            if risk_level == "caution":
                planned_hold_days = min(planned_hold_days, 1)
            planned_hold_days = max(1, min(POSITIONAL_MAX_HOLD_DAYS, planned_hold_days))

        holding_style = 'positional' if positional_allowed else 'intraday'
        return {
            "holding_style": holding_style,
            "product": 'NRML' if holding_style == 'positional' else 'MIS',
            "planned_hold_days": planned_hold_days,
            "time_exit": None if holding_style == 'positional' else TIME_EXIT_IST,
        }

    def _should_time_exit(self, pos: dict, ist_time: datetime.time) -> bool:
        if pos.get('holding_style') == 'positional':
            return False
        return ist_time >= TIME_EXIT_IST

    def _write_bot_state(self):
        try:
            positions = {}
            for key, pos in self.pos.items():
                positions[key] = {
                    "symbol": pos.get("symbol"),
                    "direction": pos.get("direction"),
                    "qty": pos.get("qty"),
                    "entry_premium": pos.get("entry_premium"),
                    "sl_premium": pos.get("sl_premium"),
                    "t1_premium": pos.get("t1_premium"),
                    "t2_premium": pos.get("t2_premium"),
                    "pnl": pos.get("pnl", 0.0),
                    "product": pos.get("product"),
                    "holding_style": pos.get("holding_style"),
                    "planned_hold_days": pos.get("planned_hold_days", 0),
                    "risk_level": pos.get("risk_level"),
                    "still_holding": True,
                }
            update_bot_state("fno", {
                "positions": positions,
                "signals": self.active_signals[-50:],
                "rejections": self.rejections[-50:],
                "pnl": self.session_pnl,
                "health": {
                    "connected": self.connected,
                    "dry_run": DRY_RUN,
                    "trades": self.trades,
                    "wins": self.wins,
                    "losses": self.losses,
                },
            })
        except Exception as e:
            log.debug(f"[STATE] fno bot_state write skipped: {e}")

    def login(self):
        import webbrowser
        url = self.kite.login_url()
        log.info(f"Opening Zerodha login: {url}")
        webbrowser.open(url)
        KiteAuthManager().authenticate(self.kite, API_SECRET, prompt=input, log=log)
        profile = self.kite.profile()
        log.info(f"Logged in as: {profile['user_name']}")
        self.connected     = True
        self._kite_inst    = self.kite   # alias for data_provider functions

        # Wire Kite into trading engine for official data
        from trading_engine import set_global_kite
        set_global_kite(self.kite)
        self.engine._kite = self.kite

        # Pre-load instrument tokens
        from data_provider import load_instrument_tokens, warmup
        load_instrument_tokens(self.kite)

        _sync_briefings_from_github()
        self._load_daily_brief()
        self._load_father_opinion()

    def _load_daily_brief(self):
        """Read daily_brief.json for global sentiment (CE/PE bias)."""
        self.daily_brief: dict = {}
        path = Path("briefings") / "daily_brief.json"
        if not path.exists():
            return
        try:
            self.daily_brief = json.loads(path.read_text(encoding='utf-8'))
            gs = self.daily_brief.get('global_sentiment', 'NEUTRAL')
            log.info(f"[BRIEF] Loaded daily brief — global_sentiment: {gs}")
        except Exception as _e:
            log.warning(f"[BRIEF] Load failed: {_e}")

    def _normalize_father_opinion(self, opinion: dict) -> dict:
        if not isinstance(opinion, dict):
            return {}

        normalized = json.loads(json.dumps(opinion))
        fno = normalized.setdefault("fno", {})
        india = normalized.setdefault("india", {})
        avoid = {
            str(sym).upper()
            for sym in normalized.get("avoid_symbols", []) or []
            if sym
        }

        raw_candidates = list(fno.get("candidate_symbols", []) or [])
        if not raw_candidates:
            raw_candidates = [
                row.get("symbol")
                for row in (fno.get("candidates", []) or [])
                if isinstance(row, dict)
            ]
        if not raw_candidates:
            raw_candidates = list(india.get("top_focus", []) or [])

        candidate_symbols = []
        candidates = []
        for symbol in raw_candidates:
            sym = str(symbol or "").upper()
            if not sym or sym in avoid or sym in candidate_symbols:
                continue
            candidate_symbols.append(sym)
            candidates.append(
                {
                    "symbol": sym,
                    "bias": "NEUTRAL",
                    "composite_score": 0.0,
                    "execution_mode": "fast_path",
                    "reason": "father_top_focus_fallback",
                }
            )
            if len(candidate_symbols) >= 3:
                break

        existing_candidates = [
            row for row in (fno.get("candidates", []) or [])
            if isinstance(row, dict) and str(row.get("symbol", "")).upper() in candidate_symbols
        ]
        if existing_candidates:
            candidates = existing_candidates[:3]
            candidate_symbols = [str(row.get("symbol", "")).upper() for row in candidates if row.get("symbol")]

        if candidate_symbols:
            fno["candidate_symbols"] = candidate_symbols
            fno["candidates"] = candidates
        return normalized

    def _load_father_opinion(self):
        self.father_opinion = {}
        for path in (STATE_DIR / "father_opinion.json", Path("briefings") / "father_opinion.json"):
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.father_opinion = self._normalize_father_opinion(raw)
                log.info(f"[FATHER] Loaded opinion from {path}")
                if not self.father_opinion.get("fno", {}).get("candidate_symbols"):
                    log.warning("[FATHER] Opinion has no shortlist candidates - broad fallback remains active")
                return
            except Exception as exc:
                log.warning(f"[FATHER] Load failed from {path.name}: {exc}")
        log.warning("[FATHER] No father_opinion.json found - falling back to broad FNO universe")

    # ── WEBSOCKET PRICE STREAMING ─────────────────────────────────
    def start_ticker(self):
        """Subscribe to NIFTY + BankNIFTY live tick stream."""
        # Verify instrument tokens from Kite instruments API
        # Hardcoded as fallback — these almost never change for NSE indices
        NIFTY_TOKEN      = 256265   # NSE:NIFTY 50
        BANKNIFTY_TOKEN  = 260105   # NSE:NIFTY BANK
        try:
            instruments = self.kite.instruments('NSE')
            for ins in instruments:
                if ins['tradingsymbol'] == 'NIFTY 50':
                    NIFTY_TOKEN = ins['instrument_token']
                elif ins['tradingsymbol'] == 'NIFTY BANK':
                    BANKNIFTY_TOKEN = ins['instrument_token']
            log.info(f"[TICKER] Verified tokens: NIFTY={NIFTY_TOKEN} BANK={BANKNIFTY_TOKEN}")
        except Exception as e:
            log.warning(f"[TICKER] Could not verify tokens: {e} — using hardcoded defaults")

        self._ticker = KiteTicker(API_KEY, self.kite.access_token)

        def on_ticks(ws, ticks):
            with self._tick_lock:
                for t in ticks:
                    tok  = t['instrument_token']
                    ltp  = t.get('last_price', 0)
                    self._ticks[tok] = ltp
                    self._last_tick_ts[tok] = time.time()

        def on_connect(ws, response):
            ws.subscribe([NIFTY_TOKEN, BANKNIFTY_TOKEN])
            ws.set_mode(ws.MODE_LTP, [NIFTY_TOKEN, BANKNIFTY_TOKEN])
            log.info("[TICKER] Subscribed to NIFTY + BankNIFTY live ticks")
            tg("📡 WebSocket connected — live tick stream active")

        def on_error(ws, code, reason):
            log.error(f"[TICKER] Error {code}: {reason}")
            tg(f"⚠️ WebSocket error {code}: {reason}")

        def on_close(ws, code, reason):
            log.warning(f"[TICKER] Closed {code}: {reason}")
            tg(f"📴 WebSocket closed — reconnecting...")
            # Auto-reconnect after 5 seconds
            def _reconnect():
                time.sleep(5)
                try:
                    ws.connect(threaded=True)
                    log.info("[TICKER] Reconnected")
                    tg("📡 WebSocket reconnected")
                except Exception as re:
                    log.error(f"[TICKER] Reconnect failed: {re}")
            threading.Thread(target=_reconnect, daemon=True).start()

        self._ticker.on_ticks    = on_ticks
        self._ticker.on_connect  = on_connect
        self._ticker.on_error    = on_error
        self._ticker.on_close    = on_close

        # Run ticker in background thread
        t = threading.Thread(target=self._ticker.connect, kwargs={'threaded': True}, daemon=True)
        t.start()
        log.info("[TICKER] WebSocket thread started")

        # Assign tokens BEFORE warmup loop — early return is safe
        self._NIFTY_TOKEN     = NIFTY_TOKEN
        self._BANKNIFTY_TOKEN = BANKNIFTY_TOKEN

        # Warm up — wait for first ticks
        for _ in range(30):
            time.sleep(1)
            with self._tick_lock:
                if NIFTY_TOKEN in self._ticks:
                    log.info(f"[TICKER] First tick: NIFTY {self._ticks[NIFTY_TOKEN]}")
                    return
        log.warning("[TICKER] No ticks received in 30s — using REST fallback for prices")

    def get_live_price(self, index_name: str) -> float | None:
        """Get latest price via WebSocket (indices) or REST (stocks)."""
        cfg = self._scan_symbol_cfg(index_name)
        tick_key = cfg.get('tick', f'NSE:{index_name}')
        tick_lock = getattr(self, '_tick_lock', None)

        # WebSocket only for NIFTY 50 and NIFTY BANK (subscribed tokens)
        if index_name == 'NIFTY 50' and tick_lock is not None:
            tok = getattr(self, '_NIFTY_TOKEN', 256265)
            with tick_lock:
                ltp  = self._ticks.get(tok)
                last = self._last_tick_ts.get(tok, 0)
            if ltp and (time.time() - last) < 10:
                return ltp
        elif index_name == 'NIFTY BANK' and tick_lock is not None:
            tok = getattr(self, '_BANKNIFTY_TOKEN', 260105)
            with tick_lock:
                ltp  = self._ticks.get(tok)
                last = self._last_tick_ts.get(tok, 0)
            if ltp and (time.time() - last) < 10:
                return ltp

        # REST fallback for all instruments
        try:
            data = self.kite.ltp(tick_key)
            return float(data[tick_key]['last_price'])
        except Exception:
            return None

    def check_tick_health(self) -> bool:
        """Returns False if ticks are stale > 30 seconds during market hours."""
        # Was 5s — WebSocket goes quiet between candles causing false blocks
        for tok, ts in self._last_tick_ts.items():
            if time.time() - ts > 30:
                log.warning("[TICKER] Stale tick >30s — WebSocket may be down")
                return False
        return True

    # ── NEWS HANDLER ──────────────────────────────────────────────
    def _on_news(self, headline: str):
        """Called by NewsMonitor on high-impact news."""
        pause_dur = 120  # default 2 min
        # Geopolitical events (war, attacks) = longer pause + directional bias
        hl_low = headline.lower()
        # Only pause on DIRECT NSE India market impact events
        # Iran/US war, Fed speeches, oil prices = NOT pausing NSE intraday
        india_direct = any(p in hl_low for p in [
            'nse halt', 'bse halt', 'nifty circuit', 'sebi', 'rbi rate decision',
            'india pakistan', 'india war', 'nuclear', 'india attack',
        ])
        if india_direct:
            self._geo_bias = 'BEARISH'
            self.news.pause(pause_dur)
            log.warning(f"[NEWS] INDIA DIRECT event — pausing entries {pause_dur}s")
            tg(f"🌏 INDIA MARKET NEWS:\n{headline[:120]}\n\n⏸ Entries paused {pause_dur}s")
        else:
            # Non-India global news: LOG only, do NOT pause
            log.info(f"[NEWS] Global event (no pause): {headline[:80]}")

        # Check if any open position is opposed by the news
        for idx_name, pos in list(self.pos.items()):
            # Simple heuristic: if headline has crash/fall/drop and we're long, exit
            bearish_words = ['crash','fall','drop','slump','plunge','cut','ban','halt']
            bullish_words = ['surge','rise','jump','rally','boost','cut rate']
            hl_low = headline.lower()
            is_bearish_news = any(w in hl_low for w in bearish_words)
            is_bullish_news = any(w in hl_low for w in bullish_words)

            is_long = pos['direction'] == 'BUY'  # BUY = Call = long market

            if (is_long and is_bearish_news) or (not is_long and is_bullish_news):
                log.warning(f"[NEWS] {idx_name}: news opposes position — emergency exit")
                tg(f"🚨 {idx_name}: NEWS OPPOSES POSITION\nEmergency exit triggered")
                self._exit_position(idx_name, reason='NEWS_EXIT')

    # ── SIGNAL → OPTION DECISION ──────────────────────────────────
    def _evaluate_signal(self, index_name: str, spot: float, source: str = "scan_universe") -> dict | None:
        """
        Run index analysis via engine. If signal valid, check option filters.
        Returns option spec dict or None.
        """
        from trading_engine import TradingEngine

        # Get NIFTY move using Kite data (official, no delay)
        nifty_spot = self.get_live_price("NIFTY 50")
        try:
            # Use kite.ohlc() for prev_close — avoids iloc[-2] holiday off-by-one errors
            _nifty_ohlc_q = self.kite.ohlc('NSE:NIFTY 50')
            prev_close = float(_nifty_ohlc_q.get('NSE:NIFTY 50', {}).get('ohlc', {}).get('close', 0))
            if prev_close <= 0:
                from data_provider import get_ohlcv
                _hist = get_ohlcv(self.kite, 'NIFTY 50', interval='day', days=5)
                if _hist is not None and not _hist.empty:
                    prev_close = float(_hist['close'].iloc[-1])  # iloc[-1] not -2
            nifty_move_pct = (nifty_spot - prev_close) / prev_close * 100 if prev_close else 0.0
        except Exception:
            nifty_move_pct = 0.0

        # Pass Kite-native index name directly — engine now handles Kite data
        # No need for yfinance '^NSEI' symbols anymore
        kite_sym = index_name
        yf_sym   = kite_sym

        # Proactively clear SKIP_SYMBOLS for ALL FNO-scanned symbols on every call.
        # Kite data failures are transient — a symbol that failed once shouldn't
        # stay banned for the entire session. This is especially critical for
        # F&O liquid stocks and indices with rock-solid data availability.
        # NOTE: We clear UNCONDITIONALLY (not just if already in set) because symbols
        # can get added mid-analyze() via exception handlers, which causes silent failures
        # on subsequent scans even if they weren't in SKIP_SYMBOLS at call start.
        from trading_engine import SKIP_SYMBOLS as _SS
        if yf_sym in _SS:
            log.warning(f"[SIGNAL] {index_name}: was in SKIP_SYMBOLS — clearing for retry")
        _SS.discard(yf_sym)  # always clear, not just when present

        # skip_adx=True: after a crash+recovery whipsaw NIFTY ADX collapses to 1-4 on daily
        # charts even though the market is strongly moving. Options are directional bets —
        # they don't need ADX >= 20, only clear RSI/MACD/MA/VWAP direction signals.
        sig = self.engine.analyze(yf_sym, live_price=spot, market_move_pct=nifty_move_pct,
                                  verbose=True, skip_adx=True, record_signal=False)
        if sig is None:
            # After verbose=True, engine should have logged WHY it returned None.
            # If no engine log appeared above this line, the symbol is in SKIP_SYMBOLS
            # (log.debug — invisible) or hit a silent path. The discard above should prevent this.
            log.info(f"[SIGNAL] {index_name}: engine returned None — no trade (check above for engine reason)")
            self._record_rejection(index_name, self._qualify_rejection_reason("engine_none", source), source=source)
            return None
        if sig.signal in ('NO TRADE', 'NEUTRAL'):
            log.info(f"[SIGNAL] {index_name}: engine returned {sig.signal} — no trade")
            self._record_rejection(index_name, "neutral", signal=sig.signal, source=source)
            return None
        direction = 'BUY' if 'BUY' in sig.signal else 'SELL'

        risk = self._event_risk_for_symbol(index_name)
        if risk.get("entry_blocked"):
            log.info(f"[SIGNAL] {index_name}: event-risk blocked â€” {risk.get('risk_reason', 'risk_block')}")
            self._record_rejection(index_name, "event_caution_block", risk_reason=risk.get("risk_reason", ""), source=source)
            return None

        min_score, min_conf = self._entry_quality_gate(index_name, direction)
        min_score += int(risk.get('score_bump', 0) or 0)
        if risk.get('risk_level') == 'caution':
            min_conf = max(min_conf, 45)
        gs = getattr(self, 'daily_brief', {}).get('global_sentiment', 'NEUTRAL')
        bump = self._brief_countertrend_bump(gs, direction)
        countertrend_blocked = bool((not AGGRESSIVE_DRY_RUN) and bump)
        if bump:
            min_score += bump
            log.info(f"[BRIEF] {index_name}: {direction} conflicts with {gs} brief — requiring score >= {min_score}")
        if abs(sig.total_score) < min_score:
            log.info(f"[SIGNAL] {index_name}: score={sig.total_score} conf={sig.confidence:.0f}% — below floor {min_score}")
            self._record_rejection(index_name, "below_score", score=sig.total_score, threshold=min_score, source=source)
            return None
        if sig.confidence < min_conf:
            log.info(f"[SIGNAL] {index_name}: conf {sig.confidence:.0f}% < {min_conf}% — skip")
            self._record_rejection(index_name, "below_confidence", confidence=sig.confidence, threshold=min_conf, source=source)
            return None

        # Brief-based directional filter. Dry-run keeps testing counter-trend
        # execution with a higher score floor; live mode still blocks it.
        if countertrend_blocked:
            log.info(f"[BRIEF] {index_name}: {direction} counter to {gs} brief — live entry blocked")
            self._record_rejection(index_name, "brief_blocked", sentiment=gs, direction=direction, source=source)
            return None

        if self.engine._is_duplicate(index_name, direction):
            log.info(f"[SIGNAL] {index_name}: duplicate {direction} setup within cooldown - skip")
            self._record_rejection(index_name, "duplicate_cooldown", direction=direction, source=source)
            return None

        expiry_day = is_expiry_day()
        # Tighter parameters on expiry day — theta kills value fast
        if expiry_day:
            sl_pct   = SL_PCT * 0.75    # -15% instead of -20% of premium
            t1_pct   = T1_PCT * 0.875   # +35% instead of +40%
            t2_pct   = T2_PCT * 0.75    # +60% instead of +80%
            max_hold = 45 * 60          # 45-min max hold on expiry (seconds)
        else:
            sl_pct   = SL_PCT
            t1_pct   = T1_PCT
            t2_pct   = T2_PCT
            max_hold = None             # signal-driven exit

        cfg       = self._scan_symbol_cfg(index_name)
        expiry    = next_expiry(index_name)
        lot_size  = int(cfg.get('lot', 1) or 1)

        # Learning: check if this hour has been good
        best_hours = get_best_hours(self.learning)
        current_hr = datetime.datetime.now(IST).hour
        if best_hours and current_hr not in best_hours and len(best_hours) >= 5:
            log.info(f"[LEARN] Hour {current_hr} not in best hours {best_hours} — skip")
            self._record_rejection(index_name, "learned_hour_rejected", hour=current_hr, best_hours=best_hours, source=source)
            return None

        # Fetch ATM option — try current expiry first, then next if OI too low
        # On expiry day (Thursday), current-week OI evaporates by afternoon;
        # next_expiry() already returns next Thursday, but if that also fails
        # (new week options not yet liquid), log details and bail out.
        option = get_atm_option(self.kite, index_name, spot, direction, expiry, lot_size, cfg=cfg)
        if option is None:
            # On expiry day: also try the week-after-next expiry (2 weeks out)
            if datetime.date.today().weekday() == 3:  # Thursday
                from data_provider import _token_cache_nfo
                _next2 = expiry + datetime.timedelta(days=7)
                log.info(f"[OPTION] Expiry day — trying week-after-next {_next2}")
                option = get_atm_option(self.kite, index_name, spot, direction, _next2, lot_size, cfg=cfg)
                if option:
                    expiry = _next2  # update expiry to match the option found
        if option is None:
            log.info(f"[OPTION] No suitable ATM option found for {index_name}")
            self._record_rejection(
                index_name,
                self._qualify_rejection_reason("no_contract", source),
                direction=direction,
                expiry=str(expiry),
                source=source,
            )
            return None

        # Greeks calculation
        T    = time_to_expiry_years(expiry)
        iv   = implied_vol(option['ltp'], spot, option['strike'], T, r=0.065,
                           option_type='call' if direction=='BUY' else 'put')
        greeks = bs_greeks(spot, option['strike'], T, 0.065, iv,
                           'call' if direction=='BUY' else 'put')
        strong_signal = abs(sig.total_score) >= IV_RELAX_STRONG_SCORE and sig.confidence >= IV_RELAX_STRONG_CONF

        # Learning: check IV range
        iv_min, iv_max = get_iv_range(self.learning)
        learned_iv_max = min(1.25, iv_max + (IV_RELAX_LEARNED_BONUS if strong_signal else 0.0))
        if not (iv_min <= iv <= learned_iv_max):
            log.info(f"[LEARN] IV {iv:.2%} outside learned sweet spot {iv_min:.0%}–{iv_max:.0%} — skip")
            self._record_rejection(index_name, "iv_learned_range", iv=iv, min=iv_min, max=learned_iv_max, source=source)
            return None

        # Filter: delta
        if abs(greeks['delta']) < MIN_DELTA:
            log.info(f"[FILTER] {index_name}: delta {greeks['delta']:.2f} < {MIN_DELTA} — too far OTM")
            self._record_rejection(index_name, "delta_rejected", delta=greeks['delta'], threshold=MIN_DELTA, source=source)
            return None

        # Filter: theta — cost per hour as % of premium
        theta_hr = abs(greeks['theta']) / 24 / option['ltp'] if option['ltp'] > 0 else 1
        if theta_hr > MAX_THETA_HR:
            log.info(f"[FILTER] {index_name}: theta/hr {theta_hr:.3%} > {MAX_THETA_HR:.3%} — too much decay")
            self._record_rejection(index_name, "theta_rejected", theta_hr=theta_hr, threshold=MAX_THETA_HR, source=source)
            return None

        # Filter: IV bands
        hard_iv_max = min(1.25, MAX_IV + (IV_RELAX_MAX_BONUS if strong_signal else 0.0))
        if not (MIN_IV <= iv <= hard_iv_max):
            log.info(f"[FILTER] {index_name}: IV {iv:.2%} outside [{MIN_IV:.0%},{hard_iv_max:.0%}] — skip")
            self._record_rejection(index_name, "iv_rejected", iv=iv, min=MIN_IV, max=hard_iv_max, source=source)
            return None

        plan = self._build_holding_plan(index_name, sig, risk, expiry_day)
        sl_pct = max(0.08, min(0.50, sl_pct * float(risk.get("stop_multiplier", 1.0) or 1.0)))
        t1_pct = max(0.12, t1_pct * float(risk.get("target_multiplier", 1.0) or 1.0))
        t2_pct = max(t1_pct + 0.12, t2_pct * float(risk.get("target_multiplier", 1.0) or 1.0))
        if plan["holding_style"] == 'positional':
            hold_days = max(1, int(math.ceil(plan["planned_hold_days"] * float(risk.get("max_hold_multiplier", 1.0) or 1.0))))
            hold_days = min(POSITIONAL_MAX_HOLD_DAYS, hold_days)
            max_hold = hold_days * 24 * 60 * 60

        return {
            'index':       index_name,
            'direction':   direction,
            'signal':      sig,
            'option':      option,
            'greeks':      greeks,
            'iv':          iv,
            'theta_hr':    theta_hr,
            'spot':        spot,
            'expiry':      expiry,
            'sl_pct':      sl_pct,
            't1_pct':      t1_pct,
            't2_pct':      t2_pct,
            'max_hold':    max_hold,
            'product':     plan['product'],
            'holding_style': plan['holding_style'],
            'planned_hold_days': plan['planned_hold_days'],
            'time_exit':   plan['time_exit'],
            'risk_level':  risk.get('risk_level'),
            'risk_reason': risk.get('risk_reason', ''),
            'position_size_multiplier': risk.get('position_size_multiplier', 1.0),
            'source':      source,
        }

    def _entry_quality_gate(self, index_name: str, direction: str) -> tuple[int, int]:
        # Dry-run normally explores lower-quality setups, but ranging sessions need a
        # tighter bar or the bot keeps forcing weak option bets in chop.
        min_score = 5 if AGGRESSIVE_DRY_RUN else 12
        min_conf = 35 if AGGRESSIVE_DRY_RUN else 50
        regime = getattr(self.engine, 'market_regime', 'UNKNOWN')
        if AGGRESSIVE_DRY_RUN and regime == 'RANGING':
            min_score = max(min_score, RANGING_DRY_RUN_MIN_SCORE)
            min_conf = max(min_conf, RANGING_DRY_RUN_MIN_CONF)
        if AGGRESSIVE_DRY_RUN and self._small_account_mode():
            min_score = min(min_score, SMALL_ACCOUNT_DRY_RUN_MIN_SCORE)
            min_conf = min(min_conf, SMALL_ACCOUNT_DRY_RUN_MIN_CONF)

        return min_score, min_conf

    def _small_account_mode(self) -> bool:
        return float(getattr(self, 'capital', 0) or 0) <= BASE_CAPITAL

    def _brief_priority_sets(self):
        brief = getattr(self, "daily_brief", {}) or {}
        top = {
            str(item.get("symbol", "")).upper()
            for item in brief.get("top_watchlist", [])
            if isinstance(item, dict) and item.get("symbol")
        }
        dynamic = {
            str(sym).upper()
            for sym in brief.get("dynamic_watchlist", [])
            if sym
        }
        avoid = {
            str(sym).upper()
            for sym in brief.get("avoid_symbols", [])
            if sym
        }
        return top, dynamic, avoid

    def _scan_symbol_cfg(self, index_name: str) -> dict:
        return _symbol_cfg(index_name, getattr(self, "_scan_cfg_map", None))

    def _derived_stock_scan_items(self):
        from data_provider import _token_cache_nfo

        derived = {}
        index_prefixes = {
            "NIFTY",
            "NIFTY50",
            "BANKNIFTY",
            "NIFTYBANK",
            "FINNIFTY",
            "NIFTYFINSERVICE",
            "NIFTYFINANCIALSERVICES",
            "MIDCPNIFTY",
            "NIFTYMIDCAPSELECT",
        }

        for info in _token_cache_nfo.values():
            name = str(info.get("name", "") or "").strip()
            if not name:
                continue
            normalized = name.upper().replace(" ", "").replace("-", "")
            if normalized in index_prefixes:
                continue
            if info.get("inst_type") not in {"CE", "PE"}:
                continue

            bucket = derived.setdefault(
                name,
                {
                    "strikes": set(),
                    "lot": int(info.get("lot_size", 1) or 1),
                    "prefix": normalized,
                    "strike_count": 0,
                },
            )
            try:
                strike = float(info.get("strike", 0) or 0)
            except Exception:
                strike = 0.0
            if strike > 0:
                bucket["strikes"].add(strike)
            bucket["strike_count"] += 1

        items = []
        for name, meta in sorted(derived.items(), key=lambda item: item[0]):
            if name in INDICES:
                continue
            strikes = sorted(meta["strikes"])
            step = 50
            affordability = float("inf")
            if len(strikes) >= 2:
                diffs = [round(b - a, 6) for a, b in zip(strikes, strikes[1:]) if (b - a) > 0]
                if diffs:
                    step = int(round(min(diffs))) or 50
            if strikes:
                affordability = float(strikes[0]) * int(meta["lot"])
            est_cost = affordability * SMALL_ACCOUNT_EST_COST_PCT if affordability != float("inf") else float("inf")
            items.append(
                (
                    name,
                    {
                        "lot": int(meta["lot"]),
                        "tick": f"NSE:{name}",
                        "prefix": meta["prefix"],
                        "step": step,
                        "expiry": "monthly",
                        "_rank_affordability": affordability,
                        "_rank_estimated_cost": est_cost,
                        "_rank_contract_count": int(meta["strike_count"]),
                    },
                )
            )
        top, dynamic, avoid = self._brief_priority_sets()

        def _rank(item):
            name, cfg = item
            sym = str(name).upper()
            if sym in top:
                priority = 0
            elif sym in dynamic:
                priority = 1
            elif sym in avoid:
                priority = 3
            else:
                priority = 2
            return (
                priority,
                float(cfg.get("_rank_affordability", float("inf"))),
                -int(cfg.get("_rank_contract_count", 0)),
                sym,
            )

        return sorted(items, key=_rank)

    def _scan_universe_items(self):
        base_items = list(INDICES.items())
        extra_items = self._derived_stock_scan_items()
        if not extra_items:
            return base_items
        if self._small_account_mode():
            max_trade_capital = float(getattr(self, "capital", BASE_CAPITAL) or BASE_CAPITAL) * 0.30
            filtered = [
                item for item in extra_items
                if float(item[1].get("_rank_estimated_cost", float("inf"))) <= max_trade_capital * SMALL_ACCOUNT_SCAN_HEADROOM
            ]
            if filtered:
                extra_items = filtered
        return base_items + extra_items

    def _likely_affordable_scan_item(self, cfg: dict) -> bool:
        affordability = float(cfg.get("_rank_estimated_cost", cfg.get("_rank_affordability", 0)) or 0)
        if affordability <= 0:
            return True
        return affordability <= float(getattr(self, "capital", BASE_CAPITAL) or BASE_CAPITAL) * 0.30

    def _scan_batches(self, scan_items):
        father = getattr(self, "father_opinion", {}) or {}
        fno = father.get("fno", {}) if isinstance(father, dict) else {}
        if str(fno.get("mode", "") or "").lower() == "risk_hold":
            return []

        top, dynamic, avoid = self._brief_priority_sets()
        avoid_set = {str(sym).upper() for sym in avoid}
        raw_candidates = list(fno.get("candidate_symbols", []) or [])
        if not raw_candidates:
            raw_candidates = [
                row.get("symbol")
                for row in (fno.get("candidates", []) or [])
                if isinstance(row, dict)
            ]
        candidate_order = [str(sym or "").upper() for sym in raw_candidates if sym]
        if not candidate_order:
            return [list(scan_items)]

        scan_map = {str(name).upper(): (name, cfg) for name, cfg in scan_items}
        primary = []
        used = set()
        for symbol in candidate_order:
            if symbol in avoid_set:
                continue
            item = scan_map.get(symbol)
            if not item:
                continue
            name, cfg = item
            if not self._likely_affordable_scan_item(cfg):
                self._record_rejection(
                    name,
                    self._qualify_rejection_reason("capital_too_high", "father_shortlist"),
                    source="father_shortlist",
                    scan_stage="father_shortlist",
                )
                continue
            tagged_cfg = dict(cfg)
            tagged_cfg["_scan_source"] = "father_shortlist"
            primary.append((name, tagged_cfg))
            used.add(symbol)

        if not primary:
            return [list(scan_items)]

        fallback = []
        for name, cfg in scan_items:
            if str(name).upper() in used:
                continue
            tagged_cfg = dict(cfg)
            tagged_cfg["_scan_source"] = "scan_universe"
            fallback.append((name, tagged_cfg))
        return [primary, fallback]

    def _can_open_direction(self, direction: str) -> tuple[bool, str]:
        if not self.pos:
            return True, "clear"
        regime = getattr(self.engine, 'market_regime', 'UNKNOWN')
        if regime == 'RANGING':
            existing_dirs = {pos.get('direction') for pos in self.pos.values()}
            if direction in existing_dirs:
                return False, "ranging_same_direction"
        return True, "clear"

    # ── ENTRY ─────────────────────────────────────────────────────
    def _enter(self, spec: dict):
        index     = spec['index']
        direction = spec['direction']
        option    = spec['option']
        greeks    = spec['greeks']
        sig       = spec['signal']
        lot       = option['lot']
        premium   = option['ltp']
        symbol    = option['symbol']
        product   = spec.get('product', 'MIS')
        holding_style = spec.get('holding_style', 'intraday')
        planned_hold_days = int(spec.get('planned_hold_days', 0) or 0)
        size_risk_mult = float(spec.get('position_size_multiplier', 1.0) or 1.0)
        source = str(spec.get('source', 'scan_universe') or 'scan_universe')

        # Position sizing with adaptive multiplier
        base_lots = max(1, int(self.capital * 0.05 / (premium * lot)))  # risk 5% per trade
        adj_lots  = max(1, round(base_lots * self.size_mult * size_risk_mult))
        cost      = premium * lot * adj_lots
        max_trade_capital = self.capital * 0.30

        if premium * lot > max_trade_capital:
            log.info(f"[CAPITAL] {symbol}: single-lot cost Rs.{premium * lot:.0f} > cap Rs.{max_trade_capital:.0f} — skip")
            self._record_rejection(
                index,
                self._qualify_rejection_reason("capital_too_high", source),
                option_symbol=symbol,
                cost=round(premium * lot, 2),
                cap=round(max_trade_capital, 2),
                source=source,
            )
            self._write_bot_state()
            return

        if cost > max_trade_capital:  # hard cap: 30% of capital per trade
            adj_lots = max(1, int(max_trade_capital / (premium * lot)))
            cost     = premium * lot * adj_lots

        # SL / targets in premium terms — use per-position pct (expiry-day override if set)
        _sl_pct    = spec.get('sl_pct', SL_PCT)
        _t1_pct    = spec.get('t1_pct', T1_PCT)
        _t2_pct    = spec.get('t2_pct', T2_PCT)
        sl_premium = round(premium * (1 - _sl_pct), 1)
        t1_premium = round(premium * (1 + _t1_pct), 1)
        t2_premium = round(premium * (1 + _t2_pct), 1)

        log.info(f"\n{'='*60}")
        log.info(f"{'DRY ' if DRY_RUN else ''}ENTRY: {index} {direction} → {symbol}")
        log.info(f"  Lots: {adj_lots} × {lot} = {adj_lots*lot} qty | Cost: Rs.{cost:.0f}")
        log.info(f"  Premium: Rs.{premium} | SL: Rs.{sl_premium} | T1: Rs.{t1_premium} | T2: Rs.{t2_premium}")
        log.info(f"  Greeks: Δ={greeks['delta']:.2f} θ={greeks['theta']:.2f}/day "
                 f"ν={greeks['vega']:.2f}/1%IV | IV={spec['iv']:.1%}")
        log.info(f"  Signal: score={sig.total_score} conf={sig.confidence}% | "
                 f"capital_mult={self.size_mult:.2f}")

        # Place order
        # Skill 8: Slippage model — FNO options have wider bid-ask (0.3%)
        entry_premium_slipped = premium
        sl_oid = None
        if DRY_RUN:
            from trading_engine import apply_slippage
            entry_premium_slipped = apply_slippage(premium, 'BUY', is_dry_run=True, is_option=True)
            log.info(f"  [DRY SLIP] {symbol}: premium {premium:.2f} → {entry_premium_slipped:.2f} (option spread)")

        oid = f"DRY_{int(time.time())}"
        if not DRY_RUN and self.connected:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=symbol, exchange='NFO',
                    transaction_type='BUY',   # always BUY call or put
                    quantity=lot * adj_lots,
                    product=product, order_type='LIMIT',
                    price=round(option['ask'] * 1.002, 1),  # slight buffer on ask
                    variety='regular',
                )
                log.info(f"  Order placed: oid={oid}")

                # Place exchange SL immediately
                sl_oid = self.kite.place_order(
                    tradingsymbol=symbol, exchange='NFO',
                    transaction_type='SELL',
                    quantity=lot * adj_lots,
                    product=product, order_type='SL-M',
                    trigger_price=round(sl_premium * 0.995, 1),
                    variety='regular',
                )
                log.info(f"  Exchange SL placed: oid={sl_oid} @ Rs.{sl_premium}")
            except Exception as e:
                log.error(f"  Order failed: {e}")
                tg(f"❌ ENTRY FAILED: {symbol}\n{e}")
                return

        now_ist = datetime.datetime.now(IST)
        self.engine._record_signal(index, direction)
        self.active_signals.append({
            "time": now_ist.isoformat(),
            "symbol": symbol,
            "underlying": index,
            "direction": direction,
            "score": sig.total_score,
            "confidence": sig.confidence,
            "source": source,
            "dry_run": DRY_RUN,
            "status": "dry-run entry recorded" if DRY_RUN else "live entry attempted",
        })
        self.active_signals = self.active_signals[-100:]
        self.pos[index] = {
            'symbol':         symbol,
            'direction':      direction,
            'lots':           adj_lots,
            'lot_size':       lot,
            'qty':            lot * adj_lots,
            'entry_premium':  entry_premium_slipped if DRY_RUN else premium,
            'sl_premium':     sl_premium,
            't1_premium':     t1_premium,
            't2_premium':     t2_premium,
            'greeks':         greeks,
            'iv_entry':       spec['iv'],
            'spot_entry':     spec['spot'],
            'signal_score':   sig.total_score,
            'signal_conf':    sig.confidence,
            'signal_type':    f"{index.replace(' ','_')}_{direction}",
            'capital_mult':   self.size_mult,
            'cost':           cost,
            'entry_time':     now_ist,
            'entry_hour':     now_ist.hour,
            'expiry':         spec.get('expiry'),
            'oid':            oid,
            'sl_oid':         sl_oid if (not DRY_RUN and self.connected) else None,
            'partial_done':   False,
            'sl_pct':         spec.get('sl_pct', SL_PCT),
            't1_pct':         spec.get('t1_pct', T1_PCT),
            't2_pct':         spec.get('t2_pct', T2_PCT),
            'max_hold':       spec.get('max_hold'),
            'product':        product,
            'holding_style':  holding_style,
            'planned_hold_days': planned_hold_days,
            'time_exit':      spec.get('time_exit'),
            'risk_level':     spec.get('risk_level', 'normal'),
            'risk_reason':    spec.get('risk_reason', ''),
            'source':         source,
        }
        self.trades += 1
        self._write_bot_state()

        tg(
            f"{'🔵 DRY' if DRY_RUN else '🟢 LIVE'} F&O ENTRY\n"
            f"{index} {direction} → {symbol}\n"
            f"Lots: {adj_lots} | Cost: Rs.{cost:.0f}\n"
            f"Premium: Rs.{premium} | SL: Rs.{sl_premium} (-25%)\n"
            f"T1: Rs.{t1_premium} (+50%) | T2: Rs.{t2_premium} (+100%)\n"
            f"Δ={greeks['delta']:.2f} IV={spec['iv']:.1%} θ={greeks['theta']:.2f}/day\n"
            f"Score: {sig.total_score} | Conf: {sig.confidence}% | Size×{self.size_mult:.2f}"
        )

        # Log trade
        log_trade([
            now_ist.isoformat(), index, direction,
            option['strike'], option['type'], adj_lots,
            premium, '', spec['iv'], '', greeks['delta'],
            round(spec['theta_hr']*100, 3), option['oi'],
            sig.total_score, sig.confidence, self.size_mult,
            '', 'ENTRY', 'DRY' if DRY_RUN else 'LIVE'
        ])

    # ── EXIT ──────────────────────────────────────────────────────
    def _exit_position(self, index: str, current_premium: float = 0.0, reason: str = 'MANUAL'):
        pos = self.pos.get(index)
        if not pos:
            return

        symbol = pos['symbol']
        qty    = pos['qty']
        entry  = pos['entry_premium']
        product = pos.get('product', 'MIS')

        # Get latest premium if not provided
        if current_premium <= 0:
            try:
                data = self.kite.ltp(f"NFO:{symbol}")
                current_premium = data[f"NFO:{symbol}"]['last_price']
            except Exception:
                current_premium = entry  # assume flat if can't fetch

        pnl = (current_premium - entry) * qty   # always bought, so profit = premium up
        now_ist = datetime.datetime.now(IST)

        log.info(f"\n{'='*60}")
        log.info(f"{'DRY ' if DRY_RUN else ''}EXIT: {index} {symbol}")
        log.info(f"  Entry: Rs.{entry} | Exit: Rs.{current_premium:.2f} | "
                 f"P&L: Rs.{pnl:+.2f} | Reason: {reason}")

        # Cancel exchange SL if still open
        sl_oid = pos.get('sl_oid')
        if sl_oid and not DRY_RUN and self.connected:
            try:
                self.kite.cancel_order(variety='regular', order_id=sl_oid)
            except Exception:
                pass

        # Place exit order
        if not DRY_RUN and self.connected:
            try:
                exit_oid = self.kite.place_order(
                    tradingsymbol=symbol, exchange='NFO',
                    transaction_type='SELL',
                    quantity=qty,
                    product=product, order_type='LIMIT',
                    price=round(current_premium * 0.998, 1),  # slight bid offset
                    variety='regular',
                )
                log.info(f"  Exit order: oid={exit_oid}")
            except Exception as e:
                log.error(f"  Exit failed: {e}")

        # Record outcome
        won = pnl > 0
        self.session_pnl += pnl
        if won:
            self.wins  += 1
        else:
            self.losses += 1

        # Adaptive capital
        self.cap_state = adapt_size_after_trade(self.cap_state, pnl)
        self.size_mult = self.cap_state['size_multiplier']

        # Learning
        iv_exit = 0.0
        try:
            T       = time_to_expiry_years(pos['expiry'] if isinstance(pos.get('expiry'), datetime.date) else datetime.date.today())
            spot    = self.get_live_price(index) or pos['spot_entry']
            iv_exit = implied_vol(current_premium, spot, pos['greeks'].get('strike', spot), T, 0.065,
                                  'call' if pos['direction']=='BUY' else 'put')
        except Exception:
            pass

        record_learning(self.learning, {
            'iv_entry':       pos['iv_entry'],
            'iv_exit':        iv_exit,
            'delta_entry':    abs(pos['greeks']['delta']),
            'entry_hour':     pos['entry_hour'],
            'signal_type':    pos['signal_type'],
            'premium_entry':  entry,
            'premium_exit':   current_premium,
            'predicted_move': abs(pos['greeks']['delta']) * (self.get_live_price(index) or 0 - pos['spot_entry']),
            'actual_move':    current_premium - entry,
            'pnl':            pnl,
        })

        # Telegram — with Gita wisdom for equanimity after win/loss
        icon = '✅' if won else '❌'
        _wisdom = gita_wisdom('win' if won else 'loss')
        tg(
            f"{icon} F&O EXIT: {index}\n"
            f"{symbol}\n"
            f"Entry Rs.{entry} → Exit Rs.{current_premium:.2f}\n"
            f"P&L: Rs.{pnl:+.0f} | Reason: {reason}\n"
            f"Session: Rs.{self.session_pnl:+.0f} | "
            f"Size×{self.size_mult:.2f}\n"
            f"{'🛑 3 LOSSES — STOPPED FOR TODAY' if self.cap_state.get('stop_day') else ''}\n\n"
            f"{_wisdom}"
        )

        # CSV log
        log_trade([
            now_ist.isoformat(), index, pos['direction'],
            '', pos['symbol'][-2:], pos['lots'],
            entry, round(current_premium, 2),
            pos['iv_entry'], round(iv_exit, 4),
            pos['greeks']['delta'], '',
            '', pos['signal_score'], pos['signal_conf'],
            pos['capital_mult'], round(pnl, 2), reason,
            'DRY' if DRY_RUN else 'LIVE'
        ])

        del self.pos[index]
        self._write_bot_state()

    def _reconcile_existing_positions(self):
        # Reconcile with Zerodha positions (handles bot restart mid-session)
        try:
            existing = self.kite.positions()
            day_pos = existing.get('day', [])
            for p in day_pos:
                product = p.get('product')
                if product not in {'MIS', 'NRML'}:
                    continue
                if p.get('quantity', 0) == 0:
                    continue
                if p.get('exchange') != 'NFO':
                    continue
                sym = p['tradingsymbol']
                qty = abs(p['quantity'])
                entry = p.get('average_price', 0)
                if entry <= 0:
                    log.warning(f"[RECONCILE] {sym} has zero entry price — skipping")
                    continue

                sym_up = sym.upper()
                idx_name = None
                for name, cfg in INDICES.items():
                    if sym_up.startswith(str(cfg.get('prefix', '')).upper()):
                        idx_name = name
                        break
                if not idx_name:
                    log.info(f"[RECONCILE] Skipping {sym} — unsupported option family")
                    continue
                if idx_name in self.pos:
                    log.info(f"[RECONCILE] {idx_name} already tracked — skipping {sym}")
                    continue

                opt_type = 'CE' if sym_up.endswith('CE') else 'PE'
                direction = 'BUY' if opt_type == 'CE' else 'SELL'
                live_spot = 0.0
                try:
                    tick_key = INDICES.get(idx_name, {}).get('tick', f"NSE:{idx_name}")
                    live_spot = float(self.kite.ltp(tick_key)[tick_key]['last_price'])
                except Exception:
                    pass

                self.pos[idx_name] = {
                    'symbol': sym, 'direction': direction,
                    'lots': 1, 'lot_size': INDICES.get(idx_name, {}).get('lot', qty),
                    'qty': qty, 'entry_premium': entry,
                    'sl_premium': round(entry * (1 - SL_PCT), 1),
                    't1_premium': round(entry * (1 + T1_PCT), 1),
                    't2_premium': round(entry * (1 + T2_PCT), 1),
                    'greeks': {'delta': 0.45, 'theta': 0, 'vega': 0, 'strike': live_spot or 0},
                    'iv_entry': 0.20,
                    'spot_entry': live_spot,
                    'signal_score': 0, 'signal_conf': 0,
                    'signal_type': f"{idx_name.replace(' ', '_')}_{direction}",
                    'capital_mult': self.size_mult,
                    'cost': entry * qty,
                    'entry_time': datetime.datetime.now(IST).isoformat(),
                    'entry_hour': datetime.datetime.now(IST).hour,
                    'expiry': None,
                    'oid': 'RECONCILED',
                    'sl_oid': None, 'partial_done': False,
                    'product': product,
                    'holding_style': 'positional' if product == 'NRML' else 'intraday',
                    'planned_hold_days': 1 if product == 'NRML' else 0,
                    'time_exit': None if product == 'NRML' else TIME_EXIT_IST,
                    'risk_level': 'normal',
                    'risk_reason': '',
                }
                log.warning(f"[RECONCILE] {sym} qty={qty} @ Rs.{entry} | spot={live_spot:.0f}")
            self._write_bot_state()
        except Exception as e:
            log.warning(f"[RECONCILE] Could not check positions: {e}")

    # ── MONITOR OPEN POSITIONS ────────────────────────────────────
    def _monitor(self):
        """Check every open position for SL/T1/T2/time/news exit. Called every tick."""
        ist_now  = datetime.datetime.now(IST)
        ist_time = ist_now.time()

        for index, pos in list(self.pos.items()):
            symbol = pos['symbol']
            product = pos.get('product', 'MIS')

            # Get live premium
            try:
                if DRY_RUN:
                    # Simulate: premium moves with spot (delta approximation)
                    # GUARD: if spot_entry is 0 or None (corrupted reconcile), dspot becomes
                    # the raw index value (~24000) → entry Rs.57 → exit Rs.12000 (fictitious).
                    # Fix: if spot_entry invalid, set it to current spot so dspot = 0 (neutral).
                    spot = self.get_live_price(index) or pos.get('spot_entry') or 0.0
                    if not pos.get('spot_entry') or pos['spot_entry'] <= 0:
                        log.warning(
                            f"[MONITOR] {index}: spot_entry={pos.get('spot_entry')} invalid — "
                            f"resetting to current spot {spot:.0f} to prevent P&L fiction"
                        )
                        pos['spot_entry'] = spot
                    dspot = spot - pos['spot_entry']
                    # Sanity cap: dspot > ±2000 pts is impossible in a single session
                    if abs(dspot) > 2000:
                        log.warning(
                            f"[MONITOR] {index}: dspot={dspot:.0f} is abnormally large — "
                            f"capping at ±200 to prevent P&L explosion"
                        )
                        dspot = max(-200.0, min(200.0, dspot))
                    cp    = pos['entry_premium'] + pos['greeks']['delta'] * dspot
                    cp    = max(0.05, round(cp, 1))
                else:
                    data  = self.kite.ltp(f"NFO:{symbol}")
                    cp    = data[f"NFO:{symbol}"]['last_price']
            except Exception:
                continue

            entry  = pos['entry_premium']
            pnl    = (cp - entry) * pos['qty']
            chg    = (cp - entry) / entry if entry > 0 else 0

            # Per-position SL/T1/T2 (expiry-day override applied at entry)
            sl_pct = pos.get('sl_pct', SL_PCT)
            t1_pct = pos.get('t1_pct', T1_PCT)
            t2_pct = pos.get('t2_pct', T2_PCT)

            # ── Expiry-day max hold check ───────────────────────
            max_hold = pos.get('max_hold')
            if max_hold is not None:
                entry_time = pos.get('entry_time')
                if entry_time is not None:
                    if isinstance(entry_time, str):
                        entry_time = datetime.datetime.fromisoformat(entry_time)
                    held_secs = (datetime.datetime.now(IST) - entry_time).total_seconds()
                    if held_secs >= max_hold:
                        self._exit_position(index, cp, 'MAX_HOLD_EXPIRY')
                        continue

            # ── Time exit ──────────────────────────────────────
            if self._should_time_exit(pos, ist_time):
                log.info(f"[MONITOR] {index}: TIME EXIT @ 15:00")
                self._exit_position(index, cp, 'TIME_EXIT')
                continue

            risk = self._event_risk_for_symbol(index)
            if pos.get('holding_style') == 'positional' and risk.get('entry_blocked'):
                log.info(f"[MONITOR] {index}: positional carry no longer allowed — {risk.get('risk_reason', 'event_risk')}")
                self._exit_position(index, cp, 'EVENT_RISK_EXIT')
                continue

            # ── SL hit (-25%) ──────────────────────────────────
            if cp <= pos['sl_premium']:
                log.info(f"[MONITOR] {index}: SL HIT @ Rs.{cp:.2f} (-{abs(chg):.1%})")
                self._exit_position(index, cp, 'SL_HIT')
                continue

            # ── T1 hit (+50%) → partial exit ───────────────────
            if cp >= pos['t1_premium'] and not pos.get('partial_done'):
                half_qty = max(1, pos['qty'] // 2) if pos['qty'] > 1 else pos['qty']
                full_exit = (pos['qty'] == 1)
                if half_qty > 0:
                    part_pnl = (cp - entry) * half_qty
                    log.info(f"[MONITOR] {index}: T1 HIT @ Rs.{cp:.2f} (+{chg:.1%}) — partial exit {half_qty}qty")
                    if not DRY_RUN and self.connected:
                        try:
                            self.kite.place_order(
                                tradingsymbol=symbol, exchange='NFO',
                                transaction_type='SELL', quantity=half_qty,
                                product=product, order_type='LIMIT',
                                price=round(cp * 0.998, 1), variety='regular',
                            )
                        except Exception as e:
                            log.error(f"  Partial exit failed: {e}")
                    self.session_pnl    += part_pnl
                    self.wins           += 1
                    pos['partial_done']  = True
                    pos['qty']           = pos['qty'] - half_qty
                    pos['sl_premium']    = entry    # trail SL to entry after T1
                    log.info(f"  Partial P&L: Rs.{part_pnl:+.0f} | SL trailed to entry Rs.{entry}")
                    tg(f"🎯 T1 HIT: {index}\n{symbol}\nExit {half_qty}qty @ Rs.{cp:.2f}\n"
                       f"Partial P&L: Rs.{part_pnl:+.0f}\nSL moved to entry Rs.{entry}")
                continue

            # ── T2 hit (+100%) → full exit ──────────────────────
            if cp >= pos['t2_premium']:
                log.info(f"[MONITOR] {index}: T2 HIT @ Rs.{cp:.2f} (+{chg:.1%})")
                self._exit_position(index, cp, 'TARGET2')
                continue

            # ── Stall: held >30min, barely moved ───────────────
            try:
                entry_t   = pos['entry_time']
                if isinstance(entry_t, str):
                    entry_t = datetime.datetime.fromisoformat(entry_t)
                mins_held = (ist_now - entry_t).total_seconds() / 60
                if mins_held > 30 and -0.15 < chg < 0.10:
                    log.info(f"[MONITOR] {index}: STALL {mins_held:.0f}min, {chg:+.1%} — exit to free capital")
                    self._exit_position(index, cp, 'STALL')
                    continue
            except Exception:
                pass

    # ── MAIN SCAN LOOP ────────────────────────────────────────────
    def scan(self):
        """Called every ~10 seconds. Evaluates signals, monitors positions."""
        global _OPTION_WARNING_CACHE
        _OPTION_WARNING_CACHE.clear()
        ist_now  = datetime.datetime.now(IST)
        ist_time = ist_now.time()

        # Market hours: 9:15 – 15:30 IST
        market_open  = datetime.time(9, 15)
        market_close = datetime.time(15, 30)
        if ist_time < market_open:
            return   # pre-market — wait
        if ist_time > market_close:
            # Market is closed — send EOD summary and exit cleanly
            log.info("[BOT] Market closed — sending session summary and shutting down")
            try:
                self._send_session_summary()
            except Exception as _e:
                log.warning(f"[BOT] Session summary failed: {_e}")
            sys.exit(0)

        # Skill 10: Peak drawdown protection
        if not hasattr(self, 'peak_pnl'):
            self.peak_pnl = 0.0
        if self.session_pnl > self.peak_pnl:
            self.peak_pnl = self.session_pnl
        from trading_engine import check_peak_drawdown
        if check_peak_drawdown(self.session_pnl, self.peak_pnl, 0.35):
            tg(f"🛡 FNO: session fell 35% from Rs.{self.peak_pnl:.0f} peak — stopping to protect profits")
            self.cap_state['stop_day'] = True
            return

        # Daily loss limit
        loss_limit = self.capital * DAILY_LOSS_LIMIT_PCT
        if self.session_pnl <= -loss_limit:
            tg(f"🚨 DAILY LOSS LIMIT HIT: Rs.{self.session_pnl:.0f} (>{loss_limit:.0f})\nTrading stopped for today.")
            return

        # 3 consecutive losses → stop
        if self.cap_state.get('stop_day'):
            return

        # Always monitor open positions first (every tick matters)
        try:
            self._monitor()
        except Exception as _me:
            log.error(f"_monitor() error (non-fatal): {_me}")

        # Telegram command check
        cmd = tg_cmd_check(self)
        if cmd:
            self._handle_cmd(cmd)

        # Entry evaluation — only if slots free and no news pause
        max_pos = 5 if AGGRESSIVE_DRY_RUN else 3  # was 3/2 — expanded for larger universe
        if len(self.pos) >= max_pos:
            return

        # Portfolio heat: don't go same direction on both indices on ranging day
        if self.news.is_paused():
            log.info(f"[SCAN] News pause active — no new entries")
            return
        if not self.check_tick_health():
            return

        # Entry cutoffs: tighter on expiry day (theta decay accelerates)
        NO_NEW_ENTRY_NORMAL = datetime.time(15, 0)
        NO_NEW_ENTRY_EXPIRY = datetime.time(14, 45)
        cutoff = NO_NEW_ENTRY_EXPIRY if is_expiry_day() else NO_NEW_ENTRY_NORMAL
        if ist_time >= cutoff:
            log.debug(f"[SCAN] Past entry cutoff {cutoff} {'(expiry day)' if is_expiry_day() else ''}")
            return

        # ── Hourly PCR + VIX refresh ──────────────────────────────────────────
        # Root cause of PCR=1.00: it was only fetched at startup, never again.
        # Refresh every 60 minutes throughout the session so PCR reflects
        # live option chain sentiment (bearish surge → PCR > 1.2, etc.)
        _pcr_stale = (
            self._pcr_last_refresh is None or
            (datetime.datetime.now(IST) - self._pcr_last_refresh).total_seconds() > 3600
        )
        if _pcr_stale:
            try:
                _fresh_pcr = fetch_pcr()
                if _fresh_pcr and _fresh_pcr > 0:
                    self._last_pcr = _fresh_pcr
                    self._pcr_last_refresh = datetime.datetime.now(IST)
                    log.info(f"[PCR] Refreshed: {self._last_pcr:.2f} ({'BEARISH' if self._last_pcr>1.2 else 'BULLISH' if self._last_pcr<0.8 else 'NEUTRAL'})")
                else:
                    log.warning(f"[PCR] Refresh failed — using stale value {self._last_pcr:.2f}")
            except Exception as _pe:
                log.warning(f"[PCR] Refresh error (non-fatal): {_pe}")
            try:
                from data_provider import get_vix as _get_vix
                _fvix = _get_vix()
                if _fvix and _fvix > 0:
                    self._last_vix = _fvix
            except Exception:
                pass

        self._load_father_opinion()

        # Evaluate father shortlist first. Only widen to the broad universe when
        # the shortlist produced no tradeable setup in this scan.
        scan_items = self._scan_universe_items()
        self._scan_cfg_map = dict(scan_items)
        scan_batches = self._scan_batches(scan_items)
        if not scan_batches:
            log.info("[FATHER] FNO mode risk_hold — new entries paused")
            return

        for batch_num, batch in enumerate(scan_batches, start=1):
            if not batch:
                continue
            trades_before_batch = self.trades
            pos_before_batch = len(self.pos)
            if batch_num == 1 and any(cfg.get("_scan_source") == "father_shortlist" for _, cfg in batch):
                log.info(f"[FATHER] Priority shortlist scan: {[name for name, _cfg in batch]}")

            for index_name, cfg in batch:
                source = str(cfg.get("_scan_source", "scan_universe") or "scan_universe")
                spot = self.get_live_price(index_name)
                if spot:
                    if index_name == 'NIFTY 50':
                        try:
                            from data_provider import get_ohlcv
                            from trading_engine import calc_adx, detect_regime
                            _df = get_ohlcv(self.engine._kite, 'NIFTY 50', 'day', 80)
                            if _df is not None and not _df.empty and len(_df) >= 14:
                                _df.columns = [c.lower() for c in _df.columns]
                                _adx, _, _ = calc_adx(_df['high'].values.astype(float),
                                                      _df['low'].values.astype(float),
                                                      _df['close'].values.astype(float))
                                try:
                                    _ohlc_resp = self.kite.ohlc('NSE:NIFTY 50')
                                    _prev = float(_ohlc_resp.get('NSE:NIFTY 50', {}).get('ohlc', {}).get('close', 0))
                                except Exception:
                                    _prev = 0
                                if _prev <= 0:
                                    _prev = float(_df['close'].iloc[-1])
                                _chg  = (spot - _prev) / _prev * 100 if _prev > 0 else 0.0
                                self.engine.market_regime = detect_regime(_adx, _chg)
                                log.info(f"[REGIME] NIFTY ADX={_adx:.1f} chg={_chg:+.2f}% → {self.engine.market_regime}")
                        except Exception as _re:
                            log.warning(f"[REGIME] Detection failed: {_re}")
                    log.info(f"[LEVELS] {index_name}: {spot:.2f} | "
                             f"PCR={self._last_pcr:.2f} VIX={self._last_vix:.1f} "
                             f"Regime={getattr(self.engine,'market_regime','UNKNOWN')}")
                if index_name in self.pos:
                    continue

                spot = self.get_live_price(index_name)
                if spot is None:
                    continue

                try:
                    spec = self._evaluate_signal(index_name, spot, source=source)
                    if spec is None:
                        continue

                    allowed, block_reason = self._can_open_direction(spec['direction'])
                    if not allowed:
                        log.info(f"[SIGNAL] {index_name}: {spec['direction']} blocked — {block_reason}")
                        self._record_rejection(index_name, block_reason, direction=spec['direction'], source=source)
                        continue

                    log.info(f"[SIGNAL] {index_name}: {spec['direction']} score={spec['signal'].total_score} "
                             f"conf={spec['signal'].confidence}% IV={spec['iv']:.1%} Δ={spec['greeks']['delta']:.2f} "
                             f"source={source}")
                    self._enter(spec)
                    if len(self.pos) >= max_pos:
                        break
                except Exception as _scan_err:
                    log.error(f"[SCAN] {index_name}: unhandled error (skipped): {_scan_err}", exc_info=True)

            if len(self.pos) >= max_pos:
                break
            if batch_num == 1 and (self.trades > trades_before_batch or len(self.pos) > pos_before_batch):
                break

    # ── SESSION SUMMARY ───────────────────────────────────────────
    def _send_session_summary(self):
        """Send end-of-day P&L summary via Telegram."""
        date_str = datetime.date.today().strftime('%d %b')
        rejection_lines = self._rejection_summary_lines()
        rejection_block = ""
        if rejection_lines:
            rejection_block = "\nTop rejects: " + ", ".join(rejection_lines)
        tg(
            f"📊 FNO SESSION SUMMARY — {date_str}\n"
            f"Trades: {self.trades} | W/L: {self.wins}/{self.losses}\n"
            f"Session P&L: Rs.{self.session_pnl:+.0f}\n"
            f"Capital: Rs.{self.capital:.0f} × {self.size_mult:.2f}\n"
            f"Streak: +{self.cap_state['consecutive_wins']}/-{self.cap_state['consecutive_losses']}"
            f"{rejection_block}"
        )

    # ── COMMAND HANDLER ───────────────────────────────────────────
    def _handle_cmd(self, cmd: str):
        cmd = cmd.strip().lower()

        if cmd == '/status':
            lines = [f"📊 F&O BOT STATUS\n{'DRY RUN' if DRY_RUN else 'LIVE'}"]
            lines.append(f"Capital: Rs.{self.capital:.0f} × {self.size_mult:.2f}")
            lines.append(f"Session: {self.trades}T {self.wins}W/{self.losses}L Rs.{self.session_pnl:+.0f}")
            lines.append(f"Streak: +{self.cap_state['consecutive_wins']}/-{self.cap_state['consecutive_losses']}")
            if self.pos:
                for idx, pos in self.pos.items():
                    cp = self.get_live_price(idx) or 0
                    lines.append(f"\n{idx}: {pos['direction']} {pos['symbol']}")
                    lines.append(f"  Entry Rs.{pos['entry_premium']} | Live ~Rs.{cp:.0f}")
            else:
                lines.append("No open positions")
            tg('\n'.join(lines))

        elif cmd.startswith('/exit'):
            parts = cmd.split()
            if len(parts) >= 2:
                idx = parts[1].upper()
                match = next((k for k in self.pos if idx in k.upper()), None)
                if match:
                    self._exit_position(match, reason='MANUAL')
                    tg(f"✅ {match}: manually exited")
                else:
                    tg(f"{idx} not found. Open: {list(self.pos.keys())}")
            else:
                tg(f"Usage: /exit NIFTY or /exit BANK")

        elif cmd == '/stop':
            self.cap_state['stop_day'] = True
            tg("🛑 Bot stopped for today. Restart tomorrow.")

        elif cmd == '/learn':
            self._send_learning_report()

        elif cmd == '/capital':
            tg(f"💰 Capital: Rs.{self.capital:.0f}\n"
               f"Size multiplier: {self.size_mult:.2f}\n"
               f"Cumulative P&L: Rs.{self.cap_state.get('cumulative_pnl', 0):+.0f}\n"
               f"Consecutive wins: {self.cap_state['consecutive_wins']}\n"
               f"Consecutive losses: {self.cap_state['consecutive_losses']}")

        elif cmd == '/unpause':
            self.news.paused_until = None
            tg("▶️ News pause cleared — entries resumed")
            log.info("[CMD] News pause manually cleared")

        elif cmd == '/help':
            tg("/status   — positions + P&L\n"
               "/exit NIFTY  — exit NIFTY position\n"
               "/exit BANK   — exit BankNIFTY position\n"
               "/stop        — stop trading today\n"
               "/unpause     — clear news pause immediately\n"
               "/learn       — show learning report\n"
               "/capital     — show capital state")

    def _send_learning_report(self):
        L   = self.learning
        tot = L.get('trades_total', 0)
        if tot == 0:
            tg("No learning data yet — need more trades.")
            return

        lines = [f"🧠 LEARNING REPORT ({tot} trades)\n"]

        lines.append("Best hours:")
        for hr, d in sorted(L['hour_performance'].items()):
            n  = d['wins'] + d['losses']
            wr = d['wins']/n*100 if n else 0
            lines.append(f"  {hr}:00 — {n}T WR:{wr:.0f}% avg Rs.{d['total_pnl']/n:+.0f}")

        lines.append("\nBest IV ranges:")
        for iv_b, d in sorted(L['iv_performance'].items()):
            n  = d['wins'] + d['losses']
            wr = d['wins']/n*100 if n else 0
            lines.append(f"  IV {iv_b} — {n}T WR:{wr:.0f}%")

        lines.append("\nSignal performance:")
        for sig, d in L['signal_performance'].items():
            n  = d['wins'] + d['losses']
            wr = d['wins']/n*100 if n else 0
            lines.append(f"  {sig} — {n}T WR:{wr:.0f}% Rs.{d['total_pnl']:+.0f}")

        tg('\n'.join(lines))

    # ── SESSION END ───────────────────────────────────────────────
    def _end_session(self):
        log.info("\n" + "="*60)
        wr = self.wins/self.trades*100 if self.trades else 0
        log.info(f"SESSION END | {self.trades}T {self.wins}W/{self.losses}L WR:{wr:.0f}% P&L:Rs.{self.session_pnl:+.0f}")

        # Exit any remaining positions
        for index in list(self.pos.keys()):
            self._exit_position(index, reason='EOD')

        # Save capital state
        save_fno_capital(self.cap_state, self.session_pnl)

        tg(
            f"📋 F&O SESSION END\n"
            f"{'DRY RUN' if DRY_RUN else 'LIVE'}\n"
            f"Trades: {self.trades} | W/L: {self.wins}/{self.losses} | WR: {wr:.0f}%\n"
            f"P&L: Rs.{self.session_pnl:+.0f}\n"
            f"Capital next: Rs.{self.capital:.0f} × {self.size_mult:.2f}\n"
            f"Cumulative: Rs.{self.cap_state.get('cumulative_pnl',0):+.0f}"
        )

    # ── RUN ───────────────────────────────────────────────────────
    def run(self):
        log.info(f"\n{'='*60}")
        log.info(f"F&O BOT v1 | {'DRY RUN' if DRY_RUN else '🔴 LIVE'}")
        log.info(f"Capital: Rs.{self.capital:.0f} × size_mult={self.size_mult:.2f}")
        log.info(f"Streak: +{self.cap_state['consecutive_wins']} wins / "
                 f"-{self.cap_state['consecutive_losses']} losses")

        # Holiday check
        NSE_HOLIDAYS = {
            datetime.date(2026, 4, 14), datetime.date(2026, 4, 18),
            datetime.date(2026, 5, 1),  datetime.date(2026, 8, 15),
            datetime.date(2026, 10, 2),
        }
        today = datetime.date.today()
        if today in NSE_HOLIDAYS:
            log.info(f"Today ({today}) is NSE holiday — F&O bot not starting")
            tg(f"🏖 NSE Holiday today ({today}) — bot not starting")
            return
        if today.weekday() >= 5:  # Saturday/Sunday
            log.info(f"Weekend — F&O bot not starting")
            return

        # Login
        try:
            self.login()
        except KiteAuthRequired as e:
            log.error(f"Kite login required: {e}")
            sys.exit(AUTH_REQUIRED_EXIT_CODE)

        self._reconcile_existing_positions()

        # Start WebSocket tick stream
        self.start_ticker()

        # Start news monitor
        self.news.start()

        # Fetch pre-market intelligence
        gift = fetch_gift_nifty()
        pcr  = fetch_pcr()
        if pcr and pcr > 0:
            self._last_pcr = pcr
            self._pcr_last_refresh = datetime.datetime.now(IST)
        else:
            self._last_pcr = 1.0
            log.warning("[PCR] Startup fetch failed — defaulting to 1.0 (neutral). Will retry hourly.")
        try:
            from data_provider import get_vix
            self._last_vix = get_vix()
        except Exception:
            pass
        gift_str = f"GIFT Nifty: {gift:.0f}" if gift else "GIFT Nifty: N/A"
        pcr_str  = (f"PCR: {self._last_pcr:.2f} ({'BEARISH' if self._last_pcr>1.2 else 'BULLISH' if self._last_pcr<0.8 else 'NEUTRAL'})"
                    if pcr else "PCR: N/A (retry scheduled)")
        log.info(f"[PRE-MARKET] {gift_str} | {pcr_str}")

        # Startup regime detection — so regime is KNOWN from first scan, not UNKNOWN.
        # Fetch with days=80 to match engine._fetch_ohlcv() default, seeding the cache
        # so the engine's first analyze() call hits the cache instead of a fresh Kite hit.
        try:
            from data_provider import get_ohlcv as _get_ohlcv_startup
            from trading_engine import calc_adx, detect_regime
            _sdf = _get_ohlcv_startup(self.kite, 'NIFTY 50', 'day', 80)
            if _sdf is not None and not _sdf.empty and len(_sdf) >= 14:
                _sdf.columns = [c.lower() for c in _sdf.columns]
                _sadx, _, _ = calc_adx(_sdf['high'].values.astype(float),
                                        _sdf['low'].values.astype(float),
                                        _sdf['close'].values.astype(float))
                _spot_now = self.get_live_price('NIFTY 50') or float(_sdf['close'].iloc[-1])
                _sprev    = float(_sdf['close'].iloc[-2])
                _schg     = (_spot_now - _sprev) / _sprev * 100
                self.engine.market_regime = detect_regime(_sadx, _schg)
                log.info(f"[REGIME] Startup detection: {self.engine.market_regime} "
                         f"(ADX={_sadx:.1f}, chg={_schg:+.1f}%)")
        except Exception as _re:
            log.warning(f"[REGIME] Startup detection failed (non-fatal): {_re}")

        # Warm up OHLCV cache for all scanned indices at days=80 (matches engine default).
        # This prevents the engine's first analyze() call from triggering a cold Kite hit
        # and avoids kt-common rate limiting emptying the cache mid-session.
        try:
            from data_provider import get_ohlcv as _warm_ohlcv
            _warm_indices = list(INDICES.keys())  # NIFTY 50, NIFTY BANK, FINNIFTY, etc.
            log.info(f"[WARMUP] Pre-loading OHLCV for {len(_warm_indices)} indices...")
            for _widx in _warm_indices:
                try:
                    _wdf = _warm_ohlcv(self.kite, _widx, 'day', 80)
                    log.debug(f"[WARMUP] {_widx}: {len(_wdf)} rows cached")
                except Exception as _we:
                    log.warning(f"[WARMUP] {_widx} failed (non-fatal): {_we}")
            log.info("[WARMUP] Index OHLCV warmup complete")
        except Exception as _wex:
            log.warning(f"[WARMUP] Index warmup failed (non-fatal): {_wex}")

        tg(
            f"🚀 F&O Bot Started ({'DRY RUN' if DRY_RUN else 'LIVE'})\n"
            f"Capital: Rs.{self.capital:.0f} | Size×{self.size_mult:.2f}\n"
            f"{gift_str} | {pcr_str}\n"
            f"Commands: /status /exit /stop /learn /capital /help\n\n"
            f"{gita_wisdom('start')}"
        )

        try:
            while True:
                if Path('STOP').exists():
                    log.warning("STOP file detected — FNO Bot shutting down")
                    tg("🛑 STOP file detected — FNO Bot shutting down")
                    sys.exit(0)
                ist_now = datetime.datetime.now(IST)

                # End of session
                if ist_now.time() >= datetime.time(15, 30):
                    self._end_session()
                    log.info("Session complete. Bot sleeping until tomorrow.")
                    break

                try:
                    self.scan()
                except Exception as _fno_e:
                    log.error(f"FNO scan() error (non-fatal): {_fno_e}")
                time.sleep(10)   # scan every 10 seconds (monitor on every call)

        except KeyboardInterrupt:
            log.info("Interrupted — ending session")
            self._end_session()
        except Exception as e:
            log.error(f"Fatal error: {e}\n{traceback.format_exc()}")
            tg(f"🚨 F&O BOT CRASH\n{e}\n\nAll positions may still be open — check Zerodha!")
            self._end_session()
        finally:
            if self._ticker:
                self._ticker.close()
            self.news.stop()


if __name__ == "__main__":
    FNOBot().run()
