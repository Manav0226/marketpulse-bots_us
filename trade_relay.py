"""
╔══════════════════════════════════════════════════════════════╗
║  TRADE RELAY — Claude Analysis → Telegram → Kite Execution   ║
║  Sends formatted trade alerts to your phone instantly          ║
║  Includes one-click Kite order links                           ║
╚══════════════════════════════════════════════════════════════╝

HOW IT WORKS:
  1. You ask Claude for trades in this chat
  2. Claude gives you analysis
  3. You run: python trade_relay.py "BUY RELIANCE 1395 T1:1420 T2:1445 SL:1370"
  4. Instantly appears on Telegram with Kite order link
  5. Tap the link → Kite opens → confirm order → done

  OR run it interactively:
  python trade_relay.py
  (then type trades one by one)

SETUP: pip install requests
"""

import os
import sys
import requests
import datetime
import json
from urllib.parse import quote

# ══════════ CONFIG ══════════
TELEGRAM_TOKEN = os.environ.get("TRADER_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TRADER_TG_CHAT", "")
KITE_API_KEY = os.environ.get("KITE_API_KEY", "")

# ══════════ TELEGRAM ══════════
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=10)
    return resp.status_code == 200

# ══════════ KITE ORDER LINK ══════════
def kite_order_url(symbol, action, qty=1, price=None, exchange="NSE"):
    """Generate a Zerodha Kite one-click order link"""
    # Kite basket order URL format
    order = {
        "tradingsymbol": symbol,
        "exchange": exchange,
        "transaction_type": action.upper(),
        "quantity": qty,
        "order_type": "LIMIT" if price else "MARKET",
    }
    if price:
        order["price"] = price
    
    # Kite publisher button URL
    encoded = quote(json.dumps([order]))
    return f"https://kite.zerodha.com/connect/basket?api_key={KITE_API_KEY}&data={encoded}"

# ══════════ PARSE TRADE ══════════
def parse_trade(text):
    """Parse trade text like: BUY RELIANCE 1395 T1:1420 T2:1445 SL:1370"""
    parts = text.strip().upper().split()
    if len(parts) < 3:
        return None
    
    trade = {
        'action': parts[0],      # BUY or SELL
        'symbol': parts[1],      # RELIANCE, TCS, etc.
        'price': None,
        'target1': None,
        'target2': None,
        'stop_loss': None,
        'qty': 1,
        'option': None,          # CE/PE strike if options
        'reasons': [],
    }
    
    for p in parts[2:]:
        if p.startswith('T1:') or p.startswith('TGT1:'):
            trade['target1'] = float(p.split(':')[1])
        elif p.startswith('T2:') or p.startswith('TGT2:'):
            trade['target2'] = float(p.split(':')[1])
        elif p.startswith('SL:') or p.startswith('STOP:'):
            trade['stop_loss'] = float(p.split(':')[1])
        elif p.startswith('QTY:') or p.startswith('Q:'):
            trade['qty'] = int(p.split(':')[1])
        elif 'CE' in p or 'PE' in p:
            trade['option'] = p
        elif p.replace('.','').isdigit():
            trade['price'] = float(p)
    
    return trade

# ══════════ FORMAT & SEND ══════════
def send_trade_alert(trade_text, source="Claude AI"):
    """Parse, format, and send trade alert to Telegram"""
    trade = parse_trade(trade_text)
    if not trade:
        # Send raw text if can't parse
        send_telegram(f"📋 <b>Trade Note:</b>\n{trade_text}")
        return
    
    action = trade['action']
    symbol = trade['symbol']
    is_buy = action == 'BUY'
    emoji = "🟢" if is_buy else "🔴"
    
    # Build message
    msg = f"{emoji} <b>{action} {symbol}</b>"
    if trade['option']:
        msg += f" {trade['option']}"
    msg += f"\n{'─' * 28}"
    
    if trade['price']:
        msg += f"\n💰 Entry: ₹{trade['price']:,.2f}"
    if trade['target1']:
        msg += f"\n🎯 Target 1: ₹{trade['target1']:,.2f}"
    if trade['target2']:
        msg += f"\n🎯 Target 2: ₹{trade['target2']:,.2f}"
    if trade['stop_loss']:
        msg += f"\n🛑 Stop Loss: ₹{trade['stop_loss']:,.2f}"
    
    # Risk:Reward
    if trade['price'] and trade['target1'] and trade['stop_loss']:
        risk = abs(trade['price'] - trade['stop_loss'])
        reward = abs(trade['target1'] - trade['price'])
        if risk > 0:
            rr = reward / risk
            msg += f"\n📊 R:R = 1:{rr:.1f}"
    
    if trade['qty'] > 1:
        msg += f"\n📦 Qty: {trade['qty']}"
    
    msg += f"\n\n⏰ {datetime.datetime.now().strftime('%H:%M:%S IST')}"
    msg += f"\n📡 Source: {source}"
    
    # Kite order link
    if trade['price']:
        kite_url = kite_order_url(symbol, action, trade['qty'], trade['price'])
        msg += f"\n\n<a href='{kite_url}'>📱 Open in Kite →</a>"
    
    msg += "\n\n⚠️ <i>Verify before executing. Not financial advice.</i>"
    
    success = send_telegram(msg)
    if success:
        print(f"✅ Sent to Telegram: {action} {symbol}")
    else:
        print(f"❌ Failed to send")
    return success

