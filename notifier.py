"""
MarketPulse Notifications — Telegram + Email
Used by both India bot and US/Crypto bot
"""
import smtplib
import requests
import logging
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger("MarketPulse")

class Notifier:
    def __init__(self, telegram_token=None, telegram_chat_id=None,
                 email_from=None, email_password=None, email_to=None,
                 smtp_host="smtp.gmail.com", smtp_port=587):
        self.tg_token = telegram_token
        self.tg_chat_id = telegram_chat_id
        self.email_from = email_from
        self.email_pass = email_password
        self.email_to = email_to
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self._last_tg = 0  # Rate limit: 1 msg/sec

    # ── TELEGRAM ──
    def send_telegram(self, message, silent=False):
        if not self.tg_token or not self.tg_chat_id:
            return False
        elapsed = time.time() - self._last_tg
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        try:
            url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
            # Strip HTML tags entirely — avoids parse errors from < > in trade reasons
            # (e.g. "MACD < Signal" breaks HTML mode, safer to send plain text)
            import re as _re
            plain = _re.sub(r'<[^>]+>', '', str(message))
            resp = requests.post(url, json={
                "chat_id": self.tg_chat_id,
                "text": plain,
                "disable_notification": silent
            }, timeout=10)
            self._last_tg = time.time()
            if resp.status_code == 200:
                return True
            else:
                log.warning(f"Telegram error {resp.status_code}: {resp.text[:100]}")
                return False
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
            return False

    def send_telegram_document(self, file_path, caption=None, silent=False):
        if not self.tg_token or not self.tg_chat_id:
            return False
        elapsed = time.time() - self._last_tg
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        try:
            url = f"https://api.telegram.org/bot{self.tg_token}/sendDocument"
            with open(file_path, "rb") as handle:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": self.tg_chat_id,
                        "caption": caption or "",
                        "disable_notification": silent,
                    },
                    files={"document": handle},
                    timeout=30,
                )
            self._last_tg = time.time()
            if resp.status_code == 200:
                return True
            log.warning(f"Telegram document error {resp.status_code}: {resp.text[:100]}")
            return False
        except Exception as e:
            log.warning(f"Telegram document send failed: {e}")
            return False

    # ── EMAIL ──
    def send_email(self, subject, body):
        if not self.email_from or not self.email_pass or not self.email_to:
            return False
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_from
            msg['To'] = self.email_to
            msg['Subject'] = f"[MarketPulse] {subject}"
            msg.attach(MIMEText(body, 'plain'))
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.email_from, self.email_pass)
                server.send_message(msg)
            return True
        except Exception as e:
            log.warning(f"Email failed: {e}")
            return False

    # ── UNIFIED ALERT ──
    def alert(self, message, subject=None, critical=False, silent=False):
        """Send alert via Telegram + Email (email only for critical)"""
        tg_ok = self.send_telegram(message, silent=silent)
        email_ok = False
        if critical and subject:
            plain = message.replace('<b>','').replace('</b>','').replace('<i>','').replace('</i>','')
            email_ok = self.send_email(subject, plain)
        if not tg_ok and not email_ok:
            log.error(f"ALL NOTIFICATIONS FAILED: {message[:100]}")

    # ── PRE-BUILT ALERTS ──
    def trade_opened(self, sym, action, price, qty, target, sl, risk, reasons, cur="₹"):
        emoji = "🟢 BUY" if action=="BUY" else "🔴 SELL"
        self.alert(
            f"{emoji} <b>{sym}</b>\n"
            f"Price: {cur}{price:.2f} x{qty}\n"
            f"Target: {cur}{target:.2f} | SL: {cur}{sl:.2f}\n"
            f"Risk: {cur}{risk:.2f}\n"
            f"Why: {', '.join(reasons[:3])}"
        )

    def trade_closed(self, sym, entry, exit_price, pnl, reason, cur="₹"):
        emoji = "🎯" if pnl>=0 else "🛑"
        tag = "TARGET" if reason=="TARGET" else "STOPLOSS" if "STOP" in reason else reason
        pnl_pct = abs(exit_price-entry)/max(entry, 0.01)*100
        self.alert(
            f"{emoji} <b>{tag}: {sym}</b>\n"
            f"Entry: {cur}{entry:.2f} → Exit: {cur}{exit_price:.2f}\n"
            f"P&L: {'+' if pnl>=0 else ''}{cur}{pnl:.2f} ({'+' if pnl>=0 else ''}{pnl_pct:.1f}%)",
            subject=f"{tag} {sym}: {'Profit' if pnl>=0 else 'Loss'} {cur}{pnl:.0f}",
            critical=(pnl<0)
        )

    def error_alert(self, title, detail):
        self.alert(
            f"⚠️ <b>ERROR: {title}</b>\n{detail}\nCheck bot immediately.",
            subject=f"ERROR: {title}",
            critical=True
        )

    def daily_summary(self, date, trades, wins, losses, pnl, capital, cur="₹"):
        pnl_pct = pnl/max(capital, 0.01)*100
        self.alert(
            f"📊 <b>DAILY SUMMARY</b>\n"
            f"Date: {date}\n"
            f"Trades: {trades} | Won: {wins} | Lost: {losses}\n"
            f"P&L: {'+' if pnl>=0 else ''}{cur}{pnl:.2f} ({pnl_pct:+.2f}%)\n"
            f"Capital: {cur}{capital+pnl:.2f}",
            subject=f"Daily P&L: {'+' if pnl>=0 else ''}{cur}{pnl:.0f} ({pnl_pct:+.1f}%)",
            critical=True
        )

    def startup(self, bot_name, mode, capital, cur="₹"):
        self.alert(
            f"🚀 <b>{bot_name} STARTED</b>\n"
            f"Mode: {'DRY RUN' if mode else 'LIVE'}\n"
            f"Capital: {cur}{capital:,.0f}",
            silent=True
        )

    def shutdown(self, bot_name, reason="Normal"):
        self.alert(
            f"🔌 <b>{bot_name} STOPPED</b>\nReason: {reason}",
            subject=f"{bot_name} stopped: {reason}",
            critical=(reason!="Normal")
        )

    def margin_warning(self, available, needed, cur="₹"):
        self.alert(
            f"⚠️ <b>LOW MARGIN</b>\n"
            f"Available: {cur}{available:.0f}\n"
            f"Needed: {cur}{needed:.0f}\n"
            f"Top up or reduce position size.",
            subject="Low Margin Warning",
            critical=True
        )

    def price_error(self, sym, detail):
        self.alert(f"⚠️ Price error: <b>{sym}</b>\n{detail}")

    def connection_lost(self, broker, detail):
        self.alert(
            f"🔌 <b>CONNECTION LOST: {broker}</b>\n{detail}\nAttempting reconnect...",
            subject=f"Connection Lost: {broker}",
            critical=True
        )

    def cant_exit(self, sym, detail):
        self.alert(
            f"🚨 <b>CANNOT EXIT: {sym}</b>\n{detail}\n<b>MANUAL ACTION REQUIRED!</b>",
            subject=f"URGENT: Cannot exit {sym}",
            critical=True
        )
