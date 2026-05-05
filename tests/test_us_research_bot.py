import unittest


class USResearchBotTests(unittest.TestCase):
    def test_resolve_earnings_date_falls_back_to_finnhub_calendar(self):
        import bot_us_research as mod

        class FakeTicker:
            calendar = None

            def get_earnings_dates(self, limit=6):
                return None

        original = mod.fetch_finnhub_earnings_date
        try:
            mod.fetch_finnhub_earnings_date = lambda symbol, now=None: "2026-05-12"
            earnings_date = mod.resolve_earnings_date("AAPL", FakeTicker())
        finally:
            mod.fetch_finnhub_earnings_date = original

        self.assertEqual(earnings_date, "2026-05-12")

    def test_build_weekly_candidates_prefers_strongest_conviction(self):
        from bot_us_research import build_weekly_candidates

        candidates = build_weekly_candidates(
            [
                {"symbol": "AAPL", "signal": "BUY", "score": 14, "conf": 72.0},
                {"symbol": "TSLA", "signal": "SELL", "score": -11, "conf": 66.0},
                {"symbol": "MSFT", "signal": "BUY", "score": 8, "conf": 58.0},
            ],
            limit=2,
        )

        self.assertEqual([row["symbol"] for row in candidates], ["AAPL", "TSLA"])

    def test_classify_pre_earnings_bias_rewards_alignment(self):
        from bot_us_research import classify_pre_earnings_bias

        bullish = classify_pre_earnings_bias(
            technical_score=12,
            news_score=3,
            revenue_growth=0.18,
            earnings_growth=0.22,
        )
        bearish = classify_pre_earnings_bias(
            technical_score=-10,
            news_score=-4,
            revenue_growth=-0.08,
            earnings_growth=-0.11,
        )

        self.assertEqual(bullish["bias"], "BULLISH")
        self.assertEqual(bearish["bias"], "BEARISH")
        self.assertGreater(bullish["confidence"], bearish["confidence"] - 10)

    def test_build_us_weekly_brief_includes_earnings_setups(self):
        from bot_us_research import build_us_weekly_brief

        brief = build_us_weekly_brief(
            results=[
                {"symbol": "AAPL", "signal": "BUY", "score": 13, "conf": 70.0},
                {"symbol": "TSLA", "signal": "SELL", "score": -12, "conf": 67.0},
            ],
            sector_rotation={"TECH": "BULLISH", "AUTO": "BEARISH"},
            earnings_setups=[
                {
                    "symbol": "AAPL",
                    "earnings_date": "2026-05-08",
                    "pre_result_bias": "BULLISH",
                    "result_day_bias": "VOLATILE_BULLISH",
                    "confidence": 74.0,
                }
            ],
            generated_at="2026-05-05T00:00:00Z",
            market_date="2026-05-05",
        )

        self.assertEqual(brief["top_bullish"][0], "AAPL")
        self.assertEqual(brief["top_bearish"][0], "TSLA")
        self.assertEqual(brief["earnings_setups"][0]["symbol"], "AAPL")
        self.assertIn("weekly_candidates", brief)

    def test_build_agent_consensus_returns_structured_views(self):
        from bot_us_research import build_agent_consensus

        consensus = build_agent_consensus(
            symbol="AAPL",
            technical_score=12,
            news_score=3,
            revenue_growth=0.18,
            earnings_growth=0.22,
        )

        self.assertEqual(consensus["symbol"], "AAPL")
        self.assertEqual(consensus["final_bias"], "BULLISH")
        self.assertIn("technical", consensus["analyst_views"])
        self.assertIn("sentiment", consensus["analyst_views"])
        self.assertIn("fundamental", consensus["analyst_views"])
        self.assertIn("risk", consensus)
        self.assertIn("bull_case", consensus)
        self.assertIn("bear_case", consensus)
        self.assertIn("tie_breaker", consensus)

    def test_apply_source_health_adjustment_downgrades_confidence(self):
        from bot_us_research import apply_source_health_adjustment

        result = apply_source_health_adjustment(
            confidence=78.0,
            source_health={
                "news_available": False,
                "earnings_calendar_available": True,
                "price_data_fresh": False,
                "fundamental_data_partial": True,
            },
        )

        self.assertLess(result["confidence"], 78.0)
        self.assertTrue(result["degraded"])
        self.assertGreaterEqual(len(result["warnings"]), 2)


if __name__ == "__main__":
    unittest.main()