# ══════════ QUICK ALERTS ══════════
def send_market_update(nifty_level, bias, key_note=""):
    """Quick market status update"""
    emoji = "📈" if "bull" in bias.lower() else "📉" if "bear" in bias.lower() else "➡️"
    msg = f"{emoji} <b>MARKET UPDATE</b>\n"
    msg += f"NIFTY: {nifty_level}\n"
    msg += f"Bias: {bias}\n"
    if key_note:
        msg += f"Note: {key_note}"
    send_telegram(msg)

def send_exit_alert(symbol, price, pnl, reason):
    """Send exit/close alert"""
    emoji = "🎯" if pnl > 0 else "🛑"
    msg = f"{emoji} <b>EXIT {symbol}</b>\n"
    msg += f"Price: ₹{price:,.2f}\n"
    msg += f"P&L: {'+'if pnl>0 else ''}₹{pnl:,.2f}\n"
    msg += f"Reason: {reason}"
    send_telegram(msg)

def send_option_trade(index, strike, option_type, action, premium, target, sl, reason):
    """Send option trade alert"""
    emoji = "🟢" if action == "BUY" else "🔴"
    msg = f"{emoji} <b>{action} {index} {strike} {option_type}</b>\n"
    msg += f"{'─' * 28}\n"
    msg += f"💰 Premium: ₹{premium}\n"
    msg += f"🎯 Target: ₹{target}\n"
    msg += f"🛑 SL: ₹{sl}\n"
    msg += f"📋 {reason}\n"
    msg += f"\n⏰ {datetime.datetime.now().strftime('%H:%M:%S')}"
    msg += "\n\n⚠️ <i>Options carry high risk. Verify before trading.</i>"
    send_telegram(msg)

# ══════════ BATCH MODE ══════════
def send_morning_plan(trades_list):
    """Send formatted morning trading plan"""
    msg = "🌅 <b>MORNING TRADING PLAN</b>\n"
    msg += f"📅 {datetime.date.today().strftime('%B %d, %Y')}\n"
    msg += f"{'─' * 28}\n\n"
    
    for i, t in enumerate(trades_list, 1):
        emoji = "🟢" if t.get('action','BUY') == 'BUY' else "🔴"
        msg += f"{emoji} <b>{i}. {t.get('action','BUY')} {t['symbol']}</b>\n"
        if t.get('price'): msg += f"   Entry: ₹{t['price']}"
        if t.get('target'): msg += f" → Target: ₹{t['target']}"
        if t.get('sl'): msg += f" | SL: ₹{t['sl']}"
        if t.get('reason'): msg += f"\n   💡 {t['reason']}"
        msg += "\n\n"
    
    msg += "⚠️ <i>Educational analysis. Verify and use stop losses.</i>"
    send_telegram(msg)

# ══════════ MAIN ══════════
if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Command line mode: python trade_relay.py "BUY RELIANCE 1395 T1:1420 SL:1370"
        trade_text = " ".join(sys.argv[1:])
        send_trade_alert(trade_text)
    else:
        # Interactive mode
        print("╔═══════════════════════════════════════╗")
        print("║  TRADE RELAY — Type trades to send     ║")
        print("║  Format: BUY RELIANCE 1395 T1:1420     ║")
        print("║          SL:1370 QTY:5                  ║")
        print("║  Type 'q' to quit                       ║")
        print("╚═══════════════════════════════════════╝")
        
        while True:
            trade = input("\n📝 Trade: ").strip()
            if trade.lower() in ('q', 'quit', 'exit'):
                break
            if trade:
                send_trade_alert(trade)

        print("\n👋 Done!")
