"""
MarketPulse Vault Writer — Layer 3
=====================================
Generates structured markdown notes for your Obsidian vault AND optionally
pushes to Notion. Run at EOD or call from inside your bot after market close.

What it writes:
  vault/sessions/    ← one note per trading day (auto-written by bot)
  vault/bugs/        ← one note per bug (you call write_bug() when discovering one)
  vault/bots/        ← one note per bot version (written once per version bump)
  vault/index.md     ← auto-updated master index with backlinks

Obsidian backlinks work automatically because notes use [[WikiLinks]] syntax.

Usage:
    # From your bot (call at EOD):
    from vault_writer import write_session
    write_session(session_data)

    # CLI — write a session from a JSON file:
    python vault_writer.py --session session_2026-04-12.json

    # CLI — write a new bug entry:
    python vault_writer.py --bug

    # CLI — rebuild the index:
    python vault_writer.py --reindex
"""

import os, sys, json, datetime, argparse, textwrap, re
import requests
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────
VAULT_DIR   = Path(os.environ.get("VAULT_DIR",   r"D:\MarketPulseVault"))
# MarketPulse Monitor bot (separate from main trading bot)
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_VAULT_PAGE_ID = os.environ.get("NOTION_VAULT_PAGE_ID",
                                      "3301ce6e-db97-81fe-a1bf-f711c34d7a7e")
MONITOR_TG_TOKEN = os.environ.get("MONITOR_TG_TOKEN", "")
MONITOR_TG_CHAT  = os.environ.get("MONITOR_TG_CHAT",  "")

# Subfolder layout inside the vault
SESSIONS_DIR = VAULT_DIR / "sessions"
BUGS_DIR     = VAULT_DIR / "bugs"
BOTS_DIR     = VAULT_DIR / "bots"
LESSONS_DIR  = VAULT_DIR / "lessons"
ROADMAP_DIR  = VAULT_DIR / "roadmap"

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _ensure_dirs():
    for d in [SESSIONS_DIR, BUGS_DIR, BOTS_DIR, LESSONS_DIR, ROADMAP_DIR]:
        d.mkdir(parents=True, exist_ok=True)

def _write(path: Path, content: str):
    path.write_text(content, encoding="utf-8")
    print(f"[Vault] Written: {path}")

def _slug(text: str) -> str:
    """Convert text to filename-safe slug."""
    return re.sub(r"[^a-z0-9_\-]", "_", text.lower().strip())[:60]

def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")

def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ══════════════════════════════════════════════════════════════════════════════
# SESSION NOTE
# ══════════════════════════════════════════════════════════════════════════════

