import json
import unittest
from pathlib import Path
import shutil


class USCloudStatusTests(unittest.TestCase):
    def test_build_snapshot_merges_runtime_supervision_and_report_status(self):
        from scripts.us_cloud_status import build_snapshot

        root = Path("tests/.tmp_us_cloud_status")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "us_runtime_status.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-06T05:00:00+00:00",
                        "alpaca_connected": True,
                        "sessions": {
                            "us": {"is_open": True, "window": "open"},
                            "crypto": {"is_open": True, "window": "always_on", "enabled": False},
                        },
                        "position_count": 1,
                        "position_snapshot": {
                            "AMD": {"side": "buy", "qty": 9, "entry": 400.0, "holding_style": "swing"}
                        },
                        "safe_mode": {"global_pause_new_entries": False, "reason": ""},
                        "performance": {"us_equities": {"pnl": 123.0}},
                        "scheduler_status": {"last_us_close_cycle": "2026-05-05T20:10:00+00:00"},
                    }
                ),
                encoding="utf-8",
            )
            (root / "us_supervision.json").write_text(
                json.dumps(
                    {
                        "allow_new_entries": True,
                        "forced_safe_mode": False,
                        "source_warnings": ["earnings calendar unavailable"],
                        "weekly_focus": ["AMD", "CRWD"],
                    }
                ),
                encoding="utf-8",
            )
            (root / "father_opinion.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-06T05:01:00+00:00",
                        "india": {"mode": "waiting_session"},
                        "fno": {"mode": "supervised"},
                        "us": {"mode": "active_light_guidance"},
                        "crypto": {"mode": "disabled"},
                        "sessions": {"india": {"is_open": False, "window": "closed"}},
                    }
                ),
                encoding="utf-8",
            )
            (root / "us_report_status.json").write_text(
                json.dumps(
                    {
                        "workbook_path": "/data/reports/MarketPulse_TradeLog.xlsx",
                        "generated_at": "2026-05-06T20:10:00+00:00",
                        "sent_to_telegram": True,
                    }
                ),
                encoding="utf-8",
            )
            (root / "bot_state.json").write_text(
                json.dumps({"bots": {"us_v4": {"positions": {"AMD": {}}, "bets": {"election": {}}}}}),
                encoding="utf-8",
            )

            snapshot = build_snapshot(root, root)
        finally:
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(snapshot["us_execution"]["alpaca_connected"])
        self.assertEqual(snapshot["supervision"]["weekly_focus"], ["AMD", "CRWD"])
        self.assertEqual(snapshot["modes"]["india"], "waiting_session")
        self.assertEqual(snapshot["reporting"]["workbook_sent_to_telegram"], True)
        self.assertIn("AMD", snapshot["us_execution"]["open_positions"])


if __name__ == "__main__":
    unittest.main()
