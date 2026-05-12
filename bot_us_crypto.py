"""
╔═══════════════════════════════════════════════════════════╗
║  BOT 2: US STOCKS + CRYPTO — Alpaca + CCXT                ║
║  Runs: Cloud server 24/7 (Railway / PythonAnywhere)        ║
║  US: Alpaca (free) | Crypto: CCXT → Binance                ║
║  Alerts: Telegram + Email                                  ║
╚═══════════════════════════════════════════════════════════╝

SETUP: pip install alpaca-trade-api ccxt yfinance pandas requests
RUN:   python bot_us_crypto.py

Alpaca setup (free):
  1. Sign up: https://alpaca.markets/
  2. Get paper trading keys (free, no deposit)
  3. Fill in ALPACA_KEY and ALPACA_SECRET below

Binance setup (free):
  1. Sign up: https://www.binance.com/
  2. Create API key in account settings
  3. Fill in BINANCE_KEY and BINANCE_SECRET below
"""
import sys,os,csv,json,time,datetime,logging,traceback
from zoneinfo import ZoneInfo
from pathlib import Path
from core.config_loader import (
    ALPACA_KEY as CFG_ALPACA_KEY,
    ALPACA_PAPER as CFG_ALPACA_PAPER,
    ALPACA_SECRET as CFG_ALPACA_SECRET,
    US_EXEC_TG_CHAT,
    US_EXEC_TG_TOKEN,
)

# ══════════ CONFIG ══════════

# Alpaca (US stocks) — Get free keys: https://app.alpaca.markets/
ALPACA_KEY = CFG_ALPACA_KEY
ALPACA_SECRET = CFG_ALPACA_SECRET
ALPACA_PAPER = CFG_ALPACA_PAPER       # True=paper trading, False=real money
ALPACA_BASE = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"

# Binance (Crypto) — Get keys: https://www.binance.com/en/my/settings/api-management
BINANCE_KEY = ""
BINANCE_SECRET = ""

# Notifications
TELEGRAM_TOKEN = US_EXEC_TG_TOKEN
TELEGRAM_CHAT_ID = US_EXEC_TG_CHAT
EMAIL_FROM = ""
EMAIL_PASSWORD = ""
EMAIL_TO = ""

# Trading
US_CAPITAL = 5000         # USD for US stocks (can start with $0 on paper)
CRYPTO_CAPITAL = 500      # USDT for crypto
MAX_RISK = 0.02           # 2% per trade
MAX_TRADES_US = 5
MAX_TRADES_CRYPTO = 3
DAILY_LOSS_US = 150       # $150
DAILY_LOSS_CRYPTO = 50    # $50
DRY_RUN = True
US_PAPER_ORDERS = True  # Submit Alpaca paper orders while crypto remains protected by DRY_RUN.

US_SCAN_INTERVAL = 180    # seconds
CRYPTO_SCAN_INTERVAL = 300 # seconds

US_WATCHLIST = [
    'AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','AMD',
    'NFLX','COIN','PLTR','SOFI','NIO','RIVN','MARA','XYZ',
    'SNOW','CRWD','UBER','SHOP',
]

CRYPTO_WATCHLIST = [
    'BTC/USDT','ETH/USDT','SOL/USDT','XRP/USDT',
    'DOGE/USDT','ADA/USDT','AVAX/USDT','DOT/USDT',
]

# ══════════ LOGGING ══════════
LOG_DIR = Path("logs"); LOG_DIR.mkdir(exist_ok=True)
D = datetime.date.today().strftime("%Y-%m-%d")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_DIR/f"uscrp_{D}.log",encoding='utf-8'),logging.StreamHandler(sys.stdout)])
log = logging.getLogger("USCrypto")
TL = LOG_DIR/f"uscrp_trades_{D}.csv"

def init_log():
    if not TL.exists():
        with open(TL,'w',newline='',encoding='utf-8') as f:
            csv.writer(f).writerow(['time','market','symbol','action','qty','price','sl','target','strength','reasons','status','pnl'])

def log_t(d):
    with open(TL,'a',newline='',encoding='utf-8') as f: csv.writer(f).writerow(d)

# ══════════ IMPORTS ══════════
try: import yfinance as yf
except: log.error("pip install yfinance"); sys.exit(1)

HAS_ALPACA = False
try:
    import alpaca_trade_api as tradeapi
    HAS_ALPACA = True
except: log.warning("pip install alpaca-trade-api (needed for US stocks)")

HAS_CCXT = False
try:
    import ccxt
    HAS_CCXT = True
except: log.warning("pip install ccxt (needed for crypto)")

from notifier import Notifier

# ══════════ MARKET HOURS (ET) ══════════
_ET = ZoneInfo("America/New_York")