def write_session(data: dict) -> Path:
    """
    Write a session note to vault/sessions/YYYY-MM-DD.md

    data keys (all optional except date):
        date            str  "2026-04-12"
        bot             str  "bot_india_v5"
        trades          list of dicts: {symbol, direction, entry, exit, pnl, outcome}
        total_pnl       float
        win_rate        float  0.0–1.0
        signals_total   int
        signals_traded  int
        nifty_move_pct  float
        errors          list of str
        notes           str    free-form notes
        capital         float  end-of-day capital
        bugs_found      list of str  (becomes [[WikiLinks]] to bugs/ folder)
    """
    _ensure_dirs()
    date  = data.get("date", _today())
    bot   = data.get("bot", "unknown")
    pnl   = data.get("total_pnl", 0.0)
    wr    = data.get("win_rate", 0.0)
    cap   = data.get("capital", 0.0)
    trades = data.get("trades", [])
    errors = data.get("errors", [])
    notes  = data.get("notes", "")
    bugs   = data.get("bugs_found", [])
    nifty  = data.get("nifty_move_pct", 0.0)
    sigs_total   = data.get("signals_total", 0)
    sigs_traded  = data.get("signals_traded", 0)

    pnl_icon = "🟢" if pnl >= 0 else "🔴"
    outcome  = "profitable" if pnl >= 0 else "loss"

    # Trade table rows
    trade_rows = ""
    for t in trades:
        sym   = t.get("symbol", "?")
        dirn  = t.get("direction", "?")
        entry = t.get("entry", 0)
        exit_ = t.get("exit", 0)
        tp    = t.get("pnl", 0)
        res   = t.get("outcome", "?")   # WIN / LOSS / BE / PARTIAL
        res_icon = {"WIN":"✅","LOSS":"❌","BE":"➖","PARTIAL":"⚡"}.get(res, "❓")
        trade_rows += f"| {sym} | {dirn} | {entry:.2f} | {exit_:.2f} | ₹{tp:+.0f} | {res_icon} {res} |\n"

    # Error list
    error_lines = "\n".join(f"- `{e}`" for e in errors) if errors else "_None logged_"

    # Bug backlinks
    bug_links = ""
    if bugs:
        bug_links = "\n\n## 🐛 Bugs discovered\n"
        for b in bugs:
            slug = _slug(b)
            bug_links += f"- [[bugs/{slug}|{b}]]\n"

    # Pre-compute all formatted values — no backslashes inside f-string expressions
    tags = ["session", f"bot/{bot}", f"date/{date}",
            "profitable" if pnl >= 0 else "loss"]
    tags_str     = ", ".join(tags)
    trade_section = trade_rows if trade_rows else "| — | — | — | — | — | No trades |"
    pnl_str      = f"{pnl:+.2f}"
    wr_str       = f"{wr:.0%}"
    wr_pct_str   = f"{wr:.2%}"
    cap_str      = f"{cap:,.0f}"
    nifty_str    = f"{nifty:+.2f}"

    content = f"""---
date: {date}
bot: "[[bots/{bot}]]"
pnl: {pnl:.2f}
win_rate: {wr_pct_str}
capital_eod: {cap:.2f}
nifty_move_pct: {nifty:.2f}
tags: [{tags_str}]
created: {_now()}
---

# {pnl_icon} Session — {date}

> **Bot:** [[bots/{bot}]] | **P&L:** Rs.{pnl_str} | **Win rate:** {wr_str} | **Capital EOD:** Rs.{cap_str}

## Overview

| Metric | Value |
|---|---|
| Date | {date} |
| Bot | [[bots/{bot}]] |
| Net P&L | Rs.{pnl_str} |
| Win rate | {wr_str} |
| Signals scanned | {sigs_total} |
| Signals traded | {sigs_traded} |
| NIFTY move | {nifty_str}% |
| Capital EOD | Rs.{cap_str} |

## Trades

| Symbol | Dir | Entry | Exit | P&L | Result |
|---|---|---|---|---|---|
{trade_section}

## ⚠️ Errors logged

{error_lines}
{bug_links}

## 📝 Notes

{notes or "_No notes for this session._"}

---
*Auto-generated by vault_writer.py at {_now()}*
"""

    path = SESSIONS_DIR / f"{date}.md"
    _write(path, content)
    _update_index()
    return path


# ══════════════════════════════════════════════════════════════════════════════
# BUG NOTE
# ══════════════════════════════════════════════════════════════════════════════

