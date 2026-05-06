import unittest
from unittest.mock import patch
import sys
import types
import importlib.util


class DashboardServerTests(unittest.TestCase):
    def test_login_and_routes_render_with_local_fallback(self):
        if importlib.util.find_spec("flask") is None:
            self.skipTest("flask not installed in current runtime")
        sys.modules.setdefault("requests", types.SimpleNamespace(get=lambda *a, **k: None))
        from dashboard.server import app

        app.config["TESTING"] = True
        client = app.test_client()

        with patch("dashboard.server.requests.get", side_effect=RuntimeError("offline")):
            response = client.get("/")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.headers["Location"])

            login_page = client.get("/login")
            self.assertEqual(login_page.status_code, 200)

            login = client.post("/login", data={"password": "marketpulse2026"}, follow_redirects=True)
            self.assertEqual(login.status_code, 200)
            self.assertIn(b"Live Dashboard", login.data)

            intelligence = client.get("/bot-intelligence")
            self.assertEqual(intelligence.status_code, 200)
            self.assertIn(b"Bot Intelligence", intelligence.data)

    def test_dashboard_renders_us_live_runtime_panel(self):
        if importlib.util.find_spec("flask") is None:
            self.skipTest("flask not installed in current runtime")
        sys.modules.setdefault("requests", types.SimpleNamespace(get=lambda *a, **k: None))
        from dashboard import server

        server.app.config["TESTING"] = True
        client = server.app.test_client()

        def fake_fetch(filename):
            mapping = {
                "positions.json": {},
                "risk_status.json": {"status": "OK", "reason": "clear"},
                "capital.json": {"session_pnl": 1000, "current_capital": 500000},
                "daily_brief.json": {"global_sentiment": "BULLISH", "date": "2026-05-06"},
                "fno_capital.json": {"session_pnl": 0, "capital": 100000},
                "council_state.json": {"cross_market_bias": {"verdict": "NEUTRAL"}, "confidence": 55},
                "bot_state.json": {"bots": {"us_v4": {"bets": {"election": {"venue": "polymarket"}}}}},
                "us_runtime_status.json": {
                    "alpaca_connected": True,
                    "generated_at": "2026-05-06T06:00:00+00:00",
                    "sessions": {"us": {"window": "open"}, "crypto": {"enabled": False}},
                    "position_snapshot": {
                        "AMD": {"side": "buy", "entry": 400.0, "current": 410.0, "pnl": 90.0, "pnl_pct": 2.25, "holding_style": "swing"}
                    },
                },
                "us_report_status.json": {"sent_to_telegram": True, "workbook_path": "/data/reports/MarketPulse_TradeLog.xlsx"},
                "father_opinion.json": {"us": {"mode": "active_light_guidance"}},
                "us_supervision.json": {"allow_new_entries": True, "source_warnings": []},
            }
            return mapping.get(filename, {})

        with patch("dashboard.server._fetch_json", side_effect=fake_fetch):
            client.post("/login", data={"password": "marketpulse2026"}, follow_redirects=True)
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"US Live P&amp;L", response.data)
        self.assertIn(b"Alpaca connected", response.data)
        self.assertIn(b"AMD", response.data)


if __name__ == "__main__":
    unittest.main()