def is_us_market_open():
    """Check if US market is open (9:30 AM - 4:00 PM ET), DST-aware."""
    now = datetime.datetime.now(_ET)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return 9*60+30 <= mins <= 16*60

def is_us_premarket():
    now = datetime.datetime.now(_ET)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return 4*60 <= mins < 9*60+30  # 4:00–9:30 AM ET extended hours

# ══════════ US STOCK ANALYZER ══════════
def analyze_us(sym):
    try:
        h = yf.Ticker(sym).history(period='60d',interval='1d')
        if h.empty or len(h)<20: return None
        p=float(h['Close'].iloc[-1]); prev=float(h['Close'].iloc[-2])
        chg=((p-prev)/prev)*100; vol=float(h['Volume'].iloc[-1])
        c=[float(x) for x in h['Close'].values]
        v=[float(x) for x in h['Volume'].values]

        rsi=None
        if len(c)>=15:
            d=[c[i]-c[i-1] for i in range(len(c)-14,len(c))]
            ag=sum(x for x in d if x>0)/14; al=sum(-x for x in d if x<0)/14
            rsi=100 if al==0 else 100-100/(1+ag/al)

        sma20=sum(c[-20:])/20 if len(c)>=20 else None

        def ema(data,per):
            if len(data)<per: return None
            k=2/(per+1); e=sum(data[:per])/per
            for x in data[per:]: e=x*k+e*(1-k)
            return e
        e12,e26=ema(c,12),ema(c,26)
        macd=(e12-e26) if (e12 and e26) else None

        avg_v=sum(v[-21:-1])/20 if len(v)>=21 else None
        vs=bool(avg_v and vol>avg_v*1.5)

        s=0; r=[]
        if rsi:
            if rsi<35: s+=3; r.append(f"RSI {rsi:.0f}")
            elif rsi<45: s+=1
            elif rsi>65: s-=3; r.append(f"RSI {rsi:.0f}")
            elif rsi>55: s-=1
        if macd:
            if macd>0: s+=2; r.append("MACD+")
            else: s-=2; r.append("MACD-")
        if sma20:
            if p>sma20: s+=1; r.append(">SMA20")
            else: s-=1
        if vs: s+=(2 if s>0 else -2); r.append("VolSpike")
        if chg>2: s+=2; r.append(f"+{chg:.1f}%")
        elif chg<-2: s-=2; r.append(f"{chg:.1f}%")

        if abs(s)<4: return None
        ib=s>0
        sig=("STRONG BUY" if s>=6 else "BUY") if ib else ("STRONG SELL" if s<=-6 else "SELL")

        n=min(14,len(h)-1)
        if n<2: atr=p*0.02
        else:
            hi=[float(x) for x in h['High'].values[-n:]]
            lo=[float(x) for x in h['Low'].values[-n:]]
            pc=[float(x) for x in h['Close'].values[-(n+1):-1]]
            ln=min(len(hi),len(lo),len(pc))
            at=[max(hi[i]-lo[i],abs(hi[i]-pc[i]),abs(lo[i]-pc[i])) for i in range(ln)]
            atr=sum(at)/len(at) if at else p*0.02

        sl=(p-atr*1.5) if ib else (p+atr*1.5)
        tgt=(p+atr*2.5) if ib else (p-atr*2.5)
        rps=abs(p-sl)
        if rps<=0: return None
        qty=max(1,int(US_CAPITAL*MAX_RISK/rps))
        if qty*p>US_CAPITAL*0.35: qty=max(1,int(US_CAPITAL*0.35/p))

        return dict(symbol=sym,price=round(p,2),change_pct=round(chg,2),signal=sig,
            strength=s,is_buy=ib,entry=round(p,2),stop_loss=round(sl,2),
            target=round(tgt,2),quantity=qty,risk=round(rps*qty,2),reasons=r)
    except: return None