def write_bug(data: dict) -> Path:
    """
    Write a bug note to vault/bugs/<slug>.md

    data keys:
        title       str   short name, e.g. "FNO T2 None on expiry day"
        severity    str   CRITICAL / HIGH / MEDIUM / MINOR
        bot         str   "bot_fno_v1"
        session     str   "2026-04-12"  (links to session note)
        symptom     str   what you observed
        root_cause  str   why it happened
        fix         str   what was changed
        status      str   OPEN / FIXED / WONTFIX
        lesson      str   one-line takeaway
    """
    _ensure_dirs()
    title      = data.get("title", "Unnamed bug")
    severity   = data.get("severity", "MEDIUM")
    bot        = data.get("bot", "unknown")
    session    = data.get("session", _today())
    symptom    = data.get("symptom", "")
    root_cause = data.get("root_cause", "")
    fix        = data.get("fix", "")
    status     = data.get("status", "OPEN")
    lesson     = data.get("lesson", "")

    sev_icon = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","MINOR":"⚪"}.get(severity,"❓")
    status_icon = {"OPEN":"🔓","FIXED":"✅","WONTFIX":"🚫"}.get(status,"❓")
    slug = _slug(title)

    content = f"""---
title: "{title}"
severity: {severity}
bot: "[[bots/{bot}]]"
session: "[[sessions/{session}]]"
status: {status}
created: {_now()}
tags: [bug, severity/{severity.lower()}, bot/{bot}, status/{status.lower()}]
---

# {sev_icon} {title}

> **Severity:** {sev_icon} {severity} | **Status:** {status_icon} {status} | **Bot:** [[bots/{bot}]]

## Discovered in
[[sessions/{session}]]

## 🔍 Symptom
{symptom or "_No symptom description provided._"}

## 🧠 Root cause
{root_cause or "_Root cause not yet identified._"}

## 🔧 Fix applied
{fix or "_Fix not yet applied._"}

## 📚 Lesson
> {lesson or "_No lesson extracted yet._"}

---
*Written by vault_writer.py at {_now()}*
"""

    path = BUGS_DIR / f"{slug}.md"
    _write(path, content)
    _update_index()
    return path


# ══════════════════════════════════════════════════════════════════════════════
# BOT VERSION NOTE
# ══════════════════════════════════════════════════════════════════════════════

def write_bot_version(data: dict) -> Path:
    """
    Write a bot version snapshot to vault/bots/<name>.md

    data keys:
        name        str   "bot_india_v5"
        label       str   "India Equity Bot v5"
        version     str   "v5"
        file        str   "bot_india_v5.py"
        created     str   date
        status      str   ACTIVE / RETIRED / DEVELOPMENT
        description str
        changelog   list  of str
        features    list  of str
        known_bugs  list  of str (becomes [[WikiLinks]])
        capital     float starting capital
    """
    _ensure_dirs()
    name        = data.get("name", "unknown_bot")
    label       = data.get("label", name)
    version     = data.get("version", "v?")
    file_       = data.get("file", f"{name}.py")
    created     = data.get("created", _today())
    status      = data.get("status", "ACTIVE")
    description = data.get("description", "")
    changelog   = data.get("changelog", [])
    features    = data.get("features", [])
    known_bugs  = data.get("known_bugs", [])
    capital     = data.get("capital", 0.0)

    status_icon = {"ACTIVE":"🟢","RETIRED":"⛔","DEVELOPMENT":"🔧"}.get(status,"❓")

    changelog_md = "\n".join(f"- {c}" for c in changelog) if changelog else "_No changelog._"
    features_md  = "\n".join(f"- {f}" for f in features)  if features  else "_No features listed._"

    bug_links = ""
    if known_bugs:
        bug_links = "\n".join(f"- [[bugs/{_slug(b)}|{b}]]" for b in known_bugs)
    else:
        bug_links = "_None known._"

    content = f"""---
name: "{name}"
label: "{label}"
version: "{version}"
file: "{file_}"
status: {status}
created: {created}
capital: {capital:.2f}
tags: [bot, version/{version}, status/{status.lower()}]
---

# {status_icon} {label}

> **Version:** {version} | **Status:** {status_icon} {status} | **File:** `{file_}`

## Description
{description or "_No description provided._"}

## Features
{features_md}

## Changelog
{changelog_md}

## Known bugs
{bug_links}

## Sessions
*Obsidian will auto-backlink sessions that reference [[bots/{name}]]*

---
*Written by vault_writer.py at {_now()}*
"""

    path = BOTS_DIR / f"{name}.md"
    _write(path, content)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# LESSON NOTE
# ══════════════════════════════════════════════════════════════════════════════

