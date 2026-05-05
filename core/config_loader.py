"""
config_loader.py — Central configuration loader.
All bots import from here instead of hardcoding keys.
Reads config/config.env via python-dotenv, falls back to os.environ.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent / "config" / "config.env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)  # don't override real env vars
except ImportError:
    pass   # python-dotenv not installed — rely on os.environ only


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("1", "true", "yes", "on")


# ── Typed accessors ───────────────────────────────────────────
KITE_API_KEY       = get("KITE_API_KEY")
KITE_API_SECRET    = get("KITE_API_SECRET")
TRADER_TG_TOKEN    = get("TRADER_TG_TOKEN")
TRADER_TG_CHAT     = get("TRADER_TG_CHAT")
INTEL_TG_TOKEN     = get("INTEL_TG_TOKEN")
INTEL_TG_CHAT      = get("INTEL_TG_CHAT")
INDIA_INTEL_TG_TOKEN = get("INDIA_INTEL_TG_TOKEN", INTEL_TG_TOKEN)
INDIA_INTEL_TG_CHAT  = get("INDIA_INTEL_TG_CHAT", INTEL_TG_CHAT)
RESEARCH_TG_TOKEN  = get("RESEARCH_TG_TOKEN", INTEL_TG_TOKEN)
RESEARCH_TG_CHAT   = get("RESEARCH_TG_CHAT", INTEL_TG_CHAT)
INDIA_RESEARCH_TG_TOKEN = get("INDIA_RESEARCH_TG_TOKEN", RESEARCH_TG_TOKEN)
INDIA_RESEARCH_TG_CHAT  = get("INDIA_RESEARCH_TG_CHAT", RESEARCH_TG_CHAT)
FNO_TG_TOKEN       = get("FNO_TG_TOKEN")
FNO_TG_CHAT        = get("FNO_TG_CHAT")
US_TG_TOKEN        = get("US_TG_TOKEN")
US_TG_CHAT         = get("US_TG_CHAT", "7973242803")
US_INTEL_TG_TOKEN  = get("US_INTEL_TG_TOKEN")
US_INTEL_TG_CHAT   = get("US_INTEL_TG_CHAT", US_TG_CHAT)
US_RESEARCH_TG_TOKEN = get("US_RESEARCH_TG_TOKEN", US_INTEL_TG_TOKEN)
US_RESEARCH_TG_CHAT  = get("US_RESEARCH_TG_CHAT", US_INTEL_TG_CHAT)
US_EXEC_TG_TOKEN   = get("US_EXEC_TG_TOKEN", US_TG_TOKEN)
US_EXEC_TG_CHAT    = get("US_EXEC_TG_CHAT", US_TG_CHAT)
FATHER_TG_TOKEN    = get("FATHER_TG_TOKEN")
FATHER_TG_CHAT     = get("FATHER_TG_CHAT", TRADER_TG_CHAT)
OPENAI_API_KEY     = get("OPENAI_API_KEY")
OPENAI_MODEL       = get("OPENAI_MODEL", "gpt-5.4-mini")
ORACLE_VM_NAME     = get("ORACLE_VM_NAME", "marketpulse-us")
ORACLE_REGION      = get("ORACLE_REGION", "us-phoenix-1")
US_PAPER_TRADING   = get_bool("US_PAPER_TRADING", True)
CRYPTO_PAPER_TRADING = get_bool("CRYPTO_PAPER_TRADING", True)
POLYMARKET_PAPER_TRADING = get_bool("POLYMARKET_PAPER_TRADING", True)
AUTO_PAUSE_ONLY    = get_bool("AUTO_PAUSE_ONLY", True)
ALPACA_KEY         = get("ALPACA_KEY")
ALPACA_SECRET      = get("ALPACA_SECRET")
ALPACA_PAPER       = get_bool("ALPACA_PAPER", True)
FINNHUB_KEY        = get("FINNHUB_KEY")
INDIA_CAPITAL      = get_int("INDIA_CAPITAL", 10000)
FNO_BASE_CAPITAL   = get_int("FNO_BASE_CAPITAL", 25000)
US_CAPITAL         = get_int("US_CAPITAL", 10000)
CRYPTO_CAPITAL     = get_int("CRYPTO_CAPITAL", 500)
GITHUB_TOKEN       = get("GITHUB_TOKEN")
GITHUB_USER        = get("GITHUB_USER", "Manav-Deakin-23")
GITHUB_REPO        = get("GITHUB_REPO", "marketpulse-bots")
DASHBOARD_PASSWORD = get("DASHBOARD_PASSWORD", "marketpulse2026")
