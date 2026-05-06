from __future__ import annotations

import argparse
import json
from pathlib import Path

from marketpulse_runtime import resolve_report_dir, resolve_state_dir


STATE_DIR = resolve_state_dir()
REPORT_DIR = resolve_report_dir()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_snapshot(state_dir: Path | None = None, report_dir: Path | None = None) -> dict:
    state_root = Path(state_dir or STATE_DIR)
    report_root = Path(report_dir or REPORT_DIR)

    runtime = _read_json(state_root / "us_runtime_status.json")
    supervision = _read_json(state_root / "us_supervision.json")
    father = _read_json(state_root / "father_opinion.json")
    bot_state = _read_json(state_root / "bot_state.json").get("bots", {}).get("us_v4", {})
    report_status = _read_json(state_root / "us_report_status.json")

    return {
        "generated_at": runtime.get("generated_at") or father.get("generated_at"),
        "sessions": {
            "india": father.get("sessions", {}).get("india", {}),
            "us": runtime.get("sessions", {}).get("us", father.get("sessions", {}).get("us", {})),
            "crypto": runtime.get("sessions", {}).get("crypto", father.get("sessions", {}).get("crypto", {})),
        },
        "modes": {
            "india": father.get("india", {}).get("mode"),
            "fno": father.get("fno", {}).get("mode"),
            "us": father.get("us", {}).get("mode"),
            "crypto": father.get("crypto", {}).get("mode"),
        },
        "supervision": {
            "allow_new_entries": supervision.get("allow_new_entries"),
            "forced_safe_mode": supervision.get("forced_safe_mode"),
            "source_warnings": supervision.get("source_warnings", []),
            "weekly_focus": supervision.get("weekly_focus", []),
        },
        "us_execution": {
            "alpaca_connected": runtime.get("alpaca_connected"),
            "crypto_enabled": runtime.get("sessions", {}).get("crypto", {}).get("enabled"),
            "safe_mode": runtime.get("safe_mode", {}),
            "position_count": runtime.get("position_count", len(runtime.get("open_positions", []))),
            "open_positions": runtime.get("position_snapshot", {}),
            "performance": runtime.get("performance", {}),
            "scheduler_status": runtime.get("scheduler_status", {}),
        },
        "reporting": {
            "workbook_path": report_status.get("workbook_path") or str(report_root / "MarketPulse_TradeLog.xlsx"),
            "workbook_generated_at": report_status.get("generated_at"),
            "workbook_sent_to_telegram": report_status.get("sent_to_telegram"),
        },
        "state_summary": {
            "tracked_positions": list((bot_state.get("positions", {}) or {}).keys()),
            "tracked_bets": list((bot_state.get("bets", {}) or {}).keys()),
        },
    }


def _print_human(snapshot: dict) -> None:
    print("MarketPulse Cloud Status")
    print("=" * 28)
    print(f"Generated at: {snapshot.get('generated_at') or 'unknown'}")
    print(f"India:  {snapshot['modes'].get('india')} | session={snapshot['sessions'].get('india', {}).get('window')}")
    print(f"FNO:    {snapshot['modes'].get('fno')}")
    print(f"US:     {snapshot['modes'].get('us')} | session={snapshot['sessions'].get('us', {}).get('window')}")
    print(f"Crypto: {snapshot['modes'].get('crypto')} | session={snapshot['sessions'].get('crypto', {}).get('window')}")
    print("")
    execution = snapshot["us_execution"]
    print(f"Alpaca connected: {execution.get('alpaca_connected')}")
    print(f"Crypto enabled:   {execution.get('crypto_enabled')}")
    print(f"Safe mode:        {execution.get('safe_mode')}")
    print(f"Open positions:   {execution.get('position_count')}")
    for symbol, position in execution.get("open_positions", {}).items():
        print(
            f"  - {symbol}: {position.get('side')} x{position.get('qty')} "
            f"entry={position.get('entry')} hold={position.get('holding_style')}"
        )
    print("")
    supervision = snapshot["supervision"]
    print(f"Allow new entries: {supervision.get('allow_new_entries')}")
    print(f"Forced safe mode:  {supervision.get('forced_safe_mode')}")
    print(f"Warnings:          {', '.join(supervision.get('source_warnings', [])) or 'none'}")
    print(f"Weekly focus:      {', '.join(supervision.get('weekly_focus', [])[:8]) or 'none'}")
    print("")
    reporting = snapshot["reporting"]
    print(f"Workbook:          {reporting.get('workbook_path')}")
    print(f"Workbook updated:  {reporting.get('workbook_generated_at') or 'unknown'}")
    print(f"Workbook sent TG:  {reporting.get('workbook_sent_to_telegram')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show MarketPulse cloud status snapshot.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args()

    snapshot = build_snapshot()
    if args.json:
        print(json.dumps(snapshot, indent=2))
        return
    _print_human(snapshot)


if __name__ == "__main__":
    main()