def write_lesson(data: dict) -> Path:
    """
    vault/lessons/<slug>.md — distilled insight.
    data keys: title, category, lesson, context, session, bot
    """
    _ensure_dirs()
    title    = data.get("title", "Unnamed lesson")
    category = data.get("category", "General")  # Data / Architecture / Trading / Code
    lesson   = data.get("lesson", "")
    context  = data.get("context", "")
    session  = data.get("session", _today())
    bot      = data.get("bot", "")
    slug = _slug(title)

    content = f"""---
title: "{title}"
category: "{category}"
session: "[[sessions/{session}]]"
bot: {"[[bots/" + bot + "]]" if bot else "N/A"}
created: {_now()}
tags: [lesson, category/{_slug(category)}]
---

# 📚 {title}

> **Category:** {category} | **Session:** [[sessions/{session}]]

## Insight
{lesson or "_No insight recorded._"}

## Context
{context or "_No context provided._"}

---
*Written by vault_writer.py at {_now()}*
"""

    path = LESSONS_DIR / f"{slug}.md"
    _write(path, content)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# MASTER INDEX
# ══════════════════════════════════════════════════════════════════════════════

def _update_index():
    """Rebuild vault/index.md with links to all notes."""
    sessions = sorted(SESSIONS_DIR.glob("*.md"), reverse=True)
    bugs_open   = []
    bugs_fixed  = []
    for bf in sorted(BUGS_DIR.glob("*.md")):
        content = bf.read_text(encoding="utf-8", errors="replace")
        if "status: FIXED" in content or "status: WONTFIX" in content:
            bugs_fixed.append(bf)
        else:
            bugs_open.append(bf)

    bots     = sorted(BOTS_DIR.glob("*.md"))
    lessons  = sorted(LESSONS_DIR.glob("*.md"), reverse=True)

    def _link(path: Path, folder: str) -> str:
        stem = path.stem
        # Try to extract a title from frontmatter
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
        label = m.group(1) if m else stem
        return f"- [[{folder}/{stem}|{label}]]"

    session_links = "\n".join(_link(p, "sessions") for p in sessions[:20])
    bug_open_links = "\n".join(_link(p, "bugs") for p in bugs_open)
    bug_fixed_links = "\n".join(_link(p, "bugs") for p in bugs_fixed)
    bot_links    = "\n".join(_link(p, "bots")    for p in bots)
    lesson_links = "\n".join(_link(p, "lessons") for p in lessons[:20])

    content = f"""---
created: {_now()}
tags: [index]
---

# 🧠 MarketPulse Knowledge Vault

> Auto-generated index. Updated after every session.

## 🤖 Bots
{bot_links or "_No bots documented yet._"}

## 📅 Sessions (recent 20)
{session_links or "_No sessions yet._"}

## 🐛 Open bugs ({len(bugs_open)})
{bug_open_links or "_No open bugs._"}

## ✅ Fixed bugs ({len(bugs_fixed)})
{bug_fixed_links or "_None fixed yet._"}

## 📚 Lessons (recent 20)
{lesson_links or "_No lessons yet._"}

---
*Last rebuilt: {_now()}*
"""

    path = VAULT_DIR / "index.md"
    _write(path, content)


# ══════════════════════════════════════════════════════════════════════════════
# NOTION SYNC (optional)
# ══════════════════════════════════════════════════════════════════════════════