# ══════════ CRYPTO ANALYZER ══════════
def analyze_crypto(sym, exchange):
    try:
        ohlcv = exchange.fetch_ohlcv(sym, '1d', limit=60)
        if len(ohlcv)<20: return None

        c=[x[4] for x in ohlcv]  # close prices
        v=[x[5] for x in ohlcv]  # volumes
        p=c[-1]; prev=c[-2]; chg=((p-prev)/prev)*100

        rsi=None
        if len(c)>=15:
            d=[c[i]-c[i-1] for i in range(len(c)-14,len(c))]
            ag=sum(x for x in d if x>0)/14; al=sum(-x for x in d if x<0)/14
            rsi=100 if al==0 else 100-100/(1+ag/al)

        def ema(data,per):
            if len(data)<per: return None
            k=2/(per+1); e=sum(data[:per])/per
            for x in data[per:]: e=x*k+e*(1-k)
            return e
        e12,e26=ema(c,12),ema(c,26)
        macd=(e12-e26) if (e12 and e26) else None

        s=0; r=[]
        if rsi:
            if rsi<30: s+=3; r.append(f"RSI {rsi:.0f}")
            elif rsi>70: s-=3; r.append(f"RSI {rsi:.0f}")
        if macd:
            if macd>0: s+=2; r.append("MACD+")
            else: s-=2; r.append("MACD-")
        if chg>3: s+=2; r.append(f"+{chg:.1f}%")
        elif chg<-3: s-=2; r.append(f"{chg:.1f}%")

        if abs(s)<3: return None  # Lower threshold for crypto
        ib=s>0
        sig=("STRONG BUY" if s>=5 else "BUY") if ib else ("STRONG SELL" if s<=-5 else "SELL")
        atr=p*0.03  # Crypto is more volatile
        sl=(p-atr*1.5) if ib else (p+atr*1.5)
        tgt=(p+atr*2.0) if ib else (p-atr*2.0)
        rps=abs(p-sl)
        if rps<=0: return None
        qty_usd=min(CRYPTO_CAPITAL*0.3, CRYPTO_CAPITAL*MAX_RISK/(rps/p))

        return dict(symbol=sym,price=round(p,2),change_pct=round(chg,2),signal=sig,
            strength=s,is_buy=ib,entry=round(p,2),stop_loss=round(sl,2),
            target=round(tgt,2),quantity_usd=round(qty_usd,2),risk=round(qty_usd*(rps/p),2),reasons=r)
    except Exception as e:
        log.debug(f"Crypto err {sym}: {e}"); return None

