"""
╔══════════════════════════════════════════════════════════════╗
║  BOT 2 v4: US + CRYPTO — Alpaca Paper Trading + CCXT         ║
║  Uses TradingEngine v4 (10-layer analysis)                    ║
║  Places REAL paper trades on Alpaca ($100K fake money)        ║
╚══════════════════════════════════════════════════════════════╝

SETUP: pip install alpaca-trade-api ccxt yfinance pandas numpy requests
RUN:   python bot_us_crypto_v4.py

VIEW YOUR TRADES:
  1. Go to https://app.alpaca.markets/
  2. Login with your account
  3. Click "Paper Trading" in top bar
  4. You'll see all positions, orders, and P&L in real-time!
"""
import sys,os,csv,time,datetime,logging,traceback,json
from pathlib import Path
from zoneinfo import ZoneInfo

from core.us_market_data import fetch_alpaca_bars
from core.config_loader import (
    ALPACA_KEY as CFG_ALPACA_KEY,
    ALPACA_PAPER as CFG_ALPACA_PAPER,
    ALPACA_SECRET as CFG_ALPACA_SECRET,
    AUTO_PAUSE_ONLY,
    CRYPTO_CAPITAL as CFG_CRYPTO_CAPITAL,
    CRYPTO_PAPER_TRADING,
    GITHUB_REPO as CFG_GITHUB_REPO,
    GITHUB_USER as CFG_GITHUB_USER,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    POLYMARKET_PAPER_TRADING,
    US_CAPITAL as CFG_US_CAPITAL,
    US_EXEC_TG_CHAT,
    US_EXEC_TG_TOKEN,
)
from core.us_market_scheduler import is_us_market_open, market_window_label
from marketpulse_runtime import resolve_log_dir, resolve_state_dir
from marketpulse_state import read_bot_state, update_bot_state

# ══════════ CONFIG ══════════
ALPACA_KEY = CFG_ALPACA_KEY
ALPACA_SECRET = CFG_ALPACA_SECRET
ALPACA_PAPER = CFG_ALPACA_PAPER
ALPACA_BASE = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"

BINANCE_KEY = ""
BINANCE_SECRET = ""

# Use the dedicated US execution bot token from config/env.
TELEGRAM_TOKEN   = US_EXEC_TG_TOKEN
TELEGRAM_CHAT_ID = US_EXEC_TG_CHAT
EMAIL_FROM = ""; EMAIL_PASSWORD = ""; EMAIL_TO = ""

US_CAPITAL = CFG_US_CAPITAL        # Use $10K of the $100K paper money
CRYPTO_CAPITAL = CFG_CRYPTO_CAPITAL
MAX_RISK = 0.02
MAX_US_TRADES = 8
MAX_CRYPTO_TRADES = 3
DAILY_LOSS_US = 300
DAILY_LOSS_CRYPTO = 100
MAX_POSITIONS = 6
MAX_NEW_US_TRADES_PER_SCAN = 3
STATE_BOT_ID = "us_v4"
POLYMARKET_ENABLED = True

US_SCAN_INTERVAL = 180
CRYPTO_SCAN_INTERVAL = 300

# Replace SQ with XYZ (delisted), remove problematic symbols
US_WATCHLIST = [
    'AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','AMD',
    'NFLX','COIN','PLTR','SOFI','NIO','MARA','XYZ',
    'SNOW','CRWD','UBER','SHOP','RIVN',
]

CRYPTO_WATCHLIST = [
    'BTC/USDT','ETH/USDT','SOL/USDT','XRP/USDT',
    'DOGE/USDT','ADA/USDT','AVAX/USDT','DOT/USDT',
]

# ══════════ LOGGING ══════════
LOG_DIR = resolve_log_dir(); LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = resolve_state_dir(); STATE_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_STATUS_PATH = STATE_DIR / "us_runtime_status.json"
REPORT_STATUS_PATH = STATE_DIR / "us_report_status.json"
POLYMARKET_WATCHLIST_PATH = STATE_DIR / "polymarket_watchlist.json"
ET = ZoneInfo("America/New_York")
D = datetime.date.today().strftime("%Y-%m-%d")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_DIR/f"uscrp4_{D}.log",encoding='utf-8'),logging.StreamHandler(sys.stdout)])
log = logging.getLogger("USCrypto4")

# ══════════ GITHUB BRIEFING SYNC ══════════
_GITHUB_USER = CFG_GITHUB_USER or "Manav-Deakin-23"
_GITHUB_REPO = CFG_GITHUB_REPO or "marketpulse-bots"
_GITHUB_API  = f"https://api.github.com/repos/{_GITHUB_USER}/{_GITHUB_REPO}/contents/briefings"

