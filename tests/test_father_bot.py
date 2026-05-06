import unittest


class FatherBotTests(unittest.TestCase):
    def test_build_father_opinion_summarizes_fast_equity_and_supervised_fno(self):
        from bot_father import build_father_opinion

        opinion = build_father_opinion(
            brain_brief={
                "date": "2026-05-04",
                "market_regime": "BULLISH",
                "equity_focus": [
                    {"symbol": "RELIANCE"},
                    {"symbol": "INFY"},
                    {"symbol": "ITC"},
                ],
                "avoid_symbols": ["PAYTM", "NYKAA"],
            },
            brain_state={"bot_modes": {"india": "active_fast_path"}},
            bot_state={
                "bots": {
                    "india": {"signals": [{"symbol": "RELIANCE"}]},
                    "fno": {"rejections": [{"reason": "engine_none"}] * 12},
                }
            },
            council_state={"cross_market_bias": {"verdict": "BULLISH"}},
            risk_status={"hold": False},
        )

        self.assertEqual(opinion["market_regime"], "BULLISH")
        self.assertEqual(opinion["india"]["top_focus"], ["RELIANCE", "INFY", "ITC"])
        self.assertEqual(opinion["fno"]["mode"], "cautious")
        self.assertEqual(opinion["fno"]["candidate_symbols"], ["RELIANCE", "INFY", "ITC"])
        self.assertEqual(
            opinion["fno"]["candidates"],
            [
                {
                    "symbol": "RELIANCE",
                    "bias": "NEUTRAL",
                    "composite_score": 0.0,
                    "execution_mode": "fast_path",
                    "reason": "father_equity_focus",
                },
                {
                    "symbol": "INFY",
                    "bias": "NEUTRAL",
                    "composite_score": 0.0,
                    "execution_mode": "fast_path",
                    "reason": "father_equity_focus",
                },
                {
                    "symbol": "ITC",
                    "bias": "NEUTRAL",
                    "composite_score": 0.0,
                    "execution_mode": "fast_path",
                    "reason": "father_equity_focus",
                },
            ],
        )
        self.assertTrue(opinion["capabilities"]["can_rank_equities_fast"])
        self.assertFalse(opinion["capabilities"]["intraday_llm"])
        self.assertIn("sessions", opinion)

    def test_build_father_opinion_caps_fno_candidates_and_skips_avoid_symbols(self):
        from bot_father import build_father_opinion

        opinion = build_father_opinion(
            brain_brief={
                "market_regime": "BULLISH",
                "equity_focus": [
                    {"symbol": "RELIANCE", "bias": "BULLISH", "composite_score": 14.0, "execution_mode": "fast_path"},
                    {"symbol": "PAYTM", "bias": "BEARISH", "composite_score": 13.0, "execution_mode": "fast_path"},
                    {"symbol": "INFY", "bias": "BULLISH", "composite_score": 12.0, "execution_mode": "fast_path"},
                    {"symbol": "ITC", "bias": "BULLISH", "composite_score": 11.0, "execution_mode": "fast_path"},
                    {"symbol": "SBIN", "bias": "BULLISH", "composite_score": 10.0, "execution_mode": "fast_path"},
                ],
                "avoid_symbols": ["PAYTM", "NYKAA"],
            },
            brain_state={"bot_modes": {"india": "active_fast_path"}},
            bot_state={"bots": {"fno": {"rejections": []}, "india": {"signals": []}}},
            council_state={},
            risk_status={"hold": False},
        )

        self.assertEqual(opinion["fno"]["candidate_symbols"], ["RELIANCE", "INFY", "ITC"])
        self.assertEqual([row["symbol"] for row in opinion["fno"]["candidates"]], ["RELIANCE", "INFY", "ITC"])
        self.assertEqual(opinion["india"]["top_focus"], ["RELIANCE", "PAYTM", "INFY"])

    def test_build_father_opinion_switches_fno_to_risk_hold(self):
        from bot_father import build_father_opinion

        opinion = build_father_opinion(
            brain_brief={"market_regime": "NEUTRAL", "equity_focus": [], "avoid_symbols": []},
            brain_state={"bot_modes": {}},
            bot_state={"bots": {"fno": {"rejections": []}, "india": {"signals": []}}},
            council_state={},
            risk_status={"hold": True},
        )

        self.assertEqual(opinion["fno"]["mode"], "risk_hold")
        self.assertIn("Risk hold", opinion["fno"]["note"])

    def test_build_father_opinion_includes_session_state_for_india_us_and_crypto(self):
        import datetime as dt
        from bot_father import build_father_opinion

        opinion = build_father_opinion(
            brain_brief={"market_regime": "NEUTRAL", "equity_focus": [], "avoid_symbols": []},
            brain_state={"bot_modes": {"india": "active_fast_path", "us": "active_light_guidance"}},
            bot_state={
                "bots": {
                    "india": {"signals": []},
                    "fno": {"rejections": []},
                    "us_v4": {
                        "positions": {},
                        "bets": {},
                        "health": {"llm_supervisor": "available", "crypto_disabled_reason": ""},
                        "performance": {"crypto": {"signals": 3, "pnl": 10.5}},
                    },
                }
            },
            council_state={},
            risk_status={"hold": False},
            now=dt.datetime(2026, 5, 4, 14, 0, tzinfo=dt.timezone.utc),
        )

        self.assertIn("sessions", opinion)
        self.assertIn("india", opinion["sessions"])
        self.assertIn("us", opinion["sessions"])
        self.assertIn("crypto", opinion["sessions"])
        self.assertIn("crypto", opinion)
        self.assertEqual(opinion["crypto"]["mode"], "always_on")


if __name__ == "__main__":
    unittest.main()