# ══════════ MAIN BOT ══════════
class USCryptoBot:
    def __init__(self):
        self.alpaca = None
        self.exchange = None
        self.us_pos={}; self.crypto_pos={}
        self.us_pnl=0.0; self.crypto_pnl=0.0
        self.us_trades=0; self.crypto_trades=0
        self.us_wins=0; self.us_losses=0; self.crypto_wins=0; self.crypto_losses=0
        self.running=False
        self.notify = Notifier(TELEGRAM_TOKEN,TELEGRAM_CHAT_ID,EMAIL_FROM,EMAIL_PASSWORD,EMAIL_TO)
        init_log()

    def connect_alpaca(self):
        if not HAS_ALPACA or not ALPACA_KEY: return False
        try:
            self.alpaca = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE, api_version='v2')
            acct = self.alpaca.get_account()
            log.info(f"Alpaca: ${float(acct.equity):,.2f} equity | Paper: {ALPACA_PAPER}")
            return True
        except Exception as e:
            log.error(f"Alpaca failed: {e}"); return False

    def connect_binance(self):
        if not HAS_CCXT: return False
        try:
            if BINANCE_KEY:
                self.exchange = ccxt.binance({'apiKey':BINANCE_KEY,'secret':BINANCE_SECRET,
                    'sandbox':DRY_RUN,'options':{'defaultType':'spot'}})
            else:
                self.exchange = ccxt.binance()  # Public data only
            self.exchange.load_markets()
            log.info(f"Binance connected ({len(self.exchange.markets)} markets)")
            return True
        except Exception as e:
            log.error(f"Binance failed: {e}"); return False

    # ── US SCAN ──
    def scan_us(self):
        if not is_us_market_open():
            return
        log.info(f"\n--- US SCAN | Trades:{self.us_trades}/{MAX_TRADES_US} P&L:${self.us_pnl:.2f} ---")
        if self.us_trades>=MAX_TRADES_US or self.us_pnl<=-DAILY_LOSS_US: return

        sigs=[]
        for sym in US_WATCHLIST:
            s=analyze_us(sym)
            if s: sigs.append(s)
            time.sleep(0.3)
        sigs.sort(key=lambda x:abs(x['strength']),reverse=True)

        for s in sigs[:3]:
            log.info(f"  US {s['signal']}: {s['symbol']} ${s['price']} str:{s['strength']}")
            self.notify.trade_opened(s['symbol'],
                "BUY" if s['is_buy'] else "SELL",
                s['price'],s['quantity'],s['target'],s['stop_loss'],s['risk'],s['reasons'],"$")

            if US_PAPER_ORDERS and self.alpaca:
                try:
                    order = self.alpaca.submit_order(
                        symbol=s['symbol'], qty=s['quantity'],
                        side='buy' if s['is_buy'] else 'sell',
                        type='market', time_in_force='day'
                    )
                    log.info(f"    Paper order placed: {s['symbol']} ({getattr(order, 'id', 'no-id')})")
                except Exception as e:
                    self.notify.error_alert(f"US Order Failed: {s['symbol']}", str(e)); continue

            log_t([datetime.datetime.now().isoformat(),'US',s['symbol'],
                'BUY' if s['is_buy'] else 'SELL',s['quantity'],s['price'],
                s['stop_loss'],s['target'],s['strength'],'|'.join(s['reasons']),
                'PAPER' if US_PAPER_ORDERS else ('DRY' if DRY_RUN else 'LIVE'),0])
            self.us_trades+=1
            if self.us_trades>=MAX_TRADES_US: break

    # ── CRYPTO SCAN ──
    def scan_crypto(self):
        if not self.exchange: return
        log.info(f"\n--- CRYPTO SCAN | Trades:{self.crypto_trades}/{MAX_TRADES_CRYPTO} P&L:${self.crypto_pnl:.2f} ---")
        if self.crypto_trades>=MAX_TRADES_CRYPTO or self.crypto_pnl<=-DAILY_LOSS_CRYPTO: return

        sigs=[]
        for sym in CRYPTO_WATCHLIST:
            s=analyze_crypto(sym, self.exchange)
            if s: sigs.append(s)
            time.sleep(0.5)
        sigs.sort(key=lambda x:abs(x['strength']),reverse=True)

        for s in sigs[:2]:
            log.info(f"  CRYPTO {s['signal']}: {s['symbol']} ${s['price']} str:{s['strength']}")
            self.notify.trade_opened(s['symbol'],
                "BUY" if s['is_buy'] else "SELL",
                s['price'],s['quantity_usd'],s['target'],s['stop_loss'],s['risk'],s['reasons'],"$")

            if not DRY_RUN and BINANCE_KEY:
                try:
                    amt = s['quantity_usd']/s['price']
                    if s['is_buy']:
                        self.exchange.create_market_buy_order(s['symbol'], amt)
                    else:
                        self.exchange.create_market_sell_order(s['symbol'], amt)
                except Exception as e:
                    self.notify.error_alert(f"Crypto Order Failed: {s['symbol']}", str(e)); continue

            log_t([datetime.datetime.now().isoformat(),'CRYPTO',s['symbol'],
                'BUY' if s['is_buy'] else 'SELL',s['quantity_usd'],s['price'],
                s['stop_loss'],s['target'],s['strength'],'|'.join(s['reasons']),
                'DRY' if DRY_RUN else 'LIVE',0])
            self.crypto_trades+=1
            if self.crypto_trades>=MAX_TRADES_CRYPTO: break

    def daily_reset(self):
        """Reset counters at midnight UTC"""
        self.us_trades=0; self.crypto_trades=0
        self.us_wins=0; self.us_losses=0; self.crypto_wins=0; self.crypto_losses=0
        self.us_pnl=0.0; self.crypto_pnl=0.0

    def summary(self):
        total_pnl = self.us_pnl + self.crypto_pnl
        total_trades = self.us_trades + self.crypto_trades
        self.notify.daily_summary(D, total_trades, self.us_wins+self.crypto_wins,
            self.us_losses+self.crypto_losses, total_pnl, US_CAPITAL+CRYPTO_CAPITAL, "$")

    # ── MAIN LOOP ──
    def run(self):
        log.info("US+CRYPTO BOT | 24/7 MODE")
        log.info(f"US paper orders enabled: {US_PAPER_ORDERS} | Crypto dry-run: {DRY_RUN}")
        self.notify.startup("US+Crypto Bot", DRY_RUN, US_CAPITAL+CRYPTO_CAPITAL, "$")

        self.connect_alpaca()
        self.connect_binance()

        self.running = True
        last_us_scan = 0
        last_crypto_scan = 0
        last_day = datetime.date.today()

        try:
            while self.running:
                now = time.time()
                today = datetime.date.today()

                # Daily reset
                if today != last_day:
                    self.summary()
                    self.daily_reset()
                    last_day = today
                    log.info(f"New day: {today}")

                # US scan
                if is_us_market_open() and (now - last_us_scan) >= US_SCAN_INTERVAL:
                    try:
                        self.scan_us()
                    except Exception as e:
                        self.notify.error_alert("US Scan Error", str(e)[:200])
                    last_us_scan = now

                # Crypto scan (24/7)
                if (now - last_crypto_scan) >= CRYPTO_SCAN_INTERVAL:
                    try:
                        self.scan_crypto()
                    except Exception as e:
                        self.notify.error_alert("Crypto Scan Error", str(e)[:200])
                    last_crypto_scan = now

                # Sleep
                time.sleep(30)

        except KeyboardInterrupt:
            log.info("Stopped by user")
            self.summary()
        except Exception as e:
            self.notify.error_alert("Bot Crash", f"{e}\n{traceback.format_exc()[:400]}")
            log.error(f"CRASH: {e}")
        self.notify.shutdown("US+Crypto Bot", "Normal")

if __name__=="__main__": USCryptoBot().run()