def tg_monitor(msg: str, dry_run=False):
    """Send to MarketPulse Monitor bot (not the main trading bot)."""
    if dry_run:
        print(f"\n[MONITOR TELEGRAM]\n{msg}\n")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{MONITOR_TG_TOKEN}/sendMessage",
            json={"chat_id": MONITOR_TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Monitor Telegram failed: {e}")

def _notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

def _md_to_notion_blocks(text: str) -> list:
    """Convert simple markdown to Notion block format (headings, bullets, paragraphs)."""
    blocks = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("## "):
            blocks.append({"object":"block","type":"heading_2",
                           "heading_2":{"rich_text":[{"type":"text","text":{"content":line[3:]}}]}})
        elif line.startswith("# "):
            blocks.append({"object":"block","type":"heading_1",
                           "heading_1":{"rich_text":[{"type":"text","text":{"content":line[2:]}}]}})
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append({"object":"block","type":"bulleted_list_item",
                           "bulleted_list_item":{"rich_text":[{"type":"text","text":{"content":line[2:]}}]}})
        else:
            blocks.append({"object":"block","type":"paragraph",
                           "paragraph":{"rich_text":[{"type":"text","text":{"content":line}}]}})
    return blocks

def push_session_to_notion(data: dict) -> str | None:
    """Push a session summary to Notion under the vault page. Returns page URL or None."""
    if not NOTION_TOKEN:
        print("[Vault] NOTION_TOKEN not set — skipping Notion sync")
        return None

    date    = data.get("date", _today())
    pnl     = data.get("total_pnl", 0.0)
    bot     = data.get("bot", "unknown")
    pnl_icon = "🟢" if pnl >= 0 else "🔴"
    title   = f"{pnl_icon} Session {date} — ₹{pnl:+.0f}"

    error_lines = "\n".join("- " + e for e in data.get("errors", [])) or "None"
    notion_notes = data.get("notes", "No notes.")
    win_rate_pct = f"{data.get('win_rate', 0):.0%}"
    capital_fmt  = f"{data.get('capital', 0):,.0f}"
    nifty_fmt    = f"{data.get('nifty_move_pct', 0):+.2f}"

    body_md = f"""## Overview
Date: {date}
Bot: {bot}
Net P&L: Rs.{pnl:+.2f}
Win rate: {win_rate_pct}
Capital EOD: Rs.{capital_fmt}
NIFTY move: {nifty_fmt}%

## Errors
{error_lines}

## Notes
{notion_notes}
"""

    payload = {
        "parent": {"page_id": NOTION_VAULT_PAGE_ID},
        "properties": {
            "title": {"title": [{"type":"text","text":{"content": title}}]}
        },
        "children": _md_to_notion_blocks(body_md),
    }

    try:
        r = requests.post("https://api.notion.com/v1/pages",
                          headers=_notion_headers(),
                          json=payload, timeout=15)
        r.raise_for_status()
        url = r.json().get("url", "")
        print(f"[Vault] Notion page created: {url}")
        return url
    except Exception as e:
        print(f"[Vault] Notion push failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SCAFFOLD — write initial bot version notes and known bugs
# ══════════════════════════════════════════════════════════════════════════════

def scaffold_vault():
    """One-time setup: write bot notes and major known bugs from history."""
    print("[Vault] Scaffolding initial vault structure...")

    # India equity bot
    write_bot_version({
        "name": "bot_india_v5",
        "label": "India Equity Bot v5",
        "version": "v5",
        "file": "bot_india_v5.py",
        "created": "2026-01-01",
        "status": "ACTIVE",
        "description": "Primary NSE intraday equity bot. Runs locally on Windows laptop. "
                       "Targets 8–15 liquid NSE stocks. Uses pivot-based signals with ATR targets.",
        "capital": 100000.0,
        "features": [
            "Pivot-based directional signals with ATR targets (T1=1.0×, T2=1.8×, SL=0.8×)",
            "Sentiment filter: NIFTY >2.5% move hard-blocks trading",
            "Buffered LIMIT exits with fill verification thread",
            "Capital compounding via briefings/capital.json",
            "Telegram alerts + manual /clear SYMBOL command",
            "MAX_TRADES=8, MAX_POSITIONS=5",
            "Re-entry blocked after SL hit (index_sl_today)",
            "VIX informational warning at >28",
        ],
        "changelog": [
            "v5: Fixed T2=None bug with ATR fallback",
            "v5: Fixed re-entry loop after SL hit",
            "v5: Switched to buffered LIMIT exits (Zerodha blocks MARKET)",
            "v5: Added fill verification thread (_verify_fill)",
            "v5: Fixed ghost positions (pos added before fill confirm)",
            "v5: Fixed circuit breaker over-scanning",
            "v5: Fixed Excel P&L corruption with float() casts",
            "v4: Added sentiment filter (NIFTY 2.5% hard block)",
            "v3: Added capital compounding",
            "v2: Initial pivot signal engine",
        ],
        "known_bugs": [
            "CSV P&L column shift bug",
            "Stale NIFTY index levels on scan failure",
        ],
    })

    # FNO bot
    write_bot_version({
        "name": "bot_fno_v1",
        "label": "FNO Bot v1",
        "version": "v1",
        "file": "bot_fno_v1.py",
        "created": "2026-04-01",
        "status": "DEVELOPMENT",
        "description": "NSE index options bot. Trades NIFTY/BANKNIFTY calls and puts. "
                       "Uses Black-Scholes Greeks, delta/theta filters, and pivot-based direction.",
        "capital": 50000.0,
        "features": [
            "Black-Scholes Greeks (delta, theta, vega)",
            "IV sweet spot tracking",
            "Expiry day cutoff at 12:00",
            "3-loss daily stop",
            "Partial exit (50%) at T1",
            "Adaptive learning: IV and hour performance",
        ],
        "known_bugs": [
            "FNO fill verify failed on live trading day",
            "FNO instrument symbol lookup format mismatch",
            "FNO half qty floor missing causing silent zero-lot exits",
        ],
    })

    # Write known bugs
    write_bug({
        "title": "FNO fill verify failed on live trading day",
        "severity": "CRITICAL",
        "bot": "bot_fno_v1",
        "session": "2026-04-12",
        "symptom": "All orders placed but none confirmed as filled. Bot tracked positions that didn't exist.",
        "root_cause": "Fill verification thread was not started on bot init, or instrument token lookup failed causing order to be placed with wrong token.",
        "fix": "Verify _verify_fill thread is started in __init__. Add instrument token validation at startup with assertion.",
        "status": "OPEN",
        "lesson": "Never assume a limit order filled. Always poll order history before updating self.pos.",
    })

    write_bug({
        "title": "FNO instrument symbol lookup format mismatch",
        "severity": "HIGH",
        "bot": "bot_fno_v1",
        "session": "2026-04-12",
        "symptom": "Order rejected by Zerodha with InputException: Invalid instrument.",
        "root_cause": "Bot was constructing symbol string manually (e.g. 'NIFTY2641223000CE') instead of looking up actual trading_symbol from NFO instrument list.",
        "fix": "Use kite.instruments('NFO') at startup and search for the correct trading_symbol by name/expiry/strike/option_type.",
        "status": "OPEN",
        "lesson": "Never construct NSE option symbol strings manually. Zerodha format changes. Always search the instrument list.",
    })

    write_bug({
        "title": "CSV P&L column shift bug",
        "severity": "MEDIUM",
        "bot": "bot_india_v5",
        "session": "2026-03-15",
        "symptom": "P&L values appear in wrong columns in MarketPulse_TradeLog.xlsx.",
        "root_cause": "datetime objects written to CSV without float() cast cause column misalignment when re-read.",
        "fix": "Cast all P&L and datetime values with float() and str() before writing to CSV.",
        "status": "FIXED",
        "lesson": "Always cast types explicitly before writing to CSV. pandas infers types on read which can shift columns.",
    })

    write_bug({
        "title": "Zerodha blocks plain MARKET orders via API",
        "severity": "CRITICAL",
        "bot": "bot_india_v5",
        "session": "2026-03-20",
        "symptom": "All exit orders fail immediately with API error.",
        "root_cause": "Zerodha's API policy blocks plain MARKET orders. Requires LIMIT with market protection or buffered LIMIT.",
        "fix": "All exits now use LIMIT at 0.5% offset from LTP. Background _verify_fill thread confirms fills.",
        "status": "FIXED",
        "lesson": "On Zerodha, always use buffered LIMIT for exits. MARKET order type is blocked at API level.",
    })

    # Write a lesson
    write_lesson({
        "title": "Always verify fills before updating position state",
        "category": "Architecture",
        "lesson": "Never add to self.pos immediately after placing an order. Limit orders may not fill. "
                  "Always use a background thread that polls order history and only updates state on confirmed fill.",
        "context": "Ghost positions caused by tracking unconfirmed limit orders led to double-entry and wrong P&L.",
        "session": "2026-03-20",
        "bot": "bot_india_v5",
    })

    write_lesson({
        "title": "yfinance is for history only, Kite LTP for live prices",
        "category": "Data",
        "lesson": "Use kite.ltp() for any live price decision. yfinance has 15-min delay and random failures. "
                  "Never use yfinance for intraday entry/exit decisions.",
        "context": "Early bot versions used yfinance for live prices causing stale entries.",
        "session": "2026-02-10",
        "bot": "bot_india_v5",
    })

    write_lesson({
        "title": "Pivot levels must be directionally validated before use",
        "category": "Trading",
        "lesson": "nearest_sup must always be genuinely below current price. nearest_res always above. "
                  "Without hard safety checks the signal direction can be inverted.",
        "context": "_index_fno_suggestion was using hardcoded pivots regardless of price position.",
        "session": "2026-04-08",
        "bot": "bot_fno_v1",
    })

    _update_index()
    print("[Vault] Scaffold complete.")
    print(f"[Vault] Open {VAULT_DIR} as your Obsidian vault.")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MarketPulse Vault Writer")
    parser.add_argument("--scaffold",  action="store_true",
                        help="One-time setup: create vault structure with all known history")
    parser.add_argument("--session",   metavar="FILE",
                        help="Write a session note from JSON file")
    parser.add_argument("--bug",       action="store_true",
                        help="Interactively add a new bug entry")
    parser.add_argument("--reindex",   action="store_true",
                        help="Rebuild the index.md")
    parser.add_argument("--vault-dir", help="Override VAULT_DIR")
    args = parser.parse_args()

    global VAULT_DIR, SESSIONS_DIR, BUGS_DIR, BOTS_DIR, LESSONS_DIR, ROADMAP_DIR
    if args.vault_dir:
        VAULT_DIR = Path(args.vault_dir)
        SESSIONS_DIR = VAULT_DIR / "sessions"
        BUGS_DIR     = VAULT_DIR / "bugs"
        BOTS_DIR     = VAULT_DIR / "bots"
        LESSONS_DIR  = VAULT_DIR / "lessons"
        ROADMAP_DIR  = VAULT_DIR / "roadmap"

    if args.scaffold:
        scaffold_vault()
        return

    if args.session:
        with open(args.session, "r") as f:
            data = json.load(f)
        path = write_session(data)
        print(f"Session note written: {path}")
        return

    if args.bug:
        print("Add a new bug entry (press Enter to skip fields)")
        data = {
            "title":      input("Title: ").strip(),
            "severity":   input("Severity [CRITICAL/HIGH/MEDIUM/MINOR]: ").strip() or "MEDIUM",
            "bot":        input("Bot (e.g. bot_fno_v1): ").strip(),
            "session":    input(f"Session date [{_today()}]: ").strip() or _today(),
            "symptom":    input("Symptom (what you saw): ").strip(),
            "root_cause": input("Root cause: ").strip(),
            "fix":        input("Fix applied: ").strip(),
            "status":     input("Status [OPEN/FIXED]: ").strip() or "OPEN",
            "lesson":     input("One-line lesson: ").strip(),
        }
        path = write_bug(data)
        print(f"Bug note written: {path}")
        return

    if args.reindex:
        _update_index()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