def _sync_briefings_from_github():
    """Pull daily_brief.json + fundamental_brief.json + us_weekly_brief.json from GitHub repo."""
    import requests as _req, base64 as _b64
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ["daily_brief.json", "fundamental_brief.json", "us_weekly_brief.json"]:
        try:
            r = _req.get(f"{_GITHUB_API}/{fname}", headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            content = _b64.b64decode(r.json()['content']).decode('utf-8')
            local_p = STATE_DIR / fname
            existing = local_p.read_text(encoding='utf-8') if local_p.exists() else ""
            if content != existing:
                local_p.write_text(content, encoding='utf-8')
                log.info(f"[GITHUB SYNC] {fname} updated from cloud")
        except Exception as _e:
            log.debug(f"[GITHUB SYNC] {fname}: {_e}")

TL = LOG_DIR/f"uscrp4_trades_{D}.csv"
def init_log():
    if not TL.exists():
        with open(TL,'w',newline='',encoding='utf-8') as f:
            csv.writer(f).writerow(['time','market','symbol','action','qty','price','sl','target','score','reasons','order_id','status','pnl'])
def log_t(d):
    with open(TL,'a',newline='',encoding='utf-8') as f: csv.writer(f).writerow(d)

# ══════════ IMPORTS ══════════
HAS_ALPACA = False
try:
    import alpaca_trade_api as tradeapi
    HAS_ALPACA = True
except: log.warning("pip install alpaca-trade-api")

HAS_CCXT = False
try:
    import ccxt
    HAS_CCXT = True
except: log.warning("pip install ccxt")

try:
    from trading_engine import TradingEngine
    HAS_ENGINE = True
except ImportError:
    log.error("trading_engine.py not found — put it in the same folder!")
    HAS_ENGINE = False

from notifier import Notifier

# ══════════ MARKET HOURS ══════════
def is_us_open():
    now = datetime.datetime.now(datetime.timezone.utc)
    return is_us_market_open(now)

# ══════════ BOT ══════════
class USCryptoBot4:
    def __init__(self):
        self.alpaca = None
        self.exchange = None
        self.engine = TradingEngine(capital=US_CAPITAL) if HAS_ENGINE else None
        if self.engine:
            self.engine._ohlcv_fetcher = self._fetch_us_ohlcv
        self.us_positions = {}   # symbol -> {order_id, entry, sl, tgt, qty, side}
        self.polymarket_bets = {}
        self.crypto_signals = {} # symbol -> last_signal_time (duplicate prevention)
        self.us_pnl = 0.0; self.crypto_pnl = 0.0
        self.us_trades = 0; self.crypto_trades = 0
        self.us_wins = 0; self.us_losses = 0
        self.running = False
        self.safe_mode = {"global_pause_new_entries": False, "reason": ""}
        self.crypto_disabled_reason = ""
        self.scheduler_status = {}
        self.performance = {
            "us_equities": {"win_rate": 0.0, "trades": 0, "pnl": 0.0},
            "crypto": {"signals": 0, "pnl": 0.0},
            "polymarket": {"bets": 0, "paper_only": True},
        }
        self.promotion_status = {
            "us_equities": {"eligible_for_live": False, "paper_only": True},
            "crypto": {"eligible_for_live": False, "paper_only": CRYPTO_PAPER_TRADING},
            "polymarket": {"eligible_for_live": False, "paper_only": POLYMARKET_PAPER_TRADING},
        }
        self.notify = Notifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO)
        init_log()
        self._restore_state()

    def _fetch_us_ohlcv(self, symbol, interval='day', days=80):
        if interval != 'day':
            return __import__('pandas').DataFrame()
        try:
            frame = fetch_alpaca_bars(
                symbol,
                ALPACA_KEY,
                ALPACA_SECRET,
                timeframe="1Day",
                days=days,
            )
            if frame is not None and not frame.empty:
                self.scheduler_status['last_us_data_provider'] = 'alpaca_market_data'
                return frame
        except Exception as exc:
            log.debug(f"Alpaca market data fetch failed for {symbol}: {exc}")
        return __import__('pandas').DataFrame()

    def _load_polymarket_watchlist(self):
        if not POLYMARKET_WATCHLIST_PATH.exists():
            return []
        try:
            payload = json.loads(POLYMARKET_WATCHLIST_PATH.read_text(encoding='utf-8'))
        except Exception as exc:
            log.warning(f"Polymarket watchlist load failed: {exc}")
            return []
        if isinstance(payload, dict):
            items = payload.get('bets') or payload.get('watchlist') or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            market_key = str(item.get('market_id') or item.get('symbol_or_market') or item.get('slug') or '').strip()
            if not market_key:
                continue
            cleaned.append(item)
        return cleaned

    def _restore_state(self):
        try:
            state = read_bot_state()
            bot = state.get("bots", {}).get(STATE_BOT_ID, {})
            self.us_positions = dict(bot.get("positions", {}) or {})
            self.polymarket_bets = dict(bot.get("bets", {}) or {})
            self.safe_mode = dict(bot.get("safe_mode", {}) or self.safe_mode)
            self.scheduler_status = dict(bot.get("scheduler_status", {}) or {})
            self.performance.update(bot.get("performance", {}) or {})
            self.promotion_status.update(bot.get("promotion_status", {}) or {})
        except Exception as exc:
            log.debug(f"State restore skipped: {exc}")

    def _health_snapshot(self):
        return {
            'connected': bool(self.alpaca or self.exchange),
            'alpaca_connected': bool(self.alpaca),
            'binance_connected': bool(self.exchange),
            'crypto_disabled_reason': self.crypto_disabled_reason,
            'llm_supervisor': 'available' if OPENAI_API_KEY else 'disabled',
            'llm_model': OPENAI_MODEL if OPENAI_API_KEY else '',
            'auto_pause_only': AUTO_PAUSE_ONLY,
            'market_window': market_window_label(datetime.datetime.now(datetime.timezone.utc)),
        }

    def _write_runtime_status(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        live_positions = self.get_alpaca_positions() if self.alpaca else {}
        position_snapshot = {}
        for symbol, position in (self.us_positions or {}).items():
            live = live_positions.get(symbol, {})
            position_snapshot[symbol] = {
                'side': position.get('side'),
                'qty': position.get('qty'),
                'entry': position.get('entry'),
                'target': position.get('tgt'),
                'stop_loss': position.get('sl'),
                'holding_style': position.get('holding_style', 'intraday'),
                'overnight_allowed': bool(position.get('overnight_allowed')),
                'opened_at': position.get('opened_at', ''),
                'current': live.get('current'),
                'pnl': live.get('pnl'),
                'pnl_pct': live.get('pnl_pct'),
            }
        report_status = {}
        if REPORT_STATUS_PATH.exists():
            try:
                report_status = json.loads(REPORT_STATUS_PATH.read_text(encoding='utf-8'))
            except Exception:
                report_status = {}
        payload = {
            'generated_at': now_utc.isoformat(),
            'alpaca_connected': bool(self.alpaca),
            'binance_connected': bool(self.exchange),
            'crypto_disabled_reason': self.crypto_disabled_reason,
            'safe_mode': self.safe_mode,
            'scheduler_status': self.scheduler_status,
            'performance': self.performance,
            'position_count': len(position_snapshot),
            'bet_count': len(self.polymarket_bets or {}),
            'open_positions': list((self.us_positions or {}).keys()),
            'position_snapshot': position_snapshot,
            'live_positions': live_positions,
            'open_bets': list((self.polymarket_bets or {}).keys()),
            'sessions': {
                'us': {
                    'is_open': is_us_market_open(now_utc),
                    'window': market_window_label(now_utc),
                    'date_et': now_utc.astimezone(ET).date().isoformat(),
                },
                'crypto': {
                    'is_open': True,
                    'window': 'always_on',
                    'enabled': not bool(self.crypto_disabled_reason),
                },
            },
            'report_status': report_status,
        }
        try:
            RUNTIME_STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        except Exception as exc:
            log.debug(f"Runtime status write skipped: {exc}")

    def _sync_state(self):
        update_bot_state(STATE_BOT_ID, {
            'positions': self.us_positions,
            'bets': self.polymarket_bets,
            'signals': [],
            'rejections': [],
            'pnl': round(self.us_pnl + self.crypto_pnl, 2),
            'health': self._health_snapshot(),
            'performance': self.performance,
            'promotion_status': self.promotion_status,
            'scheduler_status': self.scheduler_status,
            'safe_mode': self.safe_mode,
        })
        self._write_runtime_status()

    def _new_entries_paused(self):
        if self.safe_mode.get('global_pause_new_entries'):
            return True
        return (STATE_DIR / 'PAUSE_US_NEW_ENTRIES').exists()

    def _maybe_refresh_supervision(self):
        supervision_pause = False
        super_path = STATE_DIR / "us_supervision.json"
        if super_path.exists():
            try:
                supervision = json.loads(super_path.read_text(encoding='utf-8'))
                if supervision.get('forced_safe_mode'):
                    supervision_pause = True
                    self.safe_mode = {
                        'global_pause_new_entries': True,
                        'reason': ', '.join(supervision.get('source_warnings', [])[:3]) or 'us_supervision',
                    }
                elif not supervision.get('allow_new_entries', True):
                    supervision_pause = True
                    self.safe_mode = {
                        'global_pause_new_entries': True,
                        'reason': 'us_supervision_pause',
                    }
                else:
                    self.safe_mode = {'global_pause_new_entries': False, 'reason': ''}
                self.scheduler_status['last_us_supervision_load'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            except Exception as exc:
                log.debug(f"US supervision refresh skipped: {exc}")
        path = STATE_DIR / "father_opinion.json"
        if not path.exists():
            return
        try:
            opinion = json.loads(path.read_text(encoding='utf-8'))
            safe_mode = opinion.get('us', {}).get('safe_mode', {})
            if safe_mode.get('global_pause_new_entries'):
                if supervision_pause:
                    pass
                elif str(safe_mode.get('reason') or '').strip().lower() == 'brain_risk_off':
                    self.safe_mode = dict(safe_mode)
                else:
                    self.safe_mode = {'global_pause_new_entries': False, 'reason': ''}
            elif not supervision_pause:
                self.safe_mode = {'global_pause_new_entries': False, 'reason': ''}
        except Exception as exc:
            log.debug(f"Supervisor refresh skipped: {exc}")

    # ── CONNECT ALPACA ──
    def connect_alpaca(self):
        if not HAS_ALPACA or not ALPACA_KEY:
            log.warning("Alpaca not available"); return False
        try:
            self.alpaca = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE, api_version='v2')
            acct = self.alpaca.get_account()
            equity = float(acct.equity)
            buying_power = float(acct.buying_power)
            log.info(f"Alpaca connected: ${equity:,.2f} equity | ${buying_power:,.2f} buying power | Paper: {ALPACA_PAPER}")
            self.notify.alert(f"📈 <b>Alpaca Connected</b>\nEquity: ${equity:,.2f}\nBuying Power: ${buying_power:,.2f}\nPaper: {ALPACA_PAPER}")
            return True
        except Exception as e:
            log.error(f"Alpaca failed: {e}")
            self.notify.error_alert("Alpaca Connection Failed", str(e))
            return False

    # ── CONNECT BINANCE ──
    def connect_binance(self):
        if not HAS_CCXT: return False
        try:
            if BINANCE_KEY:
                self.exchange = ccxt.binance({'apiKey':BINANCE_KEY,'secret':BINANCE_SECRET})
            else:
                self.exchange = ccxt.binance()
            self.exchange.load_markets()
            self.crypto_disabled_reason = ""
            log.info(f"Binance: {len(self.exchange.markets)} markets (public data)")
            return True
        except Exception as e:
            msg = str(e)
            if "restricted location" in msg.lower():
                self.crypto_disabled_reason = "binance_restricted_location"
                log.warning("Binance disabled on this host: restricted location")
            else:
                self.crypto_disabled_reason = "binance_connect_failed"
                log.error(f"Binance failed: {e}")
            return False

    # ── LOAD DAILY BRIEF ──
    def _load_daily_brief(self):
        """Read daily_brief.json for high_risk_today and sector_heatmap filters."""
        self.daily_brief: dict = {}
        path = STATE_DIR / "daily_brief.json"
        if not path.exists():
            return
        try:
            self.daily_brief = json.loads(path.read_text('utf-8'))
            heatmap = self.daily_brief.get('sector_heatmap', {})
            self.scheduler_status['last_us_brief_load'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            log.info(f"[BRIEF] Loaded — sector_heatmap keys: {list(heatmap.keys())}")
        except Exception as _e:
            log.warning(f"[BRIEF] Load failed: {_e}")

    def _high_risk_symbols(self):
        """Return symbol blocklist only when the brief field is actually a list."""
        raw = getattr(self, 'daily_brief', {}).get('high_risk_today', [])
        if isinstance(raw, (list, tuple, set)):
            return {str(sym).upper() for sym in raw if sym}
        if isinstance(raw, str):
            return {raw.upper()} if raw else set()
        return set()

    def _load_us_weekly_brief(self):
        self.us_weekly_brief: dict = {}
        path = STATE_DIR / "us_weekly_brief.json"
        if not path.exists():
            return
        try:
            self.us_weekly_brief = json.loads(path.read_text('utf-8'))
            self.scheduler_status['last_us_weekly_brief_load'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            log.info(f"[US RESEARCH] Loaded — weekly candidates: {len(self.us_weekly_brief.get('weekly_candidates', []))}")
        except Exception as exc:
            log.warning(f"[US RESEARCH] Load failed: {exc}")

    def _prioritize_us_signals(self, signals):
        weekly = getattr(self, 'us_weekly_brief', {}).get('weekly_candidates', [])
        rank_map = {}
        for idx, item in enumerate(weekly):
            symbol = str(item.get('symbol', '')).upper()
            if symbol and symbol not in rank_map:
                rank_map[symbol] = idx
        if not rank_map:
            return list(signals)
        return sorted(
            signals,
            key=lambda sig: (
                rank_map.get(str(getattr(sig, 'symbol', '')).upper(), 999),
                -abs(float(getattr(sig, 'total_score', 0) or 0)),
            ),
        )

    def _weekly_focus_rank(self, symbol: str) -> int:
        weekly = getattr(self, 'us_weekly_brief', {}).get('weekly_candidates', [])
        target = str(symbol or '').upper()
        for idx, item in enumerate(weekly):
            if str(item.get('symbol', '')).upper() == target:
                return idx
        return 999

    def _build_us_holding_profile(self, sig, adjustment, now=None):
        current = now or datetime.datetime.now(datetime.timezone.utc)
        rank = self._weekly_focus_rank(getattr(sig, 'symbol', ''))
        confidence = float(getattr(sig, 'confidence', 0) or 0)
        signal_name = str(getattr(sig, 'signal', '')).upper()
        qty_multiplier = float(adjustment.get('qty_multiplier', 1.0) or 1.0)
        swing_candidate = (
            rank <= 3
            or (signal_name == 'STRONG BUY' and confidence >= 65)
            or qty_multiplier >= 0.75
        )
        if swing_candidate:
            return {
                'holding_style': 'swing',
                'planned_hold_days': 3,
                'overnight_allowed': True,
                'target_pnl_pct': 4.0,
                'stop_pnl_pct': -2.0,
                'trailing_arm_pct': 2.0,
                'trailing_floor_pct': 0.75,
                'opened_at': current.isoformat(),
            }
        return {
            'holding_style': 'intraday',
            'planned_hold_days': 0,
            'overnight_allowed': False,
            'target_pnl_pct': 2.5,
            'stop_pnl_pct': -1.5,
            'trailing_arm_pct': 1.5,
            'trailing_floor_pct': 0.5,
            'opened_at': current.isoformat(),
        }

    def _research_trade_adjustment(self, sig, now=None):
        current = now or datetime.datetime.now(datetime.timezone.utc)
        symbol = str(getattr(sig, 'symbol', '')).upper()
        signal_side = str(getattr(sig, 'signal', '')).upper()
        is_buy = signal_side in ('BUY', 'STRONG BUY')
        supervision_path = STATE_DIR / "us_supervision.json"
        if supervision_path.exists():
            try:
                supervision = json.loads(supervision_path.read_text(encoding='utf-8'))
                if symbol in set(supervision.get('blocked_symbols', []) or []):
                    return {'allow': False, 'qty_multiplier': 0.0, 'reason': 'us_supervision_block'}
                if symbol in (supervision.get('size_multipliers', {}) or {}):
                    return {
                        'allow': bool(supervision.get('allow_new_entries', True)),
                        'qty_multiplier': float(supervision['size_multipliers'][symbol]),
                        'reason': 'us_supervision_size',
                    }
                if not supervision.get('allow_new_entries', True):
                    return {'allow': False, 'qty_multiplier': 0.0, 'reason': 'us_supervision_pause'}
            except Exception as exc:
                log.debug(f"US supervision adjustment skipped: {exc}")
        earnings_setups = getattr(self, 'us_weekly_brief', {}).get('earnings_setups', [])
        setup = next((item for item in earnings_setups if str(item.get('symbol', '')).upper() == symbol), None)
        if not setup:
            return {'allow': True, 'qty_multiplier': 1.0, 'reason': ''}

        earnings_date = str(setup.get('earnings_date') or '')
        today = current.date().isoformat()
        pre_bias = str(setup.get('pre_result_bias', 'NONE')).upper()
        result_bias = str(setup.get('result_day_bias', 'NONE')).upper()

        def _bias_opposes(bias: str) -> bool:
            if 'BULLISH' in bias:
                return not is_buy
            if 'BEARISH' in bias:
                return is_buy
            return False

        if earnings_date and earnings_date != today and _bias_opposes(pre_bias):
            return {'allow': False, 'qty_multiplier': 0.0, 'reason': f'pre_result_bias:{pre_bias}'}
        if earnings_date and earnings_date == today and _bias_opposes(result_bias):
            return {'allow': False, 'qty_multiplier': 0.0, 'reason': f'result_day_bias:{result_bias}'}
        if earnings_date and earnings_date == today:
            return {'allow': True, 'qty_multiplier': 0.5, 'reason': f'result_day_bias:{result_bias}'}
        if earnings_date and earnings_date > today and pre_bias in {'BULLISH', 'BEARISH'}:
            return {'allow': True, 'qty_multiplier': 0.75, 'reason': f'pre_result_bias:{pre_bias}'}
        return {'allow': True, 'qty_multiplier': 1.0, 'reason': ''}

    # ── GET ALPACA POSITIONS ──
    def get_alpaca_positions(self):
        """Fetch real positions from Alpaca"""
        if not self.alpaca: return {}
        try:
            positions = self.alpaca.list_positions()
            pos_dict = {}
            for p in positions:
                pos_dict[p.symbol] = {
                    'qty': int(p.qty),
                    'side': p.side,
                    'entry': float(p.avg_entry_price),
                    'current': float(p.current_price),
                    'pnl': float(p.unrealized_pl),
                    'pnl_pct': float(p.unrealized_plpc) * 100,
                }
            return pos_dict
        except: return {}

    # ── PLACE US TRADE ON ALPACA ──
    def place_us_trade(self, sig):
        """Actually place a paper trade on Alpaca"""
        sym = sig.symbol
        side = 'buy' if sig.signal in ('BUY', 'STRONG BUY') else 'sell'
        qty = sig.quantity
        adjustment = self._research_trade_adjustment(sig)
        if not adjustment['allow']:
            log.info(f"  [US RESEARCH BLOCK] {sym}: {adjustment['reason']}")
            return None
        qty = max(1, int(round(qty * adjustment['qty_multiplier'])))

        # Check if already holding this stock
        existing = self.get_alpaca_positions()
        if sym in existing:
            log.info(f"  Already holding {sym} — skip")
            return None

        # Check position count
        if len(existing) >= MAX_POSITIONS:
            log.info(f"  Max {MAX_POSITIONS} positions — skip")
            return None

        log.info(f"  {'🟢 BUY' if side=='buy' else '🔴 SELL'} {sym} x{qty} @ ${sig.price}")
        log.info(f"    Score: {sig.total_score} | Conf: {sig.confidence}% | R:R {sig.risk_reward}")
        log.info(f"    Target: ${sig.target1} | SL: ${sig.stop_loss}")
        log.info(f"    Layers: {sig.layer_scores}")
        log.info(f"    Patterns: {sig.patterns}")

        # Send Telegram alert
        self.notify.trade_opened(sym, side.upper(), sig.price, qty,
            sig.target1, sig.stop_loss, sig.risk_amount, sig.reasons[:4], "$")

        if not self.alpaca:
            log.info("    [NO ALPACA] Signal logged only")
            return None

        try:
            # Place market order
            order = self.alpaca.submit_order(
                symbol=sym,
                qty=qty,
                side=side,
                type='market',
                time_in_force='day'
            )
            order_id = order.id
            order_status = getattr(order, 'status', 'submitted')
            holding_profile = self._build_us_holding_profile(sig, adjustment)
            log.info(f"    ✅ Order placed: {order_id} | status: {order_status}")

            # Track position
            self.us_positions[sym] = {
                'order_id': order_id,
                'side': side,
                'qty': qty,
                'entry': sig.price,
                'sl': sig.stop_loss,
                'tgt': sig.target1,
                'signal': sig.signal,
                'score': sig.total_score,
                **holding_profile,
            }

            log_t([datetime.datetime.now().isoformat(), 'US', sym, side.upper(), qty,
                sig.price, sig.stop_loss, sig.target1, sig.total_score,
                '|'.join(sig.reasons[:3]), order_id, order_status, 0])

            self.us_trades += 1
            return order_id

        except Exception as e:
            log.error(f"    ❌ Order failed: {e}")
            self.notify.error_alert(f"US Order Failed: {sym}", str(e))
            return None

    # ── MONITOR US POSITIONS ──
    def _evaluate_us_exit(self, sym, pos, tracked):
        pnl_pct = pos['pnl_pct']
        pnl = pos['pnl']
        target_pct = float(tracked.get('target_pnl_pct', 2.5) or 2.5)
        stop_pct = float(tracked.get('stop_pnl_pct', -1.5) or -1.5)
        trailing_arm_pct = float(tracked.get('trailing_arm_pct', 1.5) or 1.5)
        trailing_floor_pct = float(tracked.get('trailing_floor_pct', 0.5) or 0.5)
        holding_style = str(tracked.get('holding_style', 'intraday'))
        planned_hold_days = int(tracked.get('planned_hold_days', 0) or 0)
        opened_at = str(tracked.get('opened_at') or '')
        days_open = 0
        if opened_at:
            try:
                opened = datetime.datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=datetime.timezone.utc)
                days_open = max(0, (datetime.datetime.now(datetime.timezone.utc).date() - opened.date()).days)
            except Exception:
                days_open = 0

        if pnl_pct >= target_pct:
            log.info(f"  ðŸŽ¯ TARGET: {sym} +{pnl_pct:.1f}% (${pnl:.2f})")
            self._exit_us(sym, pos['current'], pnl, 'TARGET')
            return {'handled': True}
        if pnl_pct <= stop_pct:
            log.info(f"  ðŸ›‘ STOPLOSS: {sym} {pnl_pct:.1f}% (${pnl:.2f})")
            self._exit_us(sym, pos['current'], pnl, 'STOPLOSS')
            return {'handled': True}
        if pnl_pct >= trailing_arm_pct and tracked.get('trailing') is None:
            tracked['trailing'] = True
            log.info(f"  ðŸ“ˆ {sym}: +{pnl_pct:.1f}% â€” will exit if drops below +{trailing_floor_pct:.1f}%")
            return {'handled': True}
        if tracked.get('trailing') and pnl_pct <= trailing_floor_pct:
            log.info(f"  ðŸ”’ TRAIL EXIT: {sym} +{pnl_pct:.1f}% (${pnl:.2f})")
            self._exit_us(sym, pos['current'], pnl, 'TRAIL')
            return {'handled': True}
        if holding_style == 'swing' and planned_hold_days > 0 and days_open >= planned_hold_days:
            log.info(f"  HOLD EXIT: {sym} after {days_open} day(s) (${pnl:.2f})")
            self._exit_us(sym, pos['current'], pnl, 'TIME_EXIT')
            return {'handled': True}
        return {'handled': holding_style == 'swing'}

    def monitor_us(self):
        """Check Alpaca positions and manage exits"""
        if not self.alpaca: return
        positions = self.get_alpaca_positions()
        if not positions: return

        for sym, pos in positions.items():
            tracked = self.us_positions.get(sym)
            if not tracked: continue  # Position not from our bot
            profile = self._evaluate_us_exit(sym, pos, tracked)
            if profile.get('handled'):
                continue

            pnl_pct = pos['pnl_pct']
            pnl = pos['pnl']

            # ── Target hit (2.5%+) ──
            if pnl_pct >= 2.5:
                log.info(f"  🎯 TARGET: {sym} +{pnl_pct:.1f}% (${pnl:.2f})")
                self._exit_us(sym, pos['current'], pnl, 'TARGET')

            # ── Stop loss hit ──
            elif pnl_pct <= -1.5:
                log.info(f"  🛑 STOPLOSS: {sym} {pnl_pct:.1f}% (${pnl:.2f})")
                self._exit_us(sym, pos['current'], pnl, 'STOPLOSS')

            # ── Trail stop: if up 1.5%, protect breakeven ──
            elif pnl_pct >= 1.5 and tracked.get('trailing') is None:
                tracked['trailing'] = True
                log.info(f"  📈 {sym}: +{pnl_pct:.1f}% — will exit if drops below +0.5%")

    def _exit_us(self, sym, price, pnl, reason):
        """Close a US position on Alpaca"""
        try:
            self.alpaca.close_position(sym)
            log.info(f"    Exit order placed: {sym}")
        except Exception as e:
            log.error(f"    EXIT FAILED {sym}: {e}")
            self.notify.cant_exit(sym, f"Alpaca error: {e}\nClose manually at app.alpaca.markets")
            return

        self.us_pnl += pnl
        if pnl >= 0: self.us_wins += 1
        else: self.us_losses += 1
        self.notify.trade_closed(sym, self.us_positions.get(sym,{}).get('entry',0), price, pnl, reason, "$")
        log_t([datetime.datetime.now().isoformat(), 'US', sym, 'EXIT', '', round(price,2),
            '', '', '', reason, '', 'CLOSED', round(pnl,2)])
        self.us_positions.pop(sym, None)

    def close_intraday_us_positions(self):
        if not self.alpaca:
            return
        positions = self.get_alpaca_positions()
        if not positions:
            return
        for sym, pos in positions.items():
            tracked = self.us_positions.get(sym)
            if not tracked:
                continue
            if tracked.get('overnight_allowed'):
                continue
            self._exit_us(sym, pos['current'], pos['pnl'], 'END_OF_DAY')

    def scan_us(self):
        if not is_us_open():
            return
        if self._new_entries_paused():
            log.warning("US entries paused by safe mode")
            self.monitor_us()
            self._sync_state()
            return

        log.info(f"\n--- US SCAN | Trades:{self.us_trades}/{MAX_US_TRADES} Pos:{len(self.us_positions)}/{MAX_POSITIONS} P&L:${self.us_pnl:.2f} ---")

        if self.us_trades >= MAX_US_TRADES:
            log.info("  Max trades reached"); return
        if self.us_pnl <= -DAILY_LOSS_US:
            self.notify.alert(f"⛔ <b>US LOSS LIMIT</b>\nP&L: ${self.us_pnl:.2f}"); return

        if not self.engine:
            log.error("Trading engine not loaded"); return

        signals = self.engine.scan_watchlist(US_WATCHLIST, verbose=False)
        signals = self._prioritize_us_signals(signals)

        if signals:
            log.info(f"  Found {len(signals)} signals:")
            for s in signals[:5]:
                log.info(f"    {s.signal}: {s.symbol} ${s.price} score:{s.total_score} conf:{s.confidence}%")
                if s.patterns: log.info(f"      Patterns: {s.patterns}")

            placed = 0
            high_risk = self._high_risk_symbols()
            for s in signals:
                sym = s.symbol
                if sym in high_risk:
                    log.info(f"[BRIEF] {sym} in high_risk_today — skipped")
                    continue
                if sym in self.us_positions: continue
                if len(self.us_positions) >= MAX_POSITIONS: break
                if self.us_trades >= MAX_US_TRADES: break
                if placed >= MAX_NEW_US_TRADES_PER_SCAN: break

                self.place_us_trade(s)
                placed += 1
        else:
            log.info("  No signals")

        # Monitor existing positions
        self.monitor_us()
        self.scheduler_status['last_us_scan'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.performance['us_equities']['trades'] = self.us_trades
        self.performance['us_equities']['pnl'] = round(self.us_pnl, 2)
        self.performance['us_equities']['win_rate'] = round(self.us_wins / max(self.us_wins + self.us_losses, 1), 4)
        self._sync_state()

    # ── SCAN CRYPTO ──
    def scan_crypto(self):
        if self.crypto_disabled_reason:
            self.scheduler_status['last_crypto_skip_reason'] = self.crypto_disabled_reason
            self._sync_state()
            return
        if not self.exchange or not self.engine:
            return
        if self._new_entries_paused():
            log.warning("Crypto entries paused by safe mode")
            self._sync_state()
            return

        log.info(f"\n--- CRYPTO SCAN | Trades:{self.crypto_trades}/{MAX_CRYPTO_TRADES} P&L:${self.crypto_pnl:.2f} ---")
        if self.crypto_trades >= MAX_CRYPTO_TRADES: return
        if self.crypto_pnl <= -DAILY_LOSS_CRYPTO: return

        for sym in CRYPTO_WATCHLIST:
            try:
                # Use CCXT for crypto data
                ohlcv = self.exchange.fetch_ohlcv(sym, '1d', limit=60)
                if len(ohlcv) < 20: continue

                c = [x[4] for x in ohlcv]
                price = c[-1]; prev = c[-2]
                chg = ((price-prev)/prev)*100

                # Quick analysis (crypto is more volatile, lower thresholds)
                from trading_engine import calc_rsi, score_rsi, calc_macd, score_macd

                rsi = calc_rsi(np.array(c))
                rs, rr = score_rsi(rsi)
                mv, sv, hv = calc_macd(np.array(c))
                ms, mr = score_macd(mv, sv, hv)

                total = rs + ms
                if chg > 3: total += 3
                elif chg < -3: total -= 3

                if abs(total) < 5: continue

                # Duplicate check (4 hour window)
                now = datetime.datetime.now()
                direction = "BUY" if total > 0 else "SELL"
                key = f"{sym}_{direction}"
                if key in self.crypto_signals:
                    if (now - self.crypto_signals[key]).total_seconds() < 4*3600:
                        continue
                self.crypto_signals[key] = now

                is_buy = total > 0
                sig_type = "BUY" if is_buy else "SELL"

                log.info(f"  CRYPTO {sig_type}: {sym} ${price:.4f} ({chg:+.1f}%) score:{total}")

                self.notify.alert(
                    f"{'🟢' if is_buy else '🔴'} <b>CRYPTO {sig_type}: {sym}</b>\n"
                    f"Price: ${price:.4f} ({chg:+.1f}%)\n"
                    f"RSI: {rsi:.0f} | MACD: {'bullish' if ms>0 else 'bearish'}\n"
                    f"Score: {total}\n"
                    f"<i>Paper trade — not executed on exchange</i>"
                )

                log_t([now.isoformat(), 'CRYPTO', sym, sig_type, '',
                    round(price,4), '', '', total, f"RSI:{rsi:.0f}|MACD:{ms}", '', 'SIGNAL', 0])
                self.crypto_trades += 1
                self.performance['crypto']['signals'] = self.crypto_trades
                self.performance['crypto']['pnl'] = round(self.crypto_pnl, 2)
                time.sleep(0.5)

            except Exception as e:
                log.debug(f"  Crypto err {sym}: {e}")
        self.scheduler_status['last_crypto_scan'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._sync_state()

    def sync_polymarket_snapshot(self):
        if not POLYMARKET_ENABLED:
            return
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        watch_items = self._load_polymarket_watchlist()
        active_keys = set()

        if watch_items:
            for item in watch_items:
                market_key = str(item.get('market_id') or item.get('symbol_or_market') or item.get('slug') or '').strip()
                active_keys.add(market_key)
                existing = self.polymarket_bets.get(market_key, {})
                self.polymarket_bets[market_key] = {
                    'symbol_or_market': market_key,
                    'venue': 'polymarket',
                    'strategy_mode': item.get('strategy_mode', existing.get('strategy_mode', 'event')),
                    'entry_reason': item.get('entry_reason', existing.get('entry_reason', 'copy_tracked_watch')),
                    'confidence': float(item.get('confidence', existing.get('confidence', 0.0)) or 0.0),
                    'risk_budget': float(item.get('risk_budget', existing.get('risk_budget', 0.0)) or 0.0),
                    'opened_at': existing.get('opened_at') or now_iso,
                    'planned_horizon': item.get('planned_horizon', existing.get('planned_horizon', 'event_window')),
                    'overnight_allowed': bool(item.get('overnight_allowed', True)),
                    'brain_override_state': item.get('brain_override_state', existing.get('brain_override_state', 'paper_only')),
                    'news_sensitivity': item.get('news_sensitivity', existing.get('news_sensitivity', 'high')),
                    'invalidated_at': None if not item.get('resolved') else (existing.get('invalidated_at') or now_iso),
                    'side': item.get('side', existing.get('side', 'watch')),
                    'target_price': item.get('target_price', existing.get('target_price')),
                    'current_price': item.get('current_price', existing.get('current_price')),
                    'resolution_date': item.get('resolution_date', existing.get('resolution_date')),
                    'notes': item.get('notes', existing.get('notes', '')),
                }
        else:
            self.polymarket_bets.setdefault(
                'paper_event_watch',
                {
                    'symbol_or_market': 'paper_event_watch',
                    'venue': 'polymarket',
                    'strategy_mode': 'event',
                    'entry_reason': 'research_watch',
                    'confidence': 0.0,
                    'risk_budget': 0.0,
                    'opened_at': now_iso,
                    'planned_horizon': 'event_window',
                    'overnight_allowed': True,
                    'brain_override_state': 'paper_only',
                    'news_sensitivity': 'high',
                    'invalidated_at': None,
                    'side': 'watch',
                    'target_price': None,
                    'current_price': None,
                    'resolution_date': None,
                    'notes': '',
                },
            )
            active_keys.add('paper_event_watch')

        for market_key, bet in list(self.polymarket_bets.items()):
            if market_key in active_keys:
                continue
            if bet.get('invalidated_at'):
                continue
            bet['invalidated_at'] = now_iso

        self.scheduler_status['last_polymarket_sync'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.scheduler_status['last_polymarket_watch_items'] = len(active_keys)
        self.performance['polymarket']['bets'] = len(self.polymarket_bets)
        self._sync_state()

    # ── CLOSE ALL US POSITIONS ──
    def close_all_us(self):
        if not self.alpaca: return
        try:
            self.alpaca.close_all_positions()
            log.info("Closed all Alpaca positions")
            self.notify.alert("📦 <b>All US positions closed</b> (end of day)")
        except Exception as e:
            self.notify.error_alert("Failed to close positions", str(e))

    # ── DAILY RESET ──
    def daily_reset(self):
        self.us_trades = 0; self.crypto_trades = 0
        self.us_wins = 0; self.us_losses = 0
        self.us_pnl = 0.0; self.crypto_pnl = 0.0
        self.crypto_signals.clear()
        if self.engine:
            self.engine.recent_signals.clear()

    def summary(self, report_date=None):
        total = self.us_pnl + self.crypto_pnl
        total_trades = self.us_trades + self.crypto_trades
        label = report_date or datetime.datetime.now(ET).date().isoformat()
        self.notify.daily_summary(label, total_trades, self.us_wins, self.us_losses,
            total, US_CAPITAL + CRYPTO_CAPITAL, "$")

    def _current_us_date(self, now_utc=None):
        current = now_utc or datetime.datetime.now(datetime.timezone.utc)
        return current.astimezone(ET).date()

    def _should_run_us_close_cycle(self, now_utc, last_close_date):
        current_et = now_utc.astimezone(ET)
        if current_et.weekday() >= 5:
            return False
        cutoff = current_et.replace(hour=16, minute=10, second=0, microsecond=0)
        if current_et < cutoff:
            return False
        return last_close_date != current_et.date()

    def _run_us_close_cycle(self, now_utc):
        report_date = now_utc.astimezone(ET).date().isoformat()
        if self.us_positions:
            self.close_intraday_us_positions()
        self.summary(report_date=report_date)
        self.daily_reset()
        self.scheduler_status['last_us_close_cycle'] = now_utc.isoformat()
        self._sync_state()
        log.info(f"US close cycle complete: {report_date}")

    # ── MAIN LOOP ──
    def run(self):
        log.info("="*55 + "\n  US+CRYPTO BOT v4 | 10-Layer Engine\n" + "="*55)
        log.info(f"  US Capital: ${US_CAPITAL} | Crypto: ${CRYPTO_CAPITAL}")
        log.info(f"  Paper Trading: {ALPACA_PAPER}")

        self.notify.startup("US+Crypto Bot v4", True, US_CAPITAL + CRYPTO_CAPITAL, "$")

        self.connect_alpaca()
        _sync_briefings_from_github()
        self._load_daily_brief()
        self._load_us_weekly_brief()
        self.connect_binance()
        self.sync_polymarket_snapshot()

        if not self.engine:
            log.error("Trading engine required! Put trading_engine.py in same folder.")
            return

        # Run initial backtest as a health check only. Live monitoring must not
        # die because a data vendor changed historical column shapes.
        log.info("\nRunning quick backtest on AAPL to validate strategy...")
        try:
            bt = self.engine.backtest("AAPL", days=180, verbose=True)
        except Exception as e:
            bt = None
            log.warning(f"Backtest validation failed (non-fatal): {e}")
            try:
                self.notify.alert(f"Backtest validation skipped\n{e}")
            except Exception:
                pass
        if bt is None:
            log.warning("Backtest validation unavailable - continuing live bot startup")
        elif bt.win_rate < 40:
            log.warning("Backtest win rate below 40% - strategy may need tuning")
            self.notify.alert(f"WARNING\nBacktest win rate: {bt.win_rate}%\nStrategy may need tuning")

        self.running = True
        last_us = 0; last_crypto = 0
        last_us_close_date = None

        try:
            while self.running:
                if Path('STOP').exists():
                    log.warning("STOP file detected — US+Crypto Bot shutting down")
                    self.notify.alert("🛑 STOP file detected — US+Crypto Bot shutting down")
                    sys.exit(0)
                self._maybe_refresh_supervision()
                now_ts = time.time()
                now_utc = datetime.datetime.now(datetime.timezone.utc)

                if self._should_run_us_close_cycle(now_utc, last_us_close_date):
                    self._run_us_close_cycle(now_utc)
                    last_us_close_date = self._current_us_date(now_utc)

                # US scan
                if is_us_open() and (now_ts - last_us) >= US_SCAN_INTERVAL:
                    try: self.scan_us()
                    except Exception as e:
                        log.error(f"US scan error: {e}")
                        self.notify.error_alert("US Scan Error", str(e)[:200])
                    last_us = now_ts

                # Crypto scan (24/7)
                if (now_ts - last_crypto) >= CRYPTO_SCAN_INTERVAL:
                    try: self.scan_crypto()
                    except Exception as e:
                        log.error(f"Crypto scan error: {e}")
                    last_crypto = now_ts

                # Also monitor US positions between scans
                if is_us_open() and self.us_positions:
                    try: self.monitor_us()
                    except: pass
                self._sync_state()

                time.sleep(30)

        except KeyboardInterrupt:
            log.info("\nStopped by user")
            if self.us_positions:
                ans = input("Close all Alpaca positions? (y/n): ").strip().lower()
                if ans == 'y': self.close_all_us()
            self.summary()
        except Exception as e:
            self.notify.error_alert("Bot Crash", f"{e}\n{traceback.format_exc()[:400]}")
            log.error(f"CRASH: {e}")

        self.notify.shutdown("US+Crypto Bot v4", "Normal")


if __name__ == "__main__":
    import numpy as np  # Ensure available
    USCryptoBot4().run()
